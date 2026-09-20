---
name: Exit iterate loop after 3 failed attempts — debug manually instead
description: When Jarvis iterate has failed 3+ times on the same PR/bug, do NOT fire iterate v(N+1). Switch modes — read the file yourself, trace the logic, push a manual commit. Established 2026-05-19.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
**Rule:** After 3 consecutive iterate attempts on the same PR fail to address the actual bug, STOP firing iterate. Read the file yourself, trace the logic, form a real hypothesis with line references, and either (a) push a manual commit on the PR branch or (b) hand over to the human with the diagnosis. Do NOT fire iterate v4+ unless the input has materially changed (e.g., new visual evidence, new ticket info, new reviewer comment with a specific code pointer).

**Why:** On 2026-05-19, PR #14140 went through 5 iterations. v1–v3 were confidently wrong text-only attempts. v4 (with image + video) found the right root cause and Prasanna confirmed it worked. Prasanna then flagged a NEW follow-up bug. I almost fired iterate v5 scoped to the follow-up — Rohit corrected: *"Don't just run another iteration."* Manual code-read + diagnosis took ~10 minutes, found the actual cause (React Navigation keeps screen mounted → BottomSheet retains snap position across visits), and produced a 10-line `useFocusEffect` fix on the first try. v5 (which did run before the correction) cost $0.74 and added a redundant, off-target change.

An agent stuck in a speculation loop does not get unstuck by another iteration. It gets unstuck by either (a) more information (visuals, runtime data, repro steps) or (b) a human engaging with the actual code. Firing iterate N+1 when neither has changed is throwing dice with money attached.

**How to apply:**
- Track iteration count per PR. After failure #3 on the same logical issue, switch to manual debug mode.
- Manual debug mode: clone or read the workspace, open the relevant file, trace the code with line references, form a hypothesis grounded in specific lines, and present the diagnosis to the user with a proposed patch.
- If you'd push a commit: show the diff + commit message first, get explicit `push` approval (per the per-action approval rules), then commit and push to the same PR branch (NEVER force-push).
- If the diagnosis is uncertain: hand over with a written diagnosis so the human can start from a real hypothesis, not a blank file.
- The exception: if iterate v(N+1) would have **substantially different inputs** (e.g., a reviewer just left a comment that names a specific file:line as the cause, or a new screen recording was attached), then re-firing iterate is legitimate — the input changed.

**How to recognize the loop:** signs that iterate is stuck in speculation rather than converging:
- Each new commit "fixes" something different than the prior commit (target keeps moving)
- The agent invents a NEW root-cause theory each iteration rather than refining one
- The reviewer keeps saying "doesn't fix the bug" with the same wording
- The agent adds redundant defensive code on top of prior attempts (belt-and-suspenders without removing the suspenders)

When you see those signals: stop, read, diagnose. Don't fire.
