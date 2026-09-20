#!/usr/bin/env bash
# Jarvis migrate runner — apply the SAME task description across a LIST of repos,
# each producing its own draft PR.
#
#   jarvis_migrate.sh "<task description>" --repos repo1,repo2,... \
#                     [--budget-per-repo 1.50] [--branch-prefix migrate-X] \
#                     [--source slack|http_api] [--caller <name>] [--requester <id>] \
#                     [--migrate-id <id>] [--stop-on-failure]
#
# Each repo runs as a child invocation of jarvis_fix.sh — same plumbing, same
# safety guarantees (draft PR only, budget cap, audit log). The migrate-level
# allowlist is JARVIS_MIGRATE_ALLOWED_REPOS (separate from JARVIS_WRITE_ALLOWED_REPOS
# so the existing per-task fix allowlist stays narrow). For each child, this
# wrapper sets JARVIS_WRITE_ALLOWED_REPOS to just the current repo, so a child
# can never accidentally write to a repo other than its own.
#
# Failures of one repo do NOT abort the batch by default (one stuck repo
# shouldn't block 49 others). Pass --stop-on-failure for the strict mode.
#
# Audit log: ~/jarvis/logs/migrate_audit.jsonl
# Workspace per child: ~/jarvis/workspaces/<task_id>/ (one per repo, isolated)
#
# Exit codes:
#   0 — all repos completed (some may have refused/failed individually; see audit)
#   2 — bad args / disallowed repo
#   3 — total budget exceeded mid-batch
#   4 — --stop-on-failure tripped
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

if [ -f "$HOME/.config/astra/env" ]; then
    source "$HOME/.config/astra/env"
elif [ -f "$HOME/.config/jarvis/env" ]; then
    source "$HOME/.config/jarvis/env"
fi

eval $(python3 -c "
import sys
sys.path.append('$SCRIPT_DIR')
from agent.config import BOT_NAME, SLASH_COMMAND, GITHUB_ORG, COMPANY_NAME, get_env
print(f'BOT_NAME=\"{BOT_NAME}\"')
print(f'SLASH_COMMAND=\"{SLASH_COMMAND}\"')
print(f'GITHUB_ORG=\"{GITHUB_ORG}\"')
print(f'COMPANY_NAME=\"{COMPANY_NAME}\"')
print(f'MIGRATE_ALLOWED_REPOS=\"{get_env(\"MIGRATE_ALLOWED_REPOS\")}\"')
")

TASK=""
REPOS_CSV=""
BUDGET_PER_REPO="1.50"
BRANCH_PREFIX=""
SOURCE="cli"
CALLER=""
REQUESTER="cli"
MIGRATE_ID=""
STOP_ON_FAILURE=0
TOTAL_BUDGET_USD=""  # optional hard cap across the whole batch

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repos)              REPOS_CSV="$2"; shift 2 ;;
        --budget-per-repo)    BUDGET_PER_REPO="$2"; shift 2 ;;
        --branch-prefix)      BRANCH_PREFIX="$2"; shift 2 ;;
        --source)             SOURCE="$2"; shift 2 ;;
        --caller)             CALLER="$2"; shift 2 ;;
        --requester)          REQUESTER="$2"; shift 2 ;;
        --migrate-id)         MIGRATE_ID="$2"; shift 2 ;;
        --stop-on-failure)    STOP_ON_FAILURE=1; shift ;;
        --total-budget-usd)   TOTAL_BUDGET_USD="$2"; shift 2 ;;
        --) shift; break ;;
        -*) echo "unknown flag: $1" >&2; exit 2 ;;
        *)  if [[ -z "$TASK" ]]; then TASK="$1"; shift; else echo "extra arg: $1" >&2; exit 2; fi ;;
    esac
done

if [[ -z "$TASK" || -z "$REPOS_CSV" ]]; then
    echo "usage: jarvis_migrate.sh \"<task>\" --repos repo1,repo2,..." >&2
    exit 2
fi

# Per-repo budget hard cap (mirror fix's $5 cap; migrate intentionally tighter default)
if awk "BEGIN{exit !($BUDGET_PER_REPO > 5.00)}"; then
    echo "ERROR: --budget-per-repo $BUDGET_PER_REPO exceeds hard cap 5.00" >&2; exit 2
fi
if awk "BEGIN{exit !($BUDGET_PER_REPO <= 0)}"; then
    echo "ERROR: --budget-per-repo must be positive" >&2; exit 2
fi

# Migrate-level allowlist (separate from fix's write allowlist)
MIGRATE_ALLOWED="${MIGRATE_ALLOWED_REPOS:-}"
if [[ -z "$MIGRATE_ALLOWED" ]]; then
    echo "ERROR: migrate allowlist is unset. Set it to a space- or" >&2
    echo "       comma-separated list of repos that migrate-mode is allowed to" >&2
    echo "       write to, or pass --override-allowlist (NOT IMPLEMENTED) for" >&2
    echo "       interactive approval." >&2
    exit 2
fi
MIGRATE_ALLOWED_NORM=$(tr ',' ' ' <<< "$MIGRATE_ALLOWED")

# Parse + validate repo list
REPOS=()
while IFS= read -r repo; do
    repo=$(tr -d '[:space:]' <<< "$repo")
    [[ -z "$repo" ]] && continue
    if ! grep -qw -- "$repo" <<< " $MIGRATE_ALLOWED_NORM "; then
        echo "ERROR: repo '$repo' not in the migrate allowlist." >&2
        echo "       Currently allowed: '${MIGRATE_ALLOWED}'" >&2
        exit 2
    fi
    REPOS+=("$repo")
done <<< "$(tr ',' '\n' <<< "$REPOS_CSV")"

if [[ ${#REPOS[@]} -eq 0 ]]; then
    echo "ERROR: --repos resolved to empty list" >&2; exit 2
fi

# Migrate identifiers + audit setup
if [[ -z "$MIGRATE_ID" ]]; then
    TS=$(date -u +%Y%m%d-%H%M%S)
    SLUG=$(tr -cs '[:alnum:]' '-' <<< "$TASK" | cut -c1-30 | tr -s '-' | sed 's/^-//;s/-$//' | tr '[:upper:]' '[:lower:]')
    SUFFIX=$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')
    MIGRATE_ID="mig-${TS}-${SLUG}-${SUFFIX}"
fi
AUDIT="$ROOT_DIR/logs/migrate_audit.jsonl"
mkdir -p "$(dirname "$AUDIT")"

migrate_audit() {
    local event="$1"
    local extra="${2:-}"
    local payload="{\"migrate_id\":\"$MIGRATE_ID\",\"event\":\"$event\","
    payload+="\"requester\":\"$REQUESTER\",\"source\":\"$SOURCE\","
    [[ -n "$CALLER" ]] && payload+="\"caller\":\"$CALLER\","
    payload+="\"budget_per_repo_usd\":$BUDGET_PER_REPO,"
    payload+="\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
    [[ -n "$extra" ]] && payload+=",${extra}"
    payload+="}"
    echo "$payload" >> "$AUDIT"
}

REPOS_JSON=$(printf '"%s",' "${REPOS[@]}"); REPOS_JSON="[${REPOS_JSON%,}]"
migrate_audit start "\"task\":$(jq -Rs <<< "$TASK"),\"repos\":$REPOS_JSON,\"n_repos\":${#REPOS[@]}"

echo "=========================================================================="
echo "Migrate ID:        $MIGRATE_ID"
echo "Repos (${#REPOS[@]}):       ${REPOS[*]}"
echo "Budget per repo:   \$$BUDGET_PER_REPO"
if [[ -n "$TOTAL_BUDGET_USD" ]]; then
    echo "Total budget cap:  \$$TOTAL_BUDGET_USD"
fi
echo "Task:              $TASK"
echo "Stop on failure:   $STOP_ON_FAILURE"
echo "=========================================================================="

CUMULATIVE_COST="0"
N_SUCCESS=0
N_FAILED=0
N_REFUSED=0
PR_URLS=()
FAILURES=()

for REPO in "${REPOS[@]}"; do
    echo ""
    echo "[migrate] $(date +%H:%M:%S) --- starting repo: $REPO ---"
    migrate_audit repo_start "\"repo\":\"$REPO\""

    # Pre-check total budget (don't start a child that would push us past cap)
    if [[ -n "$TOTAL_BUDGET_USD" ]]; then
        PROJECTED=$(awk "BEGIN{printf \"%.4f\", $CUMULATIVE_COST + $BUDGET_PER_REPO}")
        if awk "BEGIN{exit !($PROJECTED > $TOTAL_BUDGET_USD)}"; then
            echo "[migrate] total budget would be exceeded ($PROJECTED > $TOTAL_BUDGET_USD); halting batch"
            migrate_audit total_budget_exceeded "\"cumulative_cost_usd\":$CUMULATIVE_COST,\"would_be\":$PROJECTED,\"cap\":$TOTAL_BUDGET_USD"
            exit 3
        fi
    fi

    # Run fix.sh as a child with WRITE_ALLOWED restricted to JUST this repo, so
    # the child's existing allowlist check still does real work (defense in depth).
    CHILD_LOG="$ROOT_DIR/workspaces/${MIGRATE_ID}_${REPO}.log"
    mkdir -p "$(dirname "$CHILD_LOG")"
    set +e
    WRITE_ALLOWED_REPOS="$REPO" \
    JARVIS_WRITE_ALLOWED_REPOS="$REPO" \
        "$ROOT_DIR/scripts/astra_fix.sh" \
            "$REPO" "$TASK" "$REQUESTER" \
            --source "$SOURCE" \
            --caller "${CALLER:-migrate}" \
            --budget "$BUDGET_PER_REPO" \
            > "$CHILD_LOG" 2>&1
    CHILD_RC=$?
    set -e

    # Extract PR_URL or refusal/fail
    PR_URL=$(grep -oE '(ASTRA|JARVIS)_PR_URL=https?://[^ ]+' "$CHILD_LOG" | tail -1 | cut -d= -f2-)
    REFUSED=$(grep -oE '(ASTRA|JARVIS)_FIX_REFUSED=[a-zA-Z0-9_]+' "$CHILD_LOG" | tail -1 | cut -d= -f2-)
    FAIL_REASON=$(grep -oE '(ASTRA|JARVIS)_FIX_FAILED=.+' "$CHILD_LOG" | tail -1 | cut -d= -f2-)
    REPO_COST=$(grep -oE 'cost: \$[0-9.]+' "$CHILD_LOG" | tail -1 | sed 's/cost: \$//' || echo "0")
    [[ -z "$REPO_COST" ]] && REPO_COST="0"

    CUMULATIVE_COST=$(awk "BEGIN{printf \"%.4f\", $CUMULATIVE_COST + $REPO_COST}")

    if [[ -n "$PR_URL" ]]; then
        N_SUCCESS=$((N_SUCCESS + 1))
        PR_URLS+=("$REPO|$PR_URL")
        migrate_audit repo_success "\"repo\":\"$REPO\",\"pr_url\":\"$PR_URL\",\"cost_usd\":$REPO_COST,\"cumulative_cost_usd\":$CUMULATIVE_COST"
        echo "[migrate] ✓ $REPO → $PR_URL  (\$$REPO_COST, cumulative \$$CUMULATIVE_COST)"
    elif [[ -n "$REFUSED" ]]; then
        N_REFUSED=$((N_REFUSED + 1))
        FAILURES+=("$REPO|refused:$REFUSED")
        migrate_audit repo_refused "\"repo\":\"$REPO\",\"reason\":\"$REFUSED\",\"cost_usd\":$REPO_COST,\"cumulative_cost_usd\":$CUMULATIVE_COST"
        echo "[migrate] ⊘ $REPO refused: $REFUSED (\$$REPO_COST, cumulative \$$CUMULATIVE_COST)"
    else
        N_FAILED=$((N_FAILED + 1))
        FAILURES+=("$REPO|failed:${FAIL_REASON:-rc=$CHILD_RC}")
        migrate_audit repo_failed "\"repo\":\"$REPO\",\"rc\":$CHILD_RC,\"reason\":$(jq -Rs <<< "${FAIL_REASON:-}"),\"cost_usd\":$REPO_COST,\"cumulative_cost_usd\":$CUMULATIVE_COST"
        echo "[migrate] ✗ $REPO failed (rc=$CHILD_RC): ${FAIL_REASON:-see $CHILD_LOG}"
        if [[ "$STOP_ON_FAILURE" -eq 1 ]]; then
            migrate_audit halt_on_failure "\"repo\":\"$REPO\""
            echo "[migrate] --stop-on-failure tripped; halting batch"
            exit 4
        fi
    fi
done

echo ""
echo "=========================================================================="
echo "[migrate] DONE  $N_SUCCESS success · $N_FAILED failed · $N_REFUSED refused"
echo "[migrate] total cost: \$$CUMULATIVE_COST"
for entry in "${PR_URLS[@]}"; do
    repo="${entry%%|*}"; url="${entry#*|}"
    echo "  ✓ $repo → $url"
done
for entry in "${FAILURES[@]}"; do
    repo="${entry%%|*}"; reason="${entry#*|}"
    echo "  ✗ $repo ($reason)"
done
echo "=========================================================================="

migrate_audit complete "\"n_success\":$N_SUCCESS,\"n_failed\":$N_FAILED,\"n_refused\":$N_REFUSED,\"total_cost_usd\":$CUMULATIVE_COST"

# Emit machine-readable summary for the HTTP/MCP/Slack callers to parse
echo ""
echo "ASTRA_MIGRATE_DONE=$MIGRATE_ID"
echo "JARVIS_MIGRATE_DONE=$MIGRATE_ID"
echo "ASTRA_MIGRATE_SUCCESS=$N_SUCCESS"
echo "JARVIS_MIGRATE_SUCCESS=$N_SUCCESS"
echo "ASTRA_MIGRATE_FAILED=$N_FAILED"
echo "JARVIS_MIGRATE_FAILED=$N_FAILED"
echo "ASTRA_MIGRATE_REFUSED=$N_REFUSED"
echo "JARVIS_MIGRATE_REFUSED=$N_REFUSED"
echo "ASTRA_MIGRATE_TOTAL_COST_USD=$CUMULATIVE_COST"
echo "JARVIS_MIGRATE_TOTAL_COST_USD=$CUMULATIVE_COST"
for entry in "${PR_URLS[@]}"; do
    echo "ASTRA_MIGRATE_PR=$entry"
    echo "JARVIS_MIGRATE_PR=$entry"
done
