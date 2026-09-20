---
name: Cross-repo companion-PR flow shipped 2026-05-19
description: ITERATE_COMPANION_PR=1 lets iterate defer to an upstream-repo draft PR when the architectural fix is in a library/design-system repo. Phase 2 shipped + validated end-to-end on RECO-133.
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---

**Status:** Shipped 2026-05-19, validated on the first real call (PR #14141 RECO-133 descender clipping).

**What it does:** When `ITERATE_COMPANION_PR=1` is set, iterate's Claude prompt is amended to instruct: if the architectural root cause is in an upstream/library repo (not the call-site), write `$WORKSPACE/companion_diagnosis.md` and output `JARVIS_NEEDS_COMPANION=<upstream-repo>` instead of committing to the source PR. The wrapper then spawns `scripts/jarvis_companion_pr.sh` which clones the upstream repo, runs a second Claude run to write the architectural fix, opens a **draft PR** in that repo, and (if `COMPANION_CROSSLINK_COMMENTS=1`) posts cross-link comments on both PRs.

**Files:**
- NEW: `scripts/jarvis_companion_pr.sh` (~285 lines) — standalone companion-PR runner. Hard cap $4 budget. Allowlist-gated on `JARVIS_WRITE_ALLOWED_REPOS`. Cross-link comments gated behind `COMPANION_CROSSLINK_COMMENTS=1` (default OFF, so smoke tests don't post visible comments).
- MODIFIED: `scripts/jarvis_iterate.sh` — adds the `ITERATE_COMPANION_PR=1` hook + prompt amendment + spawn-on-detect logic.
- ALLOWLIST: `jupiter-design-system` added to `JARVIS_WRITE_ALLOWED_REPOS` in `~/.config/jarvis/env`. Services restarted to pick it up.
- DOCS: `CLAUDE.md` updated with the new env var + Phase 2 reference.

**Validation run (the only one as of 2026-05-19 13:38 UTC):**
- Source PR: jupiter#14141 (RECO-133 Apply descender clipping, Chirag's review)
- Trigger env: `JARVIS_FORCE_ITERATE=1 JARVIS_ALLOW_FE_NO_VISUALS=1 ITERATE_COMPANION_PR=1`
- Iterate side: 240s, $0.72 — produced diagnosis + `JARVIS_NEEDS_COMPANION=jupiter-design-system`
- Companion side: 159s, $0.48 — opened jupiter-design-system#621 (draft, 2 files, +6/-2 + +1/-1)
- Total: $1.20, ~7 min, no new commit on #14141
- Cross-link comments posted (after user approval) on both PRs

**Key design choices:**
- Opt-in only (`ITERATE_COMPANION_PR=1`), no default-fire to avoid surprise design-system PRs.
- Cross-link comments gated separately (`COMPANION_CROSSLINK_COMMENTS=1`) so smoke runs are visible only to the operator.
- Companion PRs ALWAYS open as draft — design-system team owns ready-for-review + merge.
- PR body explicitly tagged "auto-drafted by Jarvis, design-system team controls merge."
- Companion Claude has a "STOP if you can't write a clean fix in ≤3 attempts" instruction to avoid bad PRs.

**Known limitations (revisit if needed):**
- Companion run is text-only (no multimodal). For descender clipping the diagnosis from iterate was rich enough; revisit if a future case needs the upstream Claude to see screenshots too.
- Companion PR author is whoever the box's `gh auth token` belongs to (currently `rkp2024`), not a dedicated `jarvis-bot` GH account — same identity-placeholder issue as the rest of Jarvis pushes. Task #48 covers this long-term.
- Iterate's commit-count cap (Guard B) doesn't currently count companion PRs as "iterations" — that's correct (different repo, different fix).
- No retry / no idempotency on the companion side. If the companion Claude fails halfway, the source-side iterate exits with `JARVIS_ITERATE_FAILED=companion_spawn_failed`; caller can re-fire.

**When to fire:** any time a reviewer explicitly asks for an upstream PR ("please fix this in <library-repo>"), or when iterate's diagnosis identifies an architectural fix outside the call-site repo. Don't fire blindly.

**Companion script audit log:** `~/jarvis/logs/companion_audit.jsonl`.
