#!/usr/bin/env bash
# Wrapper for jarvis-spend-monitor.service.
#
# Why this exists: systemd's EnvironmentFile= directive does NOT support the
# `export VAR=...` format that ~/.config/jarvis/env uses (and that every bash-
# sourced Jarvis script depends on). Loading the env file directly via systemd
# silently drops every variable. So we wrap the Python invocation in a shell
# that sources the file properly, matching the pattern used by run_slackbot.sh,
# run_api.sh, and reindex_all.sh.
set -euo pipefail
source /home/ubuntu/.config/jarvis/env
exec /home/ubuntu/jarvis/scripts/indexer/.venv/bin/python /home/ubuntu/jarvis/scripts/spend_monitor.py
