---
name: Jarvis-as-MCP — hybrid retrieval-MCP + HTTP-API-loops decision (2026-05-21)
description: Confirmed architectural call — partial MCP conversion. Expose retrieval + PR/Jira/audit fetchers as MCP tools (engineers' local Claude Code gets cross-repo intelligence inline); KEEP agent loops (fix/iterate/preflight/companion-PR) as HTTP API (long-running async + central safety/cost/audit). Optional thin MCP shim to fire HTTP loops from MCP clients.
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---

**Decision:** Jarvis becomes a **hybrid** — partial MCP server + existing HTTP API. NOT a full MCP conversion.

**Why partial, not full:**
- MCP is designed for stateless leaf tool/resource calls where the CLIENT drives the loop.
- Jarvis's high-leverage features (`/api/v1/fix`, `/api/v1/pr/iterate`, `/api/v1/preflight`, companion-PR auto-spawn) are 5-15min multi-step agent loops with central safety nets (Guards A/B, regression mode, cost caps, write-allowlist, audit log, idempotency keys). They don't fit MCP's request/response model — awkward polling pattern needed.
- Jove already uses HTTP API; forcing it to adopt MCP-client adds zero-value complexity.
- BUT: Jarvis's retrieval surface (cross-repo `search_code`, `read_file`, PR/Jira fetchers, audit lookups) is exactly what MCP is built for — stateless leaf calls that LLM clients (Cursor, local Claude Code, Claude Desktop, etc.) decide when to invoke. Today these are buried inside the agent loop, only reachable through Slack `/jarvis ask` or via the agent's internal tool dispatcher.

**Trigger context:** Jupiter engineering team has shipped CLAUDE.md files in each repo (2026-05). Engineers running Claude Code locally now have rich per-repo context. The marginal value Jarvis adds shifts from "the place that knows the code" to: (a) cross-repo retrieval, (b) centralized orchestration with safety nets.

**Phased build plan (~2 days total):**
1. **Phase 1 (~1d):** MCP server with ~6 retrieval tools (`search_code`, `read_file`, `search_prs`, `fetch_pr_diff`, `fetch_jira_ticket`, `get_audit_log` and similar). Stdio transport for local devs (SSH tunnel + MCP client config). Most-leveraged piece.
2. **Phase 2 (~0.5d):** HTTP-SSE MCP transport for remote clients (Jove, other org bots). Same tool surface, different transport.
3. **Phase 3 (~0.5d):** Thin MCP trigger-shim — `jarvis.fire_fix(jira_ticket=...)` MCP tools that POST to the existing HTTP API loops. Lets MCP clients kick off Jarvis-side orchestrations without leaving their local agent context. HTTP API still owns the actual loop + safety + cost.

**Out of scope:**
- Converting fix/iterate/preflight into native MCP tools — agent loops don't fit MCP.
- Replacing Jove's HTTP-API integration with MCP-client.

**Author / driver:** Rohit confirmed the recommendation 2026-05-21 in the same chat where the decision was discussed.

**When to start:** awaiting Rohit's "go" — could start Phase 1 immediately or batch with other work. No urgent deadline.

---

## Shipped 2026-05-21 — all 3 phases in one session

Commits on `jupitermoney/jarvis main`:
- `1abc361` — Phase 1: stdio transport + 10 retrieval tools
- `6d7a124` — Phases 2+3: streamable-http transport on `127.0.0.1:8082` + 5 trigger-shim tools (`fire_fix`, `fire_iterate`, `fire_preflight`, `get_fix_status`, `get_iterate_status`)

**Runtime state:**
- New systemd unit `jarvis-mcp.service` (active, bound to 127.0.0.1:8082, restart=on-failure)
- 15 MCP tools advertised
- Bearer auth via `JARVIS_API_KEY` (same key as jarvis-api; cuts key proliferation)
- `fire_fix` and `fire_iterate` REFUSE empty `idempotency_key` (enforced server-side; reproducible failure mode)
- Audit at `~/jarvis/logs/mcp_audit.jsonl`
- Smoke tests: `smoke_mcp.py` (stdio) + `smoke_mcp_http.py` (http) — both pass <10s combined
- Non-regression confirmed: jarvis-slack + jarvis-api unaffected, both /health endpoints green, capability self-awareness verified via /api/v1/ask

**Trigger-shim safety model (Phase 3 key choice):** MCP `fire_*` tools NEVER touch jarvis-api's in-memory job store directly. They `httpx.post()` to `http://127.0.0.1:8081/api/v1/*` with the server's Bearer + an `X-Jarvis-Caller: mcp:<caller_tag>` header. Means all existing safety nets (write-allowlist, budget caps, audit, idempotency dedup, callback support) flow through unchanged. Removing this indirection would re-introduce double-spend risk and break audit traceability.

**Exposure posture:** HTTP transport bound to `127.0.0.1` only. Remote bots that want direct access must either (a) SSH-tunnel to localhost:8082, or (b) Rohit explicitly flips `JARVIS_MCP_HOST=0.0.0.0` in `~/.config/jarvis/env`. Same network surface as jarvis-api would be at that point.

**Total build cost:** ~$0.25 (4 smoke runs + 2 self-awareness verifications + 1 real preflight). One session, ~3 hours wall-clock.

**Future deltas (if pulled, NOT scheduled):**
- Expose HTTP at `0.0.0.0:8082` when a remote bot (Jove / Janus / Juno) needs direct MCP access without SSH tunneling.
- Add MCP **resources** for read-heavy artifacts (indexed-repos list, capability manifest, recent qa_log/fix_audit slices) so clients can subscribe rather than re-querying tools.
- Add MCP **prompts** for canonical workflows ("investigate this alert", "review this PR diff").
- Per-caller quota on the retrieval semaphore (still single semaphore today — flips to per-caller if MCP traffic starts contending with Slack/Jove).

---

## Non-regression contract (added 2026-05-21 per Rohit's explicit ask)

The MCP build is PURELY ADDITIVE. Integrations that MUST NOT break:
- Jove → `/api/v1/fix`, `/api/v1/pr/iterate`, `/api/v1/preflight`, `/api/v1/ask` (HTTP API on `:8081`)
- SRE bot → `/api/v1/alert-analysis` (HTTP API)
- Slack `/jarvis ...` (socket-mode Bolt app)
- Bot-to-bot DMs (Jove/Janus/Juno IMs via `chat.postMessage`)
- iterate-auto-fire poller (polls GitHub → POSTs `/api/v1/pr/iterate`)
- Nightly reindex / spend-monitor / GC systemd timers

Build discipline (enforce on every PR touching MCP scope):
1. **No edits to `scripts/agent/tools.py`, `retriever.py`, `jove_client.py`, `api/server.py`, `api/jobs.py`** unless behavior-preserving. MCP layer imports and wraps these unchanged. If you find yourself needing to refactor a shared module, stop and rethink the MCP adapter shape first.
2. **Separate port for MCP HTTP-SSE** — `:8082` reserved. Never reuse `:8081`.
3. **Same `JARVIS_API_KEY` for read-only MCP**; do NOT proliferate a second key. MCP trigger-shim (Phase 3) inherits the same key + `JARVIS_WRITE_ALLOWED_REPOS` gate via the underlying HTTP call.
4. **New systemd unit `jarvis-mcp.service`** + bash wrapper (`run_mcp_server.sh`) sourcing `~/.config/jarvis/env`. Follow the `feedback_systemd_env_pattern.md` rule (never use `EnvironmentFile=`).
5. **Separate log file `~/jarvis/logs/mcp_audit.jsonl`** — do NOT write into `qa_log.jsonl` or `api_requests.jsonl` (would double-count in daily usage reports).
6. **Shared retrieval semaphore** — MCP-driven retrieval must contend on the same semaphore as Slack + HTTP API retrieval; add a per-caller quota so an engineer's local Claude Code session can't starve Jove/SRE-bot.
7. **Trigger-shim (Phase 3) REQUIRES `Idempotency-Key`** — refuse without one. Default key suggestion: Jira ticket key. Prevents triple-fire (MCP + Slack + Jove) on the same logical request.
8. **MCP trigger tools POST/GET via HTTP** to existing `/api/v1/*` endpoints — never reach into jarvis-api's in-memory job store directly (cross-process read would fail anyway).
9. **Pre-ship smoke test gate:** before flipping `jarvis-mcp` on, run `smoke_http_fix.py` end-to-end AND a manual Slack `/jarvis ask` — both must pass. Add an MCP-side smoke test mirroring those.
10. **Restart discipline:** if any shared module ends up edited (even by accident), restart BOTH `jarvis-slack` and `jarvis-api` and re-run the smoke gate.
