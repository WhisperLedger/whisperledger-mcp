---
name: Default to permanent root-cause fix, not interim workaround
description: When proposing fixes (in code, in PR descriptions, in DMs to engineers), default to the architectural / root-cause fix. Only suggest an interim workaround if Rohit explicitly asks for one.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
Rule: When proposing how to fix a bug — whether in Jarvis-generated code, in agent prompts, in PR review responses, or in DMs to engineers — **default to the permanent logical fix**. Do NOT offer a call-site patch / hotfix / workaround unless Rohit explicitly asks for an interim fix.

**Why:** Established 2026-05-19 after the RECO-1259 + RECO-133 PR sequence. Both PRs (and my own DM drafts to Chirag + Prasanna) defaulted to call-site workarounds:
- PR #14141 (RECO-133): Jarvis replaced sense-ui `Button` with a custom Pressable + Text + explicit `lineHeight`. Real fix is in `@jupitermoney/sense-ui`'s Button tertiary variant; every other Button-with-descender call-site still has the bug.
- PR #14140 (RECO-1259): Jarvis attempted snapPoints-equality patches both times. The architectural fix (per the screenshot-grounded diagnosis) is to stop mutating snapPoints between renders — but the attempts both kept the mutating-snapPoints pattern and tried to band-aid around it.
- My DM draft to Chirag suggested "take it as a hotfix and file the sense-ui ticket as follow-up" — Rohit flagged this framing as wrong. The default should be "do the sense-ui fix now"; hotfix is only OK if explicitly asked for.

**How to apply:**
- When Jarvis-generated code lands at a call-site workaround instead of the architecturally-correct location, flag this in the PR body (not as caveat to defend it, but as "this is a workaround; the right fix is in <library>") AND propose the upstream fix as the primary recommendation.
- When drafting agent prompts for `/api/v1/fix` or `/api/v1/pr/iterate`, instruct the agent to prefer the architectural fix even if it means a larger diff or a cross-repo PR. The bash wrapper's existing "Prefer single-file fixes" guidance is wrong as a default — it biases toward workarounds. Should be "Prefer the fix at the correct architectural layer, even if it requires touching multiple files or repos."
- When responding to reviewer comments via DM, never default to "this is a hotfix, the real fix can come later." Default to "the real fix is X; here's how we do it."
- Exception: if Rohit (or the explicit task description) asks for an interim because something is on fire, ship the interim AND name the followup ticket required for the permanent fix.
