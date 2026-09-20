"""Jarvis Developer Portal — FastAPI application.

Routes:
  GET  /               → redirect to /dashboard or /login
  GET  /login          → Google sign-in page
  GET  /auth/google    → start OAuth flow
  GET  /auth/callback  → handle OAuth callback, set session, redirect to /dashboard
  GET  /logout         → clear session, redirect to /login
  GET  /dashboard      → developer view: API keys + usage + MCP config snippet
  POST /api/keys       → create a new API key (shows full key once)
  POST /api/keys/{id}/revoke  → revoke a key
  GET  /admin          → admin view: all users + usage + write-access toggles
  POST /admin/users/{id}/write-access  → toggle write access for a user
  GET  /health         → liveness (no auth)

Runs on 127.0.0.1:8083. Engineers access via:
  ssh -L 8083:localhost:8083 ubuntu@3.6.202.121
"""
from __future__ import annotations
import os
import secrets
from pathlib import Path

from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from portal import auth, db

_PORTAL_SECRET = os.environ.get("JARVIS_PORTAL_SECRET")
if not _PORTAL_SECRET:
    raise SystemExit("JARVIS_PORTAL_SECRET env var not set — refusing to start")

_TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="Jarvis Developer Portal", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=_PORTAL_SECRET,
    max_age=86400 * 7,
    same_site="lax",
    https_only=False,
)
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

db.init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _redirect_uri(request: Request) -> str:
    return str(request.url_for("auth_callback"))


def _current_user(request: Request) -> Optional[db.UserContext]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get_user_by_id(int(user_id))


def _mcp_snippet(keys: list[db.ApiKey]) -> str:
    active = [k for k in keys if not k.revoked_at]
    if not active:
        return ""
    return (
        '# Tunnel first:\n'
        '# ssh -L 8082:localhost:8082 ubuntu@3.6.202.121\n'
        '\n'
        '# Then paste into:\n'
        '#   Claude Desktop: ~/Library/Application Support/Claude/claude_desktop_config.json\n'
        '#   Cursor:         ~/.cursor/mcp.json\n'
        '#   Claude Code:    .claude/settings.json\n'
        '{\n'
        '  "mcpServers": {\n'
        '    "jarvis": {\n'
        '      "url": "http://localhost:8082/mcp/",\n'
        '      "headers": { "Authorization": "Bearer <your-full-jrv_-key>" }\n'
        '    }\n'
        '  }\n'
        '}'
    )


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.get("/auth/google")
def auth_google(request: Request):
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    url = auth.get_auth_url(state, _redirect_uri(request))
    return RedirectResponse(url, status_code=302)


@app.get("/auth/callback", name="auth_callback")
async def auth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    if error:
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": f"Google refused sign-in: {error}"},
            status_code=400,)
    if not code or not state:
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": "Missing code or state from Google."},
            status_code=400,)
    expected = request.session.pop("oauth_state", None)
    if not expected or not secrets.compare_digest(state, expected):
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": "Invalid OAuth state — please try again."},
            status_code=400,)
    try:
        user_info = await auth.exchange_code(code, _redirect_uri(request))
    except ValueError as exc:
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": str(exc)},
            status_code=403,)
    except Exception as exc:
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": f"Authentication failed: {exc}"},
            status_code=500,)

    user = db.upsert_user(user_info.google_id, user_info.email, user_info.name)
    request.session["user_id"] = user.user_id
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ---------------------------------------------------------------------------
# Developer dashboard
# ---------------------------------------------------------------------------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login?error=Session+expired", status_code=302)
    keys = db.list_user_keys(user.user_id)
    usage = db.get_usage_for_user(user.email)
    mcp_config = _mcp_snippet(keys)
    return templates.TemplateResponse(request, "dashboard.html", {
            "request": request,
            "user": user,
            "keys": keys,
            "usage": usage,
            "mcp_config": mcp_config,
        },)


@app.post("/api/keys", response_class=HTMLResponse)
async def create_key(request: Request, label: str = Form(...)):
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired")
    label = label.strip()[:64] or "My key"
    key_id, full_key = db.create_api_key(user.user_id, label)
    return templates.TemplateResponse(request, "key_created.html", {"request": request, "user": user, "full_key": full_key, "label": label},)


@app.post("/api/keys/{key_id}/revoke")
async def revoke_key(request: Request, key_id: int):
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired")
    db.revoke_api_key(key_id, user.user_id)
    return RedirectResponse("/dashboard", status_code=302)


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login?error=Session+expired", status_code=302)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    users = db.list_all_users()
    users_meta = []
    for u in users:
        keys = db.list_user_keys(u.id)
        usage = db.get_usage_for_user(u.email)
        active_keys = [k for k in keys if not k.revoked_at]
        last_active = max(
            (k.last_used_at for k in keys if k.last_used_at), default=None
        )
        users_meta.append(
            {
                "user": u,
                "active_key_count": len(active_keys),
                "last_active": last_active,
                "usage": usage,
            }
        )

    return templates.TemplateResponse(request, "admin.html", {"request": request, "user": user, "users_meta": users_meta},)


@app.post("/admin/users/{user_id}/budget")
async def set_user_budget(
    request: Request, user_id: int, daily_budget_usd: float = Form(...),
):
    user = _current_user(request)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if daily_budget_usd < 0 or daily_budget_usd > 1000:
        raise HTTPException(status_code=400, detail="daily_budget_usd must be in [0, 1000]")
    db.set_daily_budget(user_id, float(daily_budget_usd))
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{user_id}/write-access")
async def toggle_write_access(
    request: Request, user_id: int, value: int = Form(...)
):
    user = _current_user(request)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    db.set_write_access(user_id, bool(value))
    return RedirectResponse("/admin", status_code=302)
