#!/usr/bin/env bash
# Bash wrapper for systemd-fired iterate auto-fire.
# Sources the env file (per CLAUDE.md systemd-must-have-bash-wrapper rule)
# and sets ITERATE_AUTO_FIRE_ENABLED=1 so manual runs (without this wrapper)
# stay no-op by default.
set -euo pipefail
source /home/ubuntu/.config/jarvis/env
export ITERATE_AUTO_FIRE_ENABLED=1
exec /home/ubuntu/jarvis/scripts/indexer/.venv/bin/python /home/ubuntu/jarvis/scripts/iterate_auto_fire.py
