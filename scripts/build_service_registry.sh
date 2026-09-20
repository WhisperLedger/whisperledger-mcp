#!/usr/bin/env bash
# Nightly wrapper around build_service_registry.py + drift check.
#
# Invoked from reindex_all.sh AFTER all repo clones have refreshed (so the
# registry build sees the current corpus, not yesterday's). Failure of the
# registry rebuild itself is non-fatal for the nightly — log + continue;
# stale registry is better than abort.
#
# Drift alert is its own concern (DMs Rohit on tracked-service loss or
# > 10% service-count drop). See check_registry_drift.py.
set -uo pipefail
source /home/ubuntu/.config/jarvis/env

SCRIPTS_DIR=/home/ubuntu/jarvis/scripts
PYTHON=$SCRIPTS_DIR/indexer/.venv/bin/python

ts() { date -u +"%Y-%m-%d %H:%M:%S UTC"; }

echo "[$(ts)] [registry] rebuild starting"
if ! "$PYTHON" "$SCRIPTS_DIR/build_service_registry.py"; then
    rc=$?
    echo "[$(ts)] [registry] WARN: builder failed (rc=$rc) — keeping previous registry, skipping drift check"
    exit 0
fi
echo "[$(ts)] [registry] rebuild done"

echo "[$(ts)] [registry] drift check"
if ! "$PYTHON" "$SCRIPTS_DIR/check_registry_drift.py"; then
    rc=$?
    echo "[$(ts)] [registry] WARN: drift check failed (rc=$rc) — no DM sent; investigate manually"
fi
