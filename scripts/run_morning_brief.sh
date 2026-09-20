#!/usr/bin/env bash
# Wrapper for jarvis-morning-brief.timer.
set -eu
source /home/ubuntu/.config/jarvis/env
exec /home/ubuntu/jarvis/scripts/indexer/.venv/bin/python /home/ubuntu/jarvis/scripts/morning_brief.py
