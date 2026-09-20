"""Slack Bolt app — handles /jarvis slash command via Socket Mode."""
from __future__ import annotations
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from agent.agent import ask, strip_cache_control
from agent.config import GITHUB_ORG, COMPANY_NAME, BOT_NAME, SLASH_COMMAND, ROOT_DIR, get_env

from .format import (
    ack_blocks, answer_to_blocks, error_blocks, feedback_thanks_blocks, help_blocks,
    fix_ack_blocks, fix_progress_blocks, fix_success_blocks, fix_failed_blocks,
    fix_rejected_blocks, fix_busy_blocks, fix_usage_blocks,
    claudify_ack_blocks, claudify_progress_blocks, claudify_success_blocks,
    claudify_usage_blocks,
    review_ack_blocks, review_progress_blocks, review_success_blocks,
    review_usage_blocks,
    nitpick_ack_blocks, nitpick_progress_blocks, nitpick_success_blocks,
    nitpick_usage_blocks,
    investigate_ack_blocks, investigate_usage_blocks,
    ask_ack_blocks, ask_success_blocks, ask_usage_blocks, ask_unknown_space_blocks,
    ask_disambiguation_blocks,
    refresh_inline_progress_blocks, refresh_inline_complete_blocks,
    refresh_async_started_blocks, refresh_dm_milestone_blocks, refresh_dm_complete_blocks,
    refresh_busy_blocks, refresh_usage_blocks,
)
from agent.investigate import build_investigation_prompt
from agent.investigate_intent import detect_investigate_intent
from agent import jove_client


# Match `ask <space-input> [refresh]: <question>`. Space-input may be either the cryptic
# space_key (TECH) or a friendly name (Technology, "data science"). We accept letters,
# digits, dots, dashes, underscores, and *spaces* (so multi-word names like
# "data science" work without quoting). The optional ` refresh` / ` --refresh` modifier
# sits before the colon.
ASK_RE = re.compile(
    r"^ask\s+(.+?)(\s+refresh|\s+--refresh)?\s*:\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
# Match `refresh <space-input>` — just kicks off a re-pull, no question.
REFRESH_RE = re.compile(r"^refresh\s+(.+?)\s*$", re.IGNORECASE)

# Latency contract: at ~1s/page, anything over 180s (3 min) is "big" and gets
# the async-with-DM flow; smaller spaces get inline progress in the same Slack
# ephemeral message.
REFRESH_INLINE_THRESHOLD_SEC = 180

# Polling cadences (per Jove team's recommendation, adjusted for big-job survival).
REFRESH_POLL_INLINE_SEC = 3
REFRESH_POLL_BACKGROUND_SEC = 30
REFRESH_HARD_TIMEOUT_SEC = 5400  # 90 min ceiling; covers TECH (~74min) with margin


LOG_DIR = Path.home() / "jarvis" / "logs"
QA_LOG_PATH = LOG_DIR / "qa_log.jsonl"
FEEDBACK_LOG_PATH = LOG_DIR / "feedback.jsonl"


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

logger = logging.getLogger("jarvis.slack")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

app = App(token=os.environ["SLACK_BOT_TOKEN"])

ALLOWED_CHANNELS = {
    c.strip() for c in (get_env("ALLOWED_CHANNELS") or "").split(",") if c.strip()
}
# Optional: individual users allowed to use the slash command from a *DM* with the bot,
# even when their DM channel isn't in ALLOWED_CHANNELS. Designed for granting
# access to a specific person (exec / partner / non-engineer) without
# adding them to the engineering pilot channel. Comma-separated Slack user IDs.
ALLOWED_DM_USERS = {
    u.strip() for u in (get_env("ALLOWED_DM_USERS") or "").split(",") if u.strip()
}

# --- mcp onboarding intake (DM handler) ---------------------------------
# Triggered by the mcp beta announcement to C092S7Z5HB5 on 2026-05-21,
# which tells engineers to DM the bot with their public SSH key + transport
# choice. The bot doesn't autonomously append to authorized_keys; it parses the
# request, auto-replies to the requester, forwards a structured ask to the
# operator (Rohit's DM), and logs to mcp_onboarding.jsonl.
OPERATOR_USER_ID = get_env("OPERATOR_USER") or "U0837N31T9C"
MCP_ONBOARDING_LOG = ROOT_DIR / "logs" / "mcp_onboarding.jsonl"
SSH_PUBKEY_RE = re.compile(
    r"(?:ssh-(?:rsa|ed25519|ecdsa-sha2-nistp(?:256|384|521))|sk-(?:ssh-ed25519|ecdsa-sha2-nistp256))"
    r"\s+[A-Za-z0-9+/=]{20,}(?:\s+\S+)?"
)

# --- conversation continuity (per-user in-memory sessions) ---------------------
# Subsequent /jarvis calls from the same user within SESSION_IDLE are treated as
# follow-ups. Capped at MAX_USER_TURNS to bound context size / cost.
SESSION_IDLE = timedelta(minutes=30)  # bumped 2026-06-20 per Tushar Chawla feedback (longer debugging sessions)
MAX_USER_TURNS = 6
RESET_PREFIXES = ("-new ", ":new ", "/new ")

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def _count_user_string_turns(messages: list[dict]) -> int:
    return sum(1 for m in messages
               if m.get("role") == "user" and isinstance(m.get("content"), str))


def _cap_turns(messages: list[dict], max_turns: int) -> list[dict]:
    """Drop oldest turns until <= max_turns user string-content messages remain."""
    user_idxs = [i for i, m in enumerate(messages)
                 if m.get("role") == "user" and isinstance(m.get("content"), str)]
    if len(user_idxs) <= max_turns:
        return messages
    cutoff = user_idxs[len(user_idxs) - max_turns]
    return messages[cutoff:]


def _has_orphan_tool_use(messages: list[dict]) -> bool:
    """True if any assistant message has tool_use blocks not matched by
    tool_result blocks in the very next message. Defensive guard against the
    state that caused production errors."""
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        tool_use_ids = {
            getattr(b, "id", None) or (b.get("id") if isinstance(b, dict) else None)
            for b in content
            if (getattr(b, "type", None) == "tool_use") or
               (isinstance(b, dict) and b.get("type") == "tool_use")
        }
        tool_use_ids.discard(None)
        if not tool_use_ids:
            continue
        # Next message must be a user message containing tool_result for each id
        if i + 1 >= len(messages):
            return True
        nxt = messages[i + 1]
        if nxt.get("role") != "user":
            return True
        nxt_content = nxt.get("content")
        if not isinstance(nxt_content, list):
            return True
        result_ids = {
            b.get("tool_use_id") for b in nxt_content
            if isinstance(b, dict) and b.get("type") == "tool_result"
        }
        if tool_use_ids - result_ids:
            return True
    return False


def get_session(user_id: str) -> tuple[list[dict], int]:
    """Return (prior_messages, this_turn_n). Turn 1 = fresh conversation.
    Silently resets the session if the stored history is malformed (orphan
    tool_use blocks etc.) to avoid surfacing API errors to the user."""
    with _sessions_lock:
        s = _sessions.get(user_id)
        if not s or datetime.now() - s["last_ts"] > SESSION_IDLE:
            return [], 1
        msgs = s["messages"]
        if _has_orphan_tool_use(msgs):
            logger.warning(f"discarding malformed session for {user_id}; starting fresh")
            _sessions.pop(user_id, None)
            return [], 1
        return list(msgs), _count_user_string_turns(msgs) + 1


def set_session(user_id: str, messages: list[dict]) -> None:
    with _sessions_lock:
        _sessions[user_id] = {
            "messages": _cap_turns(strip_cache_control(messages), MAX_USER_TURNS),
            "last_ts": datetime.now(),
        }


def reset_session(user_id: str) -> None:
    with _sessions_lock:
        _sessions.pop(user_id, None)


# --- fix mode ------------------------------------------------------------------
# Per-user concurrent-fix limit so one engineer can't kick off N fixes by accident.
_active_fixes: set[str] = set()
_active_fixes_lock = threading.Lock()
ASTRA_FIX_SCRIPT = str(ROOT_DIR / "scripts" / "astra_fix.sh")
ASTRA_CLAUDIFY_SCRIPT = str(ROOT_DIR / "scripts" / "astra_claudify.sh")
ASTRA_REVIEW_SCRIPT = str(ROOT_DIR / "scripts" / "astra_review.py")
ASTRA_NITPICK_SCRIPT = str(ROOT_DIR / "scripts" / "astra_nitpick.py")
FIX_RE = re.compile(r"^fix\s+([\w.\-]+)\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)
CLAUDIFY_RE = re.compile(r"^claudify\s+([\w.\-]+)\s*$", re.IGNORECASE)
# Match `review https://github.com/<org>/<repo>/pull/<num>` or `review <repo>#<num>`
REVIEW_RE = re.compile(
    rf"^review\s+(?:<?(https?://(?:www\.)?github\.com/{re.escape(GITHUB_ORG)}/[\w.\-]+/pull/\d+)>?"
    r"|([\w.\-]+)#(\d+))\s*$",
    re.IGNORECASE,
)
CLAUDIFY_HARD_TIMEOUT_SEC = 1500  # 25-min ceiling — official orchestrator can be slow on multi-module repos
REVIEW_HARD_TIMEOUT_SEC = 600  # 10-min ceiling for code review


def _allowed_fix_repos() -> list[str]:
    raw = os.environ.get("JARVIS_WRITE_ALLOWED_REPOS") or ""
    return sorted({s for s in re.split(r"[,\s]+", raw) if s})


# Cache Slack user_id → display name. The bot already calls users.info elsewhere
# (in agent.usage_report), so adding a lightweight lookup here keeps PR
# attribution readable ("Requested by Rohit Pandey" not "U0837N31T9C").
_NAME_CACHE: dict[str, str] = {}
_NAME_CACHE_LOCK = threading.Lock()


def _resolve_user_name(user_id: str) -> str:
    with _NAME_CACHE_LOCK:
        if user_id in _NAME_CACHE:
            return _NAME_CACHE[user_id]
    try:
        info = app.client.users_info(user=user_id).get("user", {})
        name = info.get("real_name") or info.get("name") or user_id
    except Exception:
        name = user_id
    with _NAME_CACHE_LOCK:
        _NAME_CACHE[user_id] = name
    return name


def _parse_fix_command(text: str) -> tuple[str, str] | None:
    """Match 'fix <repo>: <description>'. Returns (repo, description) or None."""
    m = FIX_RE.match(text.strip())
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


FIX_HARD_TIMEOUT_SEC = 600  # 10-min ceiling for the whole fix

# ───── BLOCK 1: regex + parse + allowlist helpers (insert near FIX_RE on line 201) ─────
MIGRATE_RE = re.compile(
    r"^migrate\s+([\w.\-,\s]+?)\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL
)
ASTRA_MIGRATE_SCRIPT = str(ROOT_DIR / "scripts" / "astra_migrate.sh")
MIGRATE_HARD_TIMEOUT_SEC = 7200  # 2h ceiling for the whole batch


def _allowed_migrate_repos() -> list[str]:
    raw = get_env("MIGRATE_ALLOWED_REPOS") or ""
    return sorted({s for s in re.split(r"[,\s]+", raw) if s})


def _parse_migrate_command(text: str) -> tuple[list[str], str] | None:
    """Match 'migrate <repo1>,<repo2>,...: <task>'. Returns (repos, task) or None."""
    m = MIGRATE_RE.match(text.strip())
    if not m:
        return None
    repos_raw = m.group(1).strip()
    task = m.group(2).strip()
    repos = [r.strip() for r in re.split(r"[,\s]+", repos_raw) if r.strip()]
    if not repos or len(task) < 10:
        return None
    return repos, task


def migrate_usage_blocks() -> list:
    allowed = _allowed_migrate_repos()
    allowed_text = ", ".join(f"`{r}`" for r in allowed) if allowed else "(none — ask Rohit)"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            ":outbox_tray: *`/jarvis migrate`* — apply the SAME task across N repos, one draft PR per repo.\n\n"
            "*Usage:* `/jarvis migrate <repo1>,<repo2>,...: <task>`\n"
            f"*Allowed repos:* {allowed_text}\n\n"
            "*Example:* `/jarvis migrate bff-core,jupiter: bump @types/node to 22`\n\n"
            "_Same fix-mode safety per repo (draft PR, budget cap). One repo failing doesn't abort the batch._"}},
    ]


def migrate_ack_blocks(repos: list[str], task: str, migrate_id: str) -> list:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":outbox_tray: *Migrate started* — {len(repos)} repo(s)\n"
            f"*Repos:* {', '.join(f'`{r}`' for r in repos[:15])}"
            f"{f' …+{len(repos)-15} more' if len(repos) > 15 else ''}\n"
            f"*Task:* {task[:300]}{'…' if len(task) > 300 else ''}\n"
            f"*Batch ID:* `{migrate_id}`\n\n"
            f"_Per-repo budget cap $1.50. I'll DM you a summary when the batch finishes "
            f"(usually 1-3 min per repo)._"}},
    ]


def migrate_rejected_blocks(disallowed: list[str], allowed: list[str]) -> list:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":no_entry_sign: *Migrate rejected* — {len(disallowed)} repo(s) not on allowlist\n"
            f"*Disallowed:* {', '.join(f'`{r}`' for r in disallowed[:10])}\n"
            f"*Currently allowed:* {', '.join(f'`{r}`' for r in allowed) if allowed else '(none)'}\n\n"
            f"_Ask Rohit (<@U0837N31T9C>) to add to `JARVIS_MIGRATE_ALLOWED_REPOS`._"}},
    ]


def migrate_result_blocks(task: str, success: dict[str, str], failures: dict[str, str],
                          total_cost: float, elapsed: float) -> list:
    lines = [
        f":outbox_tray: *Migrate complete* — {len(success)} ✓ / {len(failures)} ✗ "
        f"· ${total_cost:.2f} in {int(elapsed)}s",
        f"*Task:* {task[:300]}{'…' if len(task) > 300 else ''}",
    ]
    if success:
        lines.append("\n*✓ Successful PRs:*")
        for repo, url in list(success.items())[:25]:
            lines.append(f"• `{repo}` → <{url}|PR>")
    if failures:
        lines.append("\n*✗ Failed / refused:*")
        for repo, reason in list(failures.items())[:15]:
            lines.append(f"• `{repo}` — {reason[:100]}")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]


def _run_migrate_in_background(user_id: str, repos: list[str], task: str,
                                response_url: str, channel_id: str, log) -> None:
    """Spawn jarvis_migrate.sh, parse final summary, post result. No live progress
    streaming in v1 — keeps the Slack handler simple. Engineers wanting live progress
    should use the MCP path (jarvis_get_migrate_status) from their editor."""
    started = time.time()
    migrate_id = f"mig-slack-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"

    requester_label = _resolve_user_name(user_id)
    if requester_label != user_id:
        requester_label = f"{requester_label} ({user_id})"

    try:
        proc = subprocess.Popen(
            [ASTRA_MIGRATE_SCRIPT, task,
             "--repos", ",".join(repos),
             "--source", "slack",
             "--caller", "slack",
             "--requester", requester_label,
             "--migrate-id", migrate_id,
             "--budget-per-repo", "1.50"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except Exception:
        log.exception("migrate subprocess spawn failed")
        try:
            requests.post(response_url, json={
                "replace_original": True, "response_type": "ephemeral",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text":
                    ":x: Migrate failed to spawn — see jarvis-slack logs."}}],
                "text": "migrate failed"}, timeout=10)
        except Exception:
            pass
        return

    def hard_kill() -> None:
        time.sleep(MIGRATE_HARD_TIMEOUT_SEC)
        if proc.poll() is None:
            log.error(f"migrate exceeded {MIGRATE_HARD_TIMEOUT_SEC}s — killing PID={proc.pid}")
            try:
                proc.kill()
            except Exception:
                pass

    threading.Thread(target=hard_kill, daemon=True).start()

    output_lines: list[str] = []
    try:
        for line in proc.stdout:
            output_lines.append(line)
    except Exception:
        log.exception("migrate stdout iteration crashed")
    proc.wait()
    elapsed = time.time() - started
    output = "".join(output_lines)

    # Parse machine-readable summary from migrate.sh output
    success: dict[str, str] = {}
    failures: dict[str, str] = {}
    total_cost = 0.0

    for line in output.splitlines():
        if line.startswith("JARVIS_MIGRATE_PR="):
            entry = line.split("=", 1)[1]
            if "|" in entry:
                repo, url = entry.split("|", 1)
                success[repo.strip()] = url.strip()
        elif line.startswith("JARVIS_MIGRATE_TOTAL_COST_USD="):
            try:
                total_cost = float(line.split("=", 1)[1].strip())
            except ValueError:
                pass
    # Re-derive failures from "[migrate] ✗" and "⊘" lines
    for line in output.splitlines():
        m_fail = re.search(r"\[migrate\] ✗ (\S+) failed.*?: (.+)$", line)
        m_refuse = re.search(r"\[migrate\] ⊘ (\S+) refused: (\S+)", line)
        if m_fail:
            failures[m_fail.group(1)] = f"failed: {m_fail.group(2)[:80]}"
        elif m_refuse:
            failures[m_refuse.group(1)] = f"refused: {m_refuse.group(2)}"

    log.info(f"migrate done for {user_id}: n_success={len(success)} n_fail={len(failures)} "
             f"cost=${total_cost:.2f} elapsed={elapsed:.0f}s")

    try:
        requests.post(response_url, json={
            "replace_original": True, "response_type": "ephemeral",
            "blocks": migrate_result_blocks(task, success, failures, total_cost, elapsed),
            "text": f"Migrate done: {len(success)} success / {len(failures)} failed"}, timeout=10)
    except Exception:
        log.exception("failed to post migrate result")




def _run_fix_in_background(user_id: str, repo: str, description: str,
                           response_url: str, channel_id: str, log) -> None:
    """Spawn jarvis_fix.sh, stream its output, post mid-flight status
    updates by replacing the original ephemeral ack, then post the final
    PR-link or failure result."""
    started = time.time()
    branch_preview = f"jarvis/{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-…"

    def post_progress(emoji: str, status_text: str) -> None:
        elapsed = time.time() - started
        try:
            requests.post(response_url, json={
                "replace_original": True,
                "response_type": "ephemeral",
                "blocks": fix_progress_blocks(repo, description, branch_preview,
                                              emoji, status_text, elapsed),
                "text": f"Jarvis is fixing: {description[:80]}",
            }, timeout=10)
        except Exception:
            log.exception("failed to post fix progress")

    requester_label = _resolve_user_name(user_id)
    if requester_label != user_id:
        requester_label = f"{requester_label} ({user_id})"

    try:
        proc = subprocess.Popen(
            [ASTRA_FIX_SCRIPT, repo, description, requester_label],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,  # line-buffered
        )
    except Exception:
        log.exception("fix subprocess spawn failed")
        _post_fix_result(response_url, fix_failed_blocks(
            repo, description, "couldn't spawn the fix runner — see jarvis-slack logs."))
        return

    # Watchdog: kill the subprocess if it exceeds the hard ceiling.
    timed_out = threading.Event()

    def watchdog() -> None:
        if proc.wait(timeout=FIX_HARD_TIMEOUT_SEC) is None:
            return
        # If wait returned without killing, process exited normally; nothing to do.

    def hard_kill() -> None:
        time.sleep(FIX_HARD_TIMEOUT_SEC)
        if proc.poll() is None:
            timed_out.set()
            log.error(f"fix exceeded {FIX_HARD_TIMEOUT_SEC}s — killing PID={proc.pid}")
            try:
                proc.kill()
            except Exception:
                pass

    threading.Thread(target=hard_kill, daemon=True).start()

    full_output: list[str] = []
    posted_clone = False
    posted_claude = False
    pr_url: str | None = None
    fail_reason: str | None = None

    try:
        for line in proc.stdout:
            full_output.append(line)
            lower = line.lower()

            if not posted_clone and "cloning" in lower:
                posted_clone = True
                post_progress(":package:", "cloned workspace, preparing to investigate…")
            elif not posted_claude and "running claude" in lower:
                posted_claude = True
                post_progress(":mag:",
                              "Claude is investigating + drafting changes (usually 30-90s)…")
            elif "claude finished" in lower:
                post_progress(":outbox_tray:",
                              "Claude finished, opening draft PR…")

            if "JARVIS_PR_URL=" in line:
                pr_url = line.split("JARVIS_PR_URL=", 1)[1].strip()
            elif "JARVIS_FIX_FAILED=" in line:
                fail_reason = line.split("JARVIS_FIX_FAILED=", 1)[1].strip()
    except Exception:
        log.exception("fix stdout iteration crashed")

    proc.wait()
    elapsed = time.time() - started
    out = "".join(full_output)

    log.info(f"fix done for {user_id}: pr_url={pr_url} fail_reason={fail_reason} "
             f"elapsed={elapsed:.0f}s exit={proc.returncode} timed_out={timed_out.is_set()}")

    if timed_out.is_set():
        _post_fix_result(response_url, fix_failed_blocks(
            repo, description,
            f"timed out after {FIX_HARD_TIMEOUT_SEC // 60} minutes — the change may be "
            "larger than fix-mode handles. Try splitting it into a smaller request."))
        return

    if pr_url:
        _post_fix_result(response_url,
                         fix_success_blocks(repo, description, pr_url, elapsed))
    elif fail_reason:
        _post_fix_result(response_url,
                         fix_failed_blocks(repo, description, fail_reason))
    else:
        tail = "\n".join(out.strip().splitlines()[-5:])[:600]
        _post_fix_result(response_url, fix_failed_blocks(
            repo, description,
            f"unclear outcome (exit={proc.returncode}). Last log lines:\n```{tail}```"))


def _post_fix_result(response_url: str, blocks: list[dict]) -> None:
    try:
        requests.post(response_url, json={
            "replace_original": True,
            "response_type": "ephemeral",
            "blocks": blocks,
            "text": "Jarvis fix result",
        }, timeout=15)
    except Exception:
        logger.exception("failed to post fix result")


# --- Jove refresh polling helpers ------------------------------------------

def poll_inline_until_done(run_id: str, space_key: str, space_lbl: str, response_url: str) -> None:
    """Inline (small-space) refresh polling — updates the ephemeral Slack msg."""
    started = time.time()
    deadline = started + REFRESH_HARD_TIMEOUT_SEC
    eta_sec = jove_client.estimated_refresh_seconds(space_key)
    while time.time() < deadline:
        try:
            st = jove_client.get_run_status(run_id) or {}
        except Exception as e:
            logger.exception("get_run_status failed")
            try:
                requests.post(response_url, json={
                    "replace_original": True, "response_type": "ephemeral",
                    "blocks": fix_failed_blocks(space_lbl, "refresh",
                                                  f"polling failed: {type(e).__name__}: {e}"),
                    "text": "refresh polling error",
                }, timeout=10)
            except Exception:
                pass
            return
        status = st.get("status")
        elapsed = int(time.time() - started)
        pages_total = st.get("pages_discovered") or 0
        pages_done = st.get("pages_indexed") or 0
        if status in ("completed", "failed"):
            try:
                requests.post(response_url, json={
                    "replace_original": True, "response_type": "ephemeral",
                    "blocks": refresh_inline_complete_blocks(space_lbl, run_id, st, elapsed),
                    "text": f"Refresh of {space_lbl} {status}",
                }, timeout=10)
            except Exception:
                logger.exception("failed to post refresh completion")
            return
        try:
            requests.post(response_url, json={
                "replace_original": True, "response_type": "ephemeral",
                "blocks": refresh_inline_progress_blocks(
                    space_lbl, run_id, pages_done, eta_sec, elapsed, pages_total=pages_total),
                "text": f"Refreshing {space_lbl}…",
            }, timeout=10)
        except Exception:
            logger.exception("failed to post refresh progress")
        time.sleep(REFRESH_POLL_INLINE_SEC)


def poll_background_until_done(run_id: str, space_key: str, space_lbl: str, user_id: str) -> None:
    """Big-space refresh polling — DMs the requester at milestones + on completion.

    Uses the bot's DM channel with the requester. Posts at 25/50/75% and on done.
    """
    try:
        convo = app.client.conversations_open(users=user_id)
        dm_ch = convo["channel"]["id"]
    except Exception:
        logger.exception("could not open DM for refresh notifications")
        return

    started = time.time()
    deadline = started + REFRESH_HARD_TIMEOUT_SEC
    eta_sec = jove_client.estimated_refresh_seconds(space_key) or 1
    last_milestone_pct = 0  # which 25%-milestone we've sent

    while time.time() < deadline:
        try:
            st = jove_client.get_run_status(run_id) or {}
        except Exception:
            logger.exception("get_run_status failed (background)")
            time.sleep(REFRESH_POLL_BACKGROUND_SEC)
            continue
        status = st.get("status")
        elapsed = int(time.time() - started)
        pages_total = st.get("pages_discovered") or 0
        pages_done = st.get("pages_indexed") or 0
        if status in ("completed", "failed"):
            try:
                app.client.chat_postMessage(
                    channel=dm_ch,
                    blocks=refresh_dm_complete_blocks(space_lbl, run_id, st, elapsed),
                    text=f"Refresh of {space_lbl} {status}",
                    mrkdwn=True,
                )
            except Exception:
                logger.exception("failed to DM refresh completion")
            return
        # milestone-based DMs: 25/50/75%
        if pages_total > 0:
            pct = int(100 * pages_done / pages_total)
            if pct >= last_milestone_pct + 25 and pct < 100:
                next_milestone = (last_milestone_pct // 25 + 1) * 25
                if pct >= next_milestone:
                    try:
                        app.client.chat_postMessage(
                            channel=dm_ch,
                            blocks=refresh_dm_milestone_blocks(
                                space_lbl, run_id, pages_done, pages_total, elapsed, pct),
                            text=f"Refreshing {space_lbl} ({pct}%)",
                            mrkdwn=True,
                        )
                    except Exception:
                        logger.exception("failed to DM milestone")
                    last_milestone_pct = next_milestone
        time.sleep(REFRESH_POLL_BACKGROUND_SEC)
    logger.warning(f"refresh polling timeout for run_id={run_id}")


# --- claudify mode ----------------------------------------------------------
# Reuses the same _active_fixes set as fix-mode (one concurrent write-task per
# user is a sane limit regardless of which kind).

def _parse_claudify_command(text: str) -> str | None:
    """Match 'claudify <repo>'. Returns repo name or None."""
    m = CLAUDIFY_RE.match(text.strip())
    return m.group(1).strip() if m else None


def _run_claudify_in_background(user_id: str, repo: str, response_url: str,
                                 channel_id: str, log) -> None:
    """Spawn jarvis_claudify.sh, stream output, post mid-flight status updates,
    and post the final draft-PR link or failure result."""
    started = time.time()
    branch_preview = "add-claude-md-docs"

    def post_progress(emoji: str, status_text: str) -> None:
        elapsed = time.time() - started
        try:
            requests.post(response_url, json={
                "replace_original": True,
                "response_type": "ephemeral",
                "blocks": claudify_progress_blocks(repo, branch_preview,
                                                    emoji, status_text, elapsed),
                "text": f"Jarvis is claudifying {repo}",
            }, timeout=10)
        except Exception:
            log.exception("failed to post claudify progress")

    requester_label = _resolve_user_name(user_id)
    if requester_label != user_id:
        requester_label = f"{requester_label} ({user_id})"

    try:
        proc = subprocess.Popen(
            [ASTRA_CLAUDIFY_SCRIPT, repo, requester_label],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except Exception:
        log.exception("claudify subprocess spawn failed")
        _post_fix_result(response_url, fix_failed_blocks(
            repo, "Project Claudify",
            "couldn't spawn the claudify runner — see jarvis-slack logs."))
        return

    timed_out = threading.Event()

    def hard_kill() -> None:
        time.sleep(CLAUDIFY_HARD_TIMEOUT_SEC)
        if proc.poll() is None:
            timed_out.set()
            log.error(f"claudify exceeded {CLAUDIFY_HARD_TIMEOUT_SEC}s — killing PID={proc.pid}")
            try:
                proc.kill()
            except Exception:
                pass

    threading.Thread(target=hard_kill, daemon=True).start()

    full_output: list[str] = []
    posted_clone = False
    posted_claude = False
    pr_url: str | None = None
    fail_reason: str | None = None

    try:
        for line in proc.stdout:
            full_output.append(line)
            lower = line.lower()

            if not posted_clone and ("running official" in lower or "[repo]" in lower):
                posted_clone = True
                post_progress(":package:",
                              "orchestrator started — cloning repo + scanning module structure…")
            elif not posted_claude and "[gen]" in lower:
                posted_claude = True
                post_progress(":mag:",
                              "Claude is generating per-module + root CLAUDE.md files (parallel workers)…")
            elif "[pr]" in lower:
                post_progress(":outbox_tray:",
                              "generation done, ensuring `Claudify` label + opening PR…")

            if "JARVIS_PR_URL=" in line:
                pr_url = line.split("JARVIS_PR_URL=", 1)[1].strip()
            elif "JARVIS_FIX_FAILED=" in line:
                fail_reason = line.split("JARVIS_FIX_FAILED=", 1)[1].strip()
    except Exception:
        log.exception("claudify stdout iteration crashed")

    proc.wait()
    elapsed = time.time() - started
    out = "".join(full_output)

    log.info(f"claudify done for {user_id}: repo={repo} pr_url={pr_url} "
             f"fail_reason={fail_reason} elapsed={elapsed:.0f}s "
             f"exit={proc.returncode} timed_out={timed_out.is_set()}")

    if timed_out.is_set():
        _post_fix_result(response_url, fix_failed_blocks(
            repo, "Project Claudify",
            f"timed out after {CLAUDIFY_HARD_TIMEOUT_SEC // 60} minutes — repo may be unusually "
            "large. Try the `gh repo clone` and run `jarvis_claudify.sh` directly on the box."))
        return

    if pr_url:
        _post_fix_result(response_url,
                         claudify_success_blocks(repo, pr_url, elapsed))
    elif fail_reason:
        _post_fix_result(response_url,
                         fix_failed_blocks(repo, "Project Claudify", fail_reason))
    else:
        tail = "\n".join(out.strip().splitlines()[-5:])[:600]
        _post_fix_result(response_url, fix_failed_blocks(
            repo, "Project Claudify",
            f"unclear outcome (exit={proc.returncode}). Last log lines:\n```{tail}```"))


# --- review mode (cross-repo PR impact analysis) ----------------------------
# Reuses the _active_fixes set: 1 concurrent write-task per user across all modes.

def _parse_review_command(text: str) -> str | None:
    """Match `review <pr-url>` or `review <repo>#<num>`. Returns full PR URL or None."""
    m = REVIEW_RE.match(text.strip())
    if not m:
        return None
    if m.group(1):
        return m.group(1).strip()
    # repo#num shorthand
    return f"https://github.com/jupitermoney/{m.group(2)}/pull/{m.group(3)}"


def _run_review_in_background(user_id: str, pr_url: str, response_url: str,
                                channel_id: str, log) -> None:
    started = time.time()
    # Extract repo + num from URL for messaging
    m = re.search(rf"{re.escape(GITHUB_ORG)}/([^/]+)/pull/(\d+)", pr_url)
    repo = m.group(1) if m else "?"
    pr_num = m.group(2) if m else "?"

    def post_progress(emoji: str, status_text: str) -> None:
        elapsed = time.time() - started
        try:
            requests.post(response_url, json={
                "replace_original": True,
                "response_type": "ephemeral",
                "blocks": review_progress_blocks(repo, pr_num, emoji, status_text, elapsed),
                "text": f"Jarvis is reviewing {repo}#{pr_num}",
            }, timeout=10)
        except Exception:
            log.exception("failed to post review progress")

    requester_label = _resolve_user_name(user_id)
    if requester_label != user_id:
        requester_label = f"{requester_label} ({user_id})"

    try:
        proc = subprocess.Popen(
            [str(ROOT_DIR / "scripts" / "indexer" / ".venv" / "bin" / "python"),
             ASTRA_REVIEW_SCRIPT, pr_url, requester_label],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except Exception:
        log.exception("review subprocess spawn failed")
        _post_fix_result(response_url, fix_failed_blocks(
            f"{repo}#{pr_num}", "PR review",
            "couldn't spawn the review runner — see jarvis-slack logs."))
        return

    timed_out = threading.Event()

    def hard_kill() -> None:
        time.sleep(REVIEW_HARD_TIMEOUT_SEC)
        if proc.poll() is None:
            timed_out.set()
            log.error(f"review exceeded {REVIEW_HARD_TIMEOUT_SEC}s — killing PID={proc.pid}")
            try:
                proc.kill()
            except Exception:
                pass

    threading.Thread(target=hard_kill, daemon=True).start()

    full_output: list[str] = []
    posted_diff = False
    posted_agent = False
    pr_url_out: str | None = None
    fail_reason: str | None = None

    try:
        for line in proc.stdout:
            full_output.append(line)
            lower = line.lower()

            if not posted_diff and "fetching pr diff" in lower:
                posted_diff = True
                post_progress(":mag:", "fetched PR diff, scanning for cross-repo identifiers…")
            elif not posted_agent and "running agent" in lower:
                posted_agent = True
                post_progress(":brain:",
                              "searching all 261 repos for consumers of changed symbols…")
            elif "posting review comment" in lower:
                post_progress(":outbox_tray:", "synthesis done, posting review comment to PR…")

            if "JARVIS_PR_URL=" in line:
                pr_url_out = line.split("JARVIS_PR_URL=", 1)[1].strip()
            elif "JARVIS_FIX_FAILED=" in line:
                fail_reason = line.split("JARVIS_FIX_FAILED=", 1)[1].strip()
    except Exception:
        log.exception("review stdout iteration crashed")

    proc.wait()
    elapsed = time.time() - started
    out = "".join(full_output)
    log.info(f"review done for {user_id}: pr={repo}#{pr_num} comment_url={pr_url_out} "
             f"fail_reason={fail_reason} elapsed={elapsed:.0f}s "
             f"exit={proc.returncode} timed_out={timed_out.is_set()}")

    if timed_out.is_set():
        _post_fix_result(response_url, fix_failed_blocks(
            f"{repo}#{pr_num}", "PR review",
            f"timed out after {REVIEW_HARD_TIMEOUT_SEC // 60} minutes — diff may be unusually "
            "large or agent stuck. Try a smaller PR or run directly on the box."))
        return

    if pr_url_out:
        _post_fix_result(response_url,
                         review_success_blocks(repo, pr_num, pr_url_out, elapsed))
    elif fail_reason:
        _post_fix_result(response_url,
                         fix_failed_blocks(f"{repo}#{pr_num}", "PR review", fail_reason))
    else:
        tail = "\n".join(out.strip().splitlines()[-5:])[:600]
        _post_fix_result(response_url, fix_failed_blocks(
            f"{repo}#{pr_num}", "PR review",
            f"unclear outcome (exit={proc.returncode}). Last log lines:\n```{tail}```"))


# --- nitpick mode (intra-repo Kotlin/Java correctness review) -----------------

def _run_nitpick_in_background(user_id: str, pr_url: str, response_url: str,
                                channel_id: str, log) -> None:
    started = time.time()
    m = re.search(rf"{re.escape(GITHUB_ORG)}/([^/]+)/pull/(\d+)", pr_url)
    repo = m.group(1) if m else "?"
    pr_num = m.group(2) if m else "?"

    def post_progress(emoji: str, status_text: str) -> None:
        elapsed = time.time() - started
        try:
            requests.post(response_url, json={
                "replace_original": True,
                "response_type": "ephemeral",
                "blocks": nitpick_progress_blocks(repo, pr_num, emoji, status_text, elapsed),
                "text": f"Jarvis is nitpicking {repo}#{pr_num}",
            }, timeout=10)
        except Exception:
            log.exception("failed to post nitpick progress")

    requester_label = _resolve_user_name(user_id)
    if requester_label != user_id:
        requester_label = f"{requester_label} ({user_id})"

    try:
        proc = subprocess.Popen(
            [str(ROOT_DIR / "scripts" / "indexer" / ".venv" / "bin" / "python"),
             ASTRA_NITPICK_SCRIPT, pr_url, requester_label],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except Exception:
        log.exception("nitpick subprocess spawn failed")
        _post_fix_result(response_url, fix_failed_blocks(
            f"{repo}#{pr_num}", "nitpick",
            "couldn't spawn the nitpick runner — see jarvis-slack logs."))
        return

    timed_out = threading.Event()

    def hard_kill() -> None:
        time.sleep(REVIEW_HARD_TIMEOUT_SEC)
        if proc.poll() is None:
            timed_out.set()
            log.error(f"nitpick exceeded {REVIEW_HARD_TIMEOUT_SEC}s — killing PID={proc.pid}")
            try:
                proc.kill()
            except Exception:
                pass

    threading.Thread(target=hard_kill, daemon=True).start()

    full_output: list[str] = []
    posted_fetch = False
    posted_review = False
    pr_url_out: str | None = None
    fail_reason: str | None = None

    try:
        for line in proc.stdout:
            full_output.append(line)
            lower = line.lower()

            if not posted_fetch and "fetching pr context" in lower:
                posted_fetch = True
                post_progress(":mag:", "fetching PR diff, CLAUDE.md, and service contracts…")
            elif not posted_review and "running nitpick" in lower:
                posted_review = True
                post_progress(":brain:", "running Kotlin review checklist across diff…")
            elif "posting review" in lower:
                post_progress(":outbox_tray:", "analysis done, posting review with inline comments…")

            if "JARVIS_PR_URL=" in line:
                pr_url_out = line.split("JARVIS_PR_URL=", 1)[1].strip()
            elif "JARVIS_FIX_FAILED=" in line:
                fail_reason = line.split("JARVIS_FIX_FAILED=", 1)[1].strip()
    except Exception:
        log.exception("nitpick stdout iteration crashed")

    proc.wait()
    elapsed = time.time() - started
    out = "".join(full_output)
    log.info(f"nitpick done for {user_id}: pr={repo}#{pr_num} review_url={pr_url_out} "
             f"fail_reason={fail_reason} elapsed={elapsed:.0f}s "
             f"exit={proc.returncode} timed_out={timed_out.is_set()}")

    if timed_out.is_set():
        _post_fix_result(response_url, fix_failed_blocks(
            f"{repo}#{pr_num}", "nitpick",
            f"timed out after {REVIEW_HARD_TIMEOUT_SEC // 60} minutes."))
        return

    if pr_url_out:
        _post_fix_result(response_url, nitpick_success_blocks(repo, pr_num, pr_url_out, elapsed))
    elif fail_reason:
        _post_fix_result(response_url, fix_failed_blocks(f"{repo}#{pr_num}", "nitpick", fail_reason))
    else:
        tail = "\n".join(out.strip().splitlines()[-5:])[:600]
        _post_fix_result(response_url, fix_failed_blocks(
            f"{repo}#{pr_num}", "nitpick",
            f"unclear outcome (exit={proc.returncode}). Last log lines:\n```{tail}```"))


# --- graceful shutdown: drain in-flight queries on SIGTERM/SIGINT ---------------
# systemctl restart sends SIGTERM. We close the socket so no new events come in,
# then wait up to DRAIN_TIMEOUT for in-flight handlers to finish posting answers.
# Without this, restarts kill mid-flight queries and the user sees "thinking..."
# forever.
DRAIN_TIMEOUT_SEC = 120
_inflight_count = 0
_inflight_lock = threading.Lock()
_socket_handler: SocketModeHandler | None = None


def _inflight_inc() -> None:
    global _inflight_count
    with _inflight_lock:
        _inflight_count += 1


def _inflight_dec() -> None:
    global _inflight_count
    with _inflight_lock:
        _inflight_count -= 1


def _drain_and_exit(signum, _frame) -> None:
    name = signal.Signals(signum).name if isinstance(signum, int) else str(signum)
    logger.info(f"{name} received — closing socket, draining in-flight queries (up to {DRAIN_TIMEOUT_SEC}s)")
    if _socket_handler is not None:
        try:
            _socket_handler.close()
        except Exception:
            logger.exception("error closing socket handler")

    deadline = time.time() + DRAIN_TIMEOUT_SEC
    while time.time() < deadline:
        with _inflight_lock:
            n = _inflight_count
        if n == 0:
            logger.info("drained cleanly — exiting")
            sys.exit(0)
        logger.info(f"  still waiting for {n} in-flight queries...")
        time.sleep(2)

    with _inflight_lock:
        leftover = _inflight_count
    logger.warning(f"drain timeout — {leftover} queries still in flight, exiting anyway")
    sys.exit(1)



# --- investigate-mode handoff helpers (Tushar 2026-06-19) ---

_JARVIS_API_BASE = os.environ.get("JARVIS_API_BASE", "http://127.0.0.1:8081")
_AUTOSUPPORT_POLL_INTERVAL_SEC = int(os.environ.get("JARVIS_AUTOSUPPORT_SLACK_POLL_SEC", "8"))
_AUTOSUPPORT_POLL_MAX_SEC = int(os.environ.get("JARVIS_AUTOSUPPORT_SLACK_POLL_MAX_SEC", "300"))


def _fire_investigate_async(question: str, user_id: str, subject_user_id: str | None,
                            channel_id: str) -> str | None:
    """POST to /api/v1/autosupport/investigate. Returns investigation_id or None on failure."""
    import requests as _requests
    api_key = os.environ.get("JARVIS_API_KEY", "")
    if not api_key:
        logger.warning("no JARVIS_API_KEY — cannot fire autosupport investigate")
        return None
    body = {
        "request_id": f"slack-{user_id}-{uuid.uuid4().hex[:8]}",
        "channel": "slack",
        "user_id": subject_user_id or user_id,
        "issue_description": question[:8000],
    }
    try:
        r = _requests.post(
            f"{_JARVIS_API_BASE}/api/v1/autosupport/investigate",
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Jarvis-Caller": f"slack:{user_id}",
                "Idempotency-Key": f"slack-investigate-{user_id}-{int(time.time() // 60)}",
            },
            timeout=15,
        )
        if r.status_code in (200, 202):
            return r.json().get("investigation_id")
        logger.warning(f"autosupport investigate POST returned {r.status_code}: {r.text[:300]}")
    except Exception:
        logger.exception("autosupport investigate POST failed")
    return None


def _poll_investigation(investigation_id: str) -> dict | None:
    """Poll until COMPLETED/FAILED or timeout. Returns the full callback payload or None."""
    import requests as _requests
    api_key = os.environ.get("JARVIS_API_KEY", "")
    if not api_key:
        return None
    deadline = time.time() + _AUTOSUPPORT_POLL_MAX_SEC
    last_payload: dict | None = None
    while time.time() < deadline:
        try:
            r = _requests.get(
                f"{_JARVIS_API_BASE}/api/v1/autosupport/investigate/{investigation_id}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if r.status_code == 200:
                last_payload = r.json()
                if last_payload.get("status") in ("COMPLETED", "FAILED"):
                    return last_payload
        except Exception:
            logger.exception(f"poll {investigation_id} failed")
        time.sleep(_AUTOSUPPORT_POLL_INTERVAL_SEC)
    return last_payload


def _render_investigation_to_blocks(payload: dict, question: str) -> list[dict]:
    """Render the autosupport callback as Slack blocks. NOT raw JSON.

    Layout:
      header — :mag: Investigation result + confidence
      section — root cause hypothesis
      context — affected services + evidence count
      divider
      for each recommended_action: section with description + query/API/SQL excerpt
      footer — investigation_id + tip on copy-paste
    """
    status = payload.get("status", "?")
    findings = payload.get("findings", {}) or {}
    conf = findings.get("findings_confidence", "?")
    reason = findings.get("confidence_reason", "?")
    hypothesis = findings.get("root_cause_hypothesis", "(no hypothesis emitted)")
    affected = findings.get("affected_services") or []
    evidence = findings.get("evidence") or []
    actions = payload.get("recommended_actions") or []
    inv_id = payload.get("investigation_id", "?")
    icon = ":white_check_mark:" if status == "COMPLETED" else ":warning:"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"{icon} Investigation — {status} (confidence: {conf} / {reason})",
            "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Q:* _{question[:500]}_"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Root cause hypothesis:*\n{hypothesis[:2800]}"}},
    ]
    if affected or evidence:
        ev_types = ", ".join(sorted({e.get("type", "?") for e in evidence})) or "—"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": (f"*Affected services:* {', '.join(affected[:8]) or '—'}  "
                     f"\u00b7  *Evidence:* {len(evidence)} item(s) ({ev_types})")}]})
    blocks.append({"type": "divider"})

    for i, a in enumerate(actions[:6]):  # cap at 6 actions to fit in a Slack message
        adesc = (a.get("description") or "")[:600]
        atype = a.get("type", "?")
        aconf = a.get("actions_confidence", "?")
        routing = a.get("routing", "?")
        head = (f"*Action {i+1} · {atype} · routes to {routing} · confidence {aconf}*"
                f"{' · _SRE required_' if atype == 'requires_sre' else ''}")
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"{head}\n{adesc}"}})
        # Render embedded queries as code blocks for copy-paste
        payload_obj = a.get("payload") or {}
        for c in (payload_obj.get("api_contexts") or [])[:3]:
            method = c.get("method", "GET")
            ep = c.get("endpoint", "")
            svc = c.get("service", "")
            api_conf = c.get("api_confidence", "?")
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": (f"\u2937 `{method} {ep}` on `{svc}` _(api_confidence: {api_conf})_")}})
            if c.get("body_json"):
                bj = json.dumps(c["body_json"])[:400]
                blocks.append({"type": "section", "text": {"type": "mrkdwn",
                    "text": f"```{bj}```"}})
        for c in (payload_obj.get("database_contexts") or [])[:3]:
            tdb = c.get("target_db", "?")
            sql = (c.get("raw_sql") or "")[:600]
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": f"\u2937 `{tdb}` _(SRE will execute)_"}})
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": f"```{sql}```"}})
        if a.get("escalation_reason"):
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                "text": f":raising_hand: _{a['escalation_reason'][:300]}_"}]})
        blocks.append({"type": "divider"})

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": (f"_investigation_id: `{inv_id}`. Copy any code block and run it to get the "
                 f"actual data. Reach out if any query needs adjustment._")}]})
    return blocks


def _run_investigate_handoff(question: str, user_id: str, subject_user_id: str | None,
                             channel_id: str, response_url: str, ack_thinking_text: str) -> None:
    """Fire autosupport investigate + poll + post the rendered result to Slack."""
    import requests as _requests
    inv_id = _fire_investigate_async(question, user_id, subject_user_id, channel_id)
    if not inv_id:
        # Fallback to regular Q&A path
        logger.warning(f"investigate handoff for {user_id} failed to start — caller falls back")
        return  # caller's run() will continue to ask()

    # Update the placeholder with a more specific "investigating" message
    try:
        _requests.post(response_url, json={
            "replace_original": True, "response_type": "ephemeral",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": (f":mag: *Investigating* {('user `' + subject_user_id + '` ') if subject_user_id else ''}"
                         f"— pulling code paths, log lines, and recommended queries. "
                         f"~90-150s. (investigation_id: `{inv_id}`)")}}],
            "text": "Jarvis investigating",
        }, timeout=10)
    except Exception:
        logger.exception("could not update placeholder during investigate handoff")

    payload = _poll_investigation(inv_id)
    if not payload or payload.get("status") not in ("COMPLETED", "FAILED"):
        try:
            _requests.post(response_url, json={
                "replace_original": True, "response_type": "ephemeral",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                    "text": (f":hourglass_flowing_sand: Investigation `{inv_id}` is still running "
                             f"(>{ _AUTOSUPPORT_POLL_MAX_SEC }s). Check back with "
                             f"`/jarvis what is investigation {inv_id}` later, or DM Rohit.")}}],
                "text": "Jarvis investigation timed out",
            }, timeout=10)
        except Exception:
            pass
        return

    try:
        _requests.post(response_url, json={
            "replace_original": True, "response_type": "ephemeral",
            "blocks": _render_investigation_to_blocks(payload, question),
            "text": f"Jarvis investigation: {payload.get('status', '?')}",
        }, timeout=20)
    except Exception:
        logger.exception("failed to post investigation result to slack")


@app.command(SLASH_COMMAND)
def handle_astra_slash(ack, command, logger):
    return handle_astra_internal(ack, command, logger)

if SLASH_COMMAND != "/jarvis":
    @app.command("/jarvis")
    def handle_jarvis_slash(ack, command, logger):
        return handle_astra_internal(ack, command, logger)

def handle_astra_internal(ack, command, logger):
    text = (command.get("text") or "").strip()
    user_id = command.get("user_id", "?")
    channel_id = command.get("channel_id", "?")
    channel_name = command.get("channel_name", "?")

    # Pilot channel allowlist + per-user DM allowlist
    is_dm = channel_id.startswith("D")
    user_dm_allowed = is_dm and user_id in ALLOWED_DM_USERS
    channel_allowed = (not ALLOWED_CHANNELS) or (channel_id in ALLOWED_CHANNELS)

    if not (channel_allowed or user_dm_allowed):
        allowed_text = ", ".join(f"<#{c}>" for c in sorted(ALLOWED_CHANNELS))
        if is_dm:
            msg_text = (
                f":wave: Hi — {BOT_NAME} isn't enabled in DMs for you during the pilot, but the "
                f"good news: in {allowed_text}, *every {BOT_NAME} response is ephemeral* — "
                f"only *you* see your question and the answer, just like a DM. "
                f"Try `{SLASH_COMMAND} <your question>` from there. Ping <@U0837N31T9C> if "
                f"you'd prefer a different setup or have feedback."
            )
        else:
            msg_text = (
                f":wave: {BOT_NAME} is in pilot in {allowed_text} — try it there. "
                f"Responses are ephemeral (only you see them). "
                f"Ping <@U0837N31T9C> if you want it enabled in a different channel."
            )
        ack({"response_type": "ephemeral", "text": msg_text})
        logger.info(
            f"rejected q from {user_id} in {channel_id} "
            f"({'DM' if is_dm else channel_name}) — not in allowlist"
        )
        return
    if user_dm_allowed and not channel_allowed:
        logger.info(f"q from {user_id} in DM {channel_id} — allowed via JARVIS_ALLOWED_DM_USERS")

    # Explicit conversation reset
    lower = text.lower()
    if lower in ("-new", ":new", "/new"):
        reset_session(user_id)
        ack({"response_type": "ephemeral",
             "text": f":sparkles: Conversation reset. Your next `{SLASH_COMMAND} ...` will start fresh."})
        logger.info(f"reset session for {user_id}")
        return
    for prefix in RESET_PREFIXES:
        if lower.startswith(prefix):
            reset_session(user_id)
            text = text[len(prefix):].strip()
            logger.info(f"reset session for {user_id} (with new question)")
            break

    # Help
    if not text or text.lower() in ("help", "-h", "--help"):
        ack({"response_type": "ephemeral", "blocks": help_blocks(), "text": "Jarvis help"})
        return

    # Usage self-report — engineers see their own queries / cost / iterations today
    if text.lower() == "usage":
        rep = _usage_self_report(user_id)
        if rep["count"] == 0:
            ack({"response_type": "ephemeral",
                 "text": ":bar_chart: *Your Jarvis usage today:* nothing yet — ask me a question!"})
            return
        msg = (
            f":bar_chart: *Your Jarvis usage today (UTC):*\n"
            f"• Queries: *{rep['count']}*\n"
            f"• Cost: *${rep['cost_usd']:.3f}*\n"
            f"• Avg iterations / query: *{rep['avg_iter']}*\n"
            f"• Tokens: in `{rep['total_input_tokens']:,}` · out `{rep['total_output_tokens']:,}` · cache-read `{rep['total_cache_read_tokens']:,}`\n"
            f"_(per-engineer HTTP-API budgets are separate; this is your Slack `/jarvis` activity only)_"
        )
        ack({"response_type": "ephemeral", "text": msg})
        return

    # Refresh-Jove mode: `/jarvis refresh <space>` — re-pull a Confluence space into Jove's index.
    if text.lower().startswith("refresh"):
        m = REFRESH_RE.match(text.strip())
        if not m:
            ack({"response_type": "ephemeral",
                 "blocks": refresh_usage_blocks(), "text": "refresh usage"})
            return
        raw_space = m.group(1).strip()
        try:
            canonical_space = jove_client.resolve_space_key(raw_space)
        except Exception as e:
            logger.exception("jove list_spaces failed during refresh")
            ack({"response_type": "ephemeral",
                 "blocks": fix_failed_blocks(raw_space, "refresh",
                                              f"couldn't reach Jove: {type(e).__name__}: {e}"),
                 "text": "Jove unreachable"})
            return
        if not canonical_space:
            cands = jove_client.resolve_space_candidates(raw_space)[:8]
            if cands:
                ack({"response_type": "ephemeral",
                     "blocks": ask_disambiguation_blocks(raw_space, cands),
                     "text": f"ambiguous: {raw_space}"})
            else:
                sug = jove_client.suggest_spaces(raw_space, n=5)
                ack({"response_type": "ephemeral",
                     "blocks": ask_unknown_space_blocks(raw_space, sug),
                     "text": f"unknown space: {raw_space}"})
            return
        # Trigger refresh
        try:
            trigger_resp = jove_client.refresh_space(canonical_space)
        except Exception as e:
            logger.exception("jove_refresh_confluence_space failed")
            ack({"response_type": "ephemeral",
                 "blocks": fix_failed_blocks(canonical_space, "refresh",
                                              f"{type(e).__name__}: {e}"),
                 "text": "refresh trigger failed"})
            return
        status = (trigger_resp or {}).get("status")
        run_id = (trigger_resp or {}).get("run_id", "?")
        space_lbl = jove_client.space_label(canonical_space)
        eta_sec = jove_client.estimated_refresh_seconds(canonical_space)

        if status == "already_running":
            # Indexer busy with someone else's refresh
            ack({"response_type": "ephemeral",
                 "blocks": refresh_busy_blocks(space_lbl, trigger_resp),
                 "text": f"indexer busy"})
            return

        response_url = command["response_url"]
        logger.info(f"refresh request from {user_id}: space={canonical_space} run_id={run_id} eta={eta_sec}s")

        # Two paths: inline (small spaces) vs async-with-DM (big spaces)
        if eta_sec <= REFRESH_INLINE_THRESHOLD_SEC:
            ack({"response_type": "ephemeral",
                 "blocks": refresh_inline_progress_blocks(space_lbl, run_id, 0, eta_sec, 0),
                 "text": f"Refreshing {space_lbl}…"})

            def run_inline_refresh():
                _inflight_inc()
                try:
                    poll_inline_until_done(run_id, canonical_space, space_lbl, response_url)
                finally:
                    _inflight_dec()
            threading.Thread(target=run_inline_refresh, daemon=True).start()
        else:
            # Big space — short ack + background polling + DM milestones
            ack({"response_type": "ephemeral",
                 "blocks": refresh_async_started_blocks(space_lbl, run_id, eta_sec),
                 "text": f"Refresh of {space_lbl} started"})

            def run_async_refresh():
                _inflight_inc()
                try:
                    poll_background_until_done(run_id, canonical_space, space_lbl, user_id)
                finally:
                    _inflight_dec()
            threading.Thread(target=run_async_refresh, daemon=True).start()
        return

    # Ask-Jove mode: `/jarvis ask <space-key>: <question>` — Confluence-scoped Q&A via Jove.
    # Supports `/jarvis ask <space> refresh: <question>` to bust stale cache.
    if text.lower().startswith("ask"):
        m = ASK_RE.match(text.strip())
        if not m:
            ack({"response_type": "ephemeral",
                 "blocks": ask_usage_blocks(), "text": "ask usage"})
            return
        raw_space, refresh_mod, question = m.group(1).strip(), m.group(2), m.group(3).strip()
        refresh = bool(refresh_mod)
        # Validate space against Jove's index
        try:
            canonical_space = jove_client.resolve_space_key(raw_space)
        except Exception as e:
            logger.exception("jove list_spaces failed")
            ack({"response_type": "ephemeral",
                 "blocks": fix_failed_blocks(raw_space, "ask Jove",
                                              f"couldn't reach Jove: {type(e).__name__}: {e}"),
                 "text": "Jove unreachable"})
            return
        if not canonical_space:
            cands = jove_client.resolve_space_candidates(raw_space)[:8]
            if cands:
                ack({"response_type": "ephemeral",
                     "blocks": ask_disambiguation_blocks(raw_space, cands),
                     "text": f"ambiguous: {raw_space}"})
            else:
                sug = jove_client.suggest_spaces(raw_space, n=5)
                ack({"response_type": "ephemeral",
                     "blocks": ask_unknown_space_blocks(raw_space, sug),
                     "text": f"unknown Confluence space: {raw_space}"})
            return
        # Per-user concurrent limit (shared with fix/claudify/review)
        with _active_fixes_lock:
            if user_id in _active_fixes:
                ack({"response_type": "ephemeral",
                     "blocks": fix_busy_blocks(), "text": "Task in progress"})
                return
            _active_fixes.add(user_id)
        # Resolve Slack ID → email for Jove's user_id
        try:
            user_info = app.client.users_info(user=user_id).get("user", {})
            user_email = (user_info.get("profile") or {}).get("email") or f"{user_id}@jupiter.money"
        except Exception:
            user_email = f"{user_id}@jupiter.money"

        ack({"response_type": "ephemeral",
             "blocks": ask_ack_blocks(canonical_space, question, refresh=refresh),
             "text": f"Asking Jove about {canonical_space}: {question[:80]}"})
        response_url = command["response_url"]
        logger.info(f"ask request from {user_id}: space={canonical_space} refresh={refresh} q={question[:80]!r}")

        def run_ask() -> None:
            _inflight_inc()
            try:
                started = time.time()
                try:
                    resp = jove_client.ask(
                        query=question,
                        user_id_email=user_email,
                        confluence_space=canonical_space,
                        refresh=refresh,
                        timeout_sec=120,
                    )
                except Exception as e:
                    logger.exception("jove_chat failed")
                    requests.post(response_url, json={
                        "replace_original": True,
                        "response_type": "ephemeral",
                        "blocks": fix_failed_blocks(canonical_space, "ask Jove",
                                                     f"{type(e).__name__}: {e}"),
                        "text": "Jove call failed",
                    }, timeout=15)
                    return
                elapsed = time.time() - started
                answer_md = (resp or {}).get("response", "(Jove returned no response)")
                requests.post(response_url, json={
                    "replace_original": True,
                    "response_type": "ephemeral",
                    "blocks": ask_success_blocks(canonical_space, question, answer_md, elapsed, refresh=refresh),
                    "text": f"Jove answered (space={canonical_space})",
                }, timeout=15)
                # Persist for analytics
                _append_jsonl(QA_LOG_PATH, {
                    "qid": uuid.uuid4().hex[:12],
                    "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "user_id": user_id, "channel_id": channel_id, "turn": 1,
                    "q": f"[ask:{canonical_space}{' refresh' if refresh else ''}] {question}",
                    "answer": answer_md,
                    "iterations": 1,
                    "tool_calls": [{"name": "jove_chat", "args": {"confluence_space": canonical_space, "refresh": refresh}}],
                    "elapsed_sec": round(elapsed, 2),
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0,
                })
                logger.info(f"ask done for {user_id}: space={canonical_space} elapsed={elapsed:.0f}s")
            finally:
                _inflight_dec()
                with _active_fixes_lock:
                    _active_fixes.discard(user_id)

        threading.Thread(target=run_ask, daemon=True).start()
        return

    # Investigate-mode: `/jarvis investigate <alert-name-or-description>` — production debug.
    # Reuses the same agent + same indexed corpus, but with the structured incident-analysis
    # prompt (Source / Flow / Logs / Failure modes ranked by probability).
    if text.lower().startswith("investigate"):
        # Strip the leading `investigate` keyword + whitespace
        rest = text[len("investigate"):].strip()
        if not rest or len(rest) < 4:
            ack({"response_type": "ephemeral",
                 "blocks": investigate_usage_blocks(), "text": "investigate usage"})
            return
        # Acknowledge immediately + run agent in a background thread
        ack({"response_type": "ephemeral",
             "blocks": investigate_ack_blocks(rest),
             "text": f"Jarvis is investigating: {rest[:80]}"})
        response_url = command["response_url"]
        logger.info(f"investigate request from {user_id}: {rest[:120]!r}")

        def run_investigate() -> None:
            _inflight_inc()
            try:
                started = time.time()
                prompt = build_investigation_prompt(rest)
                try:
                    res = ask(prompt)
                except Exception as e:
                    logger.exception("investigate agent failed")
                    requests.post(response_url, json={
                        "replace_original": True,
                        "response_type": "ephemeral",
                        "blocks": error_blocks(rest, e),
                        "text": "Jarvis investigate error",
                    }, timeout=15)
                    return
                elapsed = time.time() - started
                # Render answer using the standard answer formatter (same as Q&A)
                blocks = answer_to_blocks(rest, res, qid=uuid.uuid4().hex[:12], turn_n=1)
                requests.post(response_url, json={
                    "replace_original": True,
                    "response_type": "ephemeral",
                    "blocks": blocks,
                    "text": f"Jarvis investigation: {rest[:120]}",
                }, timeout=15)
                # Persist for usage analytics
                _append_jsonl(QA_LOG_PATH, {
                    "qid": uuid.uuid4().hex[:12],
                    "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "user_id": user_id, "channel_id": channel_id, "turn": 1,
                    "q": f"[investigate] {rest}",
                    "answer": res.answer,
                    "iterations": res.iterations,
                    "tool_calls": [{"name": tc.get("name"), "args": tc.get("args", {})}
                                    for tc in (res.tool_calls or [])],
                    "elapsed_sec": round(elapsed, 2),
                    "input_tokens": res.input_tokens,
                    "output_tokens": res.output_tokens,
                    "cache_read_tokens": res.cache_read_tokens,
                    "cache_creation_tokens": res.cache_creation_tokens,
                })
                logger.info(f"investigate done for {user_id}: elapsed={elapsed:.0f}s "
                            f"iterations={res.iterations}")
            finally:
                _inflight_dec()

        threading.Thread(target=run_investigate, daemon=True).start()
        return

    # Review-mode: `/jarvis review <pr-url>` — posts cross-repo impact review comment to a PR.
    if text.lower().startswith("review"):
        pr_url = _parse_review_command(text)
        if not pr_url:
            ack({"response_type": "ephemeral",
                 "blocks": review_usage_blocks(), "text": "review usage"})
            return
        with _active_fixes_lock:
            if user_id in _active_fixes:
                ack({"response_type": "ephemeral",
                     "blocks": fix_busy_blocks(), "text": "Task in progress"})
                return
            _active_fixes.add(user_id)
        # Extract repo + num for ack message
        m = re.search(rf"{re.escape(GITHUB_ORG)}/([^/]+)/pull/(\d+)", pr_url)
        repo, pr_num = (m.group(1), m.group(2)) if m else ("?", "?")
        ack({"response_type": "ephemeral",
             "blocks": review_ack_blocks(repo, pr_num),
             "text": f"{BOT_NAME} is reviewing {repo}#{pr_num}"})
        response_url = command["response_url"]
        logger.info(f"review request from {user_id}: pr_url={pr_url!r}")

        def run_review() -> None:
            _inflight_inc()
            try:
                _run_review_in_background(user_id, pr_url, response_url, channel_id, logger)
            finally:
                _inflight_dec()
                with _active_fixes_lock:
                    _active_fixes.discard(user_id)

        threading.Thread(target=run_review, daemon=True).start()
        return

    # Nitpick-mode: `/jarvis nitpick <pr-url>` — intra-repo Kotlin/Java correctness review.
    if text.lower().startswith("nitpick"):
        pr_url = text[len("nitpick"):].strip()
        if not pr_url:
            ack({"response_type": "ephemeral",
                 "blocks": nitpick_usage_blocks(), "text": "nitpick usage"})
            return
        with _active_fixes_lock:
            if user_id in _active_fixes:
                ack({"response_type": "ephemeral",
                     "blocks": fix_busy_blocks(), "text": "Task in progress"})
                return
            _active_fixes.add(user_id)
        m = re.search(rf"{re.escape(GITHUB_ORG)}/([^/]+)/pull/(\d+)", pr_url)
        repo, pr_num = (m.group(1), m.group(2)) if m else ("?", "?")
        ack({"response_type": "ephemeral",
             "blocks": nitpick_ack_blocks(repo, pr_num),
             "text": f"{BOT_NAME} is nitpicking {repo}#{pr_num}"})
        response_url = command["response_url"]
        logger.info(f"nitpick request from {user_id}: pr_url={pr_url!r}")

        def run_nitpick() -> None:
            _inflight_inc()
            try:
                _run_nitpick_in_background(user_id, pr_url, response_url, channel_id, logger)
            finally:
                _inflight_dec()
                with _active_fixes_lock:
                    _active_fixes.discard(user_id)

        threading.Thread(target=run_nitpick, daemon=True).start()
        return

    # Claudify-mode: `/jarvis claudify <repo>` — opens draft PR with CLAUDE.md file(s).
    # No allowlist (purely additive markdown; pre-flight aborts if CLAUDE.md exists).
    if text.lower().startswith("claudify"):
        claudify_repo = _parse_claudify_command(text)
        if not claudify_repo:
            ack({"response_type": "ephemeral",
                 "blocks": claudify_usage_blocks(), "text": "claudify usage"})
            return
        with _active_fixes_lock:
            if user_id in _active_fixes:
                ack({"response_type": "ephemeral",
                     "blocks": fix_busy_blocks(), "text": "Task in progress"})
                return
            _active_fixes.add(user_id)
        ack({"response_type": "ephemeral",
             "blocks": claudify_ack_blocks(claudify_repo, "jarvis/claudify"),
             "text": f"Jarvis is claudifying {claudify_repo}"})
        response_url = command["response_url"]
        logger.info(f"claudify request from {user_id}: repo={claudify_repo!r}")

        def run_claudify() -> None:
            _inflight_inc()
            try:
                _run_claudify_in_background(user_id, claudify_repo, response_url,
                                            channel_id, logger)
            finally:
                _inflight_dec()
                with _active_fixes_lock:
                    _active_fixes.discard(user_id)

        threading.Thread(target=run_claudify, daemon=True).start()
        return


    # Migrate-mode: `/jarvis migrate <repo1>,<repo2>,...: <task>` — cross-repo rollout
    if text.lower().startswith("migrate"):
        parts = _parse_migrate_command(text)
        if not parts:
            ack({"response_type": "ephemeral",
                 "blocks": migrate_usage_blocks(), "text": "migrate-mode usage"})
            return
        migrate_repos, migrate_task = parts
        allowed = _allowed_migrate_repos()
        disallowed = [r for r in migrate_repos if r not in allowed]
        if disallowed:
            ack({"response_type": "ephemeral",
                 "blocks": migrate_rejected_blocks(disallowed, allowed),
                 "text": "Migrate rejected"})
            logger.info(f"rejected migrate from {user_id}: disallowed={disallowed}")
            return
        with _active_fixes_lock:
            if user_id in _active_fixes:
                ack({"response_type": "ephemeral",
                     "blocks": fix_busy_blocks(), "text": "Task in progress"})
                return
            _active_fixes.add(user_id)
        migrate_id = f"mig-slack-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
        ack({"response_type": "ephemeral",
             "blocks": migrate_ack_blocks(migrate_repos, migrate_task, migrate_id),
             "text": f"Jarvis is migrating {len(migrate_repos)} repo(s)"})
        response_url = command["response_url"]
        logger.info(f"migrate request from {user_id}: repos={migrate_repos} task={migrate_task[:80]!r}")
        def run_migrate() -> None:
            _inflight_inc()
            try:
                _run_migrate_in_background(user_id, migrate_repos, migrate_task,
                                            response_url, channel_id, logger)
            finally:
                _inflight_dec()
                with _active_fixes_lock:
                    _active_fixes.discard(user_id)
        threading.Thread(target=run_migrate, daemon=True).start()
        return

    # Fix-mode: `/jarvis fix <repo>: <description>`
    if text.lower().startswith("fix"):
        # Bare "fix" or malformed → usage hint
        parts = _parse_fix_command(text)
        if not parts:
            ack({"response_type": "ephemeral",
                 "blocks": fix_usage_blocks(), "text": "fix-mode usage"})
            return
        fix_repo, fix_desc = parts
        if len(fix_desc) < 10:
            ack({"response_type": "ephemeral",
                 "blocks": fix_usage_blocks(), "text": "fix-mode usage"})
            return
        allowed = _allowed_fix_repos()
        if fix_repo not in allowed:
            ack({"response_type": "ephemeral",
                 "blocks": fix_rejected_blocks(fix_repo, allowed),
                 "text": "Fix-mode rejected"})
            logger.info(f"rejected fix request from {user_id}: repo={fix_repo} (not allowlisted)")
            return
        with _active_fixes_lock:
            if user_id in _active_fixes:
                ack({"response_type": "ephemeral",
                     "blocks": fix_busy_blocks(), "text": "Fix in progress"})
                return
            _active_fixes.add(user_id)
        # Acknowledge immediately with an "on it" message
        branch_preview = f"jarvis/{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-…"
        ack({"response_type": "ephemeral",
             "blocks": fix_ack_blocks(fix_repo, fix_desc, branch_preview),
             "text": f"Jarvis is fixing: {fix_desc[:80]}"})
        response_url = command["response_url"]
        logger.info(f"fix request from {user_id}: repo={fix_repo!r} desc={fix_desc[:80]!r}")

        def run_fix() -> None:
            _inflight_inc()
            try:
                _run_fix_in_background(user_id, fix_repo, fix_desc, response_url,
                                       channel_id, logger)
            finally:
                _inflight_dec()
                with _active_fixes_lock:
                    _active_fixes.discard(user_id)

        threading.Thread(target=run_fix, daemon=True).start()
        return

    # Get conversation context
    prior_messages, turn_n = get_session(user_id)

    # Acknowledge immediately (must be < 3s) with a "thinking" placeholder.
    # All responses are ephemeral — only the asker sees them.
    ack({
        "response_type": "ephemeral",
        "blocks": ack_blocks(text, turn_n=turn_n),
        "text": f"Jarvis is thinking about: {text[:120]}",
    })

    response_url = command["response_url"]
    qid = uuid.uuid4().hex[:12]
    logger.info(f"q from {user_id} (turn {turn_n}, qid={qid}): {text[:80]!r}")

    def run() -> None:
        _inflight_inc()
        try:
            # Tushar 2026-06-19: investigate-intent handoff
            # If the question is shaped 'why did user X fail at Y / trace user Z's
            # journey / debug customer A', route to /api/v1/autosupport/investigate
            # instead of the lightweight ask() agent. Structured findings + log/DB/
            # Amplitude queries come back rendered as Slack blocks. Falls back to
            # ask() if the classifier says no, or if the handoff fails to start.
            try:
                _intent = detect_investigate_intent(text)
            except Exception:
                logger.exception('investigate_intent classifier raised')
                _intent = {'is_investigate_request': False}
            if _intent.get('is_investigate_request') and _intent.get('confidence') in ('medium', 'high'):
                logger.info(
                    f"routing q from {user_id} to autosupport (intent_conf="
                    f"{_intent.get('confidence')}, subject_user={_intent.get('subject_user_id')})"
                )
                _summary = _intent.get('summary') or text
                _subject = _intent.get('subject_user_id')
                _inv_id = _fire_investigate_async(_summary, user_id, _subject, channel_id)
                if _inv_id:
                    # Persist intent decision + handoff for audit
                    try:
                        _append_jsonl(QA_LOG_PATH, {
                            'qid': qid, 'ts': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
                            'user_id': user_id, 'channel_id': channel_id, 'turn': turn_n,
                            'q': text, 'answer': f'(routed to autosupport investigation {_inv_id})',
                            'routed_to': 'autosupport_investigate', 'investigation_id': _inv_id,
                            'intent_decision': _intent,
                            'iterations': 0, 'tool_calls': [], 'elapsed_sec': 0,
                            'input_tokens': 0, 'output_tokens': 0,
                            'cache_read_tokens': 0, 'cache_creation_tokens': 0,
                        })
                    except Exception:
                        logger.exception('failed to persist routed qa_log entry')
                    # Update placeholder + poll in this same thread (already running
                    # as a background thread). _run_investigate_handoff handles
                    # placeholder update + polling + final render.
                    _run_investigate_handoff(_summary, user_id, _subject, channel_id, response_url,
                                             ack_thinking_text=text)
                    return  # don't fall through to ask()
                # If handoff failed to start, fall through to regular ask() — no UX regression.
                logger.warning(f"investigate handoff failed to start for {user_id}; falling back to ask()")
            res = ask(text, prior_messages=prior_messages, caller_id=f"slack:{user_id}")
            set_session(user_id, res.messages)
            blocks = answer_to_blocks(text, res, turn_n=turn_n, qid=qid)
            # Persist the Q+A first so feedback events can join to it later.
            try:
                _append_jsonl(QA_LOG_PATH, {
                    "qid": qid,
                    "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "user_id": user_id,
                    "channel_id": channel_id,
                    "turn": turn_n,
                    "q": text,
                    "answer": res.answer,
                    "iterations": res.iterations,
                    "tool_calls": res.tool_calls,
                    "elapsed_sec": res.elapsed_sec,
                    "input_tokens": res.input_tokens,
                    "output_tokens": res.output_tokens,
                    "cache_read_tokens": res.cache_read_tokens,
                    "cache_creation_tokens": res.cache_creation_tokens,
                })
            except Exception:
                logger.exception("failed to persist qa_log entry")

            r = requests.post(response_url, json={
                "replace_original": True,
                "response_type": "ephemeral",
                "blocks": blocks,
                "text": f"Jarvis answered: {text[:80]}",
            }, timeout=20)
            logger.info(
                f"answered {user_id} (turn {turn_n}) in {res.elapsed_sec}s "
                f"(iters={res.iterations}, tools={len(res.tool_calls)}, "
                f"slack_post_status={r.status_code})"
            )
            if r.status_code >= 400:
                logger.error(f"slack post error body: {r.text[:500]}")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"agent error answering for {user_id}")
            # If the error is a malformed-conversation API rejection, the
            # user's session is poisoned. Reset it so their NEXT query starts
            # fresh and works.
            err_msg = str(e).lower()
            if "tool_use" in err_msg and "tool_result" in err_msg:
                reset_session(user_id)
                logger.info(f"reset session for {user_id} due to tool_use/tool_result mismatch")
            try:
                requests.post(response_url, json={
                    "replace_original": True,
                    "response_type": "ephemeral",
                    "blocks": error_blocks(text, e),
                    "text": "Jarvis hit an error",
                }, timeout=10)
            except Exception:
                logger.exception("failed to post error to slack")
        finally:
            _inflight_dec()

    threading.Thread(target=run, daemon=True).start()


# --- feedback button handlers --------------------------------------------------

def _handle_feedback(rating: str, ack, body, logger):
    ack()
    user_id = body["user"]["id"]
    actions = body.get("actions") or []
    qid = actions[0].get("value") if actions else None
    if not qid:
        logger.warning("feedback click missing qid")
        return

    try:
        _append_jsonl(FEEDBACK_LOG_PATH, {
            "qid": qid,
            "user_id": user_id,
            "rating": rating,
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        })
        logger.info(f"feedback {rating} from {user_id} for qid={qid}")
    except Exception:
        logger.exception("failed to persist feedback")

    # Replace the original message: drop buttons, add "thanks" context line.
    response_url = body.get("response_url")
    original_blocks = (body.get("message") or {}).get("blocks") or []
    if response_url and original_blocks:
        new_blocks = feedback_thanks_blocks(original_blocks, rating)
        try:
            requests.post(response_url, json={
                "replace_original": True,
                "response_type": "ephemeral",
                "blocks": new_blocks,
                "text": "feedback recorded",
            }, timeout=10)
        except Exception:
            logger.exception("failed to post feedback confirmation")


@app.action("feedback_up")
def handle_feedback_up(ack, body, logger):
    _handle_feedback("up", ack, body, logger)


@app.action("feedback_down")
def handle_feedback_down(ack, body, logger):
    _handle_feedback("down", ack, body, logger)


# ─── jarvis-mcp onboarding DM handler ───────────────────────────────────────
# Catches DMs to the Jarvis bot from anyone (not just JARVIS_ALLOWED_DM_USERS).
# Only acts when the message contains a recognizable SSH public key; otherwise
# silently ignores. No autonomous SSH writes — Rohit still appends manually.

def _detect_mcp_transport(text: str) -> str:
    t = text.lower()
    has_stdio = "stdio" in t
    has_http = "http" in t
    if has_stdio and not has_http:
        return "stdio"
    if has_http and not has_stdio:
        return "http"
    if has_stdio and has_http:
        return "both"
    return "unspecified"


def _log_mcp_onboarding(record: dict) -> None:
    try:
        MCP_ONBOARDING_LOG.parent.mkdir(parents=True, exist_ok=True)
        with MCP_ONBOARDING_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("failed to write mcp_onboarding.jsonl")


def _handle_pending_fix_confirmation(event, client, logger) -> bool:
    """Watch thread replies for `go`/`cancel`/edit on pending fix requests.
    Returns True if we handled the event (caller should short-circuit).
    """
    if event.get("subtype") in {"bot_message", "message_changed", "message_deleted",
                                 "channel_join", "channel_leave"}:
        return False
    channel_id = event.get("channel")
    thread_ts = event.get("thread_ts")
    user_id = event.get("user")
    text = (event.get("text") or "").strip()
    if not (channel_id and thread_ts and user_id and text):
        return False
    pending = _PENDING_FIX.get((channel_id, thread_ts))
    if not pending:
        return False
    # Reject confirmations from anyone other than the original requester.
    if pending["user_id"] != user_id:
        return False
    # Expire stale pending requests.
    if time.time() > pending["expires_at"]:
        _PENDING_FIX.pop((channel_id, thread_ts), None)
        return False

    lower = text.lower()
    if lower in ("go", "yes", "confirm", "fire", "ship it", "ship"):
        _PENDING_FIX.pop((channel_id, thread_ts), None)
        _fire_fix_from_thread(channel_id, thread_ts, pending["brief"], user_id, client, logger)
        return True
    if lower in ("cancel", "no", "abort", "stop"):
        _PENDING_FIX.pop((channel_id, thread_ts), None)
        try:
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                    text=":x: Cancelled — no PR drafted.")
        except Exception:
            pass
        return True
    # Edited brief: any non-empty message that ISN'T go/cancel becomes the new
    # task description.
    if len(text) >= 10:
        new_brief = dict(pending["brief"])
        new_brief["task_description"] = text
        new_brief["confidence"] = "medium"  # user-edited briefs are deliberate
        new_brief["missing_info"] = []
        try:
            client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts,
                text=(f":memo: Got the edited task — reply `go` to fire with the "
                      f"new brief, or `cancel` to abort."),
            )
        except Exception:
            pass
        pending["brief"] = new_brief
        pending["expires_at"] = time.time() + _PENDING_FIX_TTL_SEC
        return True
    return False


@app.event({"type": "message", "channel_type": "channel"})
def handle_channel_message(event, client, logger):
    """Watch channel + thread messages for pending fix-confirmation flow."""
    try:
        _handle_pending_fix_confirmation(event, client, logger)
    except Exception:
        logger.exception("pending fix confirmation handler crashed")


@app.event("message")
def handle_dm(body, event, client, logger):
    # Filter: must be a direct-message channel, not a thread/channel post.
    if event.get("channel_type") != "im":
        return
    # Skip message-edits, bot messages, system events, etc.
    if event.get("subtype"):
        return
    user_id = event.get("user")
    if not user_id:
        return
    # Ignore self — the bot's own DMs to itself (if any) shouldn't loop.
    auths = body.get("authorizations") or []
    bot_user_id = auths[0].get("user_id") if auths else None
    if bot_user_id and user_id == bot_user_id:
        return

    text = (event.get("text") or "").strip()
    if not text:
        return

    # Pending-fix confirmation (go/cancel/edited-brief reply in a DM thread
    # where Jarvis previously posted a fix brief). Runs first so it short-circuits
    # before the fix-intent regex or pubkey path picks the message up again.
    try:
        if _handle_pending_fix_confirmation(event, client, logger):
            return
    except Exception:
        logger.exception("pending fix confirmation handler crashed (DM)")

    # DM conversation-to-fix: allowlisted users can DM Jarvis a fix-shaped ask
    # and get the same Haiku-brief + confirm flow as @-mentions in channel.
    channel_id = event.get("channel")
    if (user_id in ALLOWED_DM_USERS
            and channel_id
            and _FIX_INTENT_RE.search(text)):
        thread_ts_anchor = event.get("thread_ts") or event.get("ts")
        _run_fix_intent_flow(
            event=event, body=body, client=client, logger=logger,
            user_id=user_id, channel_id=channel_id, raw_text=text,
            thread_ts_anchor=thread_ts_anchor, bot_uid=bot_user_id,
            source="dm",
        )
        return

    key_match = SSH_PUBKEY_RE.search(text)
    if not key_match:
        # Not an onboarding-shaped, fix-intent, or pending-confirm DM. Silently
        # ignore. (DM free-text Q&A is a separate, larger ship — for now users
        # should use `/jarvis <question>` in DM for Q&A.)
        return

    pubkey = key_match.group(0).strip()
    transport = _detect_mcp_transport(text)
    channel_id = event.get("channel")
    ts = event.get("ts")

    # 1) Auto-reply to the requester so they don't see silence.
    try:
        client.chat_postMessage(
            channel=channel_id,
            text=(
                ":white_check_mark: Got your jarvis-mcp onboarding request — "
                f"forwarded for approval (transport: *{transport}*).\n"
                "You'll hear back via DM once your SSH key is added"
                + (" and your bearer token is ready" if transport in ("http", "both") else "")
                + f". Reference: `{ts}`"
            ),
        )
    except Exception:
        logger.exception("failed to auto-reply to mcp onboarding DM")

    # 2) Forward a structured request to the operator's DM.
    try:
        forward_text = (
            f"*mcp-onboarding request from <@{user_id}>*\n"
            f"Transport: `{transport}`\n"
            f"Public key:\n```{pubkey}```\n"
            f"Requester DM channel: `{channel_id}`  ·  msg ts: `{ts}`\n"
            "_To approve: append the key to `/home/ubuntu/.ssh/authorized_keys` "
            "on the box, then DM the requester to confirm (and hand over the "
            "bearer if transport includes http)._"
        )
        client.chat_postMessage(channel=OPERATOR_USER_ID, text=forward_text)
    except Exception:
        logger.exception("failed to forward mcp onboarding DM to operator")

    # 3) Audit log (don't log full key — keep first 40 chars of key-data only).
    key_parts = pubkey.split()
    fp = (key_parts[1][:40] + "...") if len(key_parts) >= 2 else "?"
    _log_mcp_onboarding(
        {
            "ts": datetime.now().isoformat(),
            "from_user": user_id,
            "dm_channel": channel_id,
            "msg_ts": ts,
            "transport": transport,
            "key_type": key_parts[0] if key_parts else "?",
            "key_data_prefix": fp,
            "msg_text_len": len(text),
        }
    )
    logger.info(
        f"mcp-onboarding: user={user_id} transport={transport} key={key_parts[0] if key_parts else '?'}"
    )


# ──────────────────── conversation-to-fix hand-off ────────────────────
# Detects when an @-mention is asking Jarvis to draft a code fix, extracts a
# brief from the thread, posts it for confirmation, and fires /api/v1/fix on
# explicit `go`. The pending-request dict is in-memory (lost on restart —
# acceptable, the worst case is a stale "reply go to fire" prompt the user
# ignores).

def _run_qna_fallback(event, body, client, logger, user_id, channel_id,
                       raw_text, thread_ts, prior_messages, bot_uid):
    """When fix-intent regex hits but LLM rejects, fall back to normal Q&A.
    Mirrors the standard @mention path below — extracted as a function so the
    fix-intent branch can re-enter it cleanly without duplicating logic.
    """
    qid = uuid.uuid4().hex[:12]
    logger.info(f"@mention QA fallback from {user_id} qid={qid} q={raw_text[:80]!r}")
    try:
        placeholder = client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text=":thought_balloon: Thinking…",
            unfurl_links=False, unfurl_media=False,
        )
        placeholder_ts = placeholder.get("ts")
    except Exception:
        placeholder_ts = None

    _inflight_inc()
    started = time.time()
    res = None
    try:
        question = raw_text or "Please respond to the conversation above."
        res = ask(question, prior_messages=prior_messages, caller_id=f"slack:{user_id}")
        answer = (res.answer or "").strip() or ":warning: (empty answer)"
    except Exception as e:
        logger.exception("@mention QA fallback agent failed")
        answer = f":warning: Agent error: `{type(e).__name__}: {e!s}`"

    if len(answer) > 3500:
        answer = answer[:3450] + "\n\n…(truncated)"
    try:
        if placeholder_ts:
            client.chat_update(channel=channel_id, ts=placeholder_ts, text=answer)
        else:
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                    text=answer, unfurl_links=False, unfurl_media=False)
    except Exception:
        logger.exception("@mention QA fallback post failed")
    _inflight_dec()


_FIX_INTENT_RE = __import__("re").compile(
    r"\b(fix\s+(?:this|it|that)|draft\s+(?:a\s+)?pr|open\s+(?:a\s+)?fix|"
    r"make\s+this\s+change|implement\s+(?:this|it)|create\s+(?:a\s+)?fix|"
    r"raise\s+(?:a\s+)?pr|generate\s+(?:a\s+)?pr|jarvis\s+fix)\b",
    __import__("re").I,
)

_PENDING_FIX: dict = {}  # (channel_id, thread_ts) -> {user_id, brief, expires_at}
_PENDING_FIX_TTL_SEC = 600  # 10 min to confirm


def _fmt_fix_brief_block(brief: dict, requester: str) -> str:
    repo = brief.get("repo") or "?"
    task = brief.get("task_description") or "(no task extracted)"
    files = brief.get("file_pointers") or []
    conf = brief.get("confidence") or "?"
    missing = brief.get("missing_info") or []
    lines = [
        ":package: *Detected fix request from this thread.*",
        f"*Repo:* `{repo}`",
        f"*Task:* {task}",
    ]
    if files:
        lines.append(f"*Files:* " + ", ".join(f"`{f}`" for f in files[:6]))
    lines.append(f"*Confidence:* `{conf}`")
    if missing:
        lines.append(f"*Could be sharper:* " + " · ".join(missing[:4]))
    lines.append("")
    lines.append(
        f"<@{requester}> reply `go` to fire `/api/v1/fix` (~$0.50-$2, ~5 min), "
        "`cancel` to abort, or paste an *edited task line* and I'll use that brief instead."
    )
    return "\n".join(lines)


def _fire_fix_from_thread(channel_id: str, thread_ts: str, brief: dict,
                          requester: str, client, logger) -> None:
    """Call /api/v1/fix and post the eventual PR URL back into the thread."""
    import urllib.request, json as _json
    repo = brief.get("repo")
    task = brief.get("task_description") or ""
    if not repo or not task:
        try:
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                    text=":warning: Brief is incomplete — won't fire.")
        except Exception:
            pass
        return
    api_key = os.environ.get("JARVIS_API_KEY", "")
    if not api_key:
        try:
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                    text=":warning: JARVIS_API_KEY not set in env.")
        except Exception:
            pass
        return
    body = {
        "repo": repo,
        "description": task,
        "max_budget_usd": 3.0,
        # No callback_url: we'll poll OR rely on the user reading
        # ~/jarvis/logs/fix_audit.jsonl. The async pattern returns 202 + job_id.
    }
    req = urllib.request.Request(
        "http://127.0.0.1:8081/api/v1/fix",
        data=_json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Jarvis-Caller": f"slack-mention:{requester}",
            "Idempotency-Key": f"slack-mention-{channel_id}-{thread_ts}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ack = _json.loads(r.read().decode())
        job_id = ack.get("job_id", "?")
        try:
            client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts,
                text=(f":hammer_and_wrench: Fix-mode dispatched — job `{job_id}`. "
                      f"I'll post the draft-PR URL here when it lands "
                      f"(~3-7 min). Use `tail ~/jarvis/logs/fix_audit.jsonl` "
                      f"on the box to follow live."),
                unfurl_links=False,
            )
        except Exception:
            pass
        # Spawn a tiny poller — fire-and-forget, daemon thread.
        threading.Thread(
            target=_poll_and_post_fix_result,
            args=(channel_id, thread_ts, job_id, api_key, client, logger),
            daemon=True,
        ).start()
    except Exception as e:
        logger.exception("fix dispatch failed")
        try:
            client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts,
                text=f":x: Fix dispatch failed: `{type(e).__name__}: {e!s}`",
            )
        except Exception:
            pass


def _poll_and_post_fix_result(channel_id: str, thread_ts: str, job_id: str,
                              api_key: str, client, logger) -> None:
    """Poll GET /api/v1/fix/<job_id> until terminal, then post the result."""
    import urllib.request, json as _json
    started = time.time()
    while time.time() - started < 900:  # 15-min ceiling
        time.sleep(15)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:8081/api/v1/fix/{job_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                status = _json.loads(r.read().decode())
        except Exception:
            continue
        s = status.get("status")
        if s in ("queued", "running"):
            continue
        # Terminal — post result.
        if s == "completed":
            pr = status.get("pr_url") or "(no pr_url field)"
            msg = f":tada: Draft PR ready: {pr}"
        elif s == "refused":
            reason = status.get("refused_reason") or "unknown"
            msg = f":warning: Fix mode refused ({reason}). Check the brief and try again."
        else:
            msg = f":x: Fix mode finished with status `{s}`. Check `~/jarvis/logs/fix_audit.jsonl` for details."
        try:
            client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                    text=msg, unfurl_links=False)
        except Exception:
            pass
        return
    try:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                text=":hourglass_flowing_sand: Fix mode is taking longer than 15 min — "
                                     "check `~/jarvis/logs/fix_audit.jsonl` for status.")
    except Exception:
        pass


def _run_fix_intent_flow(*, event, body, client, logger, user_id, channel_id,
                         raw_text, thread_ts_anchor, bot_uid, source):
    """Shared conversation-to-fix flow used by @-mentions and DMs.

    `source` is "mention" or "dm" — controls fallback shape when the Haiku
    extractor rejects fix-intent: mentions fall through to Q&A (channel agent
    already wired), DMs post a nudge toward `/jarvis fix` / `/jarvis <q>` since
    DM free-text Q&A isn't wired yet.
    """
    qid = uuid.uuid4().hex[:12]
    prior_for_extract: list[dict] = []
    if event.get("thread_ts"):
        try:
            rr = client.conversations_replies(channel=channel_id,
                                              ts=event["thread_ts"], limit=40)
            for msg in (rr.get("messages") or []):
                if msg.get("ts") == event.get("ts"):
                    continue
                cnt = (msg.get("text") or "").strip()
                if bot_uid:
                    cnt = cnt.replace(f"<@{bot_uid}>", "").strip()
                if not cnt:
                    continue
                rl = "assistant" if msg.get("user") == bot_uid else "user"
                prior_for_extract.append({"role": rl, "content": cnt[:4000]})
        except Exception:
            logger.exception(f"conversations.replies during fix-intent failed (qid={qid})")

    def _do_fix_intent():
        try:
            from agent.thread_to_fix import extract as _extract_brief
            brief = _extract_brief(prior_for_extract, raw_text, user_id)
        except Exception:
            logger.exception(f"thread_to_fix extractor crashed (qid={qid})")
            return
        if not brief.get("is_fix_request"):
            logger.info(f"fix-intent regex hit but LLM rejected: {brief.get('reason')!r}; "
                        f"source={source} qid={qid}")
            if source == "mention":
                _run_qna_fallback(event, body, client, logger, user_id, channel_id,
                                  raw_text, thread_ts_anchor, prior_for_extract, bot_uid)
            else:
                try:
                    client.chat_postMessage(
                        channel=channel_id, thread_ts=thread_ts_anchor,
                        text=(":thinking_face: That didn't parse as a fix request. "
                              "Try `/jarvis fix <jira-ticket-url>` for a structured fix, "
                              "or `/jarvis <question>` for Q&A — free-text DMs to Jarvis "
                              "only support fix-intent today."),
                        unfurl_links=False,
                    )
                except Exception:
                    logger.exception(f"post DM Q&A nudge failed (qid={qid})")
            return
        text_block = _fmt_fix_brief_block(brief, user_id)
        try:
            client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts_anchor,
                text=text_block, unfurl_links=False, unfurl_media=False,
            )
        except Exception:
            logger.exception(f"post fix brief failed (qid={qid})")
            return
        _PENDING_FIX[(channel_id, thread_ts_anchor)] = {
            "user_id": user_id,
            "brief": brief,
            "expires_at": time.time() + _PENDING_FIX_TTL_SEC,
        }
        logger.info(f"fix-intent brief posted source={source} user={user_id} qid={qid}")

    threading.Thread(target=_do_fix_intent, daemon=True).start()


@app.event("app_mention")
def handle_app_mention(event, client, logger, body):
    """Jarvis is @-mentioned in a channel — answer in the thread with full context."""
    # Skip bot/system messages and edits
    if event.get("subtype") in {"bot_message", "message_changed", "message_deleted",
                                 "channel_join", "channel_leave"}:
        return
    user_id = event.get("user")
    channel_id = event.get("channel")
    if not user_id or not channel_id:
        return

    # Identify our own bot id so we can (a) strip our own @-mention from the
    # question text and (b) classify thread messages as assistant vs user.
    bot_uid = ((body.get("authorizations") or [{}])[0] or {}).get("user_id")
    if bot_uid and user_id == bot_uid:
        return  # never react to our own posts

    # Allowlist (same as /jarvis). Mentions in non-allowlisted channels are silently
    # ignored so we don't spam audit logs or pollute random channels with refusals.
    if ALLOWED_CHANNELS and channel_id not in ALLOWED_CHANNELS:
        logger.info(f"app_mention ignored: {channel_id} not in JARVIS_ALLOWED_CHANNELS")
        return

    # Question text = the mention message minus the literal @-mention token.
    raw_text = (event.get("text") or "").strip()
    if bot_uid:
        raw_text = raw_text.replace(f"<@{bot_uid}>", "").strip()
    # Empty mention ("hey @jarvis") is fine — we'll answer from thread context.

    # ─── Conversation-to-fix hand-off ───
    # If the user is asking Jarvis to draft a PR from this thread, extract a
    # brief via Haiku and post it for confirmation (don't fire yet).
    if raw_text and _FIX_INTENT_RE.search(raw_text):
        thread_ts_anchor = event.get("thread_ts") or event.get("ts")
        _run_fix_intent_flow(
            event=event, body=body, client=client, logger=logger,
            user_id=user_id, channel_id=channel_id, raw_text=raw_text,
            thread_ts_anchor=thread_ts_anchor, bot_uid=bot_uid,
            source="mention",
        )
        return  # don't proceed to the Q&A path


    # Decide where to reply. If the mention is inside a thread, reply in that
    # thread. If it's a top-level channel post, start a new thread anchored on
    # the mention itself (less channel noise than top-level replies).
    thread_ts = event.get("thread_ts") or event.get("ts")
    mention_ts = event.get("ts")

    # Pull prior messages from the thread for context. Skip the mention message
    # itself — we'll pass that as the current question.
    prior_messages = []
    try:
        if event.get("thread_ts"):
            resp = client.conversations_replies(channel=channel_id,
                                                ts=event["thread_ts"], limit=40)
            for msg in (resp.get("messages") or []):
                if msg.get("ts") == mention_ts:
                    continue
                content = (msg.get("text") or "").strip()
                if bot_uid:
                    content = content.replace(f"<@{bot_uid}>", "").strip()
                if not content:
                    continue
                role = "assistant" if msg.get("user") == bot_uid else "user"
                prior_messages.append({"role": role, "content": content[:4000]})
    except Exception:
        logger.exception("conversations.replies fetch failed")

    qid = uuid.uuid4().hex[:12]
    logger.info(f"@mention from {user_id} in {channel_id} thread={thread_ts} "
                f"prior_msgs={len(prior_messages)} qid={qid} q={raw_text[:80]!r}")

    # Post "thinking" placeholder in the thread immediately so the user knows
    # we heard them. Update with the answer when ready.
    try:
        placeholder = client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text=":thought_balloon: Thinking…",
            unfurl_links=False, unfurl_media=False,
        )
        placeholder_ts = placeholder.get("ts")
    except Exception:
        logger.exception("placeholder post failed")
        placeholder_ts = None

    def run() -> None:
        _inflight_inc()
        started = time.time()
        res = None
        try:
            # If the user mentioned with no extra text, treat it as
            # "respond to / continue this conversation".
            question = raw_text or "Please respond to the conversation above."
            res = ask(question, prior_messages=prior_messages, caller_id=f"slack:{user_id}")
            answer = (res.answer or "").strip()
            if not answer:
                answer = ":warning: (empty answer)"
        except Exception as e:
            logger.exception("@mention agent failed")
            answer = f":warning: Agent error: `{type(e).__name__}: {e!s}`"

        # Slack message limit is 40k chars but Block Kit text limit is 3000.
        # Truncate gracefully if needed.
        if len(answer) > 3500:
            answer = answer[:3450] + "\n\n…(truncated)"

        try:
            if placeholder_ts:
                client.chat_update(channel=channel_id, ts=placeholder_ts,
                                    text=answer)
            else:
                client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                        text=answer,
                                        unfurl_links=False, unfurl_media=False)
        except Exception:
            logger.exception("@mention reply post/update failed")

        # Audit to qa_log so usage reports + cost dashboards pick it up.
        try:
            _append_jsonl(QA_LOG_PATH, {
                "qid": qid,
                "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "user_id": user_id,
                "channel_id": channel_id,
                "source": "mention",
                "thread_ts": thread_ts,
                "prior_msgs_count": len(prior_messages),
                "turn": 1,
                "q": raw_text,
                "answer": (res.answer if res else "")[:8000],
                "iterations": getattr(res, "iterations", 0) if res else 0,
                "tool_calls": getattr(res, "tool_calls", []) if res else [],
                "elapsed_sec": round(time.time() - started, 2),
                "input_tokens": getattr(res, "input_tokens", 0) if res else 0,
                "output_tokens": getattr(res, "output_tokens", 0) if res else 0,
                "cache_read_tokens": getattr(res, "cache_read_tokens", 0) if res else 0,
                "cache_creation_tokens": getattr(res, "cache_creation_tokens", 0) if res else 0,
            })
        except Exception:
            logger.exception("failed to persist mention qa_log entry")
        finally:
            _inflight_dec()

    threading.Thread(target=run, daemon=True).start()


def main() -> None:
    global _socket_handler
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        raise SystemExit("SLACK_APP_TOKEN not set")
    signal.signal(signal.SIGTERM, _drain_and_exit)
    signal.signal(signal.SIGINT, _drain_and_exit)
    logger.info("Jarvis Slack bot starting (socket mode) — graceful drain enabled (SIGTERM/SIGINT)")
    _socket_handler = SocketModeHandler(app, app_token)
    _socket_handler.start()


if __name__ == "__main__":
    main()
