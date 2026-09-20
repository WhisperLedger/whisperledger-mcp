---
name: HTTP fix + iterate endpoints — v0.5 state
description: /api/v1/fix and /api/v1/pr/iterate. Real-world validation across 5 PRs. Callbacks, idempotency, cost-tracking, test-exec, self-review, and iterate-on-PR all live as of 2026-05-19.
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
# HTTP API surface (as of 2026-05-19 evening)

```
GET  /health                          → liveness
POST /api/v1/ask                      → generic Q&A (sync)
POST /api/v1/alert-analysis           → SRE-bot alert analysis (sync; accepts OpsGenie URL as alert_name)
POST /api/v1/fix                      → async draft-PR creation
GET  /api/v1/fix/{job_id}             → poll fix job status
POST /api/v1/pr/iterate               → async PR-comment iteration (NEW 2026-05-19)
GET  /api/v1/pr/iterate/{job_id}      → poll iterate job status
```

## What the fix + iterate endpoints support

**Headers (POST):**
- `Authorization: Bearer <JARVIS_API_KEY>` (required, same key everywhere)
- `X-Jarvis-Caller: <name>` (recommended; tags audit log)
- `Idempotency-Key: <any string ≤128 chars>` (recommended for retry-prone callers; 5-min dedup)

**Body fields shared by both POSTs:**
- `repo` (must be on `JARVIS_WRITE_ALLOWED_REPOS`)
- `max_budget_usd` (default 2.0, HTTP ceiling 5.0)
- `callback_url` (optional; Jarvis POSTs the final status payload when terminal — eliminates polling)

**`/api/v1/fix` body adds:** `description` (PRD ≤16 KB)
**`/api/v1/pr/iterate` body adds:** `pr_number` (integer)

## Operational defaults (2026-05-19 baseline)

| Setting | Value | Env var override |
|---|---|---|
| Default per-job budget | $2.00 | (per-request `max_budget_usd`) |
| HTTP budget ceiling | $5.00 | `JARVIS_FIX_HTTP_BUDGET_CAP_USD` |
| Hard timeout | 30 min | `JARVIS_FIX_HTTP_TIMEOUT_SEC` |
| Concurrent job cap | 3 (shared between fix + iterate) | `JARVIS_FIX_HTTP_CONCURRENCY` |
| Idempotency window | 5 min | `JARVIS_FIX_IDEMPOTENCY_WINDOW_SEC` |
| Callback timeout | 10 s, single attempt | `JARVIS_FIX_CALLBACK_TIMEOUT_SEC` |

## Logs

| File | Source | Contents |
|---|---|---|
| `~/jarvis/logs/api_requests.jsonl` | API layer | every POST (job_id, repo, caller, budget, callback_url, idempotency_key, description_preview) |
| `~/jarvis/logs/fix_audit.jsonl` | API + jarvis_fix.sh | `received` (API, immediate) + `start` / `claude_started` / `success`|`failed` (bash, with cost_usd as of 2026-05-19) |
| `~/jarvis/logs/iterate_audit.jsonl` | jarvis_iterate.sh | mirrors fix_audit shape for iterate jobs |
| `~/jarvis/logs/fix_callbacks.jsonl` | API runner | every callback attempt (ok, status, body_preview, url, job_id, ts) |
| `~/jarvis/logs/claudify_cost_history.jsonl` | gc_workspaces.sh | claudify cost JSONs preserved before workspace GC — survives 7-day workspace lifetime |

## Quality-improvement prompts in jarvis_fix.sh (shipped 2026-05-19)

The agent prompt now includes two mandatory steps the standards-bot would have flagged otherwise:

- **VALIDATE step** now requires running unit tests for touched modules (yarn jest / gradle test / pytest). Max 3 fix-and-rerun rounds. Aborts with `JARVIS_FIX_FAILED` if tests still fail.
- **SELF-REVIEW step** (new, between VALIDATE and CREATE BRANCH): re-read own diff as senior reviewer; hunt for hardcoded design values, leftover TODOs/console.log, nested ternaries, inline arrow handlers >1 statement, magic numbers, wrong-directory-layer (e.g. mutations in `hooks/` vs `services/`), missing tests.
- **Design-token guidance:** SELF-REVIEW points specifically at `jupitermoney/jupiter-design-system → packages/design-system/src/theme/index.ts`, lists common token names (`colors.lightBgPrimaryA = '#FFFFFF'`, etc.), shows the `useTheme<Theme>()` import pattern. v0 of Phase B from the strategic plan; v1 (dedicated `search_design_tokens(intent)` tool) deferred until empirical signal that prompt alone is insufficient.

## Real-world production PR scoreboard (Jove × Jarvis)

| Date | Ticket | Job ID | Duration | Cost | PR | Outcome |
|---|---|---|---|---|---|---|
| 2026-05-17 06:39 | smoke | `fix-20260517-063950-...` | 48s | – | [#8](https://github.com/jupitermoney/jarvis/pull/8) (closed) | success |
| 2026-05-17 08:48 | Insurance Home V2 (8-file PRD) | `fix-20260517-084856-...` | 807s | – | [#14134](https://github.com/jupitermoney/jupiter/pull/14134) | success — JPE's first production-grade run |
| 2026-05-18 17:19 | RECO-1259 (Jove first attempt — pre-Idempotency-Key) | `fix-20260518-171955-c64e52` | 480s | ~$5 | [#14139](https://github.com/jupitermoney/jupiter/pull/14139) (closed as dup) | success but duplicate-fired |
| 2026-05-18 17:20 | RECO-1259 (operator manual fire — was unaware of dup) | `fix-20260518-172054-2592f3` | 507s | ~$5 | [#14140](https://github.com/jupitermoney/jupiter/pull/14140) | **canonical** — snapPoints fix |
| 2026-05-18 18:18 | RECO-133 (Apply text descender) | `fix-20260518-181811-320884` | 317s | – | [#14141](https://github.com/jupitermoney/jupiter/pull/14141) | success — Pressable+Text workaround |
| 2026-05-19 07:54 | RECO-133 iterate (smoke of new endpoint) | `iterate-20260519-075458-8d77f3` | 289s | $0.62 | [#14141](https://github.com/jupitermoney/jupiter/pull/14141) (commit `ba8b810c4d`) | **first verified iterate run** — replaced paddingTop/paddingBottom hack with symmetric `py={'xxs'}` + clarifying comment + TODO for sense-ui fix |

## What deliberately ISN'T in v0.5

| Deferred | Why | When to revisit |
|---|---|---|
| **Durable job store** | In-memory dict; lost on jarvis-api restart | When job-loss exceeds ~5/week |
| **Streaming progress events** | Status is binary-ish; `stdout_tail` shows last 2 KB on terminal | When a caller asks for mid-flight visibility |
| **`reviewers`, `base_branch` request fields** | Bash wrappers don't support | When JPE explicitly asks |
| **`/api/v1/claudify` HTTP endpoint** | Slack `/jarvis claudify` only generates CLAUDE.md docs, not free-form code | When real demand surfaces |
| **`search_design_tokens(intent)` tool (Phase B v1)** | v0 prompt-only guidance is live; build dedicated tool only if empirical signal shows it's needed | After ~3-5 real Jove PRs ship; if any still produce hardcode violations, build it |
| **`/api/v1/pr/iterate` workspace reuse** | Always fresh-clones today (safer — picks up any human pushes) | If the clone cost becomes the bottleneck |

## Known issues still open

1. **`claude_run.log` 0 bytes until completion** under buffered output — work IS happening but tail-able progress invisible. Switching HTTP-mode runs to `--output-format json` (done as of 2026-05-19 for cost-capture) didn't change this — the JSON is written all at once at the end. **Stream-json would fix it** but requires re-engineering output parsing. Defer.
2. **`api_requests.jsonl` does NOT have cost backfilled** on async terminal — `fix_audit.jsonl` is the canonical source; not unifying intentionally.

## Pointers (verify before quoting — code moves)

- `scripts/api/server.py` — FastAPI handlers (4 schemas, 6 endpoints)
- `scripts/api/jobs.py` — async runner; `create_fix_job` + `create_iterate_job`; `_maybe_fire_callback`; `check_idempotency_key`; in-memory state
- `scripts/jarvis_fix.sh` — bash wrapper, JSON output mode, test-exec + self-review prompt steps, design-token guidance
- `scripts/jarvis_iterate.sh` — iterate bash wrapper; fetches PR comments via gh api; always fresh-clones branch
- `scripts/gc_workspaces.sh` — preserves claudify cost JSONs to durable log before deletion
- `scripts/agent/capabilities.py` — manifest entries for `/api/v1/fix` and `/api/v1/pr/iterate`
- `scripts/smoke_http_fix.py` / `scripts/smoke_callback.py` / `scripts/smoke_idempotency.py` / `scripts/smoke_iterate.py` — per-feature smoke harnesses
- JPE-facing handoff doc: `~/Downloads/jarvis-response-http-fix-shipped.md` (pre-v0.3; **needs an update** once Jove adopts the new headers — task open)
