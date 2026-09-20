#!/usr/bin/env bash
# scan_and_index_wearesumhr.sh — clone + gitleaks + index for wearesumhr org.
# Same behaviour as scan_and_index.sh but:
#   - clones from wearesumhr/<name>, not jupitermoney
#   - clones to ~/jarvis/repos/wearesumhr-<name>  (namespaced folder = namespaced Qdrant repo id)
#   - passes the namespaced folder name as the repo id to the indexer
# Usage: scan_and_index_wearesumhr.sh <upstream-repo-name> [--full]

set -uo pipefail
source /home/ubuntu/.config/jarvis/env

REPO="${1:-}"
FULL_FLAG="${2:-}"
if [[ -z "$REPO" ]]; then
    echo "usage: scan_and_index_wearesumhr.sh <upstream-repo-name> [--full]" >&2
    exit 2
fi

REPOS_DIR="/home/ubuntu/jarvis/repos"
SCAN_LOG_DIR="/home/ubuntu/jarvis/logs/secrets_scan"
mkdir -p "$REPOS_DIR" "$SCAN_LOG_DIR"

# Namespaced local id — avoids collision with jupitermoney/<name> repos (e.g. gatekeeper).
LOCAL_ID="wearesumhr-$REPO"
REPO_PATH="$REPOS_DIR/$LOCAL_ID"
SCAN_REPORT="$SCAN_LOG_DIR/$LOCAL_ID.json"

if [[ ! -d "$REPO_PATH/.git" ]]; then
    echo "[$(date +%H:%M:%S)] cloning wearesumhr/$REPO -> $REPO_PATH..."
    if ! gh repo clone "wearesumhr/$REPO" "$REPO_PATH" -- --depth=1 2>&1 | sed 's|^|  clone: |'; then
        echo "ERROR: clone failed" >&2
        exit 3
    fi
else
    echo "[$(date +%H:%M:%S)] $LOCAL_ID already cloned (skipping clone)"
fi

echo "[$(date +%H:%M:%S)] running gitleaks on $LOCAL_ID..."
if gitleaks detect --source "$REPO_PATH" --no-banner --redact \
        --report-format json --report-path "$SCAN_REPORT" 2>&1 | tail -5; then
    SCAN_EXIT=0
else
    SCAN_EXIT=$?
fi

if [[ $SCAN_EXIT -eq 0 ]]; then
    echo "[$(date +%H:%M:%S)] gitleaks clean — proceeding to index"
elif [[ $SCAN_EXIT -eq 1 ]]; then
    NUM_FINDINGS=$(python3 -c "import json; print(len(json.load(open('$SCAN_REPORT'))))" 2>/dev/null || echo "?")
    echo "[$(date +%H:%M:%S)] gitleaks: $NUM_FINDINGS findings — SKIPPING INDEX ($SCAN_REPORT)"
    echo "JARVIS_SCAN_BLOCKED=$LOCAL_ID has $NUM_FINDINGS gitleaks findings"
    exit 1
else
    echo "[$(date +%H:%M:%S)] gitleaks errored (exit $SCAN_EXIT) — proceeding cautiously" >&2
fi

echo "[$(date +%H:%M:%S)] indexing $LOCAL_ID ($FULL_FLAG)..."
cd /home/ubuntu/jarvis/scripts
if [[ "$FULL_FLAG" == "--full" ]]; then
    ./indexer/.venv/bin/python -m indexer.main "$LOCAL_ID" --full
else
    ./indexer/.venv/bin/python -m indexer.main "$LOCAL_ID"
fi
echo "[$(date +%H:%M:%S)] done indexing $LOCAL_ID"
