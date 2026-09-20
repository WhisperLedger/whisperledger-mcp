---
name: PR triage decision tree — iterate vs close vs nudge vs flip
description: When auditing Jarvis-fired PRs that need attention, use this decision tree to pick the cheapest right action. Established 2026-06-03 in a triage session that cleared 5 stuck PRs for ~$0.68 total.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
When `scripts/check_pending_prs.py` (or similar) flags a Jarvis-fired PR as needing action, walk through the decision tree below BEFORE acting. The point is to spend the absolute minimum cost (compute + human attention) to get each PR moving again — most PRs don't need an iterate.

**Always read the actual reviewer comment first** (via `scripts/read_pr_comments.py` or `gh api repos/.../pulls/N/reviews + /comments`). Don't trust the GitHub `reviewDecision` field alone — it's missing the WHY.

## The tree

**1. Reviewer says "wrong layer" / "this should be in a different repo" / "redo at BFF" →** *close + DM the reviewer*. Don't iterate at the wrong layer. Iterating burns money to produce a PR the reviewer will reject again.
*Example: jupiter#14166 (Akhil: "this is a BFF change, not app-side"). Closed, DM'd Akhil, no iterate spend.*

**2. Reviewer's ask requires evidence we don't have (device debug, runtime data, A/B results) →** *close*. Per `feedback_exit_iterate_loop_when_stuck.md`, static iterates can't solve runtime issues.
*Example: jupiter#14148 (Prasanna: "the loading-loop is in vkyc-inprogress, needs device debug"). Closed with honest acknowledgement.*

**3. Reviewer gives a specific, textual, architectural code change ("wrap X in Y from sense", "use the title prop from Page", "change Stack.tsx options to fullscreen") →** *iterate*. Even on frontend PRs without screenshots — see `feedback_frontend_ticket_needs_visuals.md` for the override criteria.
*Example: jupiter#14155 (Pratap: "Wrap it in PageWithoutScroll from sense-ui"). Iterated with `JARVIS_ALLOW_FE_NO_VISUALS=1`, $0.68, landed clean.*

**4. PR is APPROVED but still DRAFT, post-approval commits are JUST a merge from develop →** *flip to ready-for-review*. Approval is semantically valid; develop-syncs don't invalidate it. Single `gh pr ready` call, reversible.
*Example: jupiter#14156 (Akhil approved 05-26, single post-approval commit was a develop merge). Flipped, no friction.*

**5. PR is APPROVED but still DRAFT, post-approval commits change the actual subject →** *do NOT flip*. Approval is stale. Either DM the original reviewer to re-approve, or iterate to make the actual change visible.

**6. PR has no reviewer engagement for >7 days →** *nudge OR close*. Decide based on (a) is the PR still wanted, (b) is the named reviewer the right person, (c) is the codebase still in a state where the PR applies. Don't reflexively iterate — iterating doesn't get reviewers' attention.

**7. New regression flagged by QA on a previously-shipped fix →** *fresh `/jarvis fix`, NOT iterate on the original*. Why: original PR is likely APPROVED + merged or close to it; adding a commit can dismiss approval and force re-review. Separate concerns = separate PRs = easier review.
*Example: RECO-1259 follow-up — fixed in #14140, Eshwar flagged "empty-state flash" regression, opened fresh #14205 instead of iterating #14140. $0.68, clean diff, didn't touch the original snapPoints fix.*

## Per-action templates

- **Close:** `gh pr close <num> --repo <owner/repo> --comment "<honest one-paragraph why>"`. Always DM the reviewer(s) afterward — the GitHub comment alone is not enough; engineers don't get a notification for every PR close.
- **Iterate:** `POST /api/v1/pr/iterate` with `Idempotency-Key`. Read the latest comment FIRST, decide whether to add `JARVIS_ALLOW_FE_NO_VISUALS=1` (textual specific ask) or pull Jira attachments first (visual debug needed).
- **Flip ready:** `gh pr ready <num> --repo <owner/repo>`. Note: some repos dismiss approvals on draft→ready conversion — DM the approver to re-tick.
- **Fresh fix:** `POST /api/v1/fix` with a tight description naming the prior PR + the new regression. Reference the build name that QA verified on. Don't include the original fix's symptom — that already shipped.

## Why this works

The cheapest action that gets the PR moving is the right one. A $1.50 iterate that the reviewer rejects again is worse than a $0 close with a clean honest reason. A $0 flip-to-ready is better than a $1.50 iterate that risks dismissing approval. Read first, decide second, act third.
