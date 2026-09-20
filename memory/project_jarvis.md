---
name: Jarvis for Jupiter — project overview
description: Internal AI engineer for Jupiter that indexes 150+ GitHub repos + Confluence and can answer questions, fix/debug code, review PRs, and build features
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
**What:** "Jarvis for Jupiter" — an AI-powered engineer with access to all of Jupiter's GitHub repos (150+) and Confluence docs. Capabilities: answer tech-stack questions, fix code, debug, perform code reviews, build new features.

**Why:** Internal developer productivity / institutional knowledge unlock across a large multi-repo estate. Owned/initiated by Rohit.

**How to apply:**
- Treat scope as ambitious but realistic — bias toward orchestrating existing agent infra (Claude Agent SDK / Claude Code) rather than building from scratch.
- Hardest problems are the **retrieval layer across 150+ repos + Confluence** (freshness, cross-repo context) and **safe write-access** (PR creation, reviews, branch hygiene at org scale) — these are where Jupiter-specific engineering effort should go.
- Working directory: `/Users/rohitpandey/Projects/jarvis` (currently empty / pre-init, not a git repo as of 2026-05-11).
- **GitHub org:** `jupitermoney` (Rohit's GH user: `rkp2024`). All repos live there. `gh` is authed on the remote box (`ubuntu@3.6.202.121`) with scopes `repo, read:org, gist, workflow`.
- **Remote workspace:** `~/jarvis/` on the box, with subdirs `repos/ index/ logs/ scripts/ docs/ evals/`.
- **Tooling installed on remote:** git, gh 2.92, jq, ripgrep, fd-find (binary `fdfind`), python 3.12, node 20, docker, ollama.
- **Phase 0 repo slate (6 repos, locked 2026-05-11):** `platform` (Kotlin, multi-service core), `bff-core` (TS BFF), `lms` (Kotlin lending), `jupiter` (RN mobile app, 596MB — stress test), `prod.jupiter.money` (HCL infra — needs secrets scrubbing), `gateway` (contains Stargate + Bifrost edge services).
- **Indexed repos as of 2026-05-12 (17 total, 38,701 chunks):**
  - Phase 0 (5 in index, prod.jupiter.money deferred): `bff-core, platform, lms, gateway, jupiter`
  - Tier 1 (5): `bullet, lending-orchestrator, cardboard, brahma, metal`
  - Tier 2 (7): `mf-order-xpress, mf-explore-service, insurance-platform, bills, ppi-rail, ppi-pots, ppi-router`
- **Service still untraced to a repo (from Stargate spec analysis):** `rewardsService` (50 routes), `csOrchestratorService` (39), `productMarketingService` (33), `investmentDepositService` (35), `investmentExploreService` (32), `podsService` (30), `dcmsFederalService` (24), `giftCardService` (21), `mfOrderSpringService` (20), `creditCompassService` (19), `mfOnboardingService` (18). All show `(no obvious repo)` from name fuzzy-match — they likely live as sub-modules inside other repos or use codenames. Worth a manual pass with Rohit when expanding further.
