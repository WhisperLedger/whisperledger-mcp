#!/usr/bin/env bash
# Full-corpus indexing + automatic graceful bot restart.
#
# Reads /home/ubuntu/jarvis/scripts/indexed_repos.txt as the source of truth,
# runs the incremental indexer per-repo (existing repos with up-to-date hashes
# are no-ops, ~0.2s each; new repos do a full embed), then restarts jarvis-slack
# so it picks up the latest INDEXED_REPOS + system prompt.
#
# Designed to run detached (nohup setsid) so the laptop can sleep.
set -uo pipefail
source /home/ubuntu/.config/jarvis/env

REPOS_DIR=/home/ubuntu/jarvis/repos
SCRIPTS_DIR=/home/ubuntu/jarvis/scripts
REPOS_FILE=$SCRIPTS_DIR/indexed_repos.txt
PYTHON=$SCRIPTS_DIR/indexer/.venv/bin/python

ts() { date -u +"%Y-%m-%d %H:%M:%S UTC"; }

echo ""
echo "########## FULL SCAN START $(ts) ##########"

# Load the repo list (skipping comments + blank lines).
REPOS=()
while IFS= read -r line; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" ]] && continue
    REPOS+=("$line")
done < "$REPOS_FILE"

total=${#REPOS[@]}
echo "[$(ts)] indexing $total repos (incremental — unchanged files free)"

skipped=0
i=0
for r in "${REPOS[@]}"; do
    i=$((i + 1))
    if [[ ! -d "$REPOS_DIR/$r" ]]; then
        echo "[$(ts)] [$i/$total] $r: NOT CLONED, skipping"
        skipped=$((skipped + 1))
        continue
    fi
    cd "$SCRIPTS_DIR"
    "$PYTHON" -m indexer.main "$r" 2>&1 | tail -1
done

echo ""
echo "[$(ts)] indexing pass complete ($((total - skipped)) processed, $skipped skipped)"

echo "[$(ts)] gracefully restarting jarvis-slack..."
sudo systemctl restart jarvis-slack
sleep 6
if systemctl is-active jarvis-slack >/dev/null; then
    echo "[$(ts)] ✓ bot active again"
else
    echo "[$(ts)] ⚠ bot NOT active after restart — check 'systemctl status jarvis-slack'"
fi

echo ""
echo "[$(ts)] final Qdrant state:"
curl -fsS http://127.0.0.1:6333/collections/jarvis_code \
    | python3 -c "import sys,json; d=json.load(sys.stdin); r=d['result']; print(f\"  points: {r['points_count']}  status: {r['status']}  segments: {r['segments_count']}\")"

echo "########## FULL SCAN DONE $(ts) ##########"
