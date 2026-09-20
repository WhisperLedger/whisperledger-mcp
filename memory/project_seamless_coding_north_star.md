---
name: Seamless-coding north star — 4-phase rollout
description: The strategic direction for getting Jupiter engineers to use ONE tool for coding (their local Claude/Cursor/Claude-Code with Jupiter context via MCP), not "Claude separately + Jarvis separately." Decided 2026-06-03 in conversation with Rohit.
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
**The goal:** Jupiter engineers code with their existing local AI tools (Claude Desktop / Cursor / Claude Code), and Jupiter context — codebase, service registry, PR history, fire-actions — flows in transparently via MCP. They never have to switch to Slack to "use Jarvis." Adoption target: all ~30 active engineers (currently 3).

## Why this matters

Rohit's hypothesis: today engineers maintain two parallel mental models — "Claude (with my personal Anthropic key, no Jupiter context)" and "Jarvis (Slack only, Jupiter context but a different UX)." That friction loses most of Jarvis's value. The fix is structural, not feature-additive: make Jarvis the way Claude/Cursor naturally work.

This shows up in real signals: Mithun's "Jarvis can't execute shell" feedback was a discoverability problem (he used the Q&A path, not `/jarvis fix`). Aura's recurring "I exhausted my iterations" pattern. Engineers asking the same codebase questions to Claude that Jarvis could answer in one tool call.

## The 4 phases (in order)

### Phase 1 — distribute MCP to every active engineer

**Status:** installer shipped 2026-06-03 (`scripts/jarvis-onboard.sh`), beta-pending with Rohit.

**What ships:** one-command installer engineers run on their Mac. Generates ed25519 SSH key, prints the exact Slack DM the slackbot's onboarding handler parses, pauses for Rohit's approval + bearer DM, then prints ready-to-paste MCP config snippets for Claude Desktop / Cursor / Claude Code. Smoke-tests SSH + authed bearer endpoint.

**Open items before broad rollout:** Rohit's Mac beta run → fix friction → Mithun's turn → another fix pass → only then announce widely. See `feedback_post_then_update_double_post.md` for why beta-first matters.

**Mithun as collaborator (2026-06-07):** Mithun has signed up to ship PRs on Jarvis itself, not just consume it. CLAUDE.md has a new "For contributors" section pointing new dev-collaborators at the right starter areas (new agent tools, MCP tools, capability entries, digest format) and the off-limits surfaces (agent loop, fix/iterate runners, systemd units, secrets). His first contribution is the natural next test of the installer flow — when he sets up MCP, that doubles as a Phase-1 beta of `jarvis-onboard.sh`.

### Phase 2 — per-engineer bearer tokens

**Status:** not started; unblocks Phase 3.

**What ships:** replace the shared `JARVIS_API_KEY` with per-user revocable tokens issued via a Slack-gated approval flow. New endpoint `POST /api/v1/auth/provision`, new auth middleware accepting both the shared bearer (for bots — back-compat) and per-user tokens (audit-logged with `user_id`). Engineer DMs Jarvis "onboard me" → Rohit gets Block Kit approve button → bearer DM'd to engineer.

**Why this is the right next step:** today's shared bearer is fine for 3-5 engineers; problematic at 30 (no per-user audit, no revocation, no per-user budget cap). Also a hard prereq for Phase 3.

### Phase 3 — Anthropic-API-compatible proxy

**Status:** not started; unblocked by Phase 2.

**What ships:** a proxy at e.g. `api.jarvis.jupiter.money` (Anthropic-API-compatible). Engineers point Cursor / Claude Code at the proxy with a Jarvis-issued bearer. Proxy: per-engineer budget enforcement, full audit logging, optional Jupiter system-prompt auto-injection, MCP auto-attach.

**Why this is the structural unlock:** today engineers use personal Anthropic keys → no Jupiter visibility on spend, no central audit, no way to centrally enforce policy. Proxy = single org Anthropic contract, central control, removes the "Claude vs Jarvis are separate tools" friction at the BILLING layer (the deepest cause of the parallel-mental-model problem).

### Phase 4 — proactive context (the magic)

**Status:** not started; unblocked by Phase 1 adoption.

**What ships:** MCP detects which Jupiter repo Cursor has open, auto-fires `lookup_service` / `search_code` / `search_prs` for relevant terms in the active file. Engineer doesn't ask — context appears. This is the moment seamless coding stops being a slogan and starts being a felt experience.

## What this means for any feature ship in 2026-06+

Before shipping any new Jarvis surface, ask: does this make Phase 1-4 easier or harder? In-editor MCP tool > Slack command. Per-engineer auth-aware > shared-bearer. Audit-logged-per-user > unattributed. Refusal with "I don't know" > guessed answer (Aura's pattern). Choose the path that compounds toward the north star.

## Sources / reasoning thread

- 2026-06-03 conversation when Rohit asked "this is what I would like to achieve... how can this be achieved seamlessly?"
- Mithun's GitHub-migration feedback (the spark): a discoverability gap framed as a capability gap
- Recurring pattern of engineers using Claude + Jarvis separately when one would do
- CLAUDE.md "Engineer onboarding to MCP" section (committed in `7f2a1af`)
