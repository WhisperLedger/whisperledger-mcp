"""Auto-fire /api/v1/pr/iterate when a Jarvis-authored PR has a NEW
CHANGES_REQUESTED review that hasn't been addressed by a subsequent commit.

v2 trigger (all must be true):
  1. PR is open AND draft, branch starts with 'jarvis/', repo on JARVIS_WRITE_ALLOWED_REPOS
  2. Effective review decision is CHANGES_REQUESTED (computed client-side:
     for each reviewer, their LATEST review's state; if ANY is CR → effective CR)
  3. The latest CHANGES_REQUESTED review ts > the latest commit ts on the branch
     (i.e., reviewer asked for changes AFTER the most recent push)
  4. No iterate-auto-fire for this PR in the last COOLDOWN_SEC
  5. Fewer than MAX_AUTO_FIRES_PER_DAY auto-fires for this PR today
  6. No in-flight iterate on the PR (audit log within last 30 min)

State + safety unchanged from v1.
"""
import json, os, subprocess, sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

COOLDOWN_SEC = 30 * 60
MAX_AUTO_FIRES_PER_DAY = 2
STALE_CR_THRESHOLD_HOURS = int(os.environ.get("STALE_CR_THRESHOLD_HOURS", "24"))  # skip CRs older than this
STATE_PATH = Path("/home/ubuntu/jarvis/state/iterate_auto_fire.jsonl")
AUDIT_PATH = Path("/home/ubuntu/jarvis/logs/iterate_audit.jsonl")
BOT_LOGINS = {
    "jm-bot-ci", "github-actions[bot]", "github-actions", "upwind-code-us[bot]",
    "upwind-iac-us[bot]", "semgrep-app[bot]", "dependabot[bot]", "jarvis-bot",
    "rkp2024", "sentinelone-bot", "tide", "openssf-scorecard[bot]",
}
OPERATOR_UID = "U0837N31T9C"
API_KEY = os.environ.get("JARVIS_API_KEY", "")
SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
ALLOWED_REPOS = (os.environ.get("JARVIS_WRITE_ALLOWED_REPOS") or "").replace(",", " ").split()

if os.environ.get("ITERATE_AUTO_FIRE_ENABLED", "0") != "1":
    print("ITERATE_AUTO_FIRE_ENABLED != 1; exiting no-op (safety gate)")
    sys.exit(0)
if not API_KEY or not ALLOWED_REPOS:
    print("ERROR: JARVIS_API_KEY or JARVIS_WRITE_ALLOWED_REPOS not set"); sys.exit(2)

STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

def gh(args):
    return json.loads(subprocess.check_output(["gh","api"]+args, text=True, stderr=subprocess.DEVNULL))

def list_jarvis_prs(repo):
    try:
        return [p for p in gh([f"repos/jupitermoney/{repo}/pulls?state=open&per_page=50"])
                if p.get("head",{}).get("ref","").startswith("jarvis/")]
    except Exception as e:
        print(f"  [{repo}] list err: {e}"); return []

def effective_cr(repo, pr_num):
    """Return (ts, reviewer_login) of the latest CHANGES_REQUESTED review if the
    effective decision is CR; else (None, None). 'Effective CR' = some reviewer's
    LATEST review (by ts) is in CHANGES_REQUESTED state, and that latest review
    has not been superseded by a newer review (APPROVED / COMMENTED is not enough
    to clear; only DISMISSED or a new review-state from the same user)."""
    try:
        reviews = gh([f"repos/jupitermoney/{repo}/pulls/{pr_num}/reviews"])
    except Exception:
        return None, None
    # Group reviews by user, take each user's latest by ts
    by_user = {}
    for r in reviews:
        u = (r.get("user") or {}).get("login","")
        if not u or u in BOT_LOGINS: continue
        ts = r.get("submitted_at") or ""
        if not ts: continue
        state = r.get("state","")
        if u not in by_user or ts > by_user[u]["ts"]:
            by_user[u] = {"ts": ts, "state": state}
    # Find any user whose latest review is CR; pick the most-recent CR across them
    cr_candidates = [(d["ts"], u) for u, d in by_user.items()
                     if d["state"] == "CHANGES_REQUESTED"]
    if not cr_candidates: return None, None
    cr_candidates.sort(reverse=True)
    return cr_candidates[0][0], cr_candidates[0][1]

def latest_branch_commit_ts(repo, branch):
    try:
        c = gh([f"repos/jupitermoney/{repo}/commits?sha={branch}&per_page=1"])
        return c[0].get("commit",{}).get("committer",{}).get("date","")
    except Exception:
        return ""

def load_state():
    if not STATE_PATH.exists(): return []
    return [json.loads(l) for l in STATE_PATH.read_text().splitlines() if l.strip()]

def record_fire(record):
    with STATE_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")

def already_fired_recently(repo, pr_num):
    state = load_state()
    now = datetime.now(timezone.utc)
    cooldown_cutoff = now - timedelta(seconds=COOLDOWN_SEC)
    day_cutoff = now - timedelta(days=1)
    cd = False; ft = 0
    for r in state:
        if r.get("repo") != repo or r.get("pr_num") != pr_num: continue
        ts = datetime.fromisoformat(r["ts"].replace("Z","+00:00"))
        if ts > cooldown_cutoff: cd = True
        if ts > day_cutoff: ft += 1
    return cd, ft

def in_flight_iterate(repo, pr_num):
    if not AUDIT_PATH.exists(): return False
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds").replace("+00:00","Z")
    starts, terminals = set(), set()
    with AUDIT_PATH.open() as f:
        for line in f:
            try: ev = json.loads(line)
            except: continue
            if ev.get("repo") != repo or ev.get("pr_number") != pr_num: continue
            if ev.get("ts","") < cutoff: continue
            tid = ev.get("task_id","")
            if ev.get("event") == "start": starts.add(tid)
            elif ev.get("event") in ("success","failed","unclear","refused","companion_done","companion_failed","claude_exited"): terminals.add(tid)
    return bool(starts - terminals)

def fire_iterate(repo, pr_num):
    body = json.dumps({"repo": repo, "pr_number": pr_num, "max_budget_usd": 1.50})
    req = urllib.request.Request(
        "http://127.0.0.1:8081/api/v1/pr/iterate",
        data=body.encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {API_KEY}",
                 "X-Jarvis-Caller": "iterate-auto-fire",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def dm_operator(text):
    if not SLACK_TOKEN: return
    import http.client, ssl
    body = json.dumps({"channel": OPERATOR_UID, "text": text, "mrkdwn": True, "unfurl_links": False})
    conn = http.client.HTTPSConnection("slack.com", context=ssl.create_default_context())
    conn.request("POST", "/api/chat.postMessage", body=body,
                 headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json; charset=utf-8"})
    return conn.getresponse().read()[:200]

fired = []; skipped = []
for repo in ALLOWED_REPOS:
    prs = list_jarvis_prs(repo)
    print(f"[{repo}] {len(prs)} open jarvis/* PRs")
    for p in prs:
        pr_num = p["number"]
        branch = p["head"]["ref"]
        title = p.get("title","?")[:60]
        if not p.get("draft", False):
            skipped.append((repo, pr_num, "not_draft")); continue
        cr_ts, cr_user = effective_cr(repo, pr_num)
        if not cr_ts:
            skipped.append((repo, pr_num, "no_effective_changes_requested")); continue
        commit_ts = latest_branch_commit_ts(repo, branch)
        if not commit_ts or cr_ts <= commit_ts:
            skipped.append((repo, pr_num, f"cr_already_addressed (cr={cr_ts} commit={commit_ts})")); continue
        cr_dt = datetime.fromisoformat(cr_ts.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - cr_dt).total_seconds() / 3600
        if age_hours > STALE_CR_THRESHOLD_HOURS:
            skipped.append((repo, pr_num, f"cr_stale ({age_hours:.1f}h old, threshold {STALE_CR_THRESHOLD_HOURS}h)")); continue
        cd, ft = already_fired_recently(repo, pr_num)
        if cd: skipped.append((repo, pr_num, "cooldown_active")); continue
        if ft >= MAX_AUTO_FIRES_PER_DAY: skipped.append((repo, pr_num, "daily_cap")); continue
        if in_flight_iterate(repo, pr_num): skipped.append((repo, pr_num, "in_flight")); continue

        print(f"  → FIRING iterate on {repo}#{pr_num} ({title}) — CR by {cr_user} @ {cr_ts}, last commit @ {commit_ts}")
        try:
            resp = fire_iterate(repo, pr_num)
            rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z"),
                   "repo": repo, "pr_num": pr_num, "trigger_login": cr_user,
                   "job_id": resp.get("job_id"), "status_url": resp.get("status_url")}
            record_fire(rec)
            dm_operator(f":robot_face: *iterate-auto-fire v2*: `{repo}#{pr_num}` ({title}) — CHANGES_REQUESTED by `{cr_user}` @ {cr_ts} (after last commit @ {commit_ts}). job_id=`{resp.get('job_id')}`")
            fired.append((repo, pr_num, cr_user))
        except Exception as e:
            print(f"    FIRE FAILED: {e}")
            skipped.append((repo, pr_num, f"fire_err:{e}"))

print(f"\nFired: {len(fired)}  Skipped: {len(skipped)}")
for s in skipped: print(f"  skip {s[0]}#{s[1]}: {s[2]}")
