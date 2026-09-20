---
name: Never post-then-update Slack messages; finalize format first
description: When iterating on a daily/recurring Slack message format, post the FINAL version first — never post a draft and then chat.update it, and never enable a systemd timer with Persistent=true on Slack-posting units.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
Rule: when shipping a Slack message (one-shot or recurring), finalize the format BEFORE the first post. Don't post a v1 then `chat.update` to v2 mid-iteration.

**Why:** 2026-06-02 open-PR digest first-run double-posted to #technology-team. Sequence: posted v1 (plain text) → deployed Block Kit code → ran `chat.update` to refresh in place. Result: two messages in the channel (one plain-text, one Block Kit) — rather than the one in-place-updated message I expected. The audit log only captured one. Slack quirk, systemd `Persistent=true` catch-up, or my own confusion — couldn't fully diagnose.

**How to apply:**
- Iterate format in `--dry-run` until it's the version you want to ship. Get Rohit's "great" before any `--post`.
- Always confirm the rendered output in the actual destination before automating.
- On systemd timers that post to user-visible Slack channels: set `Persistent=false`. Skipping a missed run is cheaper than risking a double-post.
- If you DO need to fix a posted message, prefer `chat.delete` of the wrong one + a single fresh `chat.postMessage`, not an `chat.update` that leaves both versions floating.
- Per-message audit logging is necessary but not sufficient — Slack can have messages your script didn't create (Persistent catch-up, OnFailure notifications, edits). Always check `conversations.history` to verify channel state before assuming.
