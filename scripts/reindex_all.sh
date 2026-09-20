#!/usr/bin/env bash
# Daily re-index: fresh shallow-clones every repo in INDEXED_REPOS, then
# runs the Voyage indexer on all of them. Idempotent — safe to run anytime.
#
# Invoked by jarvis-reindex.service / jarvis-reindex.timer.
set -eu
set -o pipefail
source /home/ubuntu/.config/jarvis/env

REPOS_DIR=/home/ubuntu/jarvis/repos
SCRIPTS_DIR=/home/ubuntu/jarvis/scripts
PYTHON=$SCRIPTS_DIR/indexer/.venv/bin/python

# Repo list is loaded from /home/ubuntu/jarvis/scripts/indexed_repos.txt
# (single source of truth, also read by agent/tools.py).
REPOS=()
while IFS= read -r line; do
    line="${line%%#*}"            # strip trailing comments
    line="${line#"${line%%[![:space:]]*}"}"  # ltrim
    line="${line%"${line##*[![:space:]]}"}"  # rtrim
    [[ -z "$line" ]] && continue
    REPOS+=("$line")
done < /home/ubuntu/jarvis/scripts/indexed_repos.txt

ts() { date -u +"%Y-%m-%d %H:%M:%S UTC"; }

echo ""
echo "########## reindex starting $(ts) ##########"
mkdir -p "$REPOS_DIR"
cd "$REPOS_DIR"

# 1) Fresh shallow clones (delete existing, re-clone)
for r in "${REPOS[@]}"; do
    echo "[$(ts)] clone $r"
    rm -rf "$r"
    if ! gh repo clone "jupitermoney/$r" "$r" -- --depth=1 --quiet; then
        echo "[$(ts)] WARN: clone failed for $r — leaving stale-deleted, indexer will skip"
    fi
done

# 2) Re-index code — explicitly per-repo, NOT --all. The indexer's --all flag walks
#    every directory under repos/, which can include leftover/deferred clones
#    (e.g. prod.jupiter.money) that we don't want indexed. Always pass the
#    explicit list so the index can never include something off-list.
#
# Per-repo failures (missing clone, embed timeout, qdrant blip) MUST NOT abort
# the whole nightly reindex — log + continue. The indexer itself also warns +
# returns 0 on missing clones (see indexer/main.py); this loop is defense in
# depth for any other transient failure path.
echo "[$(ts)] indexing code, repos one by one"
cd "$SCRIPTS_DIR"
INDEXER_FAIL_COUNT=0
INDEXER_FAILED_REPOS=()
for r in "${REPOS[@]}"; do
    if ! "$PYTHON" -m indexer.main "$r"; then
        rc=$?
        echo "[$(ts)] WARN: indexer failed for $r (rc=$rc) — continuing with next repo"
        INDEXER_FAIL_COUNT=$((INDEXER_FAIL_COUNT + 1))
        INDEXER_FAILED_REPOS+=("$r")
    fi
done
if [ "$INDEXER_FAIL_COUNT" -gt 0 ]; then
    echo "[$(ts)] SUMMARY: $INDEXER_FAIL_COUNT per-repo indexer failure(s): ${INDEXER_FAILED_REPOS[*]}"
fi

# 3) Re-index PR descriptions (jarvis_prs collection). Distinct from code
#    indexing — fetched live via `gh api`, not from repo clones. The --all
#    flag here reads from indexed_repos.txt (single source of truth), so the
#    same "no off-list repos" guarantee holds. Added 2026-05-17 after
#    discovering the PR index had drifted 4 days behind (pr_indexer.py was
#    being run manually before this).
echo "[$(ts)] indexing PR descriptions"
if ! "$PYTHON" -m indexer.pr_indexer --all; then
    pr_rc=$?
    echo "[$(ts)] WARN: pr_indexer --all failed (rc=$pr_rc) — code collection still updated, PR collection may be stale"
fi

# 4) Rebuild service-discovery registry (~200 services) from the freshly-cloned
#    corpus + drift-check vs previous snapshot. Wrapper handles its own logging
#    and is non-fatal — stale registry beats abort. DMs Rohit if tracked
#    services vanish or service count drops > 10%.
echo "[$(ts)] rebuilding service registry"
bash "$SCRIPTS_DIR/build_service_registry.sh" || true

echo "[$(ts)] qdrant state:"
curl -fsS http://127.0.0.1:6333/collections/jarvis_code | python3 -c "import sys,json; d=json.load(sys.stdin); print('  jarvis_code points:', d['result']['points_count'], 'status:', d['result']['status'])"
curl -fsS http://127.0.0.1:6333/collections/jarvis_prs  | python3 -c "import sys,json; d=json.load(sys.stdin); print('  jarvis_prs  points:', d['result']['points_count'], 'status:', d['result']['status'])"
echo "########## reindex done $(ts) ##########"
