"""Daily morning-brief DM to portal users.

For each user in ~/jarvis/state/portal.db, compose a personalized DM:
- PRs awaiting their review (via gh api)
- Their in-flight Jarvis fix/iterate jobs (parse fix_audit.jsonl)
- Teammates' merged PRs in jupitermoney/* in the last 24h (top 10)
- Their daily-budget remaining (if portal user)

Run via systemd timer at 09:00 IST (03:30 UTC).
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

PORTAL_DB = Path("/home/ubuntu/jarvis/state/portal.db")
FIX_AUDIT = Path("/home/ubuntu/jarvis/logs/fix_audit.jsonl")
SLACK_USERS_JSON = Path("/home/ubuntu/jarvis/index/slack_users.json")
LOG = Path("/home/ubuntu/jarvis/logs/morning_brief.jsonl")


def _portal_users() -> list[dict]:
    if not PORTAL_DB.exists():
        return []
    with sqlite3.connect(str(PORTAL_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, email, name, daily_budget_usd, spent_today_usd, spent_today_date "
            "FROM users WHERE email IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def _slack_id_for_email(email: str) -> str | None:
    if not email:
        return None
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if not tok:
        return None
    try:
        req = urllib.request.Request(
            f"https://slack.com/api/users.lookupByEmail?email={email}",
            headers={"Authorization": f"Bearer {tok}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
        if d.get("ok"):
            return (d.get("user") or {}).get("id")
    except Exception:
        pass
    return None


def _prs_awaiting_review(github_login: str, limit: int = 5) -> list[dict]:
    """Use gh api to find open PRs where this login is a requested reviewer."""
    import subprocess
    try:
        raw = subprocess.check_output(
            ["gh", "api",
             f"search/issues?q=type:pr+state:open+org:jupitermoney+review-requested:{github_login}"
             f"&per_page={limit}"],
            text=True, timeout=20,
        )
        d = json.loads(raw)
    except Exception:
        return []
    items = d.get("items", [])
    out: list[dict] = []
    for it in items[:limit]:
        out.append({
            "title": it.get("title", "")[:120],
            "url": it.get("html_url"),
            "repo": it.get("repository_url", "").rsplit("/", 1)[-1],
            "author": (it.get("user") or {}).get("login", ""),
        })
    return out


def _inflight_jarvis_jobs_for(github_login_or_email: str, hours: int = 24) -> list[dict]:
    """Scan fix_audit.jsonl for jobs initiated by this user that are still
    running (no terminal success/failed/refused event seen).
    """
    if not FIX_AUDIT.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    jobs: dict[str, dict] = {}
    try:
        for line in FIX_AUDIT.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("ts", "") < cutoff:
                continue
            caller = (r.get("caller") or "")
            if github_login_or_email not in caller:
                continue
            jid = r.get("job_id")
            ev = r.get("event")
            if not jid:
                continue
            j = jobs.setdefault(jid, {"events": []})
            j["events"].append(ev)
            j["last_ts"] = r.get("ts")
            j["repo"] = r.get("repo") or j.get("repo")
            j["pr_url"] = r.get("pr_url") or j.get("pr_url")
    except Exception:
        return []
    out: list[dict] = []
    for jid, j in jobs.items():
        evs = set(j["events"])
        if {"success", "failed", "refused", "completed"} & evs:
            continue
        out.append({"job_id": jid, "repo": j.get("repo"),
                    "pr_url": j.get("pr_url"), "last_ts": j.get("last_ts")})
    return sorted(out, key=lambda r: r.get("last_ts", ""), reverse=True)[:5]


def _team_merged_yesterday(limit: int = 10) -> list[dict]:
    """Top merged PRs across jupitermoney/* in the last 24h."""
    import subprocess
    try:
        raw = subprocess.check_output(
            ["gh", "api",
             "search/issues?q=type:pr+state:closed+is:merged+org:jupitermoney"
             f"+merged:>{(datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
             f"&sort=updated&order=desc&per_page={limit}"],
            text=True, timeout=20,
        )
        d = json.loads(raw)
    except Exception:
        return []
    items = d.get("items", [])
    out: list[dict] = []
    for it in items[:limit]:
        author = (it.get("user") or {}).get("login", "")
        if author.endswith("[bot]"):
            continue
        out.append({
            "title": it.get("title", "")[:100],
            "url": it.get("html_url"),
            "repo": it.get("repository_url", "").rsplit("/", 1)[-1],
            "author": author,
        })
    return out


def _slack_email_to_login(email: str) -> str | None:
    """Heuristic: portal users authenticate via Google OAuth with @jupiter.money
    emails. Their GitHub login is usually the local-part or close to it.
    We don't have a mapping table — bail to None and let the user filter their
    own brief if needed. Future work: link portal account ↔ github login.
    """
    if not email or "@" not in email:
        return None
    local = email.split("@", 1)[0]
    # Common Jupiter pattern: firstname.lastname → firstname-lastname or first
    candidates = [local, local.replace(".", "-"), local.replace(".", "")]
    return candidates[0]  # best-effort; user can tell us their real login


def _post_dm(slack_id: str, text: str) -> bool:
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if not tok:
        return False
    try:
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": slack_id, "text": text,
                             "unfurl_links": False, "unfurl_media": False}).encode("utf-8"),
            headers={"Authorization": f"Bearer {tok}",
                     "Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode())
        return bool(d.get("ok"))
    except Exception:
        return False


def _audit(record: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z")
        with LOG.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def build_brief(user: dict, team_pulls: list[dict]) -> str:
    email = user.get("email", "")
    name = user.get("name", "") or email
    gh_login = _slack_email_to_login(email)

    awaiting = _prs_awaiting_review(gh_login or email) if gh_login else []
    inflight = _inflight_jarvis_jobs_for(email) if email else []

    lines = [f":coffee: *Morning brief — {name}*", ""]
    if awaiting:
        lines.append(f"*:eyes: {len(awaiting)} PRs awaiting your review:*")
        for pr in awaiting:
            lines.append(f"• <{pr['url']}|{pr['repo']}: {pr['title']}> — by {pr['author']}")
        lines.append("")
    if inflight:
        lines.append(f"*:hammer_and_wrench: {len(inflight)} Jarvis jobs still in-flight from yesterday:*")
        for j in inflight:
            lines.append(f"• `{j['job_id'][:24]}` on `{j['repo']}` "
                         + (f"→ {j['pr_url']}" if j.get('pr_url') else "(no PR yet)"))
        lines.append("")
    if team_pulls:
        lines.append(f"*:rocket: {len(team_pulls)} merges across jupitermoney in last 24h:*")
        for pr in team_pulls[:8]:
            lines.append(f"• <{pr['url']}|{pr['repo']}: {pr['title']}> — {pr['author']}")
        lines.append("")

    if user.get("daily_budget_usd"):
        remaining = float(user["daily_budget_usd"]) - float(user.get("spent_today_usd") or 0)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if user.get("spent_today_date") != today:
            remaining = float(user["daily_budget_usd"])  # reset
        lines.append(f"_Daily Jarvis budget remaining: ${remaining:.2f} of "
                     f"${float(user['daily_budget_usd']):.2f}._")

    if not awaiting and not inflight:
        lines.insert(2, "_Nothing urgent on your plate. Have a good day._\n")
    return "\n".join(lines)


def main() -> int:
    users = _portal_users()
    if not users:
        print("[morning_brief] no portal users — nothing to send")
        _audit({"event": "no_users"})
        return 0
    team_pulls = _team_merged_yesterday()
    sent = 0
    skipped = 0
    for u in users:
        email = u.get("email")
        if not email:
            skipped += 1
            continue
        slack_id = _slack_id_for_email(email)
        if not slack_id:
            skipped += 1
            _audit({"event": "skipped_no_slack_id", "email": email})
            continue
        text = build_brief(u, team_pulls)
        ok = _post_dm(slack_id, text)
        _audit({"event": "sent" if ok else "send_failed",
                "email": email, "slack_id": slack_id,
                "chars": len(text), "team_pulls_count": len(team_pulls)})
        if ok:
            sent += 1
        else:
            skipped += 1
    print(f"[morning_brief] sent={sent} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
