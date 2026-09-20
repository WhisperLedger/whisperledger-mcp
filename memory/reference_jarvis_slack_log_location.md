---
name: jarvis-slack logs to /home/ubuntu/jarvis/logs/slackbot.log, NOT systemd journal
description: The jarvis-slack systemd unit redirects StandardOutput + StandardError to a file (slackbot.log), so `journalctl -u jarvis-slack` only shows systemd lifecycle messages — NEVER the Python app logs. To debug the bot, tail slackbot.log directly.
type: reference
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---

**Symptom:** `journalctl -u jarvis-slack --since "5 min ago"` returns `-- No entries --` even though the bot is actively serving traffic.

**Cause:** The unit file at `/etc/systemd/system/jarvis-slack.service` has:
```
StandardOutput=append:/home/ubuntu/jarvis/logs/slackbot.log
StandardError=append:/home/ubuntu/jarvis/logs/slackbot.log
```
This sends all Python `logger.info(...)` / stdout / stderr output to that file. journalctl only sees systemd's own lifecycle messages (start/stop/reload).

**Where to actually look:**
- Live tail: `ssh ubuntu@3.6.202.121 'tail -f /home/ubuntu/jarvis/logs/slackbot.log'`
- Last N: `ssh ubuntu@3.6.202.121 'tail -200 /home/ubuntu/jarvis/logs/slackbot.log'`
- Filter for events: `... | grep -E "handle_dm|mcp-onboarding|handle_jarvis"`

**Same pattern likely applies to:** other Jarvis Python services that use the bash-wrapper pattern (per `feedback_systemd_env_pattern.md`). Always `cat` the unit file first to confirm where logs go before assuming journalctl is the source of truth.

**Established:** 2026-05-21 while debugging "is the MCP onboarding DM handler firing?". Wasted ~15 min tailing journalctl that was always going to be empty.
