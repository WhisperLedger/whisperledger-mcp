---
name: Alert routing default — never public channel
description: System alerts (failures, threshold crossings) should default to a DM to the operator (Rohit), never to a public channel. Public-by-default = noise + privacy issues + "did you see X" confusion.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
When configuring auto-alerts (systemd OnFailure hooks, threshold crossings, credit failures, etc.), the *default* destination should be a DM to the human operator, NOT a public channel. The notify script should require an explicit `JARVIS_ALERTS_CHANNEL` env var; if missing, fail loudly rather than fall back to a public channel.

**Why:** Rohit caught this on 2026-05-15. The notify script defaulted to `C092S7Z5HB5` (the public Jarvis channel) when `JARVIS_ALERTS_CHANNEL` was unset. A crash-loop of `jarvis-api` produced 115 alert messages visible to the entire team in ~3 hours. Embarrassing + privacy-concerning (alerts can leak service names + log snippets).

**How to apply:**
- Default alert destination = operator DM (currently Rohit's `D0B3V4XQR97`)
- If anyone wants alerts in a shared channel (e.g., on-call rotation #incidents), require explicit env-var configuration; never default
- Same principle for any new alert/notification channel I build: spend monitor, credit failure detector, etc. — DM the operator, not the channel
- If I'm tempted to default to a channel "for visibility," ask first

The cost-monitor I shipped yesterday already DMs Rohit directly (correctly). The systemd notify script was the outlier; now also fixed (`JARVIS_ALERTS_CHANNEL` set to `D0B3V4XQR97` in env).
