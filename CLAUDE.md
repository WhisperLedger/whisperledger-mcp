# CLAUDE.md — Astra

Operational guide for Claude Code (or any LLM agent) working on this codebase.
Read this before touching the bot, indexer, or deploy.

## What Astra is

A generic internal AI engineering platform for a software company. Slack-first, code-aware, retrieval-grounded.
- Indexes an organization's repositories + recent PR descriptions + selected knowledge sources (for example via MCP integrations).
- Exposes a single `/jarvis` slash command with sub-skills: bare Q&A, `ask`, `refresh`, `investigate`, `review`, `fix`, `claudify`, `help`.
- Used by engineers across all of Jupiter Engineering plus the SRE bot (HTTP API integration).

Built on the Claude Agent SDK with Anthropic models. Embeddings: voyage-code-3. Vector store: Qdrant.

## Where it runs

**Primary deployment box:** `ubuntu@3.6.202.121`
**SSH:** `ssh -i ~/Downloads/data-science.pem ubuntu@3.6.202.121` (key on Rohit's Mac)
**This repo is the source on the box:** `~/jarvis/` is a real git checkout of `jupitermoney/jarvis` on `main` (since 2026-05-16). Edits in place show up in `git status`.

**Services (systemd):**
- `jarvis-slack` — Slack Bolt app (socket mode). Entry: `scripts/run_slackbot.sh` → `scripts/slackbot/app.py`.
- `jarvis-api` — HTTP API. Entry: `scripts/run_api.sh` → `scripts/api/server.py`. Async fix-job runner at `scripts/api/jobs.py`. Used by SRE bot AND JPE/Jove for programmatic draft-PR creation.
- `jarvis-spend-monitor` (timer, every 15 min) — credit-failure + threshold alerts. Entry: `scripts/run_spend_monitor.sh` → `scripts/spend_monitor.py`. **Important:** ALL Python systemd services on this box must use a bash wrapper (`run_*.sh`) that `source`s `~/.config/jarvis/env`. Never use systemd's `EnvironmentFile=` directive — it silently drops the `export VAR=...` lines the env file uses (see `feedback_systemd_env_pattern.md`).
- `jarvis-reindex` (timer, 22:06 UTC nightly) — full re-index of code + PR descriptions. Entry: `scripts/reindex_all.sh`.
- `jarvis-open-pr-digest` (timer, 04:00 UTC / 09:30 IST daily) — posts the open-PR aging digest to `#technology-team` (C092S7Z5HB5). Entry: `scripts/run_open_pr_digest.sh` → `scripts/open_pr_digest.py`. **Timer is configured `Persistent=false`** (do not catch up on missed runs — skipping a day beats double-posting; see `feedback_post_then_update_double_post.md`). Repo→product taxonomy in `scripts/repo_products.json` (operator-curated, evolves via the unmapped-repos appendix in each digest).
- `jarvis-jove-refresh` (timer, 03:00 UTC daily) — belt-and-suspenders re-crawl of critical Confluence spaces via Jove (TECH + PROD default; override via `JARVIS_JOVE_REFRESH_SPACES`). Entry: `scripts/run_jove_scheduled_refresh.sh` → `scripts/jove_scheduled_refresh.py`. Alerts if any refresh fails or if a space's `latest_modified` is >48h ahead of `latest_indexed_at`. Audit log: `~/jarvis/logs/jove_refresh.jsonl`. Exists because Jove's own 02:00 UTC crawler has been silently skipping large spaces (TECH was 3 months stale before 2026-07-05).
- `jarvis-gc` (timer, nightly) — workspace janitor.
- Restart any service: `sudo systemctl restart <unit>`. **Caveat:** restarting `jarvis-api` kills any in-flight HTTP `/api/v1/fix` jobs (in-memory job store). Always check `~/jarvis/logs/fix_audit.jsonl` for active jobs (look for `received` or `start` events without matching `success`/`failed`) before restarting.

**Other runtime dirs on the box (gitignored):**
- `~/jarvis/repos/` — clones of all indexed `jupitermoney/*` repos
- `~/jarvis/index/` — Qdrant data
- `~/jarvis/logs/` — `qa_log.jsonl`, `claudify_audit.jsonl`, `review_audit.jsonl`, `api_requests.jsonl` (contains user query text — PII)
- `~/jarvis/state/` — in-flight agent task state
- `~/jarvis/workspaces/` — Claudify ephemerals (large)
- `~/.config/jarvis/env` — secrets (Slack token, Anthropic key, Voyage key, GitHub token)

## Architecture decisions (locked 2026-05-11)

- **Embeddings:** voyage-code-3
- **Vector store:** Qdrant (single node, on-box)
- **LLM:** Anthropic — Sonnet 4.6 default, Haiku 4.5 for cheap triage, Opus 4.7 escalation for hard agentic loops
- **Retrieval:** semantic search over chunked source + recent PR descriptions + Jove-served Confluence
- **Don't** swap any of these without a written reason — they were chosen against alternatives.

## Deploy workflow (CRITICAL: get this right)

For any change to a Python module that a running service loads:

1. Edit in place at `~/jarvis/scripts/...` (or scp a local edit up).
2. `git add` + `git commit` + `git push origin main` (proper author identity is set globally on the box).
3. **Restart EVERY service that imports the changed module.** Modules can be loaded by multiple services. `py_compile` and a CLI smoke test do NOT prove a running service has picked up the change.
   - If you change `scripts/agent/tools.py`: restart `jarvis-slack` AND `jarvis-api`.
   - If you change `scripts/slackbot/app.py`: restart `jarvis-slack`.
   - When in doubt: restart both.

## Operational rules (hard-won, do not violate)

### Slack messages require per-message human approval
Never send any Slack message (channel post, DM, edit, thread reply, reaction) from the bot without showing Rohit the draft first and getting explicit approval. "Send X to Y" is a request to *draft and propose*, not fire-and-forget. Verify every `@`-mention against `users_info` before showing the draft. Source: alert-spam (~115 messages) on 2026-05-14 + wrong-@ EOD post on 2026-05-15.

### System alerts default to operator DM, never a public channel
Any cron / error / spend-monitor alert must DM the operator (currently Rohit, `U0837N31T9C`) by default. Routing to a public channel requires an explicit env-var override (`JARVIS_ALERTS_CHANNEL`). Failure mode: cascading public-channel spam if a service crash-loops.

### Capability manifest must stay in sync
Every feature ship updates `scripts/agent/capabilities.py`. The agent's self-awareness layer reads from that manifest to answer "what can you do?" questions. Skipping the update makes Jarvis lie about its own capabilities to users.

### No secrets in source
`.gitignore` covers `.env`, `*.pem`, `*.key`, `*.log`, `*.jsonl`, plus `repos/`, `index/`, `qdrant_storage/`, `slack_users.json`, `logs/`, `state/`, `workspaces/`. All secrets live in `~/.config/jarvis/env` and are loaded at process start. Never hardcode tokens — always `os.environ[...]`.

### Channel-visibility framing
Usage / how-to / can-and-can't messages should be top-level channel posts, not threaded replies. Threads bury them.

### Never kill running jobs without explicit permission
Don't run `kill`, `pkill`, `proc.terminate()`, `proc.kill()`, or `systemctl stop`/`restart` on anything user-visible — bash wrappers, Claude subprocesses, in-flight HTTP `/api/v1/fix` jobs, services — without first showing the user (1) the process, (2) elapsed time, (3) failure modes, (4) reason to kill, and getting an explicit "kill". Default action is **wait, not kill**. Even processes that look hung (0-byte log, 0% CPU snapshot) may just be in an agent-loop bursty phase. Source: 2026-05-17 Jove `/api/v1/fix` run on a 16KB Insurance V2 brief — killing prematurely would have wasted ~$3 of compute and lost the PR. System-design auto-timeouts (`asyncio.wait_for` in `api/jobs.py`, `--max-budget-usd` in `jarvis_fix.sh`) ARE allowed to fire automatically, but flag to the user when one is imminent so they can intervene if needed.

### Iterate refuses without visuals on frontend bugs
The iterate wrapper (`scripts/jarvis_iterate.sh`) auto-refuses if a PR touches frontend files (`.tsx`/`.jsx`/`.css`/`.scss`/`.sass`/`.less`/`.html`/`.vue`/`.svelte`) AND no images or video keyframes are available from PR comments or the `ITERATE_EXTRA_ATTACHMENTS` env var. Exit code 6 with `JARVIS_ITERATE_REFUSED=fe_no_visuals`. Override with `JARVIS_ALLOW_FE_NO_VISUALS=1` only when the caller has independently verified visuals are unnecessary (e.g., pure CSS refactor driven by textual reviewer guidance). Source: PR #14140 RECO-1259 — 3 confidently-wrong text-only iterations (~$7) before image + video unlocked the correct root cause.

### Iterate caps at 3 attempts per PR without new evidence
The iterate wrapper auto-refuses a 3rd+ iterate on the same PR if no new reviewer comments or attachments have arrived since the last iterate commit (compared via commit timestamp vs. latest comment/review timestamp). Exit code 5 with `JARVIS_ITERATE_REFUSED=loop_detected`. Override with `JARVIS_FORCE_ITERATE=1` only when you have new evidence outside PR data (e.g., a private DM from a reviewer with a specific code pointer). Once the agent is stuck in a speculation loop, manual debug is the right tool — read the file yourself, form a real hypothesis with line references, push a manual commit. Source: PR #14140 v5 iterate (no new evidence since v4 landed the root-cause fix) burned $0.74 to add a redundant off-target change before this guard existed.


### Regression-test backfill — `scripts/jarvis_backfill_test.sh`

Retro-adds a regression test to an EXISTING Jarvis-fired PR (no new PR). For batches that shipped without `regression_test=true` (e.g. the Jove filter-19028 incident, 2026-06-05), use this wrapper to add tests to the existing branch:

```bash
~/jarvis/scripts/jarvis_backfill_test.sh <repo> <pr_number> [--budget 1.50]
```

Wrapper cheaply detects PRs that already have a `test:` prefix commit and exits without running Claude. Honest skip reasons: no test framework detected, fix is purely visual, fix is a 1-line typo. Audit log at `~/jarvis/logs/backfill_test_audit.jsonl`.

Validated 2026-06-06: ran on a 20-PR batch (17 backfilled, 3 honest skips, 0 failures, ~$15-25 total).

## Slack model

- **Bot user:** Jarvis (Slack app)
- **Pilot channel:** `C092S7Z5HB5` (engineering channel)
- **Allowed channels:** `JARVIS_ALLOWED_CHANNELS` env var (comma-separated channel IDs). Empty = all channels allowed.
- **DM allowlist:** `JARVIS_ALLOWED_DM_USERS` env var (comma-separated user IDs). Engineers in the engineering channel can use slash commands from channel; only allowlisted users can DM the bot directly.
- **Per-user concurrency:** capped (small N) — prevents one user from monopolising the agent loop.
- **Free-text fix-intent (channel @-mention + DM, since 2026-06-25):** `@jarvis fix this` in any allowed channel thread, OR a DM containing a fix-shaped phrase (`fix this`, `draft a PR`, `raise a PR`, `implement this`, etc.), runs the Haiku brief extractor (`scripts/agent/thread_to_fix.py`) and posts a brief for `go` confirmation before firing `/api/v1/fix`. Same brief-gate + write-allowlist + per-user budget cap as `/jarvis fix`. Shared helper: `_run_fix_intent_flow(source="mention"|"dm")` in `scripts/slackbot/app.py`. DM free-text Q&A is NOT wired — non-fix DMs from allowlisted users are silently ignored (use `/jarvis <question>` for Q&A).

## External integrations

- **Jove** (Confluence Q&A): MCP-over-Streamable-HTTP bridge in `scripts/agent/jove_client.py`. 108 Confluence spaces indexed, friendly-name resolution (`technology`, `tech`, `TECH` all map). `/api/v1/ask/stream` also detects one pasted HTTPS Atlassian Confluence URL in `question`, live-reads it through `jove_read_confluence_page`, and appends its bounded content as source context. Set `JOVE_MCP_URL` to Jove's reachable MCP endpoint and `JOVE_MCP_TOKEN` in `~/.config/jarvis/env`. The post-processing normalizer in `jove_client.normalize_jove_response` is a no-op since Jove 5bed6d6 — kept as defense-in-depth.
- **Janus** (growth bot — Amplitude user-event-stream): MCP-over-stdio bridge in `scripts/agent/janus_client.py`. Wrapper at `/home/ubuntu/test_databricks_connection/run_janus_amplitude_mcp.sh` sources Amplitude creds locally; no Jarvis-side env. Exposes two tools: `janus_user_journey(user_id, lookback_hours, event_filter, caller_id)` for per-user product-event timelines (hard ceiling 168h / 7d) and `janus_event_count(event_type, lookback_days, filter_property, filter_value, group_by, caller_id)` for fleet-wide event counts (hard ceiling 90d). Multi-step funnels still go to the Amplitude dashboard. Shipped 2026-06-24.
- **JPE / Jove (as caller, not backend)**: posts to `POST /api/v1/fix` with `X-Jarvis-Caller: jove` to turn PRDs into real draft PRs on `jupitermoney/jupiter`. Shipped 2026-05-17 (commit `2e17adf`). First production run produced [PR #14134 (Insurance Home V2)](https://github.com/jupitermoney/jupiter/pull/14134) in 13:27 / ~$3. See `project_http_fix_endpoint.md` memory for full state + v0.3 follow-ups (iterate-on-PR, longer timeout, durable jobs).
- **GitHub** (cross-repo review, claudify, fix): pushes go via stored token. Long-term plan is a dedicated `jarvis-bot` GitHub account (currently commits land as `Jarvis Bot <jarvis-bot@jupiter.money>` — placeholder; not a real mailbox or GH user yet).
- **SRE bot** (Sumith's): hits the `jarvis-api` HTTP endpoint at `POST /api/v1/alert-analysis` on real Prometheus alerts.

## HTTP API surface

| Endpoint | Auth | Purpose | Caller(s) |
|---|---|---|---|
| `GET  /health` | none | Liveness | infra |
| `POST /api/v1/ask` | Bearer | Generic Q&A passthrough (same agent as Slack `/jarvis`). Optional body field `bypass_cache: bool = false` — when true, skips the prior_match cached-answer layer (Layer 1b grounding) and always runs a fresh agent loop. **Recommended for automated callers (alert analyzers, orchestrators)** where each invocation needs current state regardless of similarity to past questions. Added 2026-06-25 in response to SRE bot feedback. Response includes `status` field (`"complete"` / `"empty"`) so callers can programmatically distinguish a substantive answer from an early-bail with empty body (added 2026-06-26 in response to Jove integration feedback). | Jove (PRD drafting), SRE bot (alert analysis with `bypass_cache: true`) |
| `POST /api/v1/ask/stream` | Bearer | SSE Q&A. v1 direct-document path: paste one HTTPS Atlassian Confluence page URL in the existing `question` field; Jarvis validates/detects it, calls Jove's direct live reader, and grounds that fresh streaming answer in up to 10K characters. No Merlin-specific field or URL parsing is required. Jove failures return `502`; arbitrary URLs are never fetched. | Merlin |
| `POST /api/v1/plan/stream` | Bearer | Standalone SSE planning agent for Merlin. Takes structured chat context `{sessionId?, intent, sessionSummary?, recentTurns?, currentPrompt, previousPlan?, feedback?, maxCostUsd?}` — no repository is required. Jarvis resolves scope with parallel retrieval, reads up to 20 high-signal files in parallel, then runs bounded adaptive gap filling. `plan_ready` contains planning/audit fields plus an agent-agnostic `execution` handoff (status, ordered target-file steps, anchors, acceptance, invariants, verification intent, and guardrails). Merlin owns any coding-agent integration. `intent` is `create`, `refine`, or `replan`; summaries and prior plans are intent context only. `JARVIS_PLAN_MAX_ITERATIONS` is an emergency ceiling of 24; `JARVIS_PLAN_MAX_ADAPTIVE_TURNS` defaults to 12 before synthesis, a source-citing critic, and one repair. `maxCostUsd` defaults to and cannot exceed `$5.00`. | Merlin |
| `POST /api/v1/alert-analysis` | Bearer | Structured analysis of a named production alert (now also accepts OpsGenie URL as `alert_name`) | SRE bot |
| `POST /api/v1/fix` | Bearer + `X-Jarvis-Caller` (+ optional `Idempotency-Key`) | Async: spawn `jarvis_fix.sh`, returns 202 + `job_id`. Same agent loop as Slack `/jarvis fix`. Default budget $2, HTTP ceiling $5. Optional `callback_url` in body for push-result-back; optional `Idempotency-Key` header for 5-min dedup. **Phase 3 (2026-05-19): /api/v1/fix at parity with iterate** — accepts `attachments: list[str]` (images/videos auto-downloaded + keyframed) and `companion_pr: bool` (additive draft PR in upstream repo when Claude flags architectural fix). Soft Guard-A in prompt nudges visual-bug-no-attachments toward `JARVIS_FIX_REFUSED=insufficient_evidence`. Brief-sufficiency gate (added 2026-06-10) refuses with `JARVIS_FIX_REFUSED=insufficient_brief` + structured `missing:[]` when the Jira description is too thin to act on. | JPE/Jove |
| `GET  /api/v1/fix/{job_id}` | Bearer | Poll job status; returns `pr_url` when `status=completed`. Use only if `callback_url` not provided. | JPE/Jove |
| `POST /api/v1/pr/iterate` | Bearer + `X-Jarvis-Caller` (+ optional `Idempotency-Key`) | Async: takes `{repo, pr_number, max_budget_usd, callback_url}`. Fetches the PR's review comments via gh api, fresh-clones the PR's branch, runs Claude to address comments, commits + pushes (NEVER force-push) so the existing draft PR auto-updates. Built so the review-comment iteration loop is automated, not manually orchestrated. | JPE/Jove (after Chirag-style reviewer leaves comments on a Jove-opened PR) |
| `GET  /api/v1/pr/iterate/{job_id}` | Bearer | Poll iterate status; same shape as fix status | JPE/Jove |
| `POST /api/v1/migrate` | Bearer + `X-Jarvis-Caller` (+ optional `Idempotency-Key`) | Async: apply the SAME task across N repos, one draft PR per repo. Wraps fix-mode mechanics under a single approval + combined budget. Returns 202 + `job_id`. Default budget $1.50/repo (server cap $5), total batch cap $100. One repo failing doesn't abort the batch by default; pass `stop_on_failure: true` for strict mode. Per-child `JARVIS_WRITE_ALLOWED_REPOS` is restricted to just that child's repo for defense-in-depth. Shipped 2026-06-03 in response to Mithun's JFrog→GHP migration feedback. | engineers via Slack `/jarvis migrate`, MCP `jarvis_fire_migrate`, or HTTP |
| `GET  /api/v1/migrate/{job_id}` | Bearer | Poll migrate batch: `current_repo`, `pr_urls` dict, `failures` dict, `n_success/failed/refused`, `total_cost_usd`. Updated live per-repo. | same |
| `POST /api/v1/autosupport/investigate` | Bearer + `X-Jarvis-Caller` (+ optional `Idempotency-Key`) | Async: AutoSupport-platform contract. Spawns a two-pass agent run (Sonnet investigates with retrieval tools; Haiku 4.5 structures the prose into the strict callback JSON). Returns 202 + `investigation_id`. Optional `callback_url` gets a POST of the terminal payload (single attempt, 10s, headers `X-Jarvis-Investigation-Id` + `X-Jarvis-Event=investigation.completed\|failed`). Confidence ordinals (`findings_confidence`/`actions_confidence`/`api_confidence` low\|medium\|high) are derived DETERMINISTICALLY in Python — LLM does NOT pick labels. `auto_executable` hardcoded to false on every action. `database_contexts` always carry `note: "sre_execution_required"`. Anti-silent-failure: on agent failure or unparseable output, returns a structurally valid escalation payload with `status=FAILED`. Audit: `~/jarvis/logs/autosupport_audit.jsonl`. | AutoSupport platform (Ritheesh Urankar / SRE team) |
| `GET  /api/v1/autosupport/investigate/{investigation_id}` | Bearer | Poll fallback. Once status is `COMPLETED` or `FAILED`, returns the same payload that was POSTed to `callback_url`. Mandatory recovery path if the callback fails. | same |
| `POST /api/v1/autosupport/sync` | Bearer | Sync drift check on a batch of recommended actions. For each `action_id`, re-queries the service registry; if a service canonicalized, populates `updated_services` + `updated_payload` with rewritten `service` names. Else mirrors with `update_required=false`. | same |

See [`docs/plan_stream.md`](docs/plan_stream.md) for the request, SSE, and artifact contract Merlin consumes.

Same `JARVIS_API_KEY` for all authed endpoints. Write endpoints (`fix`, `pr/iterate`, `migrate`) gate on env-var allowlists: `fix` and `pr/iterate` use `JARVIS_WRITE_ALLOWED_REPOS` (currently `bff-core jupiter jarvis jupiter-design-system wormhole jupiter-web-platform`). `jupiter-design-system` was added 2026-05-19 so iterate can spawn companion PRs at the architectural layer (e.g., sense-ui Button fix) when the call-site fix flags an upstream issue. The companion-PR flow is opt-in via `ITERATE_COMPANION_PR=1` env (Phase 2 work — see task tracking). `migrate` uses its OWN allowlist `JARVIS_MIGRATE_ALLOWED_REPOS` (separate from fix's; broader operator-curated scope for cross-repo rollouts). Initial value on box: just `jarvis` for self-test; expand per use case.

### /api/v1/ask caching + latency semantics (added 2026-06-26)

Two distinct mechanisms can shortcut the Sonnet agent loop on `/api/v1/ask`. Callers need to understand both:

**Layer 1a — Question router fast paths (NOT bypassable).** A cheap Haiku 4.5 classifier (`scripts/agent/question_router.py`) inspects every question and, if it cleanly maps to a deterministic lookup, executes a fast-path tool (`get_capabilities`, `lookup_service`, `lookup_symbol`) and returns the result. This is NOT a cache — it's the correct deterministic answer for that question shape. Layer 1a is NOT affected by `bypass_cache`. Typical fast-path latency: 1–5 s. Bypass via env: `JARVIS_DISABLE_QUESTION_ROUTER=1`.

**Layer 1b — `prior_match` cached-answer surface (bypassable).** For "general" route questions (Layer 1a passed-through) with a caller_id, Jarvis embeds the question with voyage-code-3 and looks for a semantically-similar prior question by the SAME caller in the last 30 days (cosine ≥ 0.85). On a match where citations are still fresh (Layer 3 freshness check), surfaces the cached answer with a disclosure header instead of running Sonnet. THIS is what `bypass_cache: true` disables. Bypass via env: `JARVIS_DISABLE_PRIOR_MATCH=1`.

**Recommendation by caller class:**
- Human Slack callers (default): both layers on → fast UX + cost savings on repeat questions
- Automated alert analyzers / orchestrators / pipelines that need current state: `bypass_cache: true` → only Layer 1b skipped; Layer 1a still fires for the small subset of questions that map to deterministic fast paths
- Eval / debugging: set both env vars to fully disable

**Expected latency** (set timeouts accordingly):

| Question shape | Typical | Recommended timeout |
|---|---|---|
| Layer 1a fast path (`what can you do`, `where is bullet-ms deployed`, `where is PaymentsController`) | 1–5 s | 30 s |
| Layer 1b cache hit | 1–3 s | 30 s |
| Probe-shape ("what's in package.json", "list the top dirs") | 5–10 s | 60 s |
| Substantive code-read (decision logic, comm templates, escalations) | 100–175 s | 240 s |
| Complex multi-step (cross-repo trace, deep state-machine analysis) | 175–300 s | 360 s |

**`AskResponse.status` field** lets callers programmatically distinguish a real answer from an early-bail (which can happen on over-broad "extract every X" questions):
- `status: "complete"` — happy path, answer is substantive (back-compat default for existing fields)
- `status: "empty"` + `reason: <string>` — agent ran but produced no actionable answer. Typical fix: narrow the query with explicit places to look (file paths, symbol names, file patterns)

### Preflight — local pre-push PR review (added 2026-05-20)
New endpoint `POST /api/v1/preflight` (sync) + new CLI `bin/jarvis-preflight`. Engineers install the CLI on their local machine, set up an SSH tunnel (`ssh -L 8081:localhost:8081 ubuntu@3.6.202.121`), and run `jarvis-preflight` inside any `jupitermoney/<repo>` clone before `git push`. Returns severity-graded inline findings (Mithun Tantri's framework: cross-repo breakage, parallel-implementation drift, breaking-change validation, CI/security workflow audit, config-value propagation, severity-graded delivery) with `file:line` citations. Exit code reflects highest severity (0/1/2) — CI-friendly. NO writes to GitHub (read-only).

### Regression-capture mode for /api/v1/fix (added 2026-05-20)
New flag `regression_test: bool = false` on `FixRequest`. When `true`, Jarvis writes a FAILING test first (verifies it fails on unpatched code via the project's test runner), then writes the implementation fix, then verifies the test now passes. PR contains TWO commits (`test: ...` then `fix: ...`) so reviewers can verify `git checkout HEAD~1` shows test fails and `HEAD` shows it passes. Refuses with `JARVIS_FIX_REFUSED=test_not_failing_before_fix` (test doesn't catch the bug) or `=test_not_passing_after_fix` (fix didn't address the bug) or `=no_test_infrastructure` (target module has no runner). Strongly recommended for bug reports — turns each bug into a permanent regression test.

### Brief sufficiency gate for /api/v1/fix (added 2026-06-10)
Engineers were complaining that Jarvis fires PRs even when the Jira ticket lacks enough info to act on. `jarvis_fix.sh` now calls `scripts/agent/brief_gate.py` (Claude Haiku 4.5, ~$0.0005/call) right after the ticket-status guard. The gate refuses with `JARVIS_FIX_REFUSED=insufficient_brief` when the description lacks BOTH a clear symptom/desired-behavior AND any concrete pointer (file/class/endpoint/screen/repro steps/attachment/stack-trace/exact-copy). The refusal includes a structured `missing: []` list so the reporter knows what to add. Every gate run is logged to `~/jarvis/logs/brief_gate.jsonl` regardless of verdict (for false-positive analysis). Fails OPEN — a Haiku outage cannot block production. Override with `JARVIS_SKIP_BRIEF_GATE=1` when the brief is intentionally minimal (e.g. typo fix from a known repo where the ticket title alone is enough).

**Phase 2 (2026-06-11):** when the gate refuses, Jarvis also posts a friendly comment on the Jira ticket (`scripts/jira_comment.py`) explaining what's missing. The comment uses Atlassian Document Format (ADF) — a paragraph intro, a bulleted list of missing pieces from the gate verdict, and a closing line encouraging the reporter to re-run. Fails open: any error in posting → refusal flow proceeds normally. Opt-out via `JARVIS_NO_JIRA_COMMENT_ON_REFUSE=1`. Auth via the existing `CONFLUENCE_EMAIL` + `CONFLUENCE_API_TOKEN` env (Atlassian creds work for both Confluence and Jira).

**Recommend for any caller doing retries:** always set `Idempotency-Key: <ticket-key>` (or similar stable string per logical request). Within 5 min, identical keys return the *original* `job_id` instead of spawning a duplicate. Prevents the kind of double-spend that hit RECO-1259 on 2026-05-18 (Jove orchestrator retried on timeout + operator separately fired; cost $10 instead of $5 before this header existed).

**Recommend for any new caller:** use `callback_url` instead of polling. Jarvis POSTs the final FixJobStatus to your URL when terminal. Headers: `X-Jarvis-Job-Id`, `X-Jarvis-Event: fix.completed|fix.failed`. Single attempt, 10s timeout — receiver should still be prepared to GET as fallback.


**Audit logs:** every `/api/v1/fix` POST writes a `received` event to `~/jarvis/logs/fix_audit.jsonl` *immediately* (closes the ~30-60s blind spot during `gh repo clone`). The api_requests log additionally captures the `regression_test`, `companion_pr`, `attachments_count`, and `jira_ticket` fields per request — added 2026-06-05 after the Jove filter-19028 incident, where a client-side `submit_fix` bug silently dropped `regression_test=true` and we couldn't tell from logs whether the flag arrived. The bash wrapper's own `start` event follows. Both events carry `caller`. Callback attempts log to `~/jarvis/logs/fix_callbacks.jsonl`.

**Real-world Jove-fired PRs to date:** [#14134](https://github.com/jupitermoney/jupiter/pull/14134) Insurance V2 (2026-05-17, 8 files), [#14140](https://github.com/jupitermoney/jupiter/pull/14140) RECO-1259 Contact Sheet (snapPoints bug, 2026-05-18), [#14141](https://github.com/jupitermoney/jupiter/pull/14141) RECO-133 Apply descender (2026-05-18). Plus [#14139](https://github.com/jupitermoney/jupiter/pull/14139) closed as RECO-1259 duplicate (Jove orchestrator retry-on-timeout, pre-Idempotency-Key).

**Known gaps:**
- Durable job store (in-memory; jobs lost on `jarvis-api` restart). Productize when loss incidents > ~5/week.
- `claude_run.log` stays 0 bytes until completion (JSON output is written at end, not streamed). Switching to `--output-format stream-json` would fix mid-flight visibility, but requires re-engineering output parsing — deferred.
- Dedicated `search_design_tokens(intent)` tool (Phase B v1) — v0 is prompt-only guidance pointing at the design-system source. Escalate if real-world Jove PRs still produce hardcode violations after the prompt change.

## Symbol-level metadata + lookup_symbol (shipped 2026-06-11)

Every chunk now carries a `symbols: list[str]` payload field — the names of classes, functions, objects, interfaces, consts, etc. declared in (or enclosing) the chunk. Sourced two ways:
1. **AST chunker** (`scripts/indexer/ast_chunker.py`) extracts names from `function_declaration`, `class_declaration`, `object_declaration`, `interface_declaration`, `lexical_declaration` etc. while walking the parse tree. Container names (e.g. enclosing class) are injected into child chunks so a method-chunk lists both its class name and method name. Imports/package nodes and identifiers shorter than 3 chars are excluded.
2. **Backfill** (`scripts/backfill_symbols.py`) — one-time per-language regex over chunk text for existing chunks indexed before the AST symbol pass. Backfilled 74,345 of 137,481 chunks (54%); the remaining 63k are non-code (YAML/MD/JSON/OpenAPI specs) with no extractable names.

**New tool: `lookup_symbol(name, repo=None, k=10)`** — deterministic Qdrant filter on the symbols array. Returns the chunks where `name` is actually defined, not chunks that mention it. Available via:
- Slack agent (`lookup_symbol` in `agent.tools.TOOL_DISPATCH`)
- HTTP API (`/api/v1/ask` agent loop picks it automatically)
- MCP server (`jarvis_lookup_symbol(name, repo?, k?)`)

Use BEFORE `search_code` when you have an exact identifier:
```python
lookup_symbol("PaymentsController")          # → growth/gift-card/.../PaymentsController.kt:1-90
lookup_symbol("useVarunaPayment")            # → jupiter/.../useVarunaPayment.ts:112-871
lookup_symbol("loansJourneySelectionMachine")# → jupiter/.../loans-journey-selection-machine.ts:86-528
lookup_symbol("EventApportionmentStrategy")  # → lms/.../EventApportionmentStrategy.kt:1-24
```

Each hit includes the same `permalink` field as search_code — commit-pinned URLs.

**Restart needed on agent changes:** `jarvis-slack`, `jarvis-api`, `jarvis-mcp` all import `agent.tools` — restart all three after any tool change.

## Developer Portal — per-engineer API keys (shipped 2026-06-11 via PR #12 by Mithun + fixes by autonomous loop)

Self-service web portal for per-engineer `jrv_<32hex>` bearer tokens, Google-OAuth gated to `@jupiter.money`. Replaces the manual shared-key bottleneck for engineer-driven Jarvis usage.

**Files:** `scripts/portal/` package — `db.py` (sqlite WAL at `~/jarvis/state/portal.db`), `auth.py` (OAuth via httpx), `server.py` (FastAPI on `127.0.0.1:8083`), `templates/` (Jinja2 + Tailwind CDN). Systemd unit: `scripts/systemd/jarvis-portal.service` → `scripts/run_portal.sh`.

**Auth flow:**
- User SSHs in with `ssh -L 8083:localhost:8083 ubuntu@3.6.202.121`, opens `http://localhost:8083`.
- Sign in with Google (`@jupiter.money` only). New signups are read-only (`write_access=0`); admin toggles per-user.
- Create API keys with labels. Full key shown once at creation, only prefix shown thereafter. SHA-256 hashed at rest.
- Revoke any key from the dashboard.

**Validation path:** `portal.db.validate_api_key(token)` is called by both `jarvis-api` (`_check_auth` → `UserContext | None`) and `jarvis-mcp` (`BearerAuth` middleware). Tokens NOT starting with `jrv_` fall through to the shared `JARVIS_API_KEY` check (SRE bot, JPE, Jove, Aura keep working unchanged).

**Per-user enforcement (added on top of Mithun's PR):**
- Daily budget cap — `users.daily_budget_usd` (default $5) checked by `_enforce_user_budget()` pre-flight on `/api/v1/ask`, `/fix`, `/iterate`, `/migrate`. Returns 429 with structured `daily_budget_exceeded` detail.
- Post-flight spend record — `record_spend(user_id, cost)` after each call. Date-keyed counter resets at 00:00 UTC.
- 80% warning DM — when spent crosses `0.8 * daily_budget` for the first time today, fire-and-forget Slack DM to the user via `slack_users.json` email→id mapping. Audited to `~/jarvis/logs/portal_alerts.jsonl`.
- Admin endpoint — `POST /admin/users/{id}/budget` (form field `daily_budget_usd`, range 0-1000) to bump caps; admin-only. Direct SQL on `~/jarvis/state/portal.db` is the fallback.
- Caller attribution — when a `jrv_` token is used, `user.email` becomes the `caller` field in `~/jarvis/logs/api_requests.jsonl` AND `~/jarvis/logs/mcp_audit.jsonl` (via contextvar in MCP middleware).

**Activation steps (operator one-time):**
1. Add to `~/.config/jarvis/env`:
   ```bash
   export GOOGLE_CLIENT_ID="<oauth-client-id>"
   export GOOGLE_CLIENT_SECRET="<oauth-client-secret>"
   export JARVIS_PORTAL_SECRET="<random-32-char-hex>"  # for session cookies
   export JARVIS_PORTAL_ADMIN_EMAIL="rohit@jupiter.money"
   ```
2. Register `http://localhost:8083/auth/callback` as an authorized redirect URI in Google Cloud Console (OAuth 2.0 Client).
3. Install portal-specific deps into the indexer venv (`run_portal.sh` uses it):
   ```bash
   ~/jarvis/scripts/indexer/.venv/bin/pip install -r ~/jarvis/scripts/portal/requirements.txt
   ```
   These (`jinja2`, `itsdangerous`, `MarkupSafe`, `httpx`) are transitive deps of `Jinja2Templates` + `SessionMiddleware` + Google OAuth; they're not pre-installed because the indexer venv predates the portal.
4. `sudo cp scripts/systemd/jarvis-portal.service /etc/systemd/system/`
5. `sudo systemctl daemon-reload && sudo systemctl enable --now jarvis-portal`
6. Verify: `curl -s http://localhost:8083/health`
7. Engineer rollout: tell engineers to `ssh -L 8083:localhost:8083 ubuntu@3.6.202.121` then visit `http://localhost:8083`.

**Known gotchas:**
- Mithun's PR was written before Starlette 1.0 shipped. The old `templates.TemplateResponse("name.html", {"request": request, ...})` signature was removed in 1.0 — the new positional form is `templates.TemplateResponse(request, "name.html", {"x": y})`. All 9 calls in `portal/server.py` were rewritten on 2026-06-11 to the new shape. If a future PR adds a new TemplateResponse call, keep it in the new form.
- If the portal returns "Internal Server Error" with `TypeError: unhashable type: 'dict'` in the journal, that's a missed TemplateResponse call — same fix.

**Audit paths:**
- `~/jarvis/logs/portal_alerts.jsonl` — 80% threshold crossings (sent or skipped, with reason)
- `~/jarvis/logs/api_requests.jsonl` — per-request with `caller=user.email` for jrv_ tokens
- `~/jarvis/logs/mcp_audit.jsonl` — per-MCP-tool-call with same `caller` shape

**Inspecting per-user usage:**
```bash
# Today's spend per user
grep "$(date -u +%Y-%m-%d)" ~/jarvis/logs/api_requests.jsonl | jq -r 'select(.caller | contains("@")) | "\(.caller)\t\(.cost_usd)"' | awk '{s[$1]+=$2} END {for (u in s) printf "%-30s $%.3f\n", u, s[u]}'

# Who's near budget cap right now
ssh ubuntu@3.6.202.121 'sqlite3 ~/jarvis/state/portal.db "SELECT email, daily_budget_usd, spent_today_usd, ROUND(spent_today_usd*100.0/daily_budget_usd, 1) AS pct FROM users WHERE spent_today_date = date(\"now\") ORDER BY pct DESC LIMIT 10;"'
```

## Retrieval reranker (shipped 2026-06-11)

`scripts/agent/tools.py::search_code` now runs vector retrieval + Claude Haiku 4.5 cross-encoder rerank by default. Pulls top-20 from Qdrant, sends the query + numbered chunk previews to Haiku with a relevance rubric, returns top-k in Haiku's order. Implementation in `scripts/agent/reranker.py`. Falls back to vector order on any error.

**Eval lift (vector-only → reranked):**
- hits@1: 18% → 28% (+10pp / +56% relative)
- hits@3: 30% → 40% (+10pp)
- MRR: 0.26 → 0.34
- Best buckets: kafka 0% → 67%, other 22% → 44%, onboarding 12.5% → 25%

**Cost / latency:** ~$0.003 per search call, ~1-2s extra latency. Currently ~$0.50/day at production volume.

**Opt-out:** `JARVIS_DISABLE_RERANK=1` falls back to pure vector. The pre-rerank function remains in `agent.tools.search_code_vector` for sub-second use cases.

## Retrieval eval (shipped 2026-06-11)

`scripts/eval/jarvis_eval_v1.jsonl` — 50 queries sampled from `qa_log.jsonl` (de-duped, length-filtered, thumbs-down filtered). Each carries auto-extracted ground-truth citations from past accepted answers. Bucketed across `api/be/fe/kafka/flow/lookup/onboarding/xstate/other`.

**Baseline captured 2026-06-11 post-AST + commit-SHA permalinks + realtime indexing:**
- hits@1 = 18% · hits@3 = 30% · hits@5 = 34% · hits@10 = 42% · MRR = 0.259 · permalink rate = 100%
- Strongest bucket: `lookup` (50% hits@1) — the deterministic `lookup_service` registry compensates well
- Weakest: `onboarding/kafka` (0% hits@1) — likely needs hybrid BM25 + symbol-level metadata

**Running:**
```bash
ssh ubuntu@3.6.202.121 'source ~/.config/jarvis/env && cd ~/jarvis/scripts && ./indexer/.venv/bin/python -m eval.run_eval --k 10'

# Save a labeled baseline before/after any retrieval change
ssh ubuntu@3.6.202.121 'source ~/.config/jarvis/env && cd ~/jarvis/scripts && ./indexer/.venv/bin/python -m eval.run_eval --k 10 --save 2026-06-15-hybrid-bm25.json'
```

**Interpretation gotchas:**
- The eval ONLY tests raw `search_code`. The full agent loop adds grep, lookup_service, read_file, etc., so end-to-end answer quality is higher than these numbers suggest.
- Ground truth was auto-extracted from past Jarvis answers — some "misses" are equally valid alternative files the agent could legitimately surface. Hand-curated v2 is on the roadmap.

**Refreshing the eval set:**
`python3 scripts/eval/build_eval_set.py` re-samples from current `qa_log.jsonl`. Re-run baselines after a refresh so comparisons stay fair.

## AST-aware chunking (shipped 2026-06-11)

Code chunks were line-windowed at ~800 tokens, cutting methods in half and producing chunks with no signature visible. Now: for `.kt/.kts/.ts/.tsx/.js/.jsx/.mjs/.cjs` files, the indexer uses **tree-sitter** to walk the parse tree and emits chunks aligned to function / class / object / interface boundaries. Each chunk now contains a full declaration (or a packed run of small ones) with the signature at the top.

**Implementation:** `scripts/indexer/ast_chunker.py` — lazy-loads `tree-sitter`, `tree-sitter-kotlin`, `tree-sitter-typescript` (installed in `indexer/.venv`). `chunker.chunk_text(text, ext)` dispatches: OpenAPI YAML → per-path chunker (unchanged), supported extension → AST chunker, else → line-window default. AST failures fall back to line-window so a grammar bug on one file never blocks indexing.

**Packing strategy:** greedy pack siblings up to `CHUNK_TARGET_TOKENS` (800). For containers (`class_declaration`, `class_body`, `object_declaration`, `interface_declaration`, `namespace_declaration`, `export_statement`) whose body exceeds target, recurse one level — each method becomes its own chunk. For leaves still exceeding `CHUNK_MAX_TOKENS` (6000), fall back to line-window inside that leaf.

**Rollout:** the big-5 (jupiter, bff-core, platform, lms, gateway) were force-reindexed to AST chunks on ship day. The long tail (~255 repos) gets AST chunks naturally as engineers push — the realtime webhook (v0.4) reindexes on every push to default branch, and each reindex now uses the AST chunker.

**Verifying chunk quality:**
```bash
# Confirm a chunk's text starts at a real declaration boundary
ssh ubuntu@3.6.202.121 'cd ~/jarvis/scripts && ./indexer/.venv/bin/python -c "
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
c = QdrantClient(host=\"localhost\", port=6333)
pts, _ = c.scroll(collection_name=\"jarvis_code\",
    scroll_filter=Filter(must=[FieldCondition(key=\"repo\", match=MatchValue(value=\"gateway\"))]),
    limit=5, with_payload=True)
for p in pts:
    print(p.payload[\"path\"], p.payload[\"start_line\"], \"--\", p.payload[\"end_line\"])
    print((p.payload[\"text\"] or \"\")[:200])
    print()
"'
```

## Slack & GitHub engineer-delight surfaces (shipped 2026-06-12)

A second session-block of features built atop today's earlier infra:

### @jarvis mention with thread context (commit b71d063)
- `@app.event("app_mention")` in `scripts/slackbot/app.py`
- Reads the surrounding thread via `conversations.replies` (up to 40 msgs)
- Passes prior messages to `agent.ask(..., prior_messages=...)` so the agent
  already knows what's been discussed
- Posts a ":thought_balloon: Thinking…" placeholder, updates with the answer
- Top-level mention → starts a thread; in-thread mention → replies in-thread
- Allowlist-gated via `JARVIS_ALLOWED_CHANNELS`; silent skip when off-allowlist
- Audit shape mirrors `/jarvis` with `source=mention`

### Conversation-to-fix hand-off (commit 62dd4f4)
When the mention text matches fix-intent regex (`fix this`, `draft a PR`,
`raise a PR`, `jarvis fix`, etc):
1. `scripts/agent/thread_to_fix.py` (Haiku 4.5) extracts a strict-JSON brief
   `{is_fix_request, repo, task_description, file_pointers, confidence, missing_info}`
2. If `is_fix_request=true`: post the brief + ask requester to reply `go` / `cancel`
   / paste-an-edited-task-line
3. State in in-memory `_PENDING_FIX` dict keyed by `(channel, thread_ts)`, TTL 10 min
4. Confirmation messages from anyone OTHER than the original requester are
   silently ignored (one-asker, one-decision)
5. On `go`: POST `/api/v1/fix` with the brief; daemon thread polls
   `/api/v1/fix/{job_id}` until terminal and posts the PR URL back in the thread

Audit: `~/jarvis/logs/thread_to_fix.jsonl`

### Webhook v0.5 — PR auto-fill description (commit 7ee7c45)
On `pull_request.opened` with empty/short body (<40 chars) from a non-bot author
on an allowlisted repo, Jarvis fetches the diff + commit subjects, calls Haiku
to draft Summary/Changes/Test plan markdown, posts it as the FIRST PR comment
(NOT replacing the description — engineer copies it in one keystroke or ignores).

`JARVIS_AUTOFILL_ALLOWED_REPOS` (default jupiter,bff-core,jupiter-design-system,jarvis),
`JARVIS_AUTOFILL_DAILY_CAP` (default 30). Skips bots, drafts, Jarvis-fired branches,
non-allowlisted repos, body ≥ 40 chars.

### Webhook v0.6 — CI failure autopsy (commit 7ee7c45)
On `check_run.completed` with `conclusion=failure` on an allowlisted repo,
when the check is tied to a PR (`pull_requests` list non-empty), Jarvis fetches
the failed job log (last 8k chars) + the PR diff, calls Haiku with a triager
rubric `{Failed, Likely cause, Why, Next step}`, and posts ONE comment on the PR.

`JARVIS_CI_AUTOPSY_ALLOWED_REPOS` (default jupiter,bff-core,jarvis),
`JARVIS_CI_AUTOPSY_DAILY_CAP` (default 20). Idempotent on `check_run_id` (grep
the audit log before posting). Skips Jarvis-fired branches (those route to
self-heal instead — see v0.8 below).

### Webhook v0.7 — autofix-on-comment (commit 17b494b)
The CI autopsy comment now ends with: "Reply with `jarvis fix` and I will draft
a PR." New `react_to_autofix_request` handler on `issue_comment.created`:
- Trigger strings: `jarvis fix`, `jarvis-fix`, `jarvis:fix`
- Commenter must be the PR author (no hijacking)
- Searches the PR's recent comments for the autopsy text (matches "CI failure
  autopsy" + "Drafted by Jarvis"), uses it as the fix brief
- POSTs `/api/v1/fix` with `caller=autopsy-reply:<login>` and idempotency key
  on the comment id

### Webhook v0.8 — self-healing CI on Jarvis-fired PRs (commit pending)
When `ci_failure_autopsy` detects the PR is on a Jarvis branch (head.ref starts
with `jarvis-fix-`, `jarvis-iterate-`, etc), it delegates to
`_ci_self_heal_jarvis_pr` INSTEAD of posting the human-facing autopsy.

Flow:
1. Count past self-heal attempts on this PR (grep audit log for
   `self_heal_iterate_dispatched`). If ≥ `JARVIS_SELF_HEAL_MAX_ATTEMPTS`
   (default 2), post a "self-heal exhausted, handing off to a human" comment
   and stop.
2. Haiku writes a minimal-fix brief from the failed log + diff. If it returns
   `"FLAKE: <reason>"`, post a "classified as flake, not iterating" comment
   and stop.
3. POST `/api/v1/pr/iterate` with the brief — the iterate flow pushes a
   follow-up commit to the same branch which triggers fresh CI.
4. Post a short status comment so reviewers know what's going on.

Idempotency on `check_run_id`. Auditable via
`grep self_heal ~/jarvis/logs/github_reactive.jsonl`.

### New agent tools (commits 7ee7c45 + 17b494b)
| Tool | Purpose |
|---|---|
| `why_was_this_changed(repo, path, line?)` | Git archaeology — combines `git blame` + the PR that introduced + linked Jira + reviewer comments |
| `impact_analysis(target, repo?)` | Downstream consumers of a file/symbol — composes `lookup_symbol` + `grep_all_repos` + `lookup_service` |
| `parse_stacktrace(stacktrace)` | Parses Kotlin/Java JVM, JS/Node V8, Python tracebacks into structured frames |
| `write_test_for(repo, file_path, function?)` | Drafts tests matching existing repo style (framework, assertions, naming) |
| `service_tour(repo)` | 15-min walkthrough: CLAUDE.md + entry points + key dirs + recent merged PRs |

System prompt nudges agent toward the right tool for each shape of question.

### Morning brief — daily personalized DM (commit 17b494b)
`scripts/morning_brief.py` + `scripts/systemd/jarvis-morning-brief.{service,timer}`.
For each portal user, builds:
- PRs awaiting their review (`gh search/issues` with `review-requested:<login>`)
- Their in-flight Jarvis fix/iterate jobs (`fix_audit.jsonl`, last 24h)
- Top 10 merged PRs across jupitermoney/* in the last 24h
- Their remaining daily Jarvis budget from `portal.db`

DMs via `chat.postMessage`. Timer fires at 03:30 UTC (09:00 IST). Audit at
`~/jarvis/logs/morning_brief.jsonl`. Activation: `sudo cp scripts/systemd/
jarvis-morning-brief.{service,timer} /etc/systemd/system/ && sudo systemctl
daemon-reload && sudo systemctl enable --now jarvis-morning-brief.timer`.

### Crash-fix postmortem (lesson worth keeping)
On 2026-06-12 ~12:26 UTC I introduced an f-string interpolation bug in the
system prompt: literal `{file, line, function}` in a Python f-string was
evaluated as variable names → `NameError` → jarvis-api crash-loop → OnFailure
hooks fired 4-5 alerts to Rohit before I caught it. Fixed in agent.py by
switching to `(file, line, function)`. Lesson: when editing the SYSTEM_PROMPT
f-string, ALWAYS escape literal `{}` as `{{}}` OR avoid curly braces entirely.
A CI guard that does `python -c 'from api import server'` on every commit to
`scripts/agent/*` or `scripts/api/*` would have caught this — half-day to add
as a follow-up.

## GitHub webhook surface (v0.1 + v0.2 + v0.3 shipped 2026-06-10/11)

`POST /api/v1/github-webhook` — receives PR / review / push events from a single GitHub organisation-level webhook configured at `jupitermoney/*`. HMAC-SHA256 verified against `GITHUB_WEBHOOK_SECRET`. Reachable from GitHub's CIDRs after Nikhil Kataria opened the AWS SG ingress on 2026-06-11.

**v0.1 — passive collection (2026-06-10):** every verified event normalized into a record (event type, action, repo, sender, PR metadata, review state + body preview, comment path) and appended to `~/jarvis/logs/github_events.jsonl`. No reactive logic.

**v0.2 — reactive DM (2026-06-11):** for `pull_request_review.submitted` and `pull_request_review_comment.created` events on PRs whose `head.ref` matches a Jarvis-fired branch prefix (`jarvis-fix-`, `jarvis-iterate-`, `jarvis-migrate-`, `jarvis-claudify-`, `jarvis-nitpick-`, `jarvis/`, `add-claude-md-docs`), Jarvis DMs the operator (Rohit by default, override via `JARVIS_REACTIVE_DM_USER`) with the reviewer name, review state, body preview, and PR link. Skips self-reviews by `jarvis-bot`. Audit log: `~/jarvis/logs/github_reactive.jsonl` captures every dispatch including skip reasons.

**v0.3 — auto-fire /jarvis review on opened PRs (2026-06-11):** for `pull_request.opened` events, spawn `scripts/jarvis_review.py` (the same cross-repo impact reviewer used by Slack `/jarvis review`) fully detached via `bash -c "nohup ... &"`. The review script posts ONE review comment to the PR via gh api. Filters (each logged): non-`opened` action, draft, bot sender (`type=Bot` or `[bot]` suffix), Jarvis-fired branch (avoid review-self loop), repo not in `JARVIS_AUTO_REVIEW_ALLOWED_REPOS` (default: `jupiter`), today's auto-review count >= `JARVIS_AUTO_REVIEW_DAILY_CAP` (default: 10). Per-fire log file at `~/jarvis/logs/auto_review_<pr>_<ts>.log`.

**v0.4 — real-time indexing on push (2026-06-11):** for `push` events on a repo's default branch, spawn `scripts/realtime_index.sh <repo> <branch>` detached via `bash -c "nohup ... &"`. The wrapper `git fetch --depth=1 + reset --hard FETCH_HEAD` brings the local clone up to date, then runs `python -m indexer.main <repo>` (incremental, hash-diffed — only re-embeds files whose SHA-256 changed). Replaces the once-a-day reindex with a ~10-second post-push refresh. Per-repo cooldown via lock-file mtime (`JARVIS_REALTIME_INDEX_COOLDOWN_SEC`, default 60s) debounces bursty pushes. Skips: non-push event, ref != `refs/heads/<default_branch>`, repo not in `indexed_repos.txt`, within cooldown. Wrapper writes a `completed` record to `~/jarvis/logs/realtime_index.jsonl` with `head` SHA + `elapsed_sec`; the nightly `jarvis-reindex` timer is still active as belt-and-suspenders for repos without the webhook attached.

**Reactive layer entrypoint:** `scripts/api/reactive.py` — two functions (`react_to_review`, `auto_review_pr`) dispatched via FastAPI `BackgroundTasks` so the webhook returns 200 within ms regardless of DM / spawn latency. Both functions are designed to NEVER raise — any failure becomes an audit-log record with the error.

**Auditing the reactive layer:**
```bash
# what fired or got skipped today
tail -100 ~/jarvis/logs/github_reactive.jsonl | jq 'select(.ts | startswith("'$(date -u +%Y-%m-%d)'")) | {action, repo, pr_number, head_ref}'

# count of auto-review fires today (vs daily cap)
grep '"action": "auto_review_fired"' ~/jarvis/logs/github_reactive.jsonl | grep "$(date -u +%Y-%m-%d)" | wc -l

# realtime reindex completions today + per-repo timing
tail -50 ~/jarvis/logs/realtime_index.jsonl | jq 'select(.action == "completed") | {repo, head, elapsed_sec}'
```

## MCP server surface (Phases 1+2+3 shipped 2026-05-21)

Hybrid architecture per `memory/project_mcp_hybrid_decision.md`: Jarvis's retrieval surface AND its agent-loop triggers are now reachable as MCP tools alongside the existing HTTP API + Slack transports. The actual fix/iterate/preflight LOOPS still live in the HTTP API on `:8081` — MCP trigger tools POST to them. All 3 phases shipped same-day.

**Entry point:** `scripts/run_mcp_server.sh` → `scripts/jarvis_mcp/server.py`. Two transports:
- `stdio` (Phase 1): spawned per-session by local MCP clients via SSH.
- `streamable-http` (Phase 2): long-running systemd service `jarvis-mcp.service` on `127.0.0.1:8082`. Bearer-auth (same `JARVIS_API_KEY` as jarvis-api). Bound to localhost — engineers tunnel via `ssh -L 8082:localhost:8082`. Flip to `0.0.0.0` if/when remote bots need direct access.

**Tools advertised (15):**
- Retrieval (10, read-only): `jarvis_search_code`, `jarvis_read_file`, `jarvis_search_prs`, `jarvis_git_history`, `jarvis_list_repo_files`, `jarvis_list_indexed_repos`, `jarvis_find_repo`, `jarvis_fetch_jira_ticket`, `jarvis_fetch_pr_diff`, `jarvis_get_capabilities`. Thin wrappers over `scripts/agent/tools.py` + `scripts/jira_fetch.py`.
- Trigger-shim (5, Phase 3): `jarvis_fire_fix`, `jarvis_fire_iterate`, `jarvis_fire_preflight`, `jarvis_get_fix_status`, `jarvis_get_iterate_status`. POST/GET to `http://127.0.0.1:8081/api/v1/*` with the server's `JARVIS_API_KEY`. **`fire_fix` and `fire_iterate` REFUSE if `idempotency_key` is empty** (anti-double-spend; see `project_http_fix_endpoint.md`). The HTTP API enforces write-allowlist + budget caps + audit unchanged — MCP triggers never bypass them.

**Local MCP client config — STDIO (Claude Desktop / Cursor / Claude Code):**
```json
{
  "mcpServers": {
    "jarvis": {
      "command": "ssh",
      "args": ["-i", "~/Downloads/data-science.pem",
               "ubuntu@3.6.202.121",
               "/home/ubuntu/jarvis/scripts/run_mcp_server.sh"]
    }
  }
}
```

**Local MCP client config — HTTP (via SSH tunnel):**
```
ssh -L 8082:localhost:8082 ubuntu@3.6.202.121   # in a separate terminal
```
```json
{
  "mcpServers": {
    "jarvis": {
      "url": "http://localhost:8082/mcp/",
      "headers": { "Authorization": "Bearer $JARVIS_API_KEY" }
    }
  }
}
```

**Smoke tests:**
- `scripts/smoke_mcp.py` — stdio transport. Lists tools, runs 4 retrieval calls. Exit 0 = PASS.
- `scripts/smoke_mcp_http.py` — HTTP transport + Bearer + trigger-shim refusal + real preflight. Exit 0 = PASS.

Run both after any edit to `jarvis_mcp/` or to any module it wraps. They take <10s combined and exercise the auth gate, transport, tool dispatch, and the end-to-end loop into jarvis-api for the trigger tools.

**Audit log:** `~/jarvis/logs/mcp_audit.jsonl` — one line per tool call (ts, caller, tool, args, latency_ms, ok, error). Deliberately separate from `qa_log.jsonl`, `api_requests.jsonl`, `fix_audit.jsonl` so daily-usage greps don't double-count.

**Operating the jarvis-mcp service:**
```bash
sudo systemctl status jarvis-mcp
sudo systemctl restart jarvis-mcp
sudo journalctl -u jarvis-mcp -f
```
Failure auto-notifies via the shared `jarvis-failure-notify@.service` hook.

**Non-regression contract (do NOT violate when extending):**
- No edits to `scripts/agent/tools.py`, `scripts/agent/retriever.py`, `scripts/api/server.py`, `scripts/api/jobs.py`, `scripts/agent/jove_client.py`. MCP layer wraps them unchanged.
- HTTP transport bound to `127.0.0.1:8082` only by default. Flipping to `0.0.0.0` requires explicit ack — same security model as jarvis-api but a new attack surface.
- Trigger tools (`jarvis_fire_*`) MUST POST to the existing HTTP API — never reach into jarvis-api's in-memory job store directly.
- `fire_fix` and `fire_iterate` MUST require non-empty `idempotency_key`. Removing this guard re-introduces the RECO-1259 double-spend pattern.
- Before flipping any new MCP capability on: run `smoke_mcp.py` AND `smoke_mcp_http.py` AND `smoke_http_fix.py` AND a manual Slack `/jarvis ask` — all four must pass.

**Roadmap (future, if pulled):**
- Expose HTTP transport at `0.0.0.0:8082` when a remote bot (Jove / Janus / Juno) needs direct MCP access without SSH tunneling.
- Add MCP resources (not just tools) for read-heavy artifacts like the indexed-repos list, capability manifest, and recent qa_log/fix_audit slices.
- Add MCP prompts for canonical workflows (e.g. "investigate this alert", "review this PR diff").

## Engineer onboarding to MCP (the seamless-coding path)

**North-star goal:** Jupiter engineers use ONE tool for coding — their local Claude Desktop / Cursor / Claude Code — with Jupiter context flowing in via MCP. Not "Claude separately + Jarvis separately." Phase 1 of this rollout is the one-command installer.

**Installer:** `scripts/jarvis-onboard.sh`. Engineers run on their Mac:
```
bash <(curl -fsSL https://raw.githubusercontent.com/jupitermoney/jarvis/main/scripts/jarvis-onboard.sh)
```
Generates ed25519 SSH key (or reuses), prints the exact Slack DM format the slackbot onboarding handler parses, pauses for Rohit's approval + bearer DM (if HTTP), then prints ready-to-paste MCP config snippets for Claude Desktop / Cursor / Claude Code (no auto-editing — engineer stays in control of their config file merge). Smoke-tests SSH + authed bearer endpoint.

**Roadmap toward seamless coding** (north-star phases):
1. **Distribute MCP via installer** (this commit) — get every active engineer's local AI tool onto Jarvis MCP. Current adoption: 3 engineers; target: all ~30 active.
2. **Per-engineer bearer tokens** — replace shared `JARVIS_API_KEY` with per-user revocable tokens; enables per-user audit + budget cap. Prereq for #3.
3. **Anthropic-API-compatible proxy** at e.g. `api.jarvis.jupiter.money` — engineers point Cursor/Claude Code at it instead of Anthropic directly. Single org Anthropic contract, full audit, per-engineer caps, auto-inject Jupiter system prompts. Removes the "Claude vs Jarvis are separate tools" friction structurally.
4. **Proactive context** — MCP detects which Jupiter repo the engineer has open, auto-fires `lookup_service` / `search_code` / `search_prs` for relevant terms in the active file. Engineer doesn't ask — context appears.

Reasoning behind beta-first ordering documented in `memory/feedback_post_then_update_double_post.md` and conversation thread 2026-06-03.

## Common operations

```bash
# usage report (today, IST)
ssh ubuntu@3.6.202.121 'source ~/.config/jarvis/env && cd ~/jarvis/scripts && ./indexer/.venv/bin/python /tmp/full_day_usage.py'

# tail logs
ssh ubuntu@3.6.202.121 'sudo journalctl -u jarvis-slack -f'

# restart everything
ssh ubuntu@3.6.202.121 'sudo systemctl restart jarvis-slack jarvis-api'

# index a new repo
ssh ubuntu@3.6.202.121 'cd ~/jarvis/scripts && ./scan_and_index.sh <repo-name>'

# smoke-test the /api/v1/fix endpoint end-to-end (opens + closes a real draft PR on jupitermoney/jarvis)
ssh ubuntu@3.6.202.121 'source ~/.config/jarvis/env && cd ~/jarvis/scripts && ./indexer/.venv/bin/python smoke_http_fix.py'

# inspect an in-flight /api/v1/fix run
ssh ubuntu@3.6.202.121 'grep <task_id> ~/jarvis/logs/fix_audit.jsonl | tail -10'

# iterate on an existing PR's review comments (one-shot manual orchestration; pattern for v0.3 API)
# 1. Workspace at ~/jarvis/workspaces/<task_id>/<repo>/ is still on disk for 24h after a run
# 2. cd in, run claude headless with prompt that reads the PR comments, commit + push to same branch
# 3. PR auto-updates; standards bot re-reviews
```

## Key contacts

- **Owner:** Rohit Pandey (Slack `U0837N31T9C`) — DMs for all system alerts; sole approver for any Slack message sent from the bot.
- **Allowed direct DM users:** see `JARVIS_ALLOWED_DM_USERS` env var.
- **SRE bot integration:** Sumith (his bot calls `jarvis-api`).
- **Jove team:** Confluence Q&A backend (don't modify Jove from here — file issues with that team).

## For contributors

Jarvis is small enough that any engineer can ship a PR. To get started:

1. **Read this CLAUDE.md and `memory/MEMORY.md` first.** Both are designed to give you the operational mental model in a single sitting. The memory directory holds 30+ short notes capturing hard-won rules from real incidents — most "wait, why does it work this way?" questions are answered there.
2. **Pick a small first contribution.** Good starter areas: a new agent tool in `scripts/agent/tools.py`, a new MCP tool in `scripts/jarvis_mcp/server.py`, a new capability entry in `scripts/agent/capabilities.py`, or improvements to the open-PR digest format. Avoid touching the agent loop, the fix/iterate runners, or the systemd units on your first PR.
3. **Run your changes locally before pushing.** Everything that matters is a Python script + bash; nothing requires a special build step. `~/jarvis/scripts/indexer/.venv/bin/python -m py_compile <your_file.py>` catches the obvious mistakes. For Slack-touching changes, use the dry-run flag patterns from existing scripts.
4. **Open the PR as draft.** Mark ready-for-review only after you've smoked it on the box (or asked Rohit to). Reviewer is Rohit by default.
5. **Update `scripts/agent/capabilities.py` on every feature ship.** That's the hard-won discipline (`feedback_capabilities_discipline.md`) — without it, Jarvis's self-description drifts from reality.
6. **Restart every service that loads the module you changed.** `py_compile` and a CLI smoke test do NOT prove a running service has picked up the change. See the deploy discipline section above.

What NOT to do on a first PR:
- Don't add new Slack channels, new bot users, or new GitHub tokens — those are owner-only changes.
- Don't change service env vars (`~/.config/jarvis/env`) without coordinating — they're shared state.
- Don't bump the `JARVIS_API_KEY` or rotate any secret — that breaks every downstream caller (SRE bot, Jove, Aura, MCP clients).
- Don't auto-merge anything. PRs always open as draft and get human review.

The codebase is intentionally simple. If something feels harder than it should be, there's probably a memory note explaining why we accepted that complexity. Read the memory before refactoring.

## When in doubt

1. Read `scripts/agent/capabilities.py` — it lists every Jarvis feature with its current state.
2. Read recent commits — `git log --oneline -20` on this repo.
3. Check `~/jarvis/logs/qa_log.jsonl` for what real questions look like (locally on the box only — never copy contents off-box).
4. Ask Rohit before doing anything that touches Slack, GitHub, or production state.
