---
name: Slack technology-team channel — C092S7Z5HB5
description: Rohit calls the broad-announcement Slack channel the "technology-team channel". The channel ID is C092S7Z5HB5 — same channel CLAUDE.md lists as the Jarvis pilot channel. Use this for top-level engineering-wide announcements (rollouts, betas, how-to posts).
type: reference
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---

**Channel:** `C092S7Z5HB5`
**Rohit's name for it:** "technology-team channel"
**Same channel as:** the Jarvis pilot channel in CLAUDE.md ("Pilot channel: C092S7Z5HB5 (engineering channel)")
**URL:** https://jupitermoney.enterprise.slack.com/archives/C092S7Z5HB5

**When to use:**
- Top-level engineering-wide announcements (rollouts, betas, feature launches, how-tos)
- Per the channel-visibility rule: usage / how-to / can-and-can't framings go here as TOP-LEVEL posts, not threaded replies
- Jarvis bot is already a member, so `chat.postMessage(channel="C092S7Z5HB5", ...)` works directly

**Note:** the channel is NOT literally named `technology-team` in Slack — `conversations.list` won't surface it with that needle. Always resolve via this saved ID, not a name search.

**Bot account posting discipline:**
- Per `feedback_slack_permission.md` — never send any message here without showing Rohit the draft and getting per-message approval
- Per `feedback_announce_style.md` — credit only direct validators, not adjacent contributors
- For broad-audience posts (e.g. across all 85+ engineers), add brief explainers of any jargon (MCP, etc.) — not every reader is familiar with internal Jarvis terminology
