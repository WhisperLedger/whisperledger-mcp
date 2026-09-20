#!/usr/bin/env bash
# realtime_index.sh — fetch latest commit on a repo's default branch + run the
# incremental indexer on that one repo. Invoked by the github-webhook reactive
# layer when a push event lands on an indexed repo's default branch.
#
# Usage: realtime_index.sh <repo-name> <default-branch>
#
# Behaviour:
#   1. If the local clone is missing, fresh-clones shallow.
#   2. Fetch + reset --hard to the new tip of <default-branch>.
#   3. Run `python -m indexer.main <repo>` — incremental, hash-diffed.
#
# Exits 0 on success or recoverable warning; non-zero only on hard failure.
# Append-only audit at ~/jarvis/logs/realtime_index.jsonl (caller writes the
# "fired" record; this script writes the "completed" record with stats).

set -uo pipefail
source /home/ubuntu/.config/jarvis/env

REPO="${1:?usage: realtime_index.sh <repo-name> <default-branch>}"
BRANCH="${2:?usage: realtime_index.sh <repo-name> <default-branch>}"

REPOS_DIR=/home/ubuntu/jarvis/repos
SCRIPTS_DIR=/home/ubuntu/jarvis/scripts
PYTHON="$SCRIPTS_DIR/indexer/.venv/bin/python"
AUDIT_LOG=/home/ubuntu/jarvis/logs/realtime_index.jsonl
REPO_PATH="$REPOS_DIR/$REPO"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log_json() {
    # log_json action key1=val1 key2=val2 ...
    local action="$1"; shift
    local pairs=""
    for kv in "$@"; do
        local k="${kv%%=*}"; local v="${kv#*=}"
        # escape double quotes in value
        v="${v//\"/\\\"}"
        pairs+=",\"$k\":\"$v\""
    done
    echo "{\"ts\":\"$(ts)\",\"action\":\"$action\",\"repo\":\"$REPO\",\"branch\":\"$BRANCH\"$pairs}" >> "$AUDIT_LOG"
}

started=$(date +%s)
echo "[$(ts)] realtime_index start: repo=$REPO branch=$BRANCH"

# 1) Ensure local clone exists
if [[ ! -d "$REPO_PATH/.git" ]]; then
    echo "[$(ts)] clone missing — fresh shallow clone"
    cd "$REPOS_DIR"
    if ! gh repo clone "jupitermoney/$REPO" "$REPO" -- --depth=1 --quiet 2>&1; then
        echo "[$(ts)] ERROR clone failed"
        log_json "completed" "ok=false" "stage=clone" "elapsed_sec=$(($(date +%s) - started))"
        exit 1
    fi
fi

# 2) Fetch + reset to the new tip on $BRANCH
cd "$REPO_PATH"
if ! git fetch --depth=1 origin "$BRANCH" --quiet 2>&1; then
    echo "[$(ts)] WARN fetch failed — attempting re-clone"
    cd "$REPOS_DIR"
    rm -rf "$REPO"
    if ! gh repo clone "jupitermoney/$REPO" "$REPO" -- --depth=1 --quiet 2>&1; then
        echo "[$(ts)] ERROR re-clone failed"
        log_json "completed" "ok=false" "stage=reclone" "elapsed_sec=$(($(date +%s) - started))"
        exit 1
    fi
    cd "$REPO_PATH"
    # Skip git reset — the fresh shallow clone is already on HEAD of default
fi

# git reset --hard works even on shallow clones; FETCH_HEAD is the just-pulled tip
git reset --hard FETCH_HEAD --quiet 2>&1 || true
NEW_HEAD=$(git rev-parse --short HEAD 2>/dev/null || echo "?")

# 3) Run incremental indexer
echo "[$(ts)] indexing $REPO (HEAD=$NEW_HEAD)"
cd "$SCRIPTS_DIR"
if "$PYTHON" -m indexer.main "$REPO" 2>&1; then
    elapsed=$(($(date +%s) - started))
    echo "[$(ts)] realtime_index done in ${elapsed}s"
    log_json "completed" "ok=true" "head=$NEW_HEAD" "elapsed_sec=$elapsed"
    exit 0
else
    rc=$?
    elapsed=$(($(date +%s) - started))
    echo "[$(ts)] indexer rc=$rc"
    log_json "completed" "ok=false" "stage=indexer" "head=$NEW_HEAD" "rc=$rc" "elapsed_sec=$elapsed"
    exit $rc
fi
