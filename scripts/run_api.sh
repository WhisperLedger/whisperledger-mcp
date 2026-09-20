#!/usr/bin/env bash
# Launcher for jarvis-api.service. Sources env + execs uvicorn.
set -eu
source /home/ubuntu/.config/jarvis/env
cd /home/ubuntu/jarvis/scripts
exec /home/ubuntu/jarvis/scripts/indexer/.venv/bin/uvicorn \
    api.server:app \
    --host 0.0.0.0 \
    --port 8081 \
    --workers 1 \
    --log-level info \
    --access-log
