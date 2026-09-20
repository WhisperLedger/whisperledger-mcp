---
name: systemd Python services must use a bash wrapper to source env (NOT EnvironmentFile=)
description: ~/.config/jarvis/env uses `export VAR=...` format which systemd's EnvironmentFile= silently drops. Every Python systemd service must wrap its ExecStart in a bash script that source-s the env file.
type: feedback
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
Rule: For any **Python** systemd service on the Jarvis box, the `ExecStart=` must point to a bash wrapper script (matching `scripts/run_slackbot.sh`, `scripts/run_api.sh`, `scripts/reindex_all.sh`, `scripts/run_spend_monitor.sh`) that first `source`s `/home/ubuntu/.config/jarvis/env` then `exec`s the Python entry point.

Do **NOT** use systemd's `EnvironmentFile=/home/ubuntu/.config/jarvis/env` directive on Python services. That directive silently drops every line in the file because `~/.config/jarvis/env` is in shell-source format (`export VAR=value`), and systemd's parser only accepts plain `VAR=value` lines. Result: every env var is missing, the Python script crashes immediately on the first `os.environ[...]` lookup, OnFailure fires, the operator gets a DM alert, and the next timer tick repeats the loop.

**Why:** Discovered on 2026-05-18 when the operator was receiving ~27 DM alerts/day from `jarvis-spend-monitor.service` (timer fires every 15 min). The unit had `EnvironmentFile=/home/ubuntu/.config/jarvis/env`, the env file was 14/14 export-prefixed lines, so `SLACK_BOT_TOKEN` was never set, Python crashed with `ERROR: SLACK_BOT_TOKEN not set`. Fixed by introducing `scripts/run_spend_monitor.sh` wrapper and dropping `EnvironmentFile=` from the unit. Commit `a9336ea`.

**How to apply:**
- Adding a new Python systemd service? Don't use `EnvironmentFile=`. Create a `run_<name>.sh` wrapper that:
  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  source /home/ubuntu/.config/jarvis/env
  exec /home/ubuntu/jarvis/scripts/indexer/.venv/bin/python /home/ubuntu/jarvis/scripts/<entry>.py
  ```
  Then `chmod +x` and point `ExecStart=` at it.
- Reviewing an existing service that's OnFailure-spamming on `SLACK_BOT_TOKEN not set` or any other env-related error? Check the unit for `EnvironmentFile=`. If present, that's the bug.
- The env file format is **not** changing — every bash-sourced script (claudify, fix, indexer wrappers, all of the `run_*.sh` family) depends on the `export VAR=...` shape. Don't convert it to plain `VAR=value` just to please systemd; wrap instead.
- Bash systemd services (e.g. `jarvis-reindex.service` -> `reindex_all.sh`) are fine as-is because the wrapper script handles sourcing.
