"""Generate a 15-min walkthrough of a service / repo for new engineers.

Surfaces:
- Repo root + CLAUDE.md (if present) — the canonical convention doc
- Entry points: controllers (Kotlin Spring), main classes, App.tsx, server.py
- Key directories with file counts
- Recent meaningful PRs (last 7 days, merged, non-bot)
- Service registry entry if it's a known service

Returns markdown the agent renders. Engineer reads, asks follow-ups.
"""
from __future__ import annotations
import json
import os
import re
from .repo_names import resolve_gh_org
import subprocess
from pathlib import Path

REPOS_DIR = Path("/home/ubuntu/jarvis/repos")


def _read_text_capped(p: Path, cap: int = 4000) -> str:
    try:
        return p.read_text(errors="replace")[:cap]
    except Exception:
        return ""


def _find_entry_points(repo_root: Path) -> dict:
    """Best-effort detect entry-point files for various stacks."""
    out: dict = {"controllers": [], "main": [], "app_roots": []}
    for pat, bucket in [
        ("**/*Controller.kt", "controllers"),
        ("**/*Application.kt", "main"),
        ("**/Main.kt", "main"),
        ("**/main.py", "main"),
        ("**/server.py", "main"),
        ("**/server.ts", "main"),
        ("**/index.ts", "main"),
        ("**/App.tsx", "app_roots"),
        ("**/_app.tsx", "app_roots"),
    ]:
        try:
            matches = list(repo_root.glob(pat))[:5]
            for m in matches:
                if any(seg in m.parts for seg in ("node_modules", "build", "dist", ".gradle", "target")):
                    continue
                out[bucket].append(str(m.relative_to(repo_root)))
        except Exception:
            pass
    return out


def _key_dirs(repo_root: Path) -> list[dict]:
    """Top-level directories with file counts (a quick map of structure)."""
    out: list[dict] = []
    for d in sorted(repo_root.iterdir()):
        if not d.is_dir():
            continue
        if d.name.startswith(".") or d.name in {"node_modules", "build", "dist", "target", ".gradle"}:
            continue
        try:
            files = sum(1 for _ in d.rglob("*") if _.is_file())
        except Exception:
            files = 0
        out.append({"dir": d.name, "files": files})
    return sorted(out, key=lambda r: -r["files"])[:8]


def _recent_prs(repo_full: str, days: int = 7, limit: int = 5) -> list[dict]:
    """Pull last N merged non-bot PRs via gh api."""
    try:
        raw = subprocess.check_output(
            ["gh", "api", f"repos/{repo_full}/pulls",
             "-X", "GET",
             "-f", "state=closed", "-f", "sort=updated", "-f", "direction=desc",
             "-f", f"per_page={limit*3}"],
            text=True, timeout=20,
        )
        data = json.loads(raw)
    except Exception:
        return []
    out: list[dict] = []
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for pr in data:
        if not pr.get("merged_at"):
            continue
        merged = pr.get("merged_at", "")
        try:
            mdt = datetime.fromisoformat(merged.replace("Z", "+00:00"))
        except Exception:
            continue
        if mdt < cutoff:
            continue
        author = (pr.get("user") or {}).get("login", "")
        if author.endswith("[bot]") or author == "jarvis-bot":
            continue
        out.append({
            "number": pr.get("number"),
            "title": pr.get("title", "")[:120],
            "author": author,
            "merged_at": merged,
            "url": pr.get("html_url"),
        })
        if len(out) >= limit:
            break
    return out


def service_tour(repo: str) -> str:
    """Tour entry point — returns JSON the agent renders."""
    repo_root = REPOS_DIR / repo
    if not (repo_root / ".git").is_dir():
        return json.dumps({"ok": False, "error": f"repo not cloned: {repo}"})

    claudemd = ""
    for candidate in ("CLAUDE.md", "Claude.md", "claude.md"):
        p = repo_root / candidate
        if p.is_file():
            claudemd = _read_text_capped(p, cap=4000)
            break

    readme = ""
    for candidate in ("README.md", "Readme.md", "readme.md"):
        p = repo_root / candidate
        if p.is_file():
            readme = _read_text_capped(p, cap=2000)
            break

    entry = _find_entry_points(repo_root)
    dirs = _key_dirs(repo_root)
    prs = _recent_prs("/".join(resolve_gh_org(repo)))

    # Service registry lookup is optional — agent has lookup_service for that.
    return json.dumps({
        "ok": True,
        "repo": repo,
        "has_claudemd": bool(claudemd),
        "claudemd_excerpt": claudemd,
        "readme_excerpt": readme,
        "entry_points": entry,
        "key_directories": dirs,
        "recent_merged_prs": prs,
        "render_hint": (
            "Render this as a walking tour for a new engineer:\n"
            "1. Start with CLAUDE.md if present (convention doc).\n"
            "2. Point at the entry-point files in order — controllers/main/app_roots.\n"
            "3. Brief sketch of key_directories (largest dirs = where most action is).\n"
            "4. List recent_merged_prs as 'here's what landed last week — read these "
            "to know the in-flight direction'.\n"
            "5. End with 'ask me follow-up questions' invitation."
        ),
    }, ensure_ascii=False)
