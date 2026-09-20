#!/usr/bin/env bash
# Wrapper for jarvis-jove-refresh.service. See feedback_systemd_env_pattern.md:
# systemd EnvironmentFile silently drops `export VAR=...` from ~/.config/jarvis/env,
# so we source the env file in a shell and exec the Python script.
set -euo pipefail
source /home/ubuntu/.config/jarvis/env
exec /home/ubuntu/jarvis/scripts/indexer/.venv/bin/python /home/ubuntu/jarvis/scripts/jove_scheduled_refresh.py
