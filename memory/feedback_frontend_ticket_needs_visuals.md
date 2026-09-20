---
name: Frontend ticket without images/videos — flag before firing iterate/fix
description: If a Jira/bug ticket is frontend-related and has no attached images or videos, FLAG that to the user before invoking iterate or fix. Established 2026-05-19 after $9 burn on PR #14140 from text-only iterations.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
**Rule:** Before firing `/api/v1/fix` or `/api/v1/pr/iterate` on a frontend bug, check the source ticket for visual attachments (images, screen recordings). If a frontend ticket has NO images or videos, STOP and flag this to the user before proceeding. Ask whether to fire anyway, wait for visuals, or hand off entirely.

**Why:** On 2026-05-19, PR #14140 (RECO-1259 Contact Bottom Sheet collapses on text input) consumed 3 failed iterations (~$7) on text-only reasoning before image+video ingestion unlocked the actual root cause. The first three Jarvis attempts were *confidently wrong* — wrote a regression test guarding the wrong invariant, shipped fixes that didn't address the bug, frustrated the human reviewer. The breakthrough came in v4 *only* because we attached Prasanna's screenshot + the original Jira screen recording.

Static code analysis is structurally insufficient for visual/transition bugs. The bug lives in *what the user sees during a state change*, not in the steady-state code. Code-only reasoning will produce plausible-sounding fixes that target the wrong layer.

**How to apply:**
- For any `/api/v1/fix` invocation tied to a frontend bug ticket: check the ticket body + attachments first.
- If no images/videos AND the description is symptom-only (no exact repro steps with expected vs. actual values): surface this BEFORE firing. Example to user: *"This Jira ticket has no screenshots or screen recording. Frontend bugs without visual evidence have a high failure rate on first iteration — recommend asking the reporter for a screen recording before firing. Fire anyway, or wait?"*
- For `/api/v1/pr/iterate` on a review-comment loop: the multimodal ingestion in `jarvis_iterate.sh` auto-extracts attachments from PR comments + Jira (if the PR description has a Jira ticket link). If the PR/comments still have no visuals, same flag applies.
- "Frontend" includes: React Native screens, web pages, any UI component, layout/styling issues, animation/transition bugs, gesture/keyboard interaction.
- Backend bugs (API contract, data shape, business logic) are unaffected by this rule — code-only reasoning works there.

**Cost calibration:** A text-only iteration averages ~$1.50. An iteration with image+video is ~$0.75 (smaller, faster because it's better-targeted). Three failed text-only attempts ≈ four good multimodal attempts. The flag is essentially free; ignoring it has burned real money.

## When `JARVIS_ALLOW_FE_NO_VISUALS=1` override IS appropriate

The Guard A check (`fe_no_visuals` refusal) catches frontend tickets without visuals. Override is appropriate when the reviewer's ask is **textual, specific, and architectural** — naming the exact component to use, the exact file to touch, the exact prop pattern to follow. Override is NOT appropriate when the reviewer says "the UI looks broken" or "fix this glitch" without specifics.

**Override-OK pattern:** "Wrap `<X>` in `PageWithoutScroll` from sense-ui, set Stack.tsx options to `fullscreen`, use Page's `title` + `leadingIcon` props." → architectural refactor, no visual debugging required, code-only reasoning works. Override is the right call. *Example: jupiter#14155 (Pratap, 2026-06-03). $0.68, landed clean.*

**Override-NOT-OK pattern:** "the button looks weird on Pixel 6, fix it" or "this animation is janky, please fix." → visual debugging needed. Get screenshots/video first.

The override exists for the first pattern. The Guard is conservative on purpose; humans (or callers verifying like above) decide when to override.
