---
name: Deploy discipline — restart ALL services that load a changed module
description: When changing imports or shared modules, restart every service that imports them. py_compile + module-level smoke test ≠ proof a running service picked up the change.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
When editing any file that's imported by multiple services (e.g., `agent/investigate.py` imported by both `api/server.py` and `slackbot/app.py`), *restart every service that imports it* as part of the deploy. Don't trust `py_compile` + a Python-CLI smoke test as proof — those validate the module loads in isolation, not that the running service successfully reloaded.

**Why:** Rohit caught this on 2026-05-15. I shipped `investigate.py` with `from .agent import ask, AgentResult` (AgentResult didn't exist). My CLI smoke test failed → I edited `investigate.py` to remove the bad import → re-deployed and re-ran the CLI smoke test (passed). I forgot to also restart `jarvis-api`, which was the actual service that loaded the broken module. `jarvis-api` crash-looped 54 times over ~3 hours until systemd's restart-rate-limit eventually picked up the corrected file. Each crash also triggered an alert to the (then-public) Slack channel — 115 messages of noise visible to the whole team.

**How to apply:** For any change that touches imports, module structure, or anything in `agent/`, `slackbot/`, or shared utility modules:

1. Identify EVERY service that imports the changed module (`grep -l "from agent.investigate" scripts/**/*.py`)
2. Restart all of them as part of the same `systemctl restart` invocation: `sudo systemctl restart jarvis-slack jarvis-api`
3. Then verify each is `active` AND check its log file (e.g., `tail /home/ubuntu/jarvis/logs/api.log`) — `systemctl is-active` only confirms the process is up; the service might still be crash-looping
4. Smoke-test through the actual user surface (Slack `/jarvis` or `curl` to API), not just module-level Python

The CLI smoke test only proves the module loads cleanly when imported directly. It doesn't prove the running service was actually restarted with the fix.
