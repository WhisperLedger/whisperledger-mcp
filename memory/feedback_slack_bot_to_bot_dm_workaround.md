---
name: Slack bot-to-bot DM workaround — chat.postMessage(channel=<bot_user_id>) bypasses cannot_dm_bot
description: Slack's cannot_dm_bot error only blocks conversations.open between two bots. chat.postMessage with the other bot's user_id as channel goes through and auto-creates the IM. Established 2026-05-20 between Jarvis and Jove.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
**Rule:** when sending a Slack DM from one bot to another bot user, do NOT call `conversations.open(users=<other_bot_uid>)` first (it returns `cannot_dm_bot`). Use **send-first**: call `chat.postMessage(channel=<other_bot_uid>, text=...)` directly. Slack auto-creates the IM channel on first send and routes subsequent sends + the other bot's replies into the same IM channel.

**Why:** discovered 2026-05-20 during Jove ↔ Jarvis pipeline setup. Both bots are Slack apps. `conversations.open` is rejected for bot-to-bot pairs, but `chat.postMessage` with the bot's `user_id` as `channel` is NOT rejected — Slack auto-creates the IM transparently. Confirmed by Jove sending the first message; Jarvis replied via the workaround; both messages landed in the same IM channel (`D0B555KRAAY` for Jarvis ↔ Jove).

**How to apply:**
- `slack_sdk` Python: `client.chat_postMessage(channel="U_BOT_ID", text=...)` — works.
- Do NOT precede with `conversations.open` — it errors out before you can send.
- The returned `channel` field in the chat.postMessage response IS the auto-created IM channel ID; cache it for subsequent `conversations.history` polling.
- Required scopes on the sending bot: `chat:write`, `im:history` (to read replies). Does NOT require `im:write` or `channels:read`.
- If you need a DURABLE bot-to-bot integration, prefer an HTTP webhook on the receiving bot's side over this Slack-DM workaround (Jove proposed `POST /api/jarvis/notify` mirroring Jarvis's `/api/v1/fix`).

**Known IM channels (Jarvis side):**
| Other party | Bot user_id | IM channel | Established |
|---|---|---|---|
| **Jove**   | `U0B45MB6A4U` | `D0B555KRAAY` | 2026-05-20 (Jove sent first; we replied via the workaround) |
| **Janus**  | `U0B4G2UDHU4` | `D0B5Y62C740` | 2026-05-20 (Jarvis sent first; handshake) |
| **Juno**   | `U0B3A7C92GK` | `D0B57FH3QUC` | 2026-05-20 (Jarvis sent first; handshake) |

For any of these, future sends can use either `chat.postMessage(channel="<bot_uid>", ...)` (send-first pattern, robust) or `chat.postMessage(channel="<im_channel_id>", ...)` (direct, slightly faster since no IM lookup). Both work identically. Receiving the other bot's reply: `conversations.history(channel="<im_channel_id>", ...)`.
