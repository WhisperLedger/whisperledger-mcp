"""Post a friendly comment on a Jira ticket explaining why /api/v1/fix refused.

Used by jarvis_fix.sh when the brief-sufficiency gate refuses with
insufficient_brief. The goal: close the feedback loop so the reporter
knows what to add without anyone triaging.

Usage:
    python3 jira_comment.py <TICKET-KEY> <missing-csv> <reason>

Auth: CONFLUENCE_EMAIL + CONFLUENCE_API_TOKEN (shared Atlassian creds).
Output: JSON with {ok, comment_id?} or {ok: false, error}.
Opt out by setting JARVIS_NO_JIRA_COMMENT_ON_REFUSE=1 in the wrapper.

Fails open — any error → log + exit 0 so the refusal flow proceeds.
"""
import json
import os
import subprocess
import sys


def emit(d):
    print(json.dumps(d))


def main() -> int:
    if len(sys.argv) < 4:
        emit({"ok": False, "error": "usage: jira_comment.py <KEY> <missing-csv> <reason>"})
        return 0

    key, missing_csv, reason = sys.argv[1], sys.argv[2], sys.argv[3]
    if os.environ.get("JARVIS_NO_JIRA_COMMENT_ON_REFUSE"):
        emit({"ok": False, "error": "opted out via env"})
        return 0

    email = os.environ.get("CONFLUENCE_EMAIL", "")
    token = os.environ.get("CONFLUENCE_API_TOKEN", "")
    if not email or not token:
        emit({"ok": False, "error": "no atlassian creds"})
        return 0

    missing = [m.strip() for m in missing_csv.split(",") if m.strip()]

    # Build the comment body in Atlassian Document Format (ADF) — Jira REST v3
    # requires it. Plain markdown bodies are rejected.
    intro_text = (
        "Hi! Jarvis tried to draft a fix for this ticket but the description "
        "doesn't have enough information to act on without guessing. The "
        "Slack-ready brief-sufficiency gate flagged it because:"
    )
    closing_text = (
        "Could you add the missing pieces above and re-run? Even one concrete "
        "pointer (file/class name, API endpoint, screenshot, or repro steps) "
        "is usually enough. — Jarvis"
    )

    list_items = [
        {"type": "listItem", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": m}]}
        ]}
        for m in missing
    ] or [
        {"type": "listItem", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "no specific symptom or code pointer"}]}
        ]}
    ]

    doc = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "🤖 ", "marks": []},
                    {"type": "text", "text": "Jarvis fix-mode refused: ",
                     "marks": [{"type": "strong"}]},
                    {"type": "text", "text": reason or "insufficient brief"},
                ]},
                {"type": "paragraph", "content": [
                    {"type": "text", "text": intro_text}
                ]},
                {"type": "bulletList", "content": list_items},
                {"type": "paragraph", "content": [
                    {"type": "text", "text": closing_text}
                ]},
            ],
        }
    }

    url = f"https://jupitermoney.atlassian.net/rest/api/3/issue/{key}/comment"
    r = subprocess.run(
        ["curl", "-sS", "--max-time", "15",
         "-u", f"{email}:{token}",
         "-H", "Accept: application/json",
         "-H", "Content-Type: application/json",
         "-X", "POST",
         "-d", json.dumps(doc),
         url],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        emit({"ok": False, "error": f"curl rc={r.returncode}: {r.stderr[:200]}"})
        return 0
    try:
        resp = json.loads(r.stdout)
    except Exception as e:
        emit({"ok": False, "error": f"parse: {e!s}", "raw": r.stdout[:200]})
        return 0
    if resp.get("errorMessages"):
        emit({"ok": False, "error": "jira: " + str(resp["errorMessages"])})
        return 0
    cid = resp.get("id")
    emit({"ok": bool(cid), "comment_id": cid})
    return 0


if __name__ == "__main__":
    sys.exit(main())
