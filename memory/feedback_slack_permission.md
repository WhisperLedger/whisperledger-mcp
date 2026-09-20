---
name: Slack messages require explicit per-message permission
description: Never send any Slack message (channel post, DM, edit, thread reply, reaction) without Rohit's prior approval for that specific message
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
Rule: Before sending **any** Slack message via the bot — including channel posts, DMs, edits to existing messages, thread replies, and reactions — first show Rohit the exact draft and channel/recipient, and wait for explicit approval. Do not assume that a prior approval extends to the next message.

**Why:** Rohit set this rule after the 2026-05-15 EOD wrap, where I posted to the engineering channel with two stale Slack IDs (mentioned `U0B25LA61RF` instead of Malyala B's `U018HBA4M1T`, and `U09SAYYJESP` instead of Priyanshu Srivastava's `U082YJUCLCA`). Although the wrong IDs happened to be invalid users so no humans got misnotified, the visual @-mentions in a public channel were still embarrassing and required a follow-up correction. Earlier the same week, the alert-spam incident (~115 alerts to public channel after I forgot to restart jarvis-api) had already burned trust on autonomous Slack sending. Pattern: messages that go out under the Jarvis bot's identity are *visible to Jupiter engineers* and reflect on Rohit's initiative — the cost of a mistake is high, the cost of waiting ~30s for approval is near zero.

**How to apply:**
- Before any `chat_postMessage`, `chat_update`, `chat_postEphemeral`, `conversations_open` + post, or `reactions_add` from a script I'm about to run, show Rohit: (1) the channel ID or recipient name + Slack ID, (2) the full text including all @-mentions resolved to real names, (3) whether it's top-level / thread / edit. Then wait for "ok" / "send" / "yes" / similar explicit go-ahead.
- This applies even when Rohit's preceding message *asks* me to send something — draft first, confirm second, send third. ("Send X to Y" is a request to compose and propose, not to fire-and-forget.)
- Exception: if Rohit explicitly says "send directly" or "no need to confirm this one", that specific message is pre-approved — but the next one resets to needing approval.
- For mention-heavy messages, always verify every Slack ID against `users_info` before drafting, so the draft Rohit sees is accurate.
- Does NOT apply to: reading Slack (channel history, user info, message lookups) — those stay autonomous.
