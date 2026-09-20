#!/usr/bin/env bash
# scan_and_index.sh — clone a repo, run gitleaks secrets scan, index if clean.
#
# Usage:
#   scan_and_index.sh <repo-name> [--full]
#
# Behaviour:
# - Clones jupitermoney/<repo-name> to ~/jarvis/repos/<repo-name> (skips if exists)
# - Runs gitleaks; if any findings → SKIP indexing + write report to ~/jarvis/logs/secrets_scan/<repo>.json
# - Otherwise → invokes the indexer (incremental by default; --full forces full reindex)
# - Skip-list also applied at file-path level: *.tfvars, secrets*.yaml, .env*, *.pem, *.key, credentials*

set -uo pipefail
source /home/ubuntu/.config/jarvis/env

REPO="${1:-}"
FULL_FLAG="${2:-}"
if [[ -z "$REPO" ]]; then
    echo "usage: scan_and_index.sh <repo-name> [--full]" >&2
    exit 2
fi

REPOS_DIR="/home/ubuntu/jarvis/repos"
SCAN_LOG_DIR="/home/ubuntu/jarvis/logs/secrets_scan"
mkdir -p "$REPOS_DIR" "$SCAN_LOG_DIR"

REPO_PATH="$REPOS_DIR/$REPO"
SCAN_REPORT="$SCAN_LOG_DIR/$REPO.json"

# --- Clone if needed -------------------------------------------------------
if [[ ! -d "$REPO_PATH/.git" ]]; then
    echo "[$(date +%H:%M:%S)] cloning jupitermoney/$REPO..."
    if ! gh repo clone "jupitermoney/$REPO" "$REPO_PATH" -- --depth=1 2>&1 | sed 's|^|  clone: |'; then
        echo "ERROR: clone failed" >&2
        exit 3
    fi
else
    echo "[$(date +%H:%M:%S)] $REPO already cloned at $REPO_PATH (skipping clone)"
fi

# --- Secrets scan with gitleaks --------------------------------------------
echo "[$(date +%H:%M:%S)] running gitleaks on $REPO..."
# --no-banner; --redact (don't print secret values); --report-format json
# Exit code: 0=clean, 1=findings, otherwise=error
if gitleaks detect --source "$REPO_PATH" --no-banner --redact \
        --report-format json --report-path "$SCAN_REPORT" 2>&1 | tail -10; then
    SCAN_EXIT=0
else
    SCAN_EXIT=$?
fi

if [[ $SCAN_EXIT -eq 0 ]]; then
    echo "[$(date +%H:%M:%S)] ✓ gitleaks clean — proceeding to index"
elif [[ $SCAN_EXIT -eq 1 ]]; then
    # Findings exist
    NUM_FINDINGS=$(python3 -c "import json; print(len(json.load(open('$SCAN_REPORT'))))" 2>/dev/null || echo "?")
    echo "[$(date +%H:%M:%S)] ⚠️  gitleaks found $NUM_FINDINGS potential secret(s) — SKIPPING INDEX"
    echo "  Report: $SCAN_REPORT"
    echo "  Review with: jq '.[] | {File, RuleID, Description, StartLine}' $SCAN_REPORT"
    echo ""
    echo "JARVIS_SCAN_BLOCKED=$REPO has $NUM_FINDINGS gitleaks findings. Index NOT updated. Review $SCAN_REPORT, scrub the file(s), and re-run."
    exit 1
else
    echo "[$(date +%H:%M:%S)] ⚠️  gitleaks errored (exit $SCAN_EXIT) — proceeding cautiously" >&2
fi

# --- Index --------------------------------------------------------------
echo "[$(date +%H:%M:%S)] indexing $REPO ($FULL_FLAG)..."
cd /home/ubuntu/jarvis/scripts
if [[ "$FULL_FLAG" == "--full" ]]; then
    ./indexer/.venv/bin/python -m indexer.main "$REPO" --full
else
    ./indexer/.venv/bin/python -m indexer.main "$REPO"
fi

echo "[$(date +%H:%M:%S)] ✅ done indexing $REPO"
