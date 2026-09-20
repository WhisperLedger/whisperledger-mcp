# Grounding — make Astra check its own knowledge before burning tokens

*Status: design doc, not yet implemented · Owner: platform operator · Created 2026-08-18*

## Problem

Last 7-day usage (2026-06-16 → 2026-06-23):

- 259 Slack `/jarvis` questions from 29 engineers
- 5–8 iterations avg per question, often 13+ for heavy debug sessions
- $103 total Slack-agent cost ($145 incl HTTP API)
- Two distinct usage profiles emerged:
  - **Heavy debug** (Tushar / Tanmay / Rutvik / Charandeep): 8–14 tool calls / 60–90 s / $0.40–$0.80 per query — real investigations
  - **Quick lookups** (Mayank 27 queries at 1.1 tool-calls avg / Pranav / Yash): deterministic lookups that should be sub-10 s and ~$0.05

The leak: every question — including the lookup-shaped ones — goes through the full 18-iter Sonnet 4.6 agent loop. The agent re-discovers things it (or some prior session) already knew. There is no pre-flight that consults Jarvis's own knowledge base before firing tool calls.

## What "grounding" means here

Before the main Sonnet agent loop runs, consult three knowledge sources Astra already has:

1. **The capabilities manifest** (`scripts/agent/capabilities.py`) — every shipped platform feature
2. **The qa_log** (`~/jarvis/logs/qa_log.jsonl`) — every prior question + answer
3. **The memory store** (`~/.claude/.../memory/`) — user / feedback / project / reference memories accumulated across sessions

Use them to:
- Short-circuit deterministic-shape questions (route directly to the right tool, skip Sonnet)
- Prepend relevant prior context to questions that DO need the full agent
- Detect and warn when grounded context might be stale

Expected payback: 30–50% cost + latency reduction on the lookup-style profile; smaller win on deep investigations (which legitimately need the full loop).

Roughly 1 engineer-week of work for all three layers + eval.

---

## Layer 1 — Question router *(~2 days, highest ROI, ship first)*

Cheap Haiku 4.5 classifier (~$0.005, ~1 s) that maps every incoming question to one of:

| Route | What | Cost | Latency |
|---|---|---|---|
| `meta_capabilities` | Answer directly from `capabilities.py` formatted answer | ~$0.005 | <2 s |
| `lookup_service` | Straight to deterministic `lookup_service` tool, no Sonnet reasoning | <$0.01 | <2 s |
| `lookup_symbol` | Straight to `lookup_symbol` tool | <$0.01 | <2 s |
| `prior_match` | Semantic match against `qa_log` (≤30 d, similarity ≥0.85); return cached answer + offer to refresh | ~$0.01 | <3 s |
| `general` | Falls through to current full agent loop (no change) | $0.30–$1.00 | 30–90 s |

**Why this works:** the `get_capabilities()` SYSTEM_PROMPT rule already does meta-routing *implicitly* for self-referential questions — it depends on Sonnet deciding to call it. A pre-router *forces* the fast path before Sonnet sees the question.

**Big win profile:** the Mayank Dwivedi usage (27 queries / week, 1.1 tools-avg) — all deterministic lookups currently going through an 18-iter loop they don't need.

**Implementation:**

```
scripts/agent/question_router.py
```

- Pattern modeled on `scripts/agent/investigate_intent.py` (Haiku classifier with strict-JSON output, falls open on any error)
- Returns `{route, confidence, fast_path_args, reason}`
- Wired into `agent.ask()` as the first call before the Sonnet loop
- On `route in {meta_capabilities, lookup_service, lookup_symbol}` with confidence ≥ medium → invoke the fast path and return; otherwise fall through
- Audit log at `~/jarvis/logs/question_router.jsonl`

**Defensive defaults:**
- Low-confidence classifications → fall through to Sonnet (false negative is cheap; false positive is wrong-answer expensive)
- Any classifier exception → fall through silently
- `prior_match` requires BOTH similarity ≥ 0.85 AND the freshness check from Layer 3 — otherwise fall through

---

## Layer 2 — Context grounding *(~2 days, medium ROI, ship second)*

For questions that DO fall through to the `general` route, build a small "what we already know" prelude (≤2 KB) and prepend it to the user's question.

Prelude contents:
- Top 3 semantically-similar prior `qa_log` entries from the last 30 days (with timestamps + a disclaimer that the underlying code may have moved since)
- Top 3 relevant memory entries from `~/.claude/.../memory/` (user / feedback / project / reference)
- Auto-detected recent commits touching any files mentioned in the question (via `git log --since=30.days -- <file>`)

**Why this works:** the prelude becomes part of the cached prompt prefix. With Sonnet's `cache_control: ephemeral` (we already use this on SYSTEM_PROMPT + tool defs + reranker rubric), the input-token cost of the prelude is 83× cheaper on cache hits ($0.30/M vs $3/M). So even a 2 KB prelude is fractions of a cent per call after the first.

**Updated SYSTEM_PROMPT nudge:**

> If the grounding prelude already answers the question with high confidence and the cited evidence still resolves in current code, surface that answer first and only re-investigate if the user pushes back.

**Implementation:**

```
scripts/agent/grounding.py
```

- `build_grounding_prelude(question: str) -> str | None` — returns the prelude or None
- Internal helpers:
  - `_semantic_search_qa_log(question, k=3, lookback_days=30)` — vector search over qa_log content; we already have voyage-code-3 embeddings and Qdrant
  - `_semantic_search_memory(question, k=3)` — same pattern over the memory/ folder; need to add memory indexing as a one-time job
  - `_recent_commits_for_mentioned_files(question, lookback_days=30)` — regex extract `<repo>/<path>` mentions, run `git log`
- Result format: small markdown bulletted list, each item with `(date · what · file:line)` signature
- Called from `agent.ask()` after the question_router fall-through, before the Sonnet loop kickoff

**Indexing memory/:** one-time job to embed all `*.md` files under the memory store into a new Qdrant collection `jarvis_memory`. Index nightly along with the code corpus. Add to `scripts/reindex_all.sh`.

---

## Layer 3 — Stale-detection + freshness signal *(~1 day, smaller ROI, ship third)*

The hard part of any caching scheme: cached answers go wrong when underlying state changes (PR merges, file moves, service renames, rule edits).

For any cached or grounded answer, before surfacing, verify:

1. **Cited file paths still exist** — cheap `read_file` header check
2. **Cited PRs still in expected state** — `gh pr view <num> --json state`
3. **Cited symbols still exist** — `lookup_symbol(<name>)` returns a hit
4. **Cited services still in registry** — `lookup_service(<name>)` returns a hit

If anything has drifted → drop the cached answer, fall through to full agent.

**Hard TTL cap:** 30 days regardless of freshness. Anything older we don't trust even if all checks pass.

**Transparency:** every surfaced cached answer carries a marker:

> _Surfaced from a similar question asked by <user> on <date>. Re-investigated only the freshness of cited files — full code search not re-run. Ask again with `/jarvis -new` to force a fresh investigation._

**Implementation:**

```
scripts/agent/freshness_check.py
```

- `is_grounded_answer_fresh(answer: GroundedAnswer) -> tuple[bool, list[str]]` — returns (is_fresh, list_of_stale_signals)
- Cheap upstream: only consulted when Layer 1 returns `prior_match` or Layer 2's prelude has high-confidence answer-shape entries
- Audit log at `~/jarvis/logs/freshness_check.jsonl`

---

## File layout (delta to current repo)

```
scripts/agent/
  question_router.py       # NEW — Layer 1 Haiku classifier
  grounding.py             # NEW — Layer 2 prelude builder
  freshness_check.py       # NEW — Layer 3 stale-detection
  agent.py                 # PATCHED — ask() consults router → grounding → freshness before Sonnet loop
  capabilities.py          # unchanged
scripts/indexer/
  # add nightly memory/ indexing into jarvis_memory Qdrant collection
scripts/reindex_all.sh     # PATCHED — append memory/ indexing step
```

Audit logs:
- `~/jarvis/logs/question_router.jsonl` — every classification + decision
- `~/jarvis/logs/grounding.jsonl` — every prelude built (size, sources)
- `~/jarvis/logs/freshness_check.jsonl` — every freshness verdict

---

## Risks to mitigate

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Stale-answer false positives** — semantic match surfaces similar-but-wrong cached answer | Medium | Freshness check (Layer 3) + high similarity threshold (≥0.85) + the "surfaced from cache" disclosure line |
| **Hidden context bias** — prelude is too leading, agent over-fits | Low–medium | Structure prelude as "evidence to consider", NOT "answer to confirm". Test on eval set before flipping default |
| **Cost-floor not zero** — Haiku pre-flight + grounding search still cost ~$0.005-0.01 per query | Low | Real savings only on correct short-circuits. False-positive routes pay full agent cost on top. Eval-driven decision on whether to enable for the `general` route at all |
| **Memory drift** — memory contains stale claims about features that have since changed | Medium | The memory-system rule "before recommending from memory, verify" already addresses this for human-Claude sessions. Apply the same rule in `grounding.py`: don't surface memory claims about file/function/service existence without verification |
| **Privacy** — qa_log contains engineering queries that may reference user_ids. Cached answers might leak that one engineer was investigating user X. | Low | Cached answers only surface within Slack to the same channel/user the prior asked; never cross-user-surface. Also: grounding consults qa_log content but doesn't directly echo user_ids from prior queries. |

---

## Eval discipline — DO THIS BEFORE FLIPPING DEFAULT

Same pattern as the reranker rollout (which got promoted to default after +10pp on hits@1):

1. Build a baseline run on `scripts/eval/jarvis_eval_v1.jsonl` (50 hand-curated queries) with grounding OFF — already have this baseline
2. Build a comparison run with grounding ON
3. Measure:
   - % of questions correctly short-circuited (Layer 1 routes that match human-graded ground truth)
   - Cost-per-query delta
   - hits@1 / hits@3 / MRR — must be unchanged or better (grounding should never hurt quality)
   - Median latency delta
4. Only flip default after both:
   - hits@k does not regress
   - meaningful cost OR latency improvement (≥ 20%)
5. Ship gated behind `JARVIS_GROUNDING=on|off` env var initially; flip to on by default after one week of shadow-mode comparison in production

---

## Decision points pending (need Rohit's call when work resumes)

1. **Layer 1 alone or all three?** Layer 1 is the highest-ROI piece and ships in 2 days. Layers 2 + 3 are additive. Could ship Layer 1 first and evaluate before committing to 2 + 3.
2. **Memory-indexing scope.** Index `~/.claude/.../memory/` only (operator's personal memory) or also include team-level docs (CLAUDE.md, design docs in `docs/`)? Operator memory has user-specific context that may not apply to other engineers' queries.
3. **`prior_match` user-scoping.** Should cached-answer lookups only consult prior questions from the SAME asker, or from any engineer? Same-asker is safer (no info leakage) but loses cross-team value.
4. **Failure mode for `prior_match`.** When stale-detection rejects a cached answer, do we (a) silently fall through, (b) tell the user "I found a similar question from <date> but it's gone stale — re-investigating", or (c) something else? Transparency is good but adds latency framing.

---

## Status

- 2026-06-24 — design doc written
- 2026-06-25 — Layer 1a shipped (commit `cb57145`): question router with 3 fast paths (meta_capabilities / lookup_service / lookup_symbol) + general fall-through. Smokes 9/9 on classification, end-to-end fast paths 0 iters / 1-2s, general 4 iters / 55s (no regression). Audit: `~/jarvis/logs/question_router.jsonl`. Bypass via `JARVIS_DISABLE_QUESTION_ROUTER=1`.
- 2026-06-25 — Layer 1b shipped (commit `51c9b43`): prior_match same-asker semantic match over qa_log (cosine ≥ 0.85 via voyage-code-3 embeddings, lookback 30d, Layer-3-lite freshness via cited-file-exists check). Audit: `~/jarvis/logs/prior_match.jsonl`. Embedding cache: `~/jarvis/state/qa_embeddings.jsonl` (durable). Bypass: `JARVIS_DISABLE_PRIOR_MATCH=1`. Threaded caller_id through ask() + 3 slackbot call sites + /api/v1/ask.
- 2026-06-25 — Layer 2 shipped: grounding_context prelude for general-route questions. Top-3 related prior questions (same asker, cosine ≥ 0.65) + recent commits on files mentioned in the question. Prepended to the question before Sonnet sees it. Per-question fresh input (not cached). Audit: `~/jarvis/logs/grounding_context.jsonl`. Bypass: `JARVIS_DISABLE_GROUNDING_CONTEXT=1`. Smoke validated: file-cited Q got 461-byte prelude with real recent commit by Rutvik on rewardsAbuseRules.yml; agent went straight to read_file (skipped exploratory search_code). Skipped operator memory by design — out of scope for engineer-facing context.
- 2026-06-25 — Layer 3 shipped: scripts/agent/freshness_check.py with comprehensive stale-detection (files + services + symbols). Cheap-first early-exit: file-exists → service-in-registry → symbol-declared. PR-state check skipped intentionally (low ROI, high latency). prior_match._freshness_check delegates to this module with a fallback to the original file-only check on import failure. Smokes 4/4 (caught a real qa_log answer that cited non-existent gradle files). All 3 layers of the grounding plan now live.

---

## Why this design (vs alternatives that were considered)

- **Why not a full Q&A cache (CDN-style)?** Hit rate would be modest — engineers rarely ask the EXACT same question. Semantic-similar matching (Layer 1's `prior_match` + Layer 2's prelude) covers the same value with less infrastructure.
- **Why not just expand the SYSTEM_PROMPT rule for `get_capabilities()` to also cover memory / qa_log?** The current rule depends on Sonnet *deciding* to call the meta-tools. We've seen it skip them when the question doesn't *look* self-referential. A pre-router FORCES the fast path before Sonnet has a chance to skip it.
- **Why Haiku, not Sonnet, for the router?** Cost and latency. Router decisions are simple classification (5 buckets); Sonnet would be overkill at 10× the price. We already use Haiku for similar pre-flight work (`brief_gate.py`, `thread_to_fix.py`, `investigate_intent.py`).
- **Why not eager auto-route on regex / keyword matching?** Tried implicitly via SYSTEM_PROMPT nudges already; engineers phrase questions in unexpected ways. Haiku handles paraphrase naturally; regex doesn't.
