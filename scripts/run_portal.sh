#!/usr/bin/env bash
# Launcher for jarvis-portal.service. Sources env + execs uvicorn.
set -eu
source /home/ubuntu/.config/jarvis/env
cd /home/ubuntu/jarvis/scripts
exec /home/ubuntu/jarvis/scripts/indexer/.venv/bin/uvicorn \
    portal.server:app \
    --host 127.0.0.1 \
    --port 8083 \
    --workers 1 \
    --log-level info \
    --access-log
