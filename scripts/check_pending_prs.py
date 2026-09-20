"""Find Jarvis-fired PRs that are still open + flag which need action.

Action-needed signals:
  - Has new CHANGES_REQUESTED reviews since the last commit → ready for iterate
  - Has new review comments after the last commit → may need iterate
  - Is approved but still draft → needs ready-for-review flip
  - Failed iterate on record without a follow-up → blocked
"""
import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

LOGS = Path("/home/ubuntu/jarvis/logs")
NOW = datetime.now(timezone.utc)
SINCE = NOW - timedelta(days=21)


def parse(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# Collect Jarvis-fired PR URLs from audit logs
prs = {}  # url → {opened_at, source}
for fname, src in [("fix_audit.jsonl", "fix"),
                   ("iterate_audit.jsonl", "iterate"),
                   ("claudify_audit.jsonl", "claudify")]:
    p = LOGS / fname
    if not p.is_file():
        continue
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = parse(r.get("ts") or r.get("timestamp"))
        if not ts or ts < SINCE:
            continue
        url = r.get("pr_url") or r.get("pull_request_url")
        if not url:
            continue
        if url not in prs or ts > prs[url]["last_event_ts"]:
            prs[url] = {"src": src, "last_event_ts": ts}


def gh(*args):
    res = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    return json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else None


print(f"# {len(prs)} Jarvis-fired PRs in last 21 days\n")
needs_action = []

for url, meta in sorted(prs.items()):
    # url looks like https://github.com/jupitermoney/<repo>/pull/<num>
    parts = url.replace("https://github.com/", "").split("/")
    if len(parts) < 4:
        continue
    owner, repo, _, num = parts[0], parts[1], parts[2], parts[3]
    full = f"{owner}/{repo}"
    detail = gh("pr", "view", num, "--repo", full,
                "--json", "state,isDraft,mergedAt,reviewDecision,reviews,commits,title")
    if not detail:
        continue
    state = detail.get("state")
    if state != "OPEN":
        continue

    last_commit_ts = None
    for c in (detail.get("commits") or []):
        t = parse(c.get("committedDate"))
        if t and (not last_commit_ts or t > last_commit_ts):
            last_commit_ts = t

    last_review_ts = None
    last_review_state = None
    for r in (detail.get("reviews") or []):
        author = (r.get("author") or {}).get("login", "")
        if author and not author.endswith("[bot]"):
            t = parse(r.get("submittedAt"))
            if t and (not last_review_ts or t > last_review_ts):
                last_review_ts = t
                last_review_state = r.get("state")

    flag = ""
    if (last_review_state == "CHANGES_REQUESTED" and last_review_ts and last_commit_ts
            and last_review_ts > last_commit_ts):
        flag = "🔧 CHANGES_REQUESTED — needs iterate"
    elif last_review_state == "COMMENTED" and last_review_ts and last_commit_ts and last_review_ts > last_commit_ts:
        flag = "💬 has reviewer comment after last commit"
    elif detail.get("reviewDecision") == "APPROVED" and detail.get("isDraft"):
        flag = "✅ APPROVED but still DRAFT — flip to ready-for-review"
    elif not last_review_ts:
        age_d = (NOW - meta["last_event_ts"]).days
        if age_d >= 7:
            flag = f"⏳ no review yet, {age_d}d since open — nudge reviewer"

    title = detail.get("title", "")[:60]
    draft = " (draft)" if detail.get("isDraft") else ""
    print(f"- [{full}#{num}]({url}){draft} — {title}")
    print(f"    src={meta['src']}  decision={detail.get('reviewDecision') or '—'}  "
          f"last_commit={last_commit_ts.strftime('%Y-%m-%d') if last_commit_ts else '—'}  "
          f"last_review={last_review_ts.strftime('%Y-%m-%d') if last_review_ts else 'none'} ({last_review_state or '—'})")
    if flag:
        print(f"    → {flag}")
        needs_action.append((url, flag))
    print()

print(f"\n## Summary: {len(needs_action)} PR(s) flagged for action\n")
for url, flag in needs_action:
    print(f"  {flag}  {url}")
