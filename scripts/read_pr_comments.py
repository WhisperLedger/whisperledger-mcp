"""Pull the latest non-author, non-bot review + comment text on the 4 iterate
candidates. Cheap (gh api only, no Jarvis agent)."""
import json
import subprocess
from datetime import datetime

TARGETS = [
    ("jupitermoney/jupiter", 14141, "Apply/Applied descender — Chirag"),
    ("jupitermoney/jupiter", 14155, "FP-473 chequebook — Tushar"),
    ("jupitermoney/jupiter", 14166, "BO-529 pre-funding deposit"),
    ("jupitermoney/jupiter", 14148, "VKYC back nav — Prasanna"),
]


def gh(*args, parse=True):
    res = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    return json.loads(res.stdout) if parse else res.stdout


def fmt_ts(s):
    if not s:
        return "—"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s


for repo, num, label in TARGETS:
    print(f"\n{'=' * 75}")
    print(f"# {repo}#{num} — {label}")
    print(f"https://github.com/{repo}/pull/{num}")

    pr = gh("pr", "view", str(num), "--repo", repo,
            "--json", "author,createdAt,headRefName,reviewDecision,isDraft,title")
    if pr:
        author = pr["author"]["login"]
        print(f"  Title: {pr.get('title', '')[:80]}")
        print(f"  Author: {author}  ·  Decision: {pr.get('reviewDecision')}  ·  "
              f"Draft: {pr.get('isDraft')}")
    else:
        author = "?"

    # Reviews (formal: APPROVED / CHANGES_REQUESTED / COMMENTED)
    reviews = gh("api", f"repos/{repo}/pulls/{num}/reviews") or []
    # Issue-level comments (general "comment" thread, not inline)
    issue_comments = gh("api", f"repos/{repo}/issues/{num}/comments") or []
    # Inline review comments (on specific lines)
    inline_comments = gh("api", f"repos/{repo}/pulls/{num}/comments") or []

    # Combine + sort by time, keep humans, last 8
    events = []
    for r in reviews:
        login = (r.get("user") or {}).get("login", "")
        if not login or login.endswith("[bot]") or login == author:
            continue
        events.append({
            "ts": r.get("submitted_at"),
            "by": login,
            "kind": f"REVIEW:{r.get('state')}",
            "body": (r.get("body") or "").strip(),
        })
    for c in issue_comments:
        login = (c.get("user") or {}).get("login", "")
        if not login or login.endswith("[bot]") or login == author:
            continue
        events.append({
            "ts": c.get("created_at"),
            "by": login,
            "kind": "COMMENT",
            "body": (c.get("body") or "").strip(),
        })
    for c in inline_comments:
        login = (c.get("user") or {}).get("login", "")
        if not login or login.endswith("[bot]") or login == author:
            continue
        events.append({
            "ts": c.get("created_at"),
            "by": login,
            "kind": "INLINE",
            "body": (c.get("body") or "").strip(),
            "path": c.get("path"),
            "line": c.get("line"),
        })

    events.sort(key=lambda e: e["ts"] or "")
    if not events:
        print("  (no human reviewer events)")
        continue

    # Show last 6 events (most recent at bottom)
    for e in events[-6:]:
        loc = (f" @ {e.get('path')}:{e.get('line')}" if e.get('path') else "")
        body = e["body"][:400].replace("\n", "\n      ")
        print(f"\n  [{fmt_ts(e['ts'])}]  {e['by']}  {e['kind']}{loc}")
        if body:
            print(f"      {body}")
        else:
            print(f"      (no body)")
