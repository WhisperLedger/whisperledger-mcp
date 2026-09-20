"""GitHub-webhook reactive layer — v0.2 (DM on review of Jarvis-fired PRs) +
v0.3 (auto-fire /jarvis review on opened PRs).

Both functions are designed to run as FastAPI BackgroundTasks: they must never
raise, must finish quickly (DM in <1s; review spawn fire-and-forget), and must
write a structured audit record for every decision (fired OR skipped — knowing
WHY we skipped is as important as knowing we fired).

Audit log: ~/jarvis/logs/github_reactive.jsonl
Every record: {ts, action, event, repo, pr_number, pr_url, sender, reason?, ...}
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from agent.config import ROOT_DIR, get_env, BOT_NAME

REACTIVE_LOG = ROOT_DIR / "logs" / "github_reactive.jsonl"

# Branches bot creates; any PR head.ref starting with one of these = bot-fired.
ASTRA_BRANCH_PREFIXES = (
    "astra-fix-",
    "astra-iterate-",
    "astra-migrate-",
    "astra-claudify-",
    "astra-nitpick-",
    "astra/",
    "jarvis-fix-",
    "jarvis-iterate-",
    "jarvis-migrate-",
    "jarvis-claudify-",
    "jarvis-nitpick-",
    "jarvis/",
    "add-claude-md-docs",  # claudify legacy fixed branch
)

# v0.2 — DM target for review-on-bot-PR events.
# Defaults to Rohit per the operator-DM rule. Override with ASTRA_REACTIVE_DM_USER.
OPERATOR_USER_ID = get_env("REACTIVE_DM_USER") or "U0837N31T9C"

# v0.3 — repos where auto-fires on opened PRs.
AUTO_REVIEW_ALLOWED_REPOS = set(
    s.strip()
    for s in (get_env("AUTO_REVIEW_ALLOWED_REPOS") or "jupiter").split(",")
    if s.strip()
)
AUTO_REVIEW_DAILY_CAP = int(get_env("AUTO_REVIEW_DAILY_CAP", "10"))

# Reviewers whose actions we ignore (would create a notification loop).
SELF_IDENTITIES = {f"{BOT_NAME.lower()}-bot", f"{BOT_NAME} Bot", "jarvis-bot", "Jarvis Bot"}

# v0.4 — real-time indexing on push events.
REALTIME_INDEX_SCRIPT = str(ROOT_DIR / "scripts" / "realtime_index.sh")
REALTIME_INDEX_LOG = ROOT_DIR / "logs" / "realtime_index.jsonl"
REALTIME_INDEX_COOLDOWN_SEC = int(get_env("REALTIME_INDEX_COOLDOWN_SEC", "60"))
REALTIME_LOCK_DIR = Path("/tmp/astra_realtime_lock")
INDEXED_REPOS_FILE = ROOT_DIR / "scripts" / "indexed_repos.txt"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _log(record: dict) -> None:
    try:
        REACTIVE_LOG.parent.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", _ts())
        with REACTIVE_LOG.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # never fail the webhook on logging


def _is_jarvis_pr(payload: dict) -> bool:
    """Return True if the PR's head branch matches a Jarvis-created prefix."""
    pr = payload.get("pull_request") or {}
    head = ((pr.get("head") or {}).get("ref") or "").strip()
    return any(head == p.rstrip("-") or head.startswith(p) for p in ASTRA_BRANCH_PREFIXES)


def _slack_post(channel: str, text: str) -> dict:
    """POST chat.postMessage. Returns the JSON response (with ok/error)."""
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if not tok:
        return {"ok": False, "error": "no_slack_token"}
    body = {"channel": channel, "text": text,
            "unfurl_links": False, "unfurl_media": False}
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e!s}"}


def _count_today(action: str) -> int:
    """Count today's audit records with matching action — used for daily caps."""
    if not REACTIVE_LOG.exists():
        return 0
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = 0
    try:
        with REACTIVE_LOG.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if r.get("action") == action and (r.get("ts") or "").startswith(today_prefix):
                        n += 1
                except Exception:
                    continue
    except Exception:
        pass
    return n


# ============================================================================
# v0.2 — react to reviews on Jarvis-fired PRs
# ============================================================================

def react_to_review(payload: dict, event: str) -> None:
    """v0.2: DM operator when a Jarvis-fired PR receives a review or review-comment.

    Triggers on:
      - pull_request_review (action=submitted)
      - pull_request_review_comment (action=created)

    Issue-comment events on PRs are NOT handled in v0.2 because the payload
    doesn't include head.ref and would require a follow-up gh-api call to
    classify. Add in v0.4 if needed.
    """
    repo = (payload.get("repository") or {}).get("full_name", "?")
    pr = payload.get("pull_request") or {}
    pr_num = pr.get("number")
    pr_url = pr.get("html_url", "")
    pr_title = pr.get("title", "")
    action = payload.get("action") or ""

    # Filter on action — only react to meaningful state changes.
    if event == "pull_request_review" and action != "submitted":
        _log({"action": "skip_review_action", "event": event, "received_action": action,
              "repo": repo, "pr_number": pr_num})
        return
    if event == "pull_request_review_comment" and action != "created":
        _log({"action": "skip_review_comment_action", "event": event,
              "received_action": action, "repo": repo, "pr_number": pr_num})
        return

    if not _is_jarvis_pr(payload):
        _log({"action": "skip_not_jarvis_pr", "event": event,
              "repo": repo, "pr_number": pr_num,
              "head_ref": ((pr.get("head") or {}).get("ref") or "")})
        return

    review = payload.get("review") or {}
    comment = payload.get("comment") or {}
    reviewer = ((review.get("user") or comment.get("user") or {}).get("login")) or "?"

    if reviewer in SELF_IDENTITIES:
        _log({"action": "skip_self_review", "event": event,
              "repo": repo, "pr_number": pr_num, "reviewer": reviewer})
        return

    state = (review.get("state") or comment.get("path") or "comment").upper()
    body = (review.get("body") or comment.get("body") or "").strip()[:500]
    file_hint = comment.get("path") or ""

    lines = [
        f":eyes: *Review on Jarvis-fired PR*",
        f"<{pr_url}|{repo}#{pr_num} — {pr_title}>",
        f"*{reviewer}* → {state}" + (f" on `{file_hint}`" if file_hint else ""),
    ]
    if body:
        # Use triple-backtick block for verbatim body.
        lines.append(f"```{body}```")
    dm_text = "\n".join(lines)

    resp = _slack_post(OPERATOR_USER_ID, dm_text)
    _log({
        "action": "reactive_dm_sent" if resp.get("ok") else "reactive_dm_failed",
        "event": event,
        "repo": repo,
        "pr_number": pr_num,
        "pr_url": pr_url,
        "reviewer": reviewer,
        "state": state,
        "dm_channel": OPERATOR_USER_ID,
        "ok": resp.get("ok"),
        "slack_error": resp.get("error"),
    })


# ============================================================================
# v0.3 — auto-fire /jarvis review on opened PRs
# ============================================================================

JARVIS_REVIEW_SCRIPT = str(ROOT_DIR / "scripts" / "astra_review.py")
JARVIS_REVIEW_VENV_PY = str(ROOT_DIR / "scripts" / "indexer" / ".venv" / "bin" / "python")


def _is_bot_sender(sender: dict) -> bool:
    login = (sender.get("login") or "").lower()
    return (sender.get("type") == "Bot") or login.endswith("[bot]") or login in {
        "dependabot", "renovate", "renovate-bot", "github-actions",
    }


def auto_review_pr(payload: dict, event: str) -> None:
    """v0.3: spawn jarvis_review.py on newly opened PRs that pass the filter.

    Skip reasons (each logged): non-opened action, draft, bot sender,
    Jarvis-fired PR (avoid review-self loop), repo not allowlisted, daily cap.
    """
    action = payload.get("action") or ""
    if action != "opened":
        _log({"action": "skip_not_opened", "event": event, "received_action": action})
        return

    pr = payload.get("pull_request") or {}
    sender = payload.get("sender") or {}
    repo_full = (payload.get("repository") or {}).get("full_name", "")
    repo_short = repo_full.split("/", 1)[-1] if "/" in repo_full else repo_full
    pr_num = pr.get("number")
    pr_url = pr.get("html_url", "")
    pr_title = pr.get("title", "")
    head_ref = (pr.get("head") or {}).get("ref") or ""

    if pr.get("draft"):
        _log({"action": "skip_draft", "event": event, "repo": repo_full,
              "pr_number": pr_num, "pr_url": pr_url})
        return

    if _is_bot_sender(sender):
        _log({"action": "skip_bot_sender", "event": event, "repo": repo_full,
              "pr_number": pr_num, "sender": sender.get("login")})
        return

    if any(head_ref.startswith(p) for p in ASTRA_BRANCH_PREFIXES):
        _log({"action": "skip_jarvis_pr", "event": event, "repo": repo_full,
              "pr_number": pr_num, "head_ref": head_ref})
        return

    if repo_short not in AUTO_REVIEW_ALLOWED_REPOS:
        _log({"action": "skip_not_allowed_repo", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "allowed": sorted(AUTO_REVIEW_ALLOWED_REPOS)})
        return

    today_count = _count_today("auto_review_fired")
    if today_count >= AUTO_REVIEW_DAILY_CAP:
        _log({"action": "skip_daily_cap", "event": event, "repo": repo_full,
              "pr_number": pr_num, "today_count": today_count,
              "cap": AUTO_REVIEW_DAILY_CAP})
        return

    # All filters passed — spawn the review subprocess fully detached.
    # bash + nohup + & double-decouples from jarvis-api so the process doesn't
    # become a zombie under us when it exits. The script posts the review
    # comment itself; we don't need to wait.
    log_path = str(ROOT_DIR / "logs" / f"auto_review_{pr_num}_{int(datetime.now(timezone.utc).timestamp())}.log")
    cmd = (
        f"nohup {JARVIS_REVIEW_VENV_PY} {JARVIS_REVIEW_SCRIPT} "
        f"'{pr_url}' 'auto-review (github webhook)' "
        f"> {log_path} 2>&1 &"
    )
    try:
        subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        ).wait(timeout=5)  # bash exits immediately after backgrounding
        _log({"action": "auto_review_fired", "event": event, "repo": repo_full,
              "pr_number": pr_num, "pr_url": pr_url, "pr_title": pr_title,
              "sender": sender.get("login"), "head_ref": head_ref,
              "log_path": log_path, "today_count_after": today_count + 1})
    except Exception as e:
        _log({"action": "auto_review_spawn_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num, "pr_url": pr_url,
              "error": f"{type(e).__name__}: {e!s}"})


# ============================================================================
# v0.4 — real-time indexing on push events
# ============================================================================

_INDEXED_REPOS_CACHE: set[str] = set()
_INDEXED_REPOS_MTIME: float = 0.0


def _load_indexed_repos() -> set[str]:
    """Load and cache the indexed_repos.txt list, refreshing on mtime change."""
    global _INDEXED_REPOS_CACHE, _INDEXED_REPOS_MTIME
    try:
        mtime = INDEXED_REPOS_FILE.stat().st_mtime
    except FileNotFoundError:
        return set()
    if mtime == _INDEXED_REPOS_MTIME and _INDEXED_REPOS_CACHE:
        return _INDEXED_REPOS_CACHE
    repos: set[str] = set()
    try:
        for line in INDEXED_REPOS_FILE.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                repos.add(line)
        _INDEXED_REPOS_CACHE = repos
        _INDEXED_REPOS_MTIME = mtime
    except Exception:
        pass
    return repos


def realtime_reindex_on_push(payload: dict, event: str) -> None:
    """v0.4: re-index a repo when push lands on its default branch.

    Filters (each logged):
      - non-push event (defensive — should be filtered by caller too)
      - ref not the repo's default branch (skip feature-branch / tag pushes)
      - repo not in indexed_repos.txt (skip repos we don't index)
      - within per-repo cooldown window (debounce bursty pushes)

    On fire: spawn realtime_index.sh detached via bash + nohup. The wrapper
    handles git fetch + reset + `python -m indexer.main <repo>`.
    """
    import time

    if event != "push":
        return  # silent — caller dispatch shouldn't route us here anyway

    ref = payload.get("ref") or ""
    repo_full = (payload.get("repository") or {}).get("full_name", "")
    repo_short = repo_full.split("/", 1)[-1] if "/" in repo_full else repo_full
    default_branch = (payload.get("repository") or {}).get("default_branch") or "main"
    expected_ref = f"refs/heads/{default_branch}"

    if ref != expected_ref:
        _log({"action": "skip_not_default_branch", "event": event,
              "repo": repo_full, "ref": ref, "default_branch": default_branch})
        return

    indexed = _load_indexed_repos()
    if repo_short not in indexed:
        _log({"action": "skip_not_indexed_repo", "event": event,
              "repo": repo_full})
        return

    # Per-repo cooldown — debounce bursty pushes by treating same-repo events
    # within COOLDOWN_SEC as already-handled. The lock-file mtime IS the timer.
    REALTIME_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = REALTIME_LOCK_DIR / f"{repo_short}.lock"
    if lock_path.exists():
        age = time.time() - lock_path.stat().st_mtime
        if age < REALTIME_INDEX_COOLDOWN_SEC:
            _log({"action": "skip_cooldown", "event": event, "repo": repo_full,
                  "age_sec": int(age), "cooldown_sec": REALTIME_INDEX_COOLDOWN_SEC})
            return
    lock_path.touch()  # claim the slot before spawning

    commits = payload.get("commits") or []
    added = sorted({f for c in commits for f in (c.get("added") or [])})
    modified = sorted({f for c in commits for f in (c.get("modified") or [])})
    removed = sorted({f for c in commits for f in (c.get("removed") or [])})

    log_path = str(
        ROOT_DIR / "logs" / f"realtime_index_{repo_short}_"
        f"{int(datetime.now(timezone.utc).timestamp())}.log"
    )
    cmd = (
        f"nohup bash {REALTIME_INDEX_SCRIPT} '{repo_short}' '{default_branch}' "
        f"> {log_path} 2>&1 &"
    )
    try:
        subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        ).wait(timeout=5)
        _log({"action": "realtime_reindex_fired", "event": event,
              "repo": repo_full, "ref": ref, "default_branch": default_branch,
              "n_added": len(added), "n_modified": len(modified),
              "n_removed": len(removed),
              "after_sha": (payload.get("after") or "")[:12],
              "before_sha": (payload.get("before") or "")[:12],
              "sender": (payload.get("sender") or {}).get("login"),
              "log_path": log_path})
    except Exception as e:
        _log({"action": "realtime_reindex_spawn_failed", "event": event,
              "repo": repo_full, "error": f"{type(e).__name__}: {e!s}"})

# ============================================================================
# v0.5 — PR auto-fill description on opened PRs with empty body
# ============================================================================

AUTOFILL_ALLOWED_REPOS = set(
    s.strip() for s in
    os.environ.get("JARVIS_AUTOFILL_ALLOWED_REPOS", "jupiter,bff-core,jupiter-design-system,jarvis").split(",")
    if s.strip()
)
AUTOFILL_DAILY_CAP = int(os.environ.get("JARVIS_AUTOFILL_DAILY_CAP", "30"))
AUTOFILL_MIN_BODY_LEN = 40  # treat shorter bodies as "empty enough to help"


def autofill_pr_description(payload: dict, event: str) -> None:
    """v0.5 — draft a PR description when the author left it empty."""
    action = payload.get("action") or ""
    if action != "opened":
        return
    pr = payload.get("pull_request") or {}
    sender = payload.get("sender") or {}
    repo_full = (payload.get("repository") or {}).get("full_name", "")
    repo_short = repo_full.split("/", 1)[-1] if "/" in repo_full else repo_full
    pr_num = pr.get("number")
    pr_url = pr.get("html_url", "")
    head_ref = (pr.get("head") or {}).get("ref") or ""

    if pr.get("draft"):
        _log({"action": "autofill_skip_draft", "event": event,
              "repo": repo_full, "pr_number": pr_num})
        return
    if _is_bot_sender(sender):
        _log({"action": "autofill_skip_bot", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "sender": sender.get("login")})
        return
    if any(head_ref.startswith(p) for p in ASTRA_BRANCH_PREFIXES):
        _log({"action": "autofill_skip_jarvis_pr", "event": event,
              "repo": repo_full, "pr_number": pr_num, "head_ref": head_ref})
        return
    if repo_short not in AUTOFILL_ALLOWED_REPOS:
        _log({"action": "autofill_skip_not_allowed_repo", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "allowed": sorted(AUTOFILL_ALLOWED_REPOS)})
        return

    body = (pr.get("body") or "").strip()
    if len(body) >= AUTOFILL_MIN_BODY_LEN:
        _log({"action": "autofill_skip_has_body", "event": event,
              "repo": repo_full, "pr_number": pr_num, "body_len": len(body)})
        return

    today_count = _count_today("autofill_posted")
    if today_count >= AUTOFILL_DAILY_CAP:
        _log({"action": "autofill_skip_daily_cap", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "today_count": today_count, "cap": AUTOFILL_DAILY_CAP})
        return

    # Pull diff + author commit subjects via gh api. Cheap, runs in <2s.
    import subprocess as _sp
    try:
        diff = _sp.check_output(
            ["gh", "api", f"repos/{repo_full}/pulls/{pr_num}",
             "-H", "Accept: application/vnd.github.v3.diff"],
            text=True, timeout=15,
        )
    except Exception as e:
        _log({"action": "autofill_skip_diff_fetch_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})
        return
    try:
        commits_blob = _sp.check_output(
            ["gh", "api", f"repos/{repo_full}/pulls/{pr_num}/commits"],
            text=True, timeout=15,
        )
        import json as _json
        commits_data = _json.loads(commits_blob)
        commit_subjects = [c["commit"]["message"].split("\n", 1)[0]
                           for c in commits_data[:6]]
    except Exception:
        commit_subjects = []

    # Truncate diff aggressively — Haiku doesn't need 80k tokens.
    diff_preview = diff[:12000]
    diff_truncated = len(diff) > 12000

    rubric = (
        "You draft GitHub PR descriptions from raw diffs. Engineers will copy "
        "your output into the PR description (or ignore it). Keep it tight, "
        "factual, and skimmable.\n\n"
        "Output markdown with these sections:\n"
        "## Summary\n"
        "2-4 sentences: what changed and why. Imperative voice.\n\n"
        "## Changes\n"
        "Bulleted list of specific file/area changes. One line per bullet, "
        "include file paths in backticks when relevant.\n\n"
        "## Test plan\n"
        "Bulleted checklist of how to verify. Prefer concrete commands or "
        "scenarios over 'tested locally'. Include 'CI green' as the last item.\n\n"
        "Do NOT invent context that isn't in the diff or commit messages. If "
        "uncertain about intent, say 'See commits for context' instead of "
        "guessing.\n\n"
        "End with this exact line: \n"
        "_Drafted by Jarvis from the diff. Copy-paste into the PR description "
        "or ignore — your call._"
    )
    user_prompt = (
        f"PR: {repo_full}#{pr_num} — {pr.get('title','')}\n\n"
        f"Commit subjects ({len(commit_subjects)} of {len(commits_data) if 'commits_data' in dir() else '?'}):\n"
        + "\n".join(f"- {s}" for s in commit_subjects)
        + "\n\nDiff "
        + (f"(truncated, first 12k chars of {len(diff)}):\n" if diff_truncated else ":\n")
        + diff_preview
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system=rubric,
            messages=[{"role": "user", "content": user_prompt}],
        )
        draft = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        _log({"action": "autofill_skip_haiku_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})
        return

    if not draft:
        _log({"action": "autofill_skip_empty_draft", "event": event,
              "repo": repo_full, "pr_number": pr_num})
        return

    # Post as a PR comment (NOT description edit).
    body_text = (
        ":memo: *Draft PR description (auto-generated from the diff)*\n\n"
        + draft
    )
    try:
        _sp.run(
            ["gh", "api", f"repos/{repo_full}/issues/{pr_num}/comments",
             "-f", f"body={body_text}"],
            check=True, timeout=15,
            stdout=_sp.PIPE, stderr=_sp.PIPE,
        )
        _log({"action": "autofill_posted", "event": event,
              "repo": repo_full, "pr_number": pr_num, "pr_url": pr_url,
              "draft_chars": len(draft), "diff_truncated": diff_truncated,
              "today_count_after": today_count + 1})
    except Exception as e:
        _log({"action": "autofill_post_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})

# ============================================================================
# v0.6 — CI failure autopsy on check_run.completed events
# ============================================================================

CI_AUTOPSY_ALLOWED_REPOS = set(
    s.strip() for s in
    os.environ.get("JARVIS_CI_AUTOPSY_ALLOWED_REPOS", "jupiter,bff-core,jarvis").split(",")
    if s.strip()
)
CI_AUTOPSY_DAILY_CAP = int(os.environ.get("JARVIS_CI_AUTOPSY_DAILY_CAP", "20"))


def ci_failure_autopsy(payload: dict, event: str) -> None:
    """v0.6 — diagnose a failed CI run and post the analysis on the PR."""
    action = payload.get("action") or ""
    if action != "completed":
        return
    check_run = payload.get("check_run") or {}
    if check_run.get("conclusion") != "failure":
        _log({"action": "autopsy_skip_not_failure", "event": event,
              "conclusion": check_run.get("conclusion")})
        return

    repo_full = (payload.get("repository") or {}).get("full_name", "")
    repo_short = repo_full.split("/", 1)[-1] if "/" in repo_full else repo_full
    if repo_short not in CI_AUTOPSY_ALLOWED_REPOS:
        _log({"action": "autopsy_skip_not_allowed_repo", "event": event,
              "repo": repo_full,
              "allowed": sorted(CI_AUTOPSY_ALLOWED_REPOS)})
        return

    # check_runs can fire for branches outside PRs (e.g., default-branch CI).
    # We only autopsy when it's tied to a PR — pull_requests list non-empty.
    prs = check_run.get("pull_requests") or []
    if not prs:
        _log({"action": "autopsy_skip_not_pr", "event": event,
              "check_run_id": check_run.get("id")})
        return
    pr_num = prs[0].get("number")
    check_run_id = check_run.get("id")
    check_name = check_run.get("name", "")
    sender = payload.get("sender") or {}

    # Pull PR metadata to detect Jarvis-fired PRs (skip those).
    import subprocess as _sp
    try:
        pr_meta_raw = _sp.check_output(
            ["gh", "api", f"repos/{repo_full}/pulls/{pr_num}"],
            text=True, timeout=15,
        )
        pr_meta = json.loads(pr_meta_raw)
    except Exception as e:
        _log({"action": "autopsy_skip_pr_fetch_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})
        return
    head_ref = ((pr_meta.get("head") or {}).get("ref") or "")
    if any(head_ref.startswith(p) for p in ASTRA_BRANCH_PREFIXES):
        # Jarvis-fired PR — try self-healing instead of posting human-facing autopsy.
        _ci_self_heal_jarvis_pr(payload, event, pr_meta, check_run, check_run_id,
                                check_name, repo_full, repo_short, pr_num, head_ref)
        return

    # Daily cap.
    today_count = _count_today("autopsy_posted")
    if today_count >= CI_AUTOPSY_DAILY_CAP:
        _log({"action": "autopsy_skip_daily_cap", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "today_count": today_count, "cap": CI_AUTOPSY_DAILY_CAP})
        return

    # Idempotency — have we already autopsied this check_run_id?
    # Cheap check: grep the reactive log.
    if check_run_id is not None:
        try:
            if REACTIVE_LOG.exists():
                tag = f'"check_run_id": {check_run_id}, "action": "autopsy_posted"'
                # Reverse-read efficiency: full read is fine, files are <100MB.
                if tag in REACTIVE_LOG.read_text():
                    _log({"action": "autopsy_skip_already_posted", "event": event,
                          "check_run_id": check_run_id})
                    return
        except Exception:
            pass

    # Fetch the failed job log. check_run id maps to a jobs/{id} endpoint.
    # The `logs` endpoint returns a redirect to a signed S3 URL; gh follows it.
    try:
        log_text = _sp.check_output(
            ["gh", "api", f"repos/{repo_full}/actions/jobs/{check_run_id}/logs",
             "--include"],
            text=True, timeout=30,
        )
    except Exception as e:
        _log({"action": "autopsy_skip_log_fetch_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})
        return
    # The log is verbose. Extract failure-relevant tail (last 8000 chars).
    log_tail = log_text[-8000:] if len(log_text) > 8000 else log_text

    # Pull the diff so we can correlate.
    try:
        diff = _sp.check_output(
            ["gh", "api", f"repos/{repo_full}/pulls/{pr_num}",
             "-H", "Accept: application/vnd.github.v3.diff"],
            text=True, timeout=15,
        )
    except Exception as e:
        _log({"action": "autopsy_skip_diff_fetch_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})
        return
    diff_preview = diff[:8000]

    rubric = (
        "You are a CI-failure triager. Given a failed CI job's log tail and the PR diff, "
        "identify:\n"
        "1. Which test or build step failed and the EXACT error message.\n"
        "2. The most likely cause from the diff: a specific file:line in the diff that "
        "looks related to the failure, OR 'no diff line clearly matches — likely flaky' "
        "if nothing fits.\n"
        "3. The smallest next-step the engineer should take to verify or fix.\n\n"
        "Be specific. Cite the exact failing test name, file path, and line numbers. "
        "Never invent files or symbols that aren't in the diff or log.\n\n"
        "Output STRICT markdown with these sections:\n"
        "**Failed:** [test/step name + 1-line error]\n"
        "**Likely cause:** [file:line in diff OR 'no diff line clearly matches']\n"
        "**Why:** [1-2 sentence reasoning]\n"
        "**Next step:** [smallest verification — exact command or 1-line check]\n\n"
        "End with: \n"
        "_Drafted by Jarvis from the failed job log + your diff. Could be wrong — "
        "always verify locally._"
    )
    user_prompt = (
        f"PR: {repo_full}#{pr_num}\n"
        f"Failed check: {check_name}\n\n"
        f"Log tail (last {len(log_tail)} of {len(log_text)} chars):\n"
        f"{log_tail}\n\n"
        f"PR diff (first {len(diff_preview)} of {len(diff)} chars):\n"
        f"{diff_preview}"
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system=rubric,
            messages=[{"role": "user", "content": user_prompt}],
        )
        analysis = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        _log({"action": "autopsy_skip_haiku_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})
        return

    if not analysis:
        return

    body_text = (
        f":mag: *CI failure autopsy — `{check_name}`*\n\n"
        + analysis
        + "\n\n---\n_Want me to try an auto-fix?_ Reply to this comment "
        + "with `jarvis fix` and I will draft a PR from the analysis above."
    )

    try:
        _sp.run(
            ["gh", "api", f"repos/{repo_full}/issues/{pr_num}/comments",
             "-f", f"body={body_text}"],
            check=True, timeout=15,
            stdout=_sp.PIPE, stderr=_sp.PIPE,
        )
        _log({"action": "autopsy_posted", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "check_run_id": check_run_id, "check_name": check_name,
              "today_count_after": today_count + 1})
    except Exception as e:
        _log({"action": "autopsy_post_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})


# ============================================================================
# v0.7 — autofix offer on autopsy comments
# ============================================================================

AUTOFIX_TRIGGER = ("jarvis fix", "jarvis-fix", "jarvis:fix")


def react_to_autofix_request(payload: dict, event: str) -> None:
    """When someone replies to a Jarvis autopsy comment with `jarvis fix`,
    fire /api/v1/fix using the autopsy analysis as the brief.

    Triggered by `issue_comment.created` events. Filters:
    - Comment body must contain a trigger string
    - Comment author must be the PR author (avoid hijacking)
    - The comment must be on a PR (issue.pull_request present)
    - Repo must be in JARVIS_WRITE_ALLOWED_REPOS
    """
    action = payload.get("action") or ""
    if action != "created":
        return
    comment = payload.get("comment") or {}
    body = (comment.get("body") or "").lower().strip()
    if not any(t in body for t in AUTOFIX_TRIGGER):
        return
    issue = payload.get("issue") or {}
    if not issue.get("pull_request"):
        _log({"action": "autofix_skip_not_pr_comment", "event": event,
              "comment_id": comment.get("id")})
        return

    repo_full = (payload.get("repository") or {}).get("full_name", "")
    repo_short = repo_full.split("/", 1)[-1] if "/" in repo_full else repo_full

    write_allowed = set(
        s.strip() for s in
        os.environ.get("JARVIS_WRITE_ALLOWED_REPOS", "jupiter bff-core jarvis jupiter-design-system").split()
        if s.strip()
    )
    if repo_short not in write_allowed:
        _log({"action": "autofix_skip_repo_not_allowed", "event": event,
              "repo": repo_full})
        return

    pr_num = issue.get("number")
    comment_author = (comment.get("user") or {}).get("login")
    issue_author = (issue.get("user") or {}).get("login")
    if comment_author != issue_author:
        _log({"action": "autofix_skip_not_author", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "comment_author": comment_author, "pr_author": issue_author})
        return

    # Look up the autopsy comment that this reply is responding to. GitHub's
    # issue comments don't have explicit threading, so we fetch recent comments
    # and find the most recent autopsy from us.
    import subprocess as _sp
    try:
        raw = _sp.check_output(
            ["gh", "api", f"repos/{repo_full}/issues/{pr_num}/comments?per_page=30&sort=created&direction=desc"],
            text=True, timeout=15,
        )
        comments = json.loads(raw)
    except Exception as e:
        _log({"action": "autofix_skip_comments_fetch_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})
        return

    autopsy_text = None
    for c in comments:
        b = c.get("body") or ""
        if "CI failure autopsy" in b and "Drafted by Jarvis" in b:
            autopsy_text = b
            break
    if not autopsy_text:
        _log({"action": "autofix_skip_no_autopsy_found", "event": event,
              "repo": repo_full, "pr_number": pr_num})
        return

    # Compose the brief: pull the "Likely cause" + "Why" + "Next step" lines
    # from the autopsy + glue it to "fix the underlying issue".
    brief = (
        "Auto-fix request from PR author following Jarvis's CI failure autopsy. "
        f"PR: {repo_full}#{pr_num}. The autopsy identified:\n\n"
        + autopsy_text[:4000]
        + "\n\nUse the analysis above to produce the minimum fix."
    )

    api_key = os.environ.get("JARVIS_API_KEY", "")
    if not api_key:
        _log({"action": "autofix_skip_no_api_key", "event": event})
        return

    import urllib.request, json as _json
    req = urllib.request.Request(
        "http://127.0.0.1:8081/api/v1/fix",
        data=_json.dumps({
            "repo": repo_short,
            "description": brief,
            "max_budget_usd": 3.0,
        }).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Jarvis-Caller": f"autopsy-reply:{comment_author}",
            "Idempotency-Key": f"autopsy-{repo_full}-{pr_num}-{comment.get('id')}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ack = _json.loads(r.read().decode())
        job_id = ack.get("job_id", "?")
        # Post a reply on the PR confirming the dispatch.
        _sp.run(
            ["gh", "api", f"repos/{repo_full}/issues/{pr_num}/comments",
             "-f", f"body=:hammer_and_wrench: Auto-fix dispatched — job `{job_id}`. I'll post the draft-PR URL here when it lands."],
            check=False, timeout=15, stdout=_sp.PIPE, stderr=_sp.PIPE,
        )
        _log({"action": "autofix_dispatched", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "job_id": job_id, "comment_id": comment.get("id")})
    except Exception as e:
        _log({"action": "autofix_dispatch_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})


# ============================================================================
# v0.8 — Self-healing CI on Jarvis-fired PRs (closes the loop)
# ============================================================================

SELF_HEAL_MAX_ATTEMPTS = int(os.environ.get("JARVIS_SELF_HEAL_MAX_ATTEMPTS", "2"))


def _count_self_heal_attempts(repo_full: str, pr_num: int) -> int:
    """Count past self-heal attempts on this PR by scanning the reactive log."""
    if not REACTIVE_LOG.exists():
        return 0
    n = 0
    needle_pr = f'"pr_number": {pr_num}'
    needle_repo = f'"repo": "{repo_full}"'
    needle_action = '"action": "self_heal_iterate_dispatched"'
    try:
        for line in REACTIVE_LOG.open():
            if needle_action in line and needle_pr in line and needle_repo in line:
                n += 1
    except Exception:
        pass
    return n


def _ci_self_heal_jarvis_pr(payload: dict, event: str, pr_meta: dict,
                            check_run: dict, check_run_id, check_name: str,
                            repo_full: str, repo_short: str, pr_num: int,
                            head_ref: str) -> None:
    """Self-healing path for Jarvis-fired PRs whose CI just failed."""
    # Idempotency on check_run_id.
    if check_run_id is not None:
        try:
            if REACTIVE_LOG.exists():
                tag = f'"check_run_id": {check_run_id}, "action": "self_heal'
                if tag in REACTIVE_LOG.read_text():
                    _log({"action": "self_heal_skip_already_handled", "event": event,
                          "check_run_id": check_run_id})
                    return
        except Exception:
            pass

    # Retry cap.
    attempts = _count_self_heal_attempts(repo_full, pr_num)
    if attempts >= SELF_HEAL_MAX_ATTEMPTS:
        msg = (
            f":raising_hand: *Self-heal exhausted ({attempts}/{SELF_HEAL_MAX_ATTEMPTS} attempts).*\n\n"
            f"CI keeps failing on `{check_name}` after my retries. Likely needs human eyes. "
            f"Reply with `jarvis fix` if you want another shot, otherwise a reviewer can take it from here."
        )
        try:
            import subprocess as _sp
            _sp.run(
                ["gh", "api", f"repos/{repo_full}/issues/{pr_num}/comments",
                 "-f", f"body={msg}"],
                check=False, timeout=15,
                stdout=_sp.PIPE, stderr=_sp.PIPE,
            )
        except Exception:
            pass
        _log({"action": "self_heal_giving_up", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "check_run_id": check_run_id, "head_ref": head_ref,
              "attempts": attempts})
        return

    # Pull failed log + diff (cheap; same as autopsy path).
    import subprocess as _sp
    try:
        log_text = _sp.check_output(
            ["gh", "api", f"repos/{repo_full}/actions/jobs/{check_run_id}/logs",
             "--include"],
            text=True, timeout=30,
        )
    except Exception as e:
        _log({"action": "self_heal_skip_log_fetch_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})
        return
    log_tail = log_text[-8000:] if len(log_text) > 8000 else log_text
    try:
        diff = _sp.check_output(
            ["gh", "api", f"repos/{repo_full}/pulls/{pr_num}",
             "-H", "Accept: application/vnd.github.v3.diff"],
            text=True, timeout=15,
        )
    except Exception as e:
        _log({"action": "self_heal_skip_diff_fetch_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})
        return
    diff_preview = diff[:8000]

    # Haiku writes a minimal-fix brief that the iterate flow will execute.
    rubric = (
        "You write fix-instructions for an automated code-iteration bot. The "
        "bot will execute your instructions verbatim against an existing PR "
        "branch. You're given a failed CI job log and the PR diff. Identify "
        "the exact failure and the minimum change needed.\n\n"
        "Output a TIGHT 2-4 sentence brief in imperative form (e.g. 'Add a "
        "null check before line 47 of FooController.kt to handle the missing "
        "Idempotency-Key header — return 400 instead of throwing NPE'). "
        "Reference specific file paths and line numbers from the diff. Don't "
        "invent files. If the failure is environmental (network blip, "
        "infra-side timeout, dependency-resolution flake), reply exactly: "
        "'FLAKE: <one-sentence reason>' and the iterate flow will skip.\n\n"
        "No markdown headings, no sections. Just the brief."
    )
    user_prompt = (
        f"PR: {repo_full}#{pr_num} (head=`{head_ref}`)\n"
        f"Failed check: {check_name}\n\n"
        f"Log tail (last {len(log_tail)} of {len(log_text)} chars):\n"
        f"{log_tail}\n\n"
        f"PR diff (first {len(diff_preview)} of {len(diff)} chars):\n"
        f"{diff_preview}"
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=rubric,
            messages=[{"role": "user", "content": user_prompt}],
        )
        brief = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        _log({"action": "self_heal_skip_haiku_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})
        return

    if not brief:
        _log({"action": "self_heal_skip_empty_brief", "event": event,
              "repo": repo_full, "pr_number": pr_num})
        return

    if brief.upper().startswith("FLAKE"):
        _log({"action": "self_heal_skip_flake", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "brief": brief[:200]})
        try:
            _sp.run(
                ["gh", "api", f"repos/{repo_full}/issues/{pr_num}/comments",
                 "-f", f"body=:zzz: Classified the CI failure as a flake. Not iterating. ({brief})"],
                check=False, timeout=15,
                stdout=_sp.PIPE, stderr=_sp.PIPE,
            )
        except Exception:
            pass
        return

    # Fire /api/v1/pr/iterate.
    api_key = os.environ.get("JARVIS_API_KEY", "")
    if not api_key:
        _log({"action": "self_heal_skip_no_api_key", "event": event})
        return

    import urllib.request, json as _json
    body = {
        "repo": repo_short,
        "pr_number": pr_num,
        "instructions": brief,
        "max_budget_usd": 3.0,
    }
    req = urllib.request.Request(
        "http://127.0.0.1:8081/api/v1/pr/iterate",
        data=_json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Jarvis-Caller": "self-heal",
            "Idempotency-Key": f"self-heal-{repo_full}-{pr_num}-{check_run_id}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ack = _json.loads(r.read().decode())
        job_id = ack.get("job_id", "?")
        # Post a short comment so reviewers know what's going on.
        next_attempt = attempts + 1
        comment = (
            f":robot_face: *Self-heal attempt {next_attempt}/{SELF_HEAL_MAX_ATTEMPTS} — '"
            f"`{check_name}` failed; iterating.*\n\n"
            f"*Brief:* {brief[:600]}\n\n"
            f"_Job `{job_id}`. I'll push a follow-up commit shortly. "
            f"If this is the last allowed attempt and it still fails, I'll hand off._"
        )
        try:
            _sp.run(
                ["gh", "api", f"repos/{repo_full}/issues/{pr_num}/comments",
                 "-f", f"body={comment}"],
                check=False, timeout=15,
                stdout=_sp.PIPE, stderr=_sp.PIPE,
            )
        except Exception:
            pass
        _log({"action": "self_heal_iterate_dispatched", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "check_run_id": check_run_id, "head_ref": head_ref,
              "attempt": next_attempt, "max_attempts": SELF_HEAL_MAX_ATTEMPTS,
              "job_id": job_id, "brief_preview": brief[:300]})
    except Exception as e:
        _log({"action": "self_heal_dispatch_failed", "event": event,
              "repo": repo_full, "pr_number": pr_num,
              "error": f"{type(e).__name__}: {e!s}"})
