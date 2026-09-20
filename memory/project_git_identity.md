---
name: Git author identity on Jarvis box (placeholder; swap when bot account lands)
description: Box-side git is configured as "Jarvis Bot <jarvis-bot@jupiter.money>" — placeholder; swap to the dedicated jarvis-bot GitHub account's noreply email once task #48 ships
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
The remote box `ubuntu@3.6.202.121` has global git config:
- `user.name`  = `Jarvis Bot`
- `user.email` = `jarvis-bot@jupiter.money`

Set on 2026-05-16 after discovering all of week-1 development was uncommitted (zero-commit `~/jarvis/.git`). Two recovery commits landed before this config was set, so they still show the unset-author defaults (`ubuntu@<hostname>`): `e499d23` (full week-1 sync) and `60b8b00` (gitignore tightening). Not worth force-pushing to fix; future commits are properly authored.

**Why this is a placeholder:** `jarvis-bot@jupiter.money` is not a real mailbox and there is no GitHub account behind it. Commits land on `jupitermoney/jarvis` as unverified author. Rohit acknowledged this as acceptable short-term.

**How to apply (future-me trigger):** When task #48 ("Long-term: dedicated jarvis-bot GitHub account") ships and a real bot account exists, immediately swap the box's git identity to that account's GitHub-issued `<ID>+jarvis-bot@users.noreply.github.com` email. That makes commits show as a verified bot author in the GitHub UI and avoids the "unverified placeholder" warning. The `user.name` can stay "Jarvis Bot".

Also rotate the GH push token from Rohit's personal token to the bot account's token at the same time.
