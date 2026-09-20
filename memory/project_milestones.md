---
name: Jarvis project milestones
description: Significant validation moments and decisions in the Jarvis-for-Jupiter build. Update as we hit new ones.
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
**2026-05-12 — First engineering validation.** Engineering team confirmed Jarvis's first end-to-end answer (the Stargate → lendingOrchestratorService routing question) was accurate. Validated facts: `StarMap.kt:137` upstream mapping, `JupiterConfig.kt:309` injected `lateinit var`, the two custom OpenAPI extensions (`x-stargate-internal-service`, `x-stargate-internal-path`), and the path-rewrite pattern across `lendingorchestrator.yaml`. No hallucinated paths.

**Why this matters:** Confirms that the Phase 0 stack (Voyage `voyage-code-3` + Qdrant + single-Sonnet-4.6-with-3-tools + ~800-token line-based chunking) produces truthful, useful answers without symbol-aware chunking, multi-agent routing, or eval-driven tuning. The "good enough to build on" bar has been cleared.

**How to apply:** Don't pre-optimize chunking, retrieval, or routing — invest in adoption (Slack/CLI distribution, broader repo coverage, secrets scrubber for prod.jupiter.money) before adding LLM-side complexity. Quality improvements should be eval-driven from this point forward, not speculative.

---

**2026-05-12 (afternoon) — Slack pilot launched, multi-engineer validation.** Bot live in `<#C092S7Z5HB5>`, ephemeral-only. In ~90 min: 6 unique engineers, 8 questions, zero errors, ~$1.50 in API.

**Direct engineer feedback:**
- **Akhil Choubey (PPI):** "Answered quite accurately, including edge cases. The response covered the FE implementation well, however, we should also include the BE services to provide end-to-end context for specific flows. Given the current context across the 5 repos, the overall answer was accurate."
- **Armaan Miglani (FE/onboarding):** "Happy and concerned at the same time that I am not needed anymore for context-related queries." Tried 3-4 FE-flow questions; **some better than Claude Code Sonnet** for codebase lookup. "What" and "how" accurate; testing "why" next.
- **Prasanna Hegde (CKYC):** Wants reply-in-thread support so follow-up questions stay in the same Slack thread.

**Confirmed gaps (priority order):**
1. **No thread / conversation context** — every `/jarvis` is standalone. Engineers want follow-ups in-thread without re-stating context. Critical for "why" questions and any multi-turn debugging.
2. **No BE repo coverage** — Akhil's note. Phase 0 has 5 repos; full end-to-end (FE → BFF → Stargate → BE service) needs more backend services indexed beyond `platform` and `lms`.
3. **"Why" questions untested** — pure RAG often weak here (no explicit "we did this because Y" text in code). ADR indexing + tighter chunking around docs may help.

**Confirmed product calls:**
- **No DMs** — Rohit explicitly said the pilot stays channel-only ("Only you can see your question and answer and myself"). Don't reopen the DM allowlist suggestion.

**How to apply:** Threading/conversation persistence is now the highest-leverage next feature — both for UX and for unlocking the "why" question class. Backend repo expansion is the second priority. Don't reopen DM access without explicit user request.

---

**2026-05-12 (later) — Tier 1 BE expansion shipped + structured PAN-flow feedback.** Added 5 BE repos (bullet, lending-orchestrator, cardboard, brahma, metal). Total now 10 indexed repos, 35,269 chunks. Bot promoted to systemd. Daily auto-reindex timer enabled (22:00 UTC).

**Detailed feedback on PAN verification flow answer (engineer at Jupiter, 2026-05-12):**
- ✅ Validated: Manual vs ePAN two-path split was accurate; Stargate route table was "genuinely useful"; per-file source references "specific enough to be actionable, will help reduce tokens when shared with claude for code generation"; final summary diagram is a "nice touch".
- ❌ Gap 1 — **XState machines not surfaced.** "That's often where the real logic lives." Jarvis described the flow at a screen level but didn't identify the underlying XState machine states/transitions.
- ❌ Gap 2 — **Entry points missing.** Jumps straight to PAN input screen without explaining what triggers it (which screen/event/deeplink invokes the flow). That's "step zero."
- 💡 Quote worth remembering: "Seems like this is ready to move to a chat hosted internally instead of just slackbot 😅" — first user-driven request for richer UI.

**Concrete prompt-side fixes (applied 2026-05-12):**
- For `jupiter` flow questions: explicitly look for XState machines (`*-machine.ts` files, `state/` directories) — they encode canonical flow logic.
- For any flow question: identify entry points (deeplinks, React Navigation config, parent screens with CTAs into the flow) as a distinct section.

**2026-05-12 (afternoon) — Personal Loan flow feedback drove another prompt fix.** Engineer (U09REQSHHLP) flagged: (1) IDENTITY_VERIFICATION milestone skipped, (2) APPLICANT_CREATION and IDENTITY_VERIFICATION swapped in the state diagram. Root cause: agent reconstructed milestone order from scattered usages in tests/services instead of reading the source-of-truth enum file. Fix: prompt step (f) now explicitly instructs to **locate the canonical state enum file** (look in `domain/`, `enums/`, `models/`, `state/` dirs) and **extract it verbatim** (`enum class LoanState { ... }`), citing file:line. Never reconstruct order from scattered usages.

Post-fix re-run: all 8 `LoanState` values present (was 0), order correct verbatim, also surfaced `LoanSubState` + `FailureState`. Validation PDF: `~/Downloads/jarvis-loan-validation.pdf`.

**Generalizable lesson:** for any "list" or "ordered set" question (state machines, status codes, error types, permissions, partner enums), the agent must find the **definition file** and quote it. Reconstruction is unsafe.

**How to apply:**
- "Internally hosted chat" is Phase 2 — Slack works, don't build a web UI yet, but track that the demand is there. If demand grows, evaluate Streamlit/internal portal.
- For RN/jupiter flow questions, system prompt now nudges toward XState + entry points. If still weak after rollout, consider adding an explicit `find_state_machines` or `find_callers` tool.
- **The "specific file:line citations reduce Claude Code token cost" insight is a meta-validation** of the citation-discipline design choice. Don't loosen citation rules.

---

**2026-05-17 — First production `/api/v1/fix` end-to-end.** Jove (JPE orchestrator) POSTed a comprehensive Insurance Home V2 brief (16KB PRD: 8 files, design-system constraints, analytics events, happy + edge-case scenarios) to the new `/api/v1/fix` endpoint shipped earlier today. The async job ran 13:27 on the box, beat the 15-min hard timeout by 47s, and **opened real draft PR [#14134](https://github.com/jupitermoney/jupiter/pull/14134) on jupitermoney/jupiter** — 920 additions across 9 files including the 3 new components (RiskSelector, CoverageGapBanner, PartnersGrid), the React Query data hooks, typed analytics wrappers, types, and a README with backend acceptance criteria. End-to-end programmatic flow: PM brief → Jove → Jarvis → real PR URL back to caller via GET poll, no human touch.

**Followed immediately by first review-iteration cycle.** Standards-review bot (armaan-miglani) posted 10 inline comments (2 HIGH for hardcoded hex colors, 3 MEDIUM for leftover TODOs + nested ternary, 5 LOW for missing tokens + inline arrow handlers + service-layer convention). Manually orchestrated a second Claude run on the existing workspace + branch (NOT a new `/api/v1/fix` call — that would have opened a 2nd PR); 282s, $<$1.50, addressed all 10 comments, pushed `ec6448c5a5` to the same branch, PR auto-updated. Net: complete PM→PR→review→fix loop closed in <30 min wall-clock.

**Why this matters:** First validation that Jarvis can produce **real, reviewable, design-system-compliant code** from a PRD via API — not just retrieval answers. The standards bot accepting the iteration commit proves the loop is closeable, not just openable. JPE can now compose a Jove tool that POSTs to `/api/v1/fix` and gets PR URLs back without copy-paste.

**Confirmed: agent-loop time is *not* the bottleneck for an 8-file fix** — 13:27 is well inside the timeout we set. What WAS tight: I had set 15min hard timeout in v0.2; bump to 30min for v0.3 since real PRDs can be heavier. Budget cap of $5 was also right-sized; Insurance V2 used ~$3 by the end of the iteration.

**How to apply:**
- Bump `JARVIS_FIX_HTTP_TIMEOUT_SEC` default from 900 → 1800 next deploy.
- Build the "iterate on existing PR using its own review comments" flow into the API (see `project_http_fix_endpoint.md`) — the manual orchestration I did today is the prototype; productize as `/api/v1/fix/{job_id}/iterate` or similar.
- The `--output-format text` buffering quirk (claude_run.log stays 0 bytes until completion) is fix-worthy — switch HTTP-mode runs to `--output-format stream-json` so we can watch progress.
- Daily `claudify` cap of $100 was untouched today; HTTP `fix` does NOT count against that cap — needs unifying if HTTP traffic grows.

---

**2026-05-18 — Jove × Jarvis loop hardened + validated at 3-PR scale.** Three production PRs from Jove via `/api/v1/fix` landed on `jupitermoney/jupiter` today: RECO-1259 (Contact Sheet snapPoints bug, PR [#14140](https://github.com/jupitermoney/jupiter/pull/14140), shipped earlier in the day), RECO-1259 retry-duplicate ([#14139](https://github.com/jupitermoney/jupiter/pull/14139) — closed as dup; see overspend incident below), and RECO-133 (Apply/Applied text-descender clipping, [#14141](https://github.com/jupitermoney/jupiter/pull/14141)).

**Overspend / double-submit incident on RECO-1259** — Jove's orchestrator retried on timeout AND the operator (me) separately fired a duplicate POST after a flawed audit-log check (the log was empty during `gh repo clone` blind spot). Cost: ~$10 instead of intended $5. Three fixes shipped same day to prevent recurrence:
- `callback_url` field on `/api/v1/fix` (commit `1b98e3a`) — callers no longer need to poll, eliminating the orchestrator's retry-on-timeout failure mode.
- `Idempotency-Key` header (commit `5ec89c6`) — same key within 5 min returns the original job_id, blocks duplicate spend.
- Early `received` audit-log write (commit `5ec89c6`) — closes the clone blind spot so audit-log checks become race-free.

**Quality observations from the 3 PRs:**
- Right file every time (semantic search reliably bullseyes).
- Root-cause quality varies: RECO-1259 was a precise architectural diagnosis (snapPoints array-reference-equality bug in `@gorhom/bottom-sheet`); RECO-133 was a call-site workaround (replaced sense-ui Button with Pressable+Text + explicit lineHeight) — works but doesn't fix the underlying sense-ui Button defect.
- Tests added: RECO-1259 had 128 lines of regression tests; RECO-133 had 0 (visual bugs hard to unit-test).
- Both PRs used magic numbers instead of design tokens — standards bot will flag both at review time.

**Operational discovery: spend-monitor systemd EnvironmentFile bug.** Operator received 27 OnFailure DM alerts in a day before flagging. Root cause: systemd's `EnvironmentFile=` directive silently dropped every line of `~/.config/jarvis/env` because the file uses `export VAR=...` format (compatible with bash-sourced scripts but not with systemd). Fix: replace `EnvironmentFile=` with `ExecStart=run_spend_monitor.sh` (bash wrapper that `source`s env), matching the pattern other Jarvis services already use. Pre-existing bug, not introduced today; just finally squashed (commit `a9336ea`). Memory rule saved: never use `EnvironmentFile=` on Python systemd services.

**OpsGenie + PR-indexer + spend-monitor: three infra cleanups landed.** Beyond the Jove fixes: OpsGenie context now inlined into `/jarvis investigate` (commit `33c693c`, shipped Sunday); PR-descriptions now refreshed nightly as part of `reindex_all.sh` (commit `437dd87`, fixing a 4-day-stale PR index); spend-monitor crash-loop fixed.

**How to apply:**
- Jove team needs to adopt `Idempotency-Key` + `callback_url` (not yet using either). Update the handoff doc at `~/Downloads/jarvis-response-http-fix-shipped.md` to reflect v0.4.
- Next iteration ideas: PR-iterate endpoint (`/api/v1/fix/{job_id}/iterate`), durable SQLite job store, 30-min default timeout.
- The "Jarvis writes code, human reviewer judges architectural intent" pattern is now validated at scale. Don't try to remove the reviewer — they're catching real architectural drift (workaround vs root-cause fix, design-token violations).

---

**2026-05-19 — hardening + iterate-on-PR sprint (9 commits in one day).** Closed all 6 outstanding /api/v1/fix follow-ups + shipped the iterate-on-PR endpoint that was on the roadmap. End-of-day state: 7 HTTP API endpoints live (was 5), all hardened for production-scale Jove traffic.

**Shipped today:**
- `jarvis_fix.sh` — test execution + self-review prompt steps + design-token guidance pointing at `jupiter-design-system` (Phase B v0; defers dedicated `search_design_tokens` tool until empirical signal)
- `JARVIS_FIX_HTTP_TIMEOUT_SEC` default bumped 15min → 30min (Insurance V2 finished with only 47s headroom under old ceiling)
- Workspace `TASK_ID` collision fixed (random suffix appended; same-second same-body POSTs no longer collide)
- Cost-logging gaps closed (task #61): `claude --output-format json` parses `total_cost_usd` into `fix_audit.jsonl` success/failed events; `gc_workspaces.sh` preserves claudify `*_costs.json` to durable `claudify_cost_history.jsonl` before workspace deletion
- `POST /api/v1/pr/iterate` — async endpoint that fetches PR review comments via gh api, fresh-clones the PR's branch, runs Claude to address comments, pushes (NEVER force-push) so existing draft PR auto-updates
- Reindex systemd `TimeoutStartSec` 40min → 4h (silently SIGTERM'd two nights running because pr_indexer addition pushed combined runtime past the old ceiling)

**Validated end-to-end:** `/api/v1/pr/iterate` smoke against PR #14141 (RECO-133). Iterate job `iterate-20260519-075458-8d77f3` ran in 288s, cost $0.62, landed real commit `ba8b810c4d` on the PR — replaced asymmetric `paddingTop/paddingBottom` with symmetric `py={'xxs'}`, added explanatory comment + TODO for upstream sense-ui fix. Reviewer (Chirag Khatri) sees the updated PR in their normal review flow.

**OnFailure-alert mystery resolved:** the two nights of silently-failed reindex DID fire OnFailure alerts (journal shows `notify_failure.sh[X]: alert sent for jarvis-reindex.service to D0B3V4XQR97` both nights) — operator just didn't notice amid the spend-monitor 27/day spam (separately fixed earlier in week). Lesson: when investigating alert-not-firing, query journal by the correct instance name (`%n` expansion adds `.service` to the parent unit's `.service` → double-`.service` instance name in journal).

**How to apply:**
- The deferred items in `project_http_fix_endpoint.md` should be revisited only on signal: durable job store on >5 loss incidents/week; Phase B v1 only if standards-bot keeps flagging hardcode violations after v0; stream-json output only if mid-flight visibility becomes a real ask.
- Channel announcement of today's improvements was deferred (per Slack-permission discipline) until next real Jove call validates the new prompt-driven quality work.
- Update the JPE-facing handoff doc (`~/Downloads/jarvis-response-http-fix-shipped.md`) to reflect v0.5 (callback_url, Idempotency-Key, iterate endpoint).
