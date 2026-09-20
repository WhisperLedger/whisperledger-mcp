---
name: Jove ticket-prefix routing — verify the target repo, not just the ticket key
description: Discovered 2026-06-06 that Jove fired PLZ-* tickets against `jupiter`, but the PLZ project belongs to `jupiter-web-ob`. Two PRs (jupiter#14248, jupiter#14251) had to be closed after Prasanna flagged it. Rule: before opening a fix-mode PR for a Jira ticket, check the ticket's project → repo mapping, not just the prefix.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
**Rule:** Jira ticket prefix → repo is NOT one-to-one. Before firing `/api/v1/fix` for ticket `X-N`, verify the ticket's actual product owner / target repo. Prefixes can be ambiguous, and the project's owning team determines the target repo, not the prefix string.

**Why:** 2026-06-06, Jove fired 20 PRs from filter-19028. Two of them targeted `jupitermoney/jupiter` for tickets `PLZ-510` and `PLZ-514` (both UI bugs). Prasanna's review on both said the same thing verbatim: *"This issue raised is for the jupiter-web-ob project. The PR shouldn't be raised in this repository. Also the issue raised is already being fixed by devs."*

Both PRs had to be closed (jupiter#14248, jupiter#14251). Wasted ~$3 of compute + the reviewer's time.

**How to apply:**
- When wiring a new Jira project into Jove (or Jarvis's fix-mode), capture an explicit `project → repo` mapping. The PLZ project maps to `jupiter-web-ob`, not `jupiter`.
- For prefixes that genuinely span multiple repos (e.g. CCZ tickets that sometimes target `jupiter` and sometimes `bullet`), use the ticket's `components` or `labels` field to disambiguate before firing.
- If unsure, refuse with `JARVIS_FIX_REFUSED=ambiguous_target_repo` rather than guessing. A clean refusal is cheaper than a closed PR.
- Mithun-style cross-repo preflight (`/api/v1/preflight`) could catch this kind of misroute pre-PR — adding a "is this ticket's project a known fit for this repo?" check would be a small but high-value addition.

**Known prefix → repo mappings (extend as discovered):**
- `PLZ-*` → `jupiter-web-ob` (NOT `jupiter`)
- `CCZ-*`, `BO-*`, `RECO-*`, `FP-*`, `INV-*` → `jupiter` (the mobile app, primary surface)
- `INSTECH-*` → unclear; needs check
- `CSCX-*` → unclear; needs check
- `RECO-*` with infra/data flavor → can target `growth` or `jupiter-investments`

**Source incident:** jupiter#14248, jupiter#14251 — closed 2026-06-06 with comment crediting Prasanna's call. Followed up with a DM acknowledging. Listed as a known gap in Jove's repo routing for Rohit to patch on the Jove side.
