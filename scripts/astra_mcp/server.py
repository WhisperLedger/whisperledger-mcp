"""Jarvis MCP server — Phases 1+2+3.

Transports:
  stdio          — Phase 1. Spawned per-session by a local MCP client over SSH.
  streamable-http — Phase 2. Long-running service on 127.0.0.1:8082 (bind via
                    JARVIS_MCP_HOST/PORT). Bearer-auth required (same
                    JARVIS_API_KEY as jarvis-api). Engineers tunnel via
                    `ssh -L 8082:localhost:8082`; remote bots can target
                    direct once we expose the port externally.

Tool surface (13 total):
  Phase 1 (read-only retrieval, 10 tools):
    jarvis_search_code, jarvis_read_file, jarvis_search_prs,
    jarvis_git_history, jarvis_list_repo_files, jarvis_list_indexed_repos,
    jarvis_find_repo, jarvis_fetch_jira_ticket, jarvis_fetch_pr_diff,
    jarvis_get_capabilities
  Phase 3 (trigger-shim — POST to localhost jarvis-api, 5 tools):
    jarvis_fire_fix, jarvis_fire_iterate, jarvis_fire_preflight,
    jarvis_get_fix_status, jarvis_get_iterate_status

Per the non-regression contract in memory/project_mcp_hybrid_decision.md:
  - No edits to scripts/agent/tools.py or any HTTP-API / Slack code path.
  - Phase 3 trigger tools POST to localhost:8081 via the existing HTTP API —
    they never reach into jarvis-api's in-memory job store directly.
  - Phase 3 REQUIRES Idempotency-Key on write triggers (fix, iterate) —
    refuses without one to prevent the kind of double-spend that hit
    RECO-1259 before Idempotency-Key existed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from agent import tools as agent_tools  # noqa: E402
from agent import acl as _acl  # noqa: E402

AUDIT_LOG = Path.home() / "jarvis" / "logs" / "mcp_audit.jsonl"
CALLER = os.environ.get("JARVIS_MCP_CALLER") or "local"
# Per-request user email (set by BearerAuth middleware on jrv_ tokens). Reads
# from contextvar so _audit can attribute MCP tool calls to the right engineer.
import contextvars as _contextvars
_CURRENT_USER_EMAIL: _contextvars.ContextVar[str | None] = _contextvars.ContextVar(
    "_CURRENT_USER_EMAIL", default=None,
)
JARVIS_API_BASE = os.environ.get("JARVIS_API_BASE", "http://127.0.0.1:8081")

mcp = FastMCP("jarvis")


# ───────────────────────── audit + wrap helpers ──────────────────────────


def _audit(tool: str, args: dict, latency_ms: int, ok: bool, error: str | None = None) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "caller": (_CURRENT_USER_EMAIL.get() or CALLER),
                        "tool": tool,
                        "args": {
                            k: (str(v)[:200] if isinstance(v, str) else v)
                            for k, v in args.items()
                        },
                        "latency_ms": latency_ms,
                        "ok": ok,
                        "error": error,
                    }
                )
                + "\n"
            )
    except Exception:
        pass


def _wrap(name: str, fn, **kwargs) -> str:
    t0 = time.time()
    # MCP maps the per-engineer token to the caller email (via bearer auth
    # middleware); pass that into the ACL contextvar so restricted-collection
    # gates in agent/tools.py evaluate against the right identity.
    _caller = _CURRENT_USER_EMAIL.get() or CALLER
    _tok = _acl.set_caller(_caller)
    try:
        out = fn(**kwargs)
        _audit(name, kwargs, int((time.time() - t0) * 1000), True)
        return out
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _audit(name, kwargs, int((time.time() - t0) * 1000), False, msg)
        return json.dumps({"error": msg})
    finally:
        _acl.reset_caller(_tok)


def _api_request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    extra_headers: dict | None = None,
    timeout: float = 30.0,
    tool_name: str = "api",
) -> str:
    """POST/GET against the local jarvis-api. Returns JSON string with the response
    body (or {"error": "..."}). Audited.
    """
    t0 = time.time()
    api_key = os.environ.get("JARVIS_API_KEY")
    if not api_key:
        msg = "JARVIS_API_KEY not set on server"
        _audit(tool_name, {"path": path}, 0, False, msg)
        return json.dumps({"error": msg})
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    url = f"{JARVIS_API_BASE}{path}"
    try:
        with httpx.Client(timeout=timeout) as client:
            if method == "POST":
                resp = client.post(url, headers=headers, json=json_body or {})
            elif method == "GET":
                resp = client.get(url, headers=headers)
            else:
                raise ValueError(f"unsupported method: {method}")
        ms = int((time.time() - t0) * 1000)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:2000]}
        ok = 200 <= resp.status_code < 300
        _audit(
            tool_name,
            {"path": path, "status": resp.status_code},
            ms,
            ok,
            None if ok else f"HTTP {resp.status_code}",
        )
        return json.dumps({"http_status": resp.status_code, "body": body})
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _audit(tool_name, {"path": path}, int((time.time() - t0) * 1000), False, msg)
        return json.dumps({"error": msg})


# ──────────────────────── Phase 1: retrieval tools ───────────────────────


@mcp.tool()
def jarvis_search_code_reranked(query: str, repo: str | None = None, k: int = 5) -> str:
    """Vector search + Haiku 4.5 cross-encoder reranker. Pulls top-20 candidates,
    reranks against a relevance rubric, returns top-k. Adds ~1-2s latency + ~$0.003
    per query for higher answer quality. Falls back to vector order on Haiku error.
    """
    return _wrap("search_code_reranked", agent_tools.search_code_reranked,
                 query=query, repo=repo, k=k)


@mcp.tool()
def jarvis_search_code(query: str, repo: str | None = None, k: int = 8) -> str:
    """Semantic search across the indexed Jupiter code corpus (261+ jupitermoney/* repos, voyage-code-3 + Qdrant).

    Args:
        query: Natural-language search query.
        repo: Optional. Restrict to one indexed repo.
        k: Number of hits (1-20). Default 8.
    """
    return _wrap("search_code", agent_tools.search_code, query=query, repo=repo, k=k)


@mcp.tool()
def jarvis_read_file(
    repo: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read a file (or line range) from an indexed jupitermoney/<repo> clone. 200KB / 2000-line cap."""
    return _wrap(
        "read_file",
        agent_tools.read_file,
        repo=repo,
        path=path,
        start_line=start_line,
        end_line=end_line,
    )


@mcp.tool()
def jarvis_search_prs(query: str, repo: str | None = None, k: int = 8) -> str:
    """Semantic search over PR descriptions (rationale / 'why' content). Use for design-intent questions."""
    return _wrap("search_prs", agent_tools.search_prs, query=query, repo=repo, k=k)


@mcp.tool()
def jarvis_git_history(repo: str, path: str, limit: int = 10) -> str:
    """Last N commits touching a file. Returns commit subject + body (the WHY)."""
    return _wrap("git_history", agent_tools.git_history, repo=repo, path=path, limit=limit)


@mcp.tool()
def jarvis_list_repo_files(repo: str, glob: str = "**/*", limit: int = 100) -> str:
    """List file paths in a repo matching a glob (e.g. '**/auth/**/*.kt'). Up to 200 entries."""
    return _wrap(
        "list_repo_files", agent_tools.list_repo_files, repo=repo, glob=glob, limit=limit
    )


@mcp.tool()
def jarvis_list_indexed_repos() -> str:
    """Return the list of all jupitermoney/* repos Jarvis has indexed."""
    return json.dumps(
        {"repos": agent_tools.INDEXED_REPOS, "count": len(agent_tools.INDEXED_REPOS)}
    )


@mcp.tool()
def jarvis_find_repo(needles: list[str]) -> str:
    """Fuzzy-find indexed repos by name/description substrings (any-match, case-insensitive)."""
    repos_json = Path.home() / "jarvis" / "index" / "repos_v2.json"
    if not repos_json.exists():
        return json.dumps({"error": f"repos_v2.json not found at {repos_json}"})
    t0 = time.time()
    try:
        lowered = [n.lower() for n in needles]
        repos = json.loads(repos_json.read_text())
        hits = [
            r
            for r in repos
            if any(
                n in r["name"].lower() or n in (r.get("description") or "").lower()
                for n in lowered
            )
        ]
        hits.sort(key=lambda r: r["pushedAt"], reverse=True)
        out = []
        for r in hits[:30]:
            lang_obj = r.get("primaryLanguage")
            out.append(
                {
                    "name": r["name"],
                    "language": lang_obj.get("name") if lang_obj else None,
                    "pushed_at": r["pushedAt"],
                    "description": r.get("description"),
                    "archived": r["isArchived"],
                    "fork": r["isFork"],
                }
            )
        _audit("find_repo", {"needles": needles}, int((time.time() - t0) * 1000), True)
        return json.dumps({"matches": out, "count_total": len(hits), "returned": len(out)})
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _audit("find_repo", {"needles": needles}, int((time.time() - t0) * 1000), False, msg)
        return json.dumps({"error": msg})


@mcp.tool()
def jarvis_lookup_symbol(name: str, repo: str | None = None, k: int = 10) -> str:
    """Find chunks that DECLARE a given symbol (class / function / const / interface).

    Deterministic filter on the chunk's `symbols` payload array — returns the chunks
    where the name is actually defined, not chunks that mention it. USE BEFORE
    `jarvis_search_code` when you have an exact identifier (`PaymentsController`,
    `useVarunaPayment`, `loansJourneySelectionMachine`). Optional `repo` narrows to
    one repo. Returns per-hit repo / path / lines + commit-pinned permalink.
    """
    return _wrap("lookup_symbol", agent_tools.lookup_symbol, name=name, repo=repo, k=k)


@mcp.tool()
def jarvis_lookup_service(name_or_alias: str) -> str:
    """Look up a Jupiter internal microservice in the pre-built registry.

    Returns K8s in-cluster URL, Route53 cross-cluster URL, namespace, port,
    consumer repos, source repo, OpenAPI spec file, exposed paths, sample
    Feign-client file. Sub-second answer for service-discovery questions.
    Accepts canonical name (e.g. 'bullet-ms'), name-without-suffix ('bullet'),
    Jupiter acronyms ('llm'), or human-readable names ('Loan Lifecycle Manager').
    """
    return _wrap("lookup_service", agent_tools.lookup_service, name_or_alias=name_or_alias)


@mcp.tool()
def jarvis_fetch_jira_ticket(key: str) -> str:
    """Fetch a Jira ticket (jupitermoney.atlassian.net). Returns summary, description, image/video attachment URLs."""
    t0 = time.time()
    try:
        r = subprocess.run(
            ["python3", str(SCRIPTS_DIR / "jira_fetch.py"), key],
            capture_output=True,
            text=True,
            timeout=25,
            env=os.environ.copy(),
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()[:300]
            _audit(
                "fetch_jira_ticket",
                {"key": key},
                int((time.time() - t0) * 1000),
                False,
                err,
            )
            return json.dumps({"ok": False, "error": err or "non-zero exit"})
        _audit("fetch_jira_ticket", {"key": key}, int((time.time() - t0) * 1000), True)
        return r.stdout.strip() or "{}"
    except subprocess.TimeoutExpired:
        _audit(
            "fetch_jira_ticket",
            {"key": key},
            int((time.time() - t0) * 1000),
            False,
            "timeout",
        )
        return json.dumps({"ok": False, "error": "jira_fetch timeout"})


@mcp.tool()
def jarvis_fetch_pr_diff(repo: str, pr_number: int) -> str:
    """Fetch a PR's metadata + diff from jupitermoney/<repo>. Body truncated to 8KB, diff to 200KB."""
    t0 = time.time()
    if repo not in agent_tools.INDEXED_REPOS:
        return json.dumps({"error": f"unknown repo: {repo}"})
    try:
        meta_r = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                f"jupitermoney/{repo}",
                "--json",
                "number,title,body,state,author,createdAt,mergedAt,headRefName,baseRefName,additions,deletions,changedFiles,url",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if meta_r.returncode != 0:
            err = meta_r.stderr.strip()[:300]
            _audit(
                "fetch_pr_diff",
                {"repo": repo, "pr_number": pr_number},
                int((time.time() - t0) * 1000),
                False,
                err,
            )
            return json.dumps({"error": f"gh pr view failed: {err}"})
        meta = json.loads(meta_r.stdout)
        if meta.get("body") and len(meta["body"]) > 8000:
            meta["body"] = meta["body"][:8000] + "\n...[truncated]"
        if meta.get("changedFiles", 0) > 500:
            _audit(
                "fetch_pr_diff",
                {"repo": repo, "pr_number": pr_number},
                int((time.time() - t0) * 1000),
                True,
            )
            return json.dumps(
                {"meta": meta, "diff": None, "note": "diff omitted: >500 files changed"}
            )
        diff_r = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--repo", f"jupitermoney/{repo}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        diff = diff_r.stdout if diff_r.returncode == 0 else None
        if diff and len(diff) > 200_000:
            diff = diff[:200_000] + "\n...[truncated to 200KB]"
        _audit(
            "fetch_pr_diff",
            {"repo": repo, "pr_number": pr_number},
            int((time.time() - t0) * 1000),
            True,
        )
        return json.dumps({"meta": meta, "diff": diff})
    except subprocess.TimeoutExpired:
        _audit(
            "fetch_pr_diff",
            {"repo": repo, "pr_number": pr_number},
            int((time.time() - t0) * 1000),
            False,
            "timeout",
        )
        return json.dumps({"error": "gh timeout"})


@mcp.tool()
def jarvis_get_capabilities(category: str | None = None, search: str | None = None) -> str:
    """Return Jarvis's capability manifest. Filter by category or substring."""
    return _wrap(
        "get_capabilities", agent_tools.get_capabilities, category=category, search=search
    )


# ──────────────────── Phase 3: trigger-shim tools ─────────────────────────


@mcp.tool()
def jarvis_fire_fix(
    repo: str,
    description: str,
    idempotency_key: str,
    max_budget_usd: float = 2.0,
    caller_tag: str = "mcp",
    callback_url: str | None = None,
    attachments: list[str] | None = None,
    regression_test: bool = False,
    companion_pr: bool = False,
    jira_ticket: str | None = None,
) -> str:
    """Fire an asynchronous /api/v1/fix run. Returns {http_status, body} where body contains job_id.

    WRITES: opens a draft PR in jupitermoney/<repo>. Repo must be on JARVIS_WRITE_ALLOWED_REPOS
    (currently bff-core, jupiter, jarvis, jupiter-design-system).

    REQUIRED: `idempotency_key` (non-empty). Refuses without one. Use a stable string per logical
    request (e.g. Jira ticket key + commit short-sha) — within 5 min, identical keys return the
    ORIGINAL job_id instead of spawning a duplicate. Prevents the kind of double-spend that hit
    RECO-1259 (Jove orchestrator retry-on-timeout + operator separately fired = $10 instead of $5).

    Args:
        repo: Short repo name on allowlist.
        description: Free-form task spec (8-16000 chars).
        idempotency_key: REQUIRED, non-empty. Stable per logical request.
        max_budget_usd: Hard cap on Claude spend (default 2.0, server-side ceiling 5.0).
        caller_tag: Audit identifier (appears in fix_audit.jsonl as 'mcp:<caller_tag>').
        callback_url: Optional. Jarvis POSTs final FixJobStatus here when terminal.
        attachments: Optional. URLs to images/videos for multimodal context. Strongly
                     recommended for frontend bugs.
        regression_test: TDD/regression-capture mode (failing test → fix → verify pass).
        companion_pr: If Claude flags an upstream/library architectural fix, ALSO open a
                      draft PR in that repo (e.g. jupiter-design-system).
        jira_ticket: Optional ticket key (e.g. 'RECO-1259') — Jarvis auto-fetches
                     summary/description/attachments from Atlassian.
    """
    if not idempotency_key or not idempotency_key.strip():
        return json.dumps(
            {
                "error": "idempotency_key is REQUIRED (non-empty). "
                "Use a stable string per logical request (e.g. Jira key + commit sha). "
                "Prevents duplicate spend on caller retries — see memory/project_http_fix_endpoint.md."
            }
        )
    body: dict = {"repo": repo, "description": description, "max_budget_usd": max_budget_usd}
    if callback_url:
        body["callback_url"] = callback_url
    if attachments:
        body["attachments"] = attachments
    if regression_test:
        body["regression_test"] = True
    if companion_pr:
        body["companion_pr"] = True
    if jira_ticket:
        body["jira_ticket"] = jira_ticket
    extra = {
        "X-Jarvis-Caller": f"mcp:{caller_tag}",
        "Idempotency-Key": idempotency_key.strip(),
    }
    return _api_request(
        "POST",
        "/api/v1/fix",
        json_body=body,
        extra_headers=extra,
        timeout=30.0,
        tool_name="fire_fix",
    )


@mcp.tool()
def jarvis_fire_iterate(
    repo: str,
    pr_number: int,
    idempotency_key: str,
    max_budget_usd: float = 2.0,
    caller_tag: str = "mcp",
    callback_url: str | None = None,
) -> str:
    """Fire an asynchronous /api/v1/pr/iterate run. Returns {http_status, body} with job_id.

    WRITES: pushes new commit(s) to the PR's branch on jupitermoney/<repo>. Repo must be on
    JARVIS_WRITE_ALLOWED_REPOS. NEVER force-pushes. Iterate auto-refuses if PR is FE-only
    with no visuals (Guard A) or if last iterate had no new reviewer evidence (Guard B).

    REQUIRED: `idempotency_key` (non-empty). Same anti-double-spend semantics as fire_fix.

    Args:
        repo: Short repo name on allowlist.
        pr_number: GitHub PR number.
        idempotency_key: REQUIRED. Stable per logical request.
        max_budget_usd: Hard cap (default 2.0).
        caller_tag: Audit identifier.
        callback_url: Optional. POST'd with final IterateJobStatus.
    """
    if not idempotency_key or not idempotency_key.strip():
        return json.dumps(
            {
                "error": "idempotency_key is REQUIRED (non-empty). "
                "Use a stable string per logical request (e.g. 'iterate-<repo>-<pr>-<review-id>'). "
                "Prevents duplicate spend on caller retries."
            }
        )
    body: dict = {"repo": repo, "pr_number": pr_number, "max_budget_usd": max_budget_usd}
    if callback_url:
        body["callback_url"] = callback_url
    extra = {
        "X-Jarvis-Caller": f"mcp:{caller_tag}",
        "Idempotency-Key": idempotency_key.strip(),
    }
    return _api_request(
        "POST",
        "/api/v1/pr/iterate",
        json_body=body,
        extra_headers=extra,
        timeout=30.0,
        tool_name="fire_iterate",
    )


@mcp.tool()
def jarvis_fire_preflight(repo: str, diff: str, requester: str = "mcp") -> str:
    """Run preflight cross-repo / breaking-change review on a local diff. SYNC, read-only — no writes.

    Returns severity-graded findings (LOW / MEDIUM / HIGH / CRITICAL) with file:line citations
    per Mithun Tantri's framework. Same engine as the jarvis-preflight CLI.

    No Idempotency-Key required (sync, idempotent).

    Args:
        repo: Short repo name.
        diff: Unified diff text (output of `git diff main...HEAD`). 1-300_000 chars.
        requester: Audit identifier.
    """
    body = {"repo": repo, "diff": diff, "requester": requester}
    return _api_request(
        "POST",
        "/api/v1/preflight",
        json_body=body,
        timeout=120.0,
        tool_name="fire_preflight",
    )


@mcp.tool()
def jarvis_get_fix_status(job_id: str) -> str:
    """Poll a fix job's status. Returns {http_status, body} with current status + pr_url if complete."""
    return _api_request(
        "GET",
        f"/api/v1/fix/{job_id}",
        timeout=15.0,
        tool_name="get_fix_status",
    )


@mcp.tool()
def jarvis_get_iterate_status(job_id: str) -> str:
    """Poll an iterate job's status. Returns {http_status, body}."""
    return _api_request(
        "GET",
        f"/api/v1/pr/iterate/{job_id}",
        timeout=15.0,
        tool_name="get_iterate_status",
    )


# ─────────────────────── HTTP transport helpers ──────────────────────────


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    from starlette.responses import JSONResponse

    return JSONResponse(
        {
            "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "service": "jarvis-mcp",
        }
    )



@mcp.tool()
def jarvis_fire_migrate(
    task: str,
    repos: list[str],
    idempotency_key: str,
    budget_per_repo_usd: float = 1.50,
    total_budget_usd: float | None = None,
    caller_tag: str = "mcp",
    callback_url: str | None = None,
    stop_on_failure: bool = False,
) -> str:
    """Fire an asynchronous /api/v1/migrate run — apply the SAME task across N
    Jupiter repos, each producing its own draft PR. Returns {http_status, body}
    where body contains job_id.

    WRITES: opens up to N draft PRs (one per repo). Every repo must be on
    JARVIS_MIGRATE_ALLOWED_REPOS (separate from fix's allowlist — operator-
    curated, broader scope). Endpoint rejects fast if any repo is disallowed.

    REQUIRED: `idempotency_key` (non-empty). Stable per logical batch (e.g.
    "jfrog-to-ghp-2026-06-03" or a Jira epic key). Within 5 min, identical
    keys return the ORIGINAL job_id instead of spawning a duplicate batch.

    Per-repo failures do NOT abort the batch by default — one stuck repo
    shouldn't block 49 others. Pass `stop_on_failure=True` for strict mode.

    Use cases this is for: JFrog→GitHub-Packages migrations, dependency-version
    bumps, CI workflow rollouts, security patches, codemods. NOT for: a
    one-off bug fix (use jarvis_fire_fix), iterating an existing PR
    (jarvis_fire_iterate), or any single-repo work.

    Args:
        task: Free-form task description applied uniformly across all repos.
              The same string is passed to fix-mode for each repo, so Claude
              should be able to apply it independently per repo.
        repos: List of short repo names (no jupitermoney/ prefix). 1-200.
               Every repo must be on JARVIS_MIGRATE_ALLOWED_REPOS or the
               whole request 403s.
        idempotency_key: REQUIRED, non-empty. Stable per logical batch.
        budget_per_repo_usd: Hard cap on Claude spend per repo (default 1.50,
                             server cap 5.0). Tighter than fix's default of 2.0
                             because migrate tasks tend to be more uniform.
        total_budget_usd: Optional hard cap on cumulative spend across the
                          whole batch. Server cap 100. Batch halts mid-flight
                          if would-be-exceeded.
        caller_tag: Audit identifier (appears as 'mcp:<caller_tag>').
        callback_url: Optional. Jarvis POSTs final MigrateJobStatus here when
                      the batch reaches a terminal state. Single attempt,
                      10s timeout. Still GET as fallback.
        stop_on_failure: If True, abort batch on first per-repo failure.

    Poll: jarvis_get_migrate_status(job_id) → current_repo, pr_urls dict,
    failures dict, n_success/failed/refused, total_cost_usd.
    """
    if not idempotency_key or not idempotency_key.strip():
        return json.dumps({
            "error": "idempotency_key is REQUIRED (non-empty). Use a stable string per "
                     "logical batch (e.g. 'jfrog-to-ghp-2026-06-03' or a Jira epic key). "
                     "Prevents double-spend on caller retries. See memory/"
                     "feedback_post_then_update_double_post.md for the pattern."
        })
    if not repos:
        return json.dumps({"error": "repos list must contain ≥1 entry"})
    body: dict = {
        "task": task,
        "repos": list(repos),
        "budget_per_repo_usd": budget_per_repo_usd,
        "stop_on_failure": stop_on_failure,
    }
    if total_budget_usd is not None:
        body["total_budget_usd"] = total_budget_usd
    if callback_url:
        body["callback_url"] = callback_url
    extra = {
        "X-Jarvis-Caller": f"mcp:{caller_tag}",
        "Idempotency-Key": idempotency_key.strip(),
    }
    return _api_request(
        "POST",
        "/api/v1/migrate",
        json_body=body,
        extra_headers=extra,
        timeout=30.0,
        tool_name="fire_migrate",
    )


@mcp.tool()
def jarvis_get_migrate_status(job_id: str) -> str:
    """Poll the status of a migrate batch by job_id. Returns the live
    MigrateJobStatus: status, current_repo, n_success/failed/refused,
    pr_urls dict (per-repo PR URLs), failures dict (per-repo reasons),
    total_cost_usd. Safe to poll every 5-15s while status=running."""
    return _api_request(
        "GET",
        f"/api/v1/migrate/{job_id}",
        timeout=10.0,
        tool_name="get_migrate_status",
    )

def _run_http(host: str, port: int) -> None:
    import secrets as _secrets
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    api_key = os.environ.get("JARVIS_API_KEY")
    if not api_key:
        print("FATAL: JARVIS_API_KEY not set", file=sys.stderr)
        sys.exit(2)

    try:
        from portal.db import validate_api_key as _validate_portal_key
    except ImportError:
        _validate_portal_key = None

    class BearerAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # /health is public
            if request.url.path == "/health":
                return await call_next(request)
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                return JSONResponse(
                    {"error": "missing or malformed Authorization header"},
                    status_code=401,
                )
            token = auth[len("Bearer ") :].strip()
            if _secrets.compare_digest(token, api_key):
                return await call_next(request)  # shared key
            if _validate_portal_key is not None:
                user = _validate_portal_key(token)
                if user is not None:
                    request.state.portal_user = user
                    tok_ctx = _CURRENT_USER_EMAIL.set(user.email)
                    try:
                        return await call_next(request)
                    finally:
                        _CURRENT_USER_EMAIL.reset(tok_ctx)
            return JSONResponse({"error": "bad bearer"}, status_code=401)

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuth)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Jarvis MCP server")
    ap.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("JARVIS_MCP_TRANSPORT", "stdio"),
        help="stdio for local spawn-per-session; http for long-running streamable-http on :8082.",
    )
    ap.add_argument(
        "--host",
        default=os.environ.get("JARVIS_MCP_HOST", "127.0.0.1"),
        help="HTTP bind host (transport=http only). Default 127.0.0.1.",
    )
    ap.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("JARVIS_MCP_PORT", "8082")),
        help="HTTP bind port (transport=http only). Default 8082.",
    )
    args = ap.parse_args()
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        _run_http(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
