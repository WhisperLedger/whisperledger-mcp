"""SQLite persistence for the Jarvis developer portal.

Two tables: users (Google OAuth identity) and api_keys (per-engineer bearer
tokens). The DB is read by jarvis-api and jarvis-mcp for every authed request,
and written by jarvis-portal on signup / key creation. WAL mode is enabled so
concurrent reads from jarvis-api don't block portal writes.
"""
from __future__ import annotations
import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_DB_PATH = Path(
    os.environ.get("JARVIS_PORTAL_DB", "")
    or str(Path.home() / "jarvis" / "state" / "portal.db")
)


@dataclass
class UserContext:
    user_id: int
    email: str
    name: str
    write_access: bool
    is_admin: bool
    daily_budget_usd: float = 5.0
    spent_today_usd: float = 0.0
    spent_today_date: Optional[str] = None

    @property
    def budget_remaining_usd(self) -> float:
        # If the recorded spend date is not today, the counter has reset.
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        spent = self.spent_today_usd if self.spent_today_date == today else 0.0
        return max(0.0, self.daily_budget_usd - spent)


@dataclass
class ApiKey:
    id: int
    key_prefix: str
    label: str
    created_at: str
    last_used_at: Optional[str]
    revoked_at: Optional[str]


@dataclass
class User:
    id: int
    email: str
    name: str
    is_admin: bool
    write_access: bool
    created_at: str


@dataclass
class UsageSummary:
    api_calls: int
    fix_jobs: int
    mcp_calls: int
    total_cost_usd: float


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                google_id TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                write_access INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                daily_budget_usd REAL NOT NULL DEFAULT 5.0,
                spent_today_usd REAL NOT NULL DEFAULT 0.0,
                spent_today_date TEXT
            );
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                key_prefix TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                label TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                revoked_at TEXT,
                UNIQUE(key_hash)
            );
        """)
        # Migration: add budget columns to existing tables (idempotent — skip if present).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "daily_budget_usd" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN daily_budget_usd REAL NOT NULL DEFAULT 5.0")
        if "spent_today_usd" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN spent_today_usd REAL NOT NULL DEFAULT 0.0")
        if "spent_today_date" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN spent_today_date TEXT")
        if "warned_at_80pct_today" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN warned_at_80pct_today TEXT")
    _seed_admin()


def _seed_admin() -> None:
    admin_email = os.environ.get("JARVIS_PORTAL_ADMIN_EMAIL", "")
    if not admin_email:
        return
    try:
        with _conn() as conn:
            conn.execute(
                "UPDATE users SET is_admin=1 WHERE email=?", (admin_email,)
            )
    except Exception:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_user(google_id: str, email: str, name: str) -> UserContext:
    admin_email = os.environ.get("JARVIS_PORTAL_ADMIN_EMAIL", "")
    is_admin = 1 if (admin_email and email == admin_email) else 0
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO users (google_id, email, name, is_admin, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(google_id) DO UPDATE SET
                name = excluded.name,
                is_admin = MAX(users.is_admin, excluded.is_admin)
            """,
            (google_id, email, name, is_admin, _now()),
        )
        row = conn.execute(
            "SELECT id, email, name, write_access, is_admin FROM users WHERE google_id=?",
            (google_id,),
        ).fetchone()
    return UserContext(
        user_id=row["id"],
        email=row["email"],
        name=row["name"],
        write_access=bool(row["write_access"]),
        is_admin=bool(row["is_admin"]),
    )


def get_user_by_id(user_id: int) -> Optional[UserContext]:
    if not _DB_PATH.exists():
        return None
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT id, email, name, write_access, is_admin FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return UserContext(
            user_id=row["id"],
            email=row["email"],
            name=row["name"],
            write_access=bool(row["write_access"]),
            is_admin=bool(row["is_admin"]),
        )
    except Exception:
        return None


def create_api_key(user_id: int, label: str) -> tuple[int, str]:
    """Generate a new jrv_ key. Returns (key_id, full_key). Full key shown once."""
    raw = "jrv_" + secrets.token_hex(16)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    key_prefix = raw[:10]
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO api_keys (user_id, key_prefix, key_hash, label, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, key_prefix, key_hash, label, _now()),
        )
        return cur.lastrowid, raw


def validate_api_key(raw_token: str) -> Optional[UserContext]:
    """Validate a raw bearer token. Returns UserContext if valid, None otherwise.

    Called on every authed request to jarvis-api and jarvis-mcp. Only attempts
    DB lookup for tokens with the jrv_ prefix; returns None immediately for the
    shared JARVIS_API_KEY (handled by its own check in each server).
    """
    if not raw_token.startswith("jrv_"):
        return None
    if not _DB_PATH.exists():
        return None
    try:
        key_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT k.id, k.revoked_at,
                       u.id AS user_id, u.email, u.name, u.write_access, u.is_admin,
                       u.daily_budget_usd, u.spent_today_usd, u.spent_today_date
                FROM api_keys k JOIN users u ON u.id = k.user_id
                WHERE k.key_hash = ?
                """,
                (key_hash,),
            ).fetchone()
            if not row or row["revoked_at"]:
                return None
            conn.execute(
                "UPDATE api_keys SET last_used_at=? WHERE id=?",
                (_now(), row["id"]),
            )
        return UserContext(
            user_id=row["user_id"],
            email=row["email"],
            name=row["name"],
            write_access=bool(row["write_access"]),
            is_admin=bool(row["is_admin"]),
            daily_budget_usd=float(row["daily_budget_usd"] or 5.0),
            spent_today_usd=float(row["spent_today_usd"] or 0.0),
            spent_today_date=row["spent_today_date"],
        )
    except Exception:
        return None


def set_daily_budget(user_id: int, daily_budget_usd: float) -> bool:
    """Admin-only: change the per-day spend cap for a user. Returns True if
    a row was updated. Idempotent; safe to call repeatedly.
    """
    if daily_budget_usd < 0:
        return False
    try:
        with _conn() as conn:
            cur = conn.execute(
                "UPDATE users SET daily_budget_usd = ? WHERE id = ?",
                (float(daily_budget_usd), user_id),
            )
        return cur.rowcount > 0
    except Exception:
        return False


def revoke_api_key(key_id: int, user_id: int) -> bool:
    try:
        with _conn() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                (_now(), key_id, user_id),
            )
        return cur.rowcount > 0
    except Exception:
        return False


def list_user_keys(user_id: int) -> list[ApiKey]:
    if not _DB_PATH.exists():
        return []
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT id, key_prefix, label, created_at, last_used_at, revoked_at
            FROM api_keys WHERE user_id=? ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [
        ApiKey(
            id=r["id"],
            key_prefix=r["key_prefix"],
            label=r["label"],
            created_at=r["created_at"],
            last_used_at=r["last_used_at"],
            revoked_at=r["revoked_at"],
        )
        for r in rows
    ]


def list_all_users() -> list[User]:
    if not _DB_PATH.exists():
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, email, name, is_admin, write_access, created_at FROM users ORDER BY created_at DESC"
        ).fetchall()
    return [
        User(
            id=r["id"],
            email=r["email"],
            name=r["name"],
            is_admin=bool(r["is_admin"]),
            write_access=bool(r["write_access"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]


def set_write_access(user_id: int, value: bool) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET write_access=? WHERE id=?",
            (1 if value else 0, user_id),
        )


def get_usage_for_user(email: str) -> UsageSummary:
    """Aggregate usage from JSONL audit logs for a given caller (user email)."""
    logs_dir = Path.home() / "jarvis" / "logs"
    api_calls = 0
    fix_count = 0
    mcp_calls = 0
    total_cost = 0.0

    api_log = logs_dir / "api_requests.jsonl"
    if api_log.exists():
        try:
            with api_log.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        if rec.get("caller") == email:
                            api_calls += 1
                            total_cost += float(rec.get("cost_usd") or 0)
                    except Exception:
                        pass
        except Exception:
            pass

    fix_log = logs_dir / "fix_audit.jsonl"
    if fix_log.exists():
        try:
            with fix_log.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        if rec.get("caller") == email and rec.get("event") == "success":
                            fix_count += 1
                    except Exception:
                        pass
        except Exception:
            pass

    mcp_log = logs_dir / "mcp_audit.jsonl"
    if mcp_log.exists():
        try:
            with mcp_log.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        if rec.get("caller") == email:
                            mcp_calls += 1
                    except Exception:
                        pass
        except Exception:
            pass

    return UsageSummary(
        api_calls=api_calls,
        fix_jobs=fix_count,
        mcp_calls=mcp_calls,
        total_cost_usd=round(total_cost, 4),
    )


WARN_80PCT = 0.80


def record_spend(user_id: int, cost_usd: float) -> None:
    """Add cost_usd to the user's spent_today_usd, resetting on date rollover.

    Idempotent w.r.t. concurrent writes via the SQLite WAL mode the rest of the
    portal uses. Safe to call after every authed request that consumed compute.

    Side effect: if the spend crosses the 80% threshold for the first time today,
    fire-and-forget a Slack DM to the user warning them. warned_at_80pct_today
    is set to today's date so we don't spam on subsequent calls.
    """
    if cost_usd <= 0:
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with _conn() as conn:
            # Capture pre-update state to detect the threshold crossing.
            pre = conn.execute(
                "SELECT email, daily_budget_usd, spent_today_usd, spent_today_date, warned_at_80pct_today "
                "FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not pre:
                return
            pre_spent = float(pre["spent_today_usd"] or 0.0) if pre["spent_today_date"] == today else 0.0
            budget = float(pre["daily_budget_usd"] or 5.0)
            new_spent = pre_spent + cost_usd

            conn.execute(
                """
                UPDATE users SET
                    spent_today_usd = ?,
                    spent_today_date = ?
                WHERE id = ?
                """,
                (new_spent, today, user_id),
            )

            crossed = (pre_spent < budget * WARN_80PCT <= new_spent)
            already_warned = (pre["warned_at_80pct_today"] == today)
            if crossed and not already_warned:
                conn.execute(
                    "UPDATE users SET warned_at_80pct_today = ? WHERE id = ?",
                    (today, user_id),
                )
                # Fire DM outside the with-conn block so the update commits first.
                _fire_threshold_warning(pre["email"], new_spent, budget)
    except Exception:
        # Never let bookkeeping crash a request; the next call will re-attempt.
        pass


def _fire_threshold_warning(email: str, spent: float, budget: float) -> None:
    """Best-effort Slack DM when a user crosses 80% of daily budget.

    Resolves Slack user id from email via slack_users.json (maintained by
    usage_report). Audits to portal_alerts.jsonl. Never raises.
    """
    try:
        slack_token = os.environ.get("SLACK_BOT_TOKEN")
        if not slack_token:
            return
        # Resolve slack id from email — slack_users.json maps slack_id -> {email, name}.
        users_path = Path("/home/ubuntu/jarvis/slack_users.json")
        slack_id = None
        if users_path.exists():
            try:
                users = json.loads(users_path.read_text())
                for sid, info in users.items():
                    if (info or {}).get("email", "").lower() == (email or "").lower():
                        slack_id = sid
                        break
            except Exception:
                pass
        if not slack_id:
            # No mapping — log only, don't DM.
            _audit_alert(email, spent, budget, sent=False, reason="no_slack_id_mapping")
            return
        import urllib.request
        body = (
            f":warning: *Jarvis daily-budget alert*\n"
            f"You've spent *${spent:.3f}* of your *${budget:.2f}* daily budget today (UTC). "
            f"Once you hit the cap, `/api/v1/ask` and the write endpoints return 429 until "
            f"the counter resets at 00:00 UTC.\n\n"
            f"_If you need a higher cap, ping <@U0837N31T9C> (Rohit)._"
        )
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": slack_id, "text": body,
                             "unfurl_links": False, "unfurl_media": False}).encode("utf-8"),
            headers={"Authorization": f"Bearer {slack_token}",
                     "Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode())
        _audit_alert(email, spent, budget, sent=bool(resp.get("ok")),
                     reason=resp.get("error") or "")
    except Exception as e:
        try:
            _audit_alert(email, spent, budget, sent=False,
                         reason=f"{type(e).__name__}: {e!s}")
        except Exception:
            pass


def _audit_alert(email: str, spent: float, budget: float, sent: bool, reason: str = "") -> None:
    try:
        log = Path("/home/ubuntu/jarvis/logs/portal_alerts.jsonl")
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
                "event": "budget_80pct_warning",
                "email": email,
                "spent_today_usd": spent,
                "daily_budget_usd": budget,
                "sent": sent,
                "reason": reason,
            }) + "\n")
    except Exception:
        pass
