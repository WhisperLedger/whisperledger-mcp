---
name: Jarvis architecture decisions (locked 2026-05-11)
description: Phase 0 stack choices for Jarvis — embeddings, vector store, LLM brain. Locked after evaluation; revisit only with reason.
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
**Embedding model:** **Voyage AI `voyage-code-3`**.
- Why: best retrieval quality on code benchmarks; purpose-built for code.
- Cost: ~$0.18/1M tokens. One-time index of 6 Phase 0 repos ≈ $5–15.
- Needs: `VOYAGE_API_KEY` env var on the remote box.

**Vector store:** **Qdrant via Docker**.
- Why: easiest single-container setup; strong metadata filtering (repo, language, path) for routing queries.
- Lives at `~/jarvis/index/qdrant_storage/` on remote, exposed on `:6333` (HTTP) and `:6334` (gRPC). Localhost-only — do NOT expose publicly.

**LLM brain:** **Anthropic API with smart routing.**
- **Sonnet 4.6** — default for ~95% of work (Q&A, small fixes, reviews). Best price/quality on agentic code.
- **Haiku 4.5** — triage/classification/routing (cheap, fast).
- **Opus 4.7** — escalation for hardest 2% (multi-file feature build, gnarly debugging).
- Why: best code reasoning today; native prompt caching cuts repeated-context cost ~90%; pairs with Claude Agent SDK orchestration.
- Needs: `ANTHROPIC_API_KEY` env var on the remote box.

**Cost target:** ~$50–130/day at full adoption (1,500 queries/day across ~150 devs). Phase 0 pilot ≈ $5–20/day.

**How to apply:**
- Don't substitute models without telling Rohit — these were deliberately chosen.
- Always use prompt caching for system prompt + tool definitions.
- API keys live in `~/.config/jarvis/env` (chmod 600), sourced by all Jarvis processes — not committed, not in shell rc.

**Phase 0 simplification (2026-05-12):** Started with **single Sonnet 4.6 agent + 3 tools** (search_code, read_file, list_repo_files), NOT the Haiku-triage-then-Sonnet routing originally planned. Rationale: with prompt caching + good tool descriptions, a single Sonnet agent handles the bread-and-butter Q&A cleanly; adding a router introduces complexity without measured benefit. Add Haiku triage / sub-agents later only if real usage shows latency or cost issues.
