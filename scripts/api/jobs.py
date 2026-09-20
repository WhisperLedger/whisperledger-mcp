"""Async job runner + in-memory store for HTTP-exposed long-running operations.

Currently supports:
  - fix jobs (POST /api/v1/fix → spawns jarvis_fix.sh, polls for PR URL)

Design notes:
  - Job state lives in a module-level dict — lost on jarvis-api restart.
    Acceptable for v1 since fix runs are bounded (<10min, <$5) and callers
    can re-submit. If durable state is needed, swap to SQLite later.
  - Global asyncio.Semaphore caps concurrent in-flight jobs (default 3).
  - The bash script (jarvis_fix.sh) does its own audit logging, allowlist
    check, and budget enforcement. This module is a thin async wrapper.
  - Optional callback_url on a job: when the job terminates (success OR
    failed), POST the final FixJobStatus-shaped payload to that URL. Single
    attempt, 10s timeout. Callback failures are logged but do NOT change
    the job's own status — callers can still GET /api/v1/fix/{id} as a
    fallback.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import secrets
from agent.config import ROOT_DIR, get_env
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("astra.api.jobs")

_FIX_SCRIPT = str(ROOT_DIR / "scripts" / "astra_fix.sh")
_ITERATE_SCRIPT = str(ROOT_DIR / "scripts" / "astra_iterate.sh")
_FIX_CONCURRENCY = int(get_env("FIX_HTTP_CONCURRENCY", "3"))
_FIX_HARD_TIMEOUT_SEC = int(get_env("FIX_HTTP_TIMEOUT_SEC", "1800"))  # 30min
_CALLBACK_TIMEOUT_SEC = int(get_env("FIX_CALLBACK_TIMEOUT_SEC", "10"))
_CALLBACK_LOG = ROOT_DIR / "logs" / "fix_callbacks.jsonl"
_IDEMPOTENCY_WINDOW_SEC = int(get_env("FIX_IDEMPOTENCY_WINDOW_SEC", "300"))  # 5 min

_fix_semaphore: asyncio.Semaphore | None = None  # lazy-init on first use
_jobs: dict[str, dict] = {}
_jobs_lock = asyncio.Lock()

# Idempotency: maps caller-supplied Idempotency-Key → (job_id, created_unix_ts)
# GC'd inline whenever a check happens (no background sweeper needed).
_idempotency_keys: dict[str, tuple[str, float]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _new_job_id(prefix: str = "fix") -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


async def _get_semaphore() -> asyncio.Semaphore:
    global _fix_semaphore
    if _fix_semaphore is None:
        _fix_semaphore = asyncio.Semaphore(_FIX_CONCURRENCY)
    return _fix_semaphore


async def create_fix_job(*, repo: str, description: str, caller: str,
                         budget_usd: float = 2.0,
                         callback_url: Optional[str] = None,
                         attachments: Optional[list[str]] = None,
                         companion_pr: bool = False,
                         regression_test: bool = False,
                         jira_ticket: Optional[str] = None) -> str:
    """Create + spawn an async fix job. Returns job_id immediately (non-blocking).

    If callback_url is set, the final FixJobStatus payload is POSTed to that URL
    when the job reaches a terminal state (completed / failed). Single attempt,
    10s timeout. Receiver should still be prepared to GET /api/v1/fix/{job_id}
    if the callback never arrives (network issues, receiver down).
    """
    job_id = _new_job_id()
    async with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "endpoint": "fix",
            "status": "queued",
            "repo": repo,
            "caller": caller,
            "budget_usd": budget_usd,
            "callback_url": callback_url,
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "pr_url": None,
            "elapsed_sec": None,
            "error": None,
            "stdout_tail": None,
        }
    asyncio.create_task(_run_fix_job(job_id, repo, description, caller, budget_usd, attachments, companion_pr, regression_test, jira_ticket))
    return job_id


async def _run_fix_job(job_id: str, repo: str, description: str,
                       caller: str, budget_usd: float,
                       attachments: Optional[list[str]] = None,
                       companion_pr: bool = False,
                       regression_test: bool = False,
                       jira_ticket: Optional[str] = None) -> None:
    """Background runner. Updates the job dict through its lifecycle."""
    sem = await _get_semaphore()
    async with sem:
        async with _jobs_lock:
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["started_at"] = _now_iso()
        started = time.time()
        output = ""
        try:
            import os
            env = os.environ.copy()
            if attachments:
                env["FIX_ATTACHMENTS"] = ",".join(attachments)
            if companion_pr:
                env["FIX_COMPANION_PR"] = "1"
            if regression_test:
                env["FIX_REGRESSION_TEST"] = "1"
            if jira_ticket:
                env["FIX_JIRA_TICKET"] = jira_ticket
            proc = await asyncio.create_subprocess_exec(
                _FIX_SCRIPT,
                repo,
                description,
                f"http_api/{caller}",
                "--source", "http_api",
                "--caller", caller,
                "--budget", f"{budget_usd:.2f}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=_FIX_HARD_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                log.error("fix job %s exceeded hard timeout %ds — killing",
                          job_id, _FIX_HARD_TIMEOUT_SEC)
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                async with _jobs_lock:
                    _jobs[job_id].update(
                        status="failed",
                        finished_at=_now_iso(),
                        elapsed_sec=round(time.time() - started, 2),
                        error=f"hard timeout exceeded ({_FIX_HARD_TIMEOUT_SEC}s)",
                    )
                await _maybe_fire_callback(job_id)
                return

            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            elapsed = round(time.time() - started, 2)

            pr_url: Optional[str] = None
            error: Optional[str] = None
            m_pr = re.search(r"JARVIS_PR_URL=(\S+)", output)
            if m_pr:
                pr_url = m_pr.group(1)
            else:
                m_refuse = re.search(r"JARVIS_FIX_REFUSED=(\S+)", output)
                m_fail = re.search(r"JARVIS_FIX_FAILED=(.+)", output)
                if m_refuse:
                    error = f"Refused: {m_refuse.group(1)}"
                elif m_fail:
                    error = m_fail.group(1).strip()
                else:
                    error = f"no PR URL or failure marker in output (exit={proc.returncode})"

            async with _jobs_lock:
                j = _jobs[job_id]
                j["status"] = "completed" if pr_url else "failed"
                j["finished_at"] = _now_iso()
                j["elapsed_sec"] = elapsed
                j["pr_url"] = pr_url
                j["error"] = error
                j["stdout_tail"] = output[-2000:] if output else None
        except Exception as e:
            log.exception("fix job %s crashed", job_id)
            async with _jobs_lock:
                _jobs[job_id].update(
                    status="failed",
                    finished_at=_now_iso(),
                    elapsed_sec=round(time.time() - started, 2),
                    error=f"runner exception: {type(e).__name__}: {e}",
                    stdout_tail=output[-2000:] if output else None,
                )
        # Fire callback (best-effort) regardless of terminal reason
        await _maybe_fire_callback(job_id)


async def _maybe_fire_callback(job_id: str) -> None:
    """If the job has a callback_url, POST the terminal payload. Best-effort."""
    async with _jobs_lock:
        job = _jobs.get(job_id)
        snapshot = dict(job) if job else None
    if not snapshot:
        return
    url = snapshot.get("callback_url")
    if not url:
        return

    # Drop fields the receiver shouldn't need
    payload = {k: v for k, v in snapshot.items() if k != "callback_url"}

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _post_callback, url, payload, job_id)
        _log_callback({**result, "job_id": job_id, "url": url, "ts": _now_iso()})
        if result.get("ok"):
            log.info("callback fired for %s → %s (HTTP %s)",
                     job_id, url, result.get("status"))
        else:
            log.warning("callback failed for %s → %s: %s",
                        job_id, url, result.get("error"))
    except Exception as e:
        log.exception("callback runner crashed for %s", job_id)
        _log_callback({"job_id": job_id, "url": url, "ts": _now_iso(),
                       "ok": False, "error": f"runner: {type(e).__name__}: {e}"})


def _post_callback(url: str, payload: dict, job_id: str) -> dict:
    """Blocking single-attempt POST. Runs in the default executor (thread pool)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "jarvis-api/0.3",
            "X-Jarvis-Job-Id": job_id,
            "X-Jarvis-Event": f"fix.{payload.get('status', 'unknown')}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_CALLBACK_TIMEOUT_SEC) as resp:
            return {"ok": True, "status": resp.status,
                    "body_preview": resp.read(512).decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code,
                "error": f"HTTP {e.code}",
                "body_preview": e.read(512).decode("utf-8", errors="replace")}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"URL error: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _log_callback(record: dict) -> None:
    try:
        _CALLBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _CALLBACK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("failed to persist callback log")


async def get_job(job_id: str) -> Optional[dict]:
    async with _jobs_lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


async def list_jobs(limit: int = 50) -> list[dict]:
    """Return most-recent jobs (for ops visibility, not surfaced via HTTP)."""
    async with _jobs_lock:
        items = sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)
        return [dict(j) for j in items[:limit]]


def is_repo_write_allowed(repo: str) -> bool:
    raw = os.environ.get("JARVIS_WRITE_ALLOWED_REPOS") or ""
    allowed = {r.strip() for r in raw.replace(",", " ").split() if r.strip()}
    return repo in allowed


# --- iterate job (PR-comment iteration) -------------------------------------

async def create_iterate_job(*, repo: str, pr_number: int, caller: str,
                              budget_usd: float = 2.0,
                              callback_url: Optional[str] = None) -> str:
    """Create + spawn an async PR-iterate job. Returns job_id immediately.

    Spawns jarvis_iterate.sh which fetches review comments via gh api, runs
    Claude in a fresh checkout of the PR's branch, commits any changes, and
    pushes (NEVER force-pushes) so the existing PR auto-updates.
    """
    job_id = _new_job_id(prefix="iterate")
    async with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "endpoint": "iterate",
            "status": "queued",
            "repo": repo,
            "pr_number": pr_number,
            "caller": caller,
            "budget_usd": budget_usd,
            "callback_url": callback_url,
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "pr_url": None,
            "elapsed_sec": None,
            "error": None,
            "stdout_tail": None,
        }
    asyncio.create_task(_run_iterate_job(job_id, repo, pr_number, caller, budget_usd))
    return job_id


async def _run_iterate_job(job_id: str, repo: str, pr_number: int,
                            caller: str, budget_usd: float) -> None:
    """Background runner for iterate jobs. Shares the fix-concurrency semaphore."""
    sem = await _get_semaphore()
    async with sem:
        async with _jobs_lock:
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["started_at"] = _now_iso()
        started = time.time()
        output = ""
        try:
            proc = await asyncio.create_subprocess_exec(
                _ITERATE_SCRIPT,
                repo,
                str(pr_number),
                f"http_api/{caller}",
                "--source", "http_api",
                "--caller", caller,
                "--budget", f"{budget_usd:.2f}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=_FIX_HARD_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                log.error("iterate job %s exceeded hard timeout %ds — killing",
                          job_id, _FIX_HARD_TIMEOUT_SEC)
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                async with _jobs_lock:
                    _jobs[job_id].update(
                        status="failed",
                        finished_at=_now_iso(),
                        elapsed_sec=round(time.time() - started, 2),
                        error=f"hard timeout exceeded ({_FIX_HARD_TIMEOUT_SEC}s)",
                    )
                await _maybe_fire_callback(job_id)
                return

            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            elapsed = round(time.time() - started, 2)

            pr_url: Optional[str] = None
            error: Optional[str] = None
            # jarvis_iterate.sh emits JARVIS_ITERATE_DONE=<url> AND (for back-compat
            # with shared callback payload shape) JARVIS_PR_URL=<url>.
            m_done = re.search(r"JARVIS_ITERATE_DONE=(\S+)", output)
            if m_done:
                pr_url = m_done.group(1)
            else:
                m_refuse = re.search(r"JARVIS_ITERATE_REFUSED=(\S+)", output)
                m_fail = re.search(r"JARVIS_ITERATE_FAILED=(.+)", output)
                if m_refuse:
                    error = f"Refused: {m_refuse.group(1)}"
                elif m_fail:
                    error = m_fail.group(1).strip()
                else:
                    error = f"no DONE / FAILED marker in output (exit={proc.returncode})"

            async with _jobs_lock:
                j = _jobs[job_id]
                j["status"] = "completed" if pr_url else "failed"
                j["finished_at"] = _now_iso()
                j["elapsed_sec"] = elapsed
                j["pr_url"] = pr_url
                j["error"] = error
                j["stdout_tail"] = output[-2000:] if output else None
        except Exception as e:
            log.exception("iterate job %s crashed", job_id)
            async with _jobs_lock:
                _jobs[job_id].update(
                    status="failed",
                    finished_at=_now_iso(),
                    elapsed_sec=round(time.time() - started, 2),
                    error=f"runner exception: {type(e).__name__}: {e}",
                    stdout_tail=output[-2000:] if output else None,
                )
        await _maybe_fire_callback(job_id)


# --- idempotency helpers ----------------------------------------------------

async def check_idempotency_key(key: str) -> Optional[str]:
    """If `key` was used within the dedup window, return the original job_id.

    Returns None if the key is new, expired, or points to a job that no
    longer exists (e.g. lost on jarvis-api restart). GC happens inline.
    """
    if not key:
        return None
    now = time.time()
    async with _jobs_lock:
        # GC expired entries
        expired = [k for k, (_, ts) in _idempotency_keys.items()
                   if now - ts > _IDEMPOTENCY_WINDOW_SEC]
        for k in expired:
            _idempotency_keys.pop(k, None)
        # Look up
        entry = _idempotency_keys.get(key)
        if not entry:
            return None
        existing_job_id, _ = entry
        # Verify the job still exists (could have been lost on restart)
        if existing_job_id not in _jobs:
            _idempotency_keys.pop(key, None)
            return None
        return existing_job_id


async def record_idempotency_key(key: str, job_id: str) -> None:
    if not key:
        return
    async with _jobs_lock:
        _idempotency_keys[key] = (job_id, time.time())


# ─── migrate-mode async runner ────────────────────────────────────────────

_MIGRATE_SCRIPT = str(ROOT_DIR / "scripts" / "astra_migrate.sh")
_MIGRATE_HARD_TIMEOUT_SEC = int(get_env("MIGRATE_HTTP_TIMEOUT_SEC", "7200"))  # 2h default
_MIGRATE_BUDGET_PER_REPO_HARD_CAP = float(get_env("MIGRATE_PER_REPO_HARD_CAP_USD", "5.00"))
_MIGRATE_TOTAL_BUDGET_HARD_CAP = float(get_env("MIGRATE_TOTAL_HARD_CAP_USD", "100.00"))


def is_repo_migrate_allowed(repo: str) -> bool:
    """Migrate has its own allowlist (broader than fix's). Defaults to empty
    if unset — operator must explicitly enable repos for migrate."""
    raw = get_env("MIGRATE_ALLOWED_REPOS", "")
    allowed = {r.strip() for r in raw.replace(",", " ").split() if r.strip()}
    return repo in allowed


def migrate_total_budget_cap() -> float:
    return _MIGRATE_TOTAL_BUDGET_HARD_CAP


def migrate_per_repo_budget_cap() -> float:
    return _MIGRATE_BUDGET_PER_REPO_HARD_CAP


async def create_migrate_job(*, task: str, repos: list[str], caller: str,
                              budget_per_repo_usd: float = 1.50,
                              total_budget_usd: Optional[float] = None,
                              callback_url: Optional[str] = None,
                              stop_on_failure: bool = False) -> str:
    """Create + spawn an async migrate job. Returns job_id immediately."""
    job_id = _new_job_id(prefix="mig")
    async with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "endpoint": "migrate",
            "status": "queued",
            "task": task,
            "repos": list(repos),
            "caller": caller,
            "budget_per_repo_usd": budget_per_repo_usd,
            "total_budget_usd": total_budget_usd,
            "callback_url": callback_url,
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "n_success": 0,
            "n_failed": 0,
            "n_refused": 0,
            "total_cost_usd": 0.0,
            "pr_urls": {},      # {repo: pr_url}
            "failures": {},     # {repo: reason}
            "current_repo": None,
            "elapsed_sec": None,
            "error": None,
        }
    asyncio.create_task(_run_migrate_job(
        job_id, task, repos, caller, budget_per_repo_usd,
        total_budget_usd, stop_on_failure,
    ))
    return job_id


async def _run_migrate_job(job_id: str, task: str, repos: list[str],
                            caller: str, budget_per_repo_usd: float,
                            total_budget_usd: Optional[float],
                            stop_on_failure: bool) -> None:
    """Background runner. Spawns jarvis_migrate.sh and streams per-repo updates."""
    sem = await _get_semaphore()
    async with sem:
        async with _jobs_lock:
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["started_at"] = _now_iso()
        started = time.time()
        try:
            args = [
                _MIGRATE_SCRIPT,
                task,
                "--repos", ",".join(repos),
                "--budget-per-repo", f"{budget_per_repo_usd:.2f}",
                "--source", "http_api",
                "--caller", caller,
                "--requester", f"http_api/{caller}",
                "--migrate-id", job_id,
            ]
            if total_budget_usd is not None:
                args += ["--total-budget-usd", f"{total_budget_usd:.2f}"]
            if stop_on_failure:
                args += ["--stop-on-failure"]
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=os.environ.copy(),
            )

            # Stream output, parse per-repo events as they happen
            output_chunks: list[str] = []
            assert proc.stdout is not None
            try:
                while True:
                    try:
                        line_b = await asyncio.wait_for(
                            proc.stdout.readline(),
                            timeout=_MIGRATE_HARD_TIMEOUT_SEC,
                        )
                    except asyncio.TimeoutError:
                        log.error("migrate %s exceeded hard timeout %ds — killing",
                                  job_id, _MIGRATE_HARD_TIMEOUT_SEC)
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        async with _jobs_lock:
                            _jobs[job_id].update(
                                status="failed",
                                finished_at=_now_iso(),
                                elapsed_sec=round(time.time() - started, 2),
                                error=f"hard timeout exceeded ({_MIGRATE_HARD_TIMEOUT_SEC}s)",
                            )
                        await _maybe_fire_callback(job_id)
                        return
                    if not line_b:
                        break
                    line = line_b.decode("utf-8", errors="replace").rstrip("\n")
                    output_chunks.append(line)

                    # Per-repo progress markers (parsed live for the GET /status endpoint)
                    m_start = re.search(r"starting repo: (\S+)", line)
                    m_success = re.search(
                        r"^\[migrate\] ✓ (\S+) → (https?://\S+).*cumulative \$(\d+\.\d+)", line)
                    m_refused = re.search(
                        r"^\[migrate\] ⊘ (\S+) refused: (\S+).*cumulative \$(\d+\.\d+)", line)
                    m_failed = re.search(
                        r"^\[migrate\] ✗ (\S+) failed.*", line)
                    if m_start:
                        async with _jobs_lock:
                            _jobs[job_id]["current_repo"] = m_start.group(1)
                    elif m_success:
                        async with _jobs_lock:
                            j = _jobs[job_id]
                            j["pr_urls"][m_success.group(1)] = m_success.group(2)
                            j["n_success"] += 1
                            j["total_cost_usd"] = float(m_success.group(3))
                    elif m_refused:
                        async with _jobs_lock:
                            j = _jobs[job_id]
                            j["failures"][m_refused.group(1)] = f"refused: {m_refused.group(2)}"
                            j["n_refused"] += 1
                            j["total_cost_usd"] = float(m_refused.group(3))
                    elif m_failed:
                        async with _jobs_lock:
                            j = _jobs[job_id]
                            j["failures"][m_failed.group(1)] = "failed"
                            j["n_failed"] += 1
            finally:
                rc = await proc.wait()

            output = "\n".join(output_chunks)
            elapsed = round(time.time() - started, 2)

            # Final summary parsing (defense beyond live-streamed parse)
            m_total = re.search(r"JARVIS_MIGRATE_TOTAL_COST_USD=([\d.]+)", output)
            m_ns = re.search(r"JARVIS_MIGRATE_SUCCESS=(\d+)", output)
            m_nf = re.search(r"JARVIS_MIGRATE_FAILED=(\d+)", output)
            m_nr = re.search(r"JARVIS_MIGRATE_REFUSED=(\d+)", output)

            async with _jobs_lock:
                j = _jobs[job_id]
                if m_total: j["total_cost_usd"] = float(m_total.group(1))
                if m_ns:    j["n_success"]      = int(m_ns.group(1))
                if m_nf:    j["n_failed"]       = int(m_nf.group(1))
                if m_nr:    j["n_refused"]      = int(m_nr.group(1))
                j["current_repo"] = None
                j["elapsed_sec"] = elapsed
                j["finished_at"] = _now_iso()
                # Any-success ⇒ completed; all-fail ⇒ failed. A migrate with N
                # repos where some succeed is "partially completed" but we
                # report "completed" — caller inspects pr_urls + failures.
                if rc == 0:
                    j["status"] = "completed"
                else:
                    j["status"] = "failed"
                    j["error"] = f"jarvis_migrate.sh exited with rc={rc}"
                j["stdout_tail"] = output[-3000:]
        except Exception as e:
            log.exception("migrate job %s crashed", job_id)
            async with _jobs_lock:
                _jobs[job_id].update(
                    status="failed",
                    finished_at=_now_iso(),
                    elapsed_sec=round(time.time() - started, 2),
                    error=f"runner exception: {type(e).__name__}: {e}",
                )
        await _maybe_fire_callback(job_id)
