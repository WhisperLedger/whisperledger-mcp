#!/usr/bin/env bash
# Jarvis claudify runner — thin wrapper around the official Project Claudify
# orchestrator (generate_org_claude_docs.py). We don't reinvent prompts or PR
# templates anymore — the official tooling handles structure, label, cost
# tracking, and PR description.
#
#   jarvis_claudify.sh <repo> [requester_id]
#
# Behaviour:
# - Branch: add-claude-md-docs (per official spec)
# - PR opened with label 'Claudify' + cost/duration table in body
# - --skip-existing-pr respects squad work already in flight
# - Audit log at ~/jarvis/logs/claudify_audit.jsonl
#
# Outputs on success:
#   JARVIS_PR_URL=https://github.com/jupitermoney/<repo>/pull/<n>
# On failure:
#   JARVIS_FIX_FAILED=<reason>
#
# Exit codes: 0 success, 2 bad-args, 3 prereq failure, 4 orchestrator failure
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
print(f'DAILY_CAP_USD=\"{get_env(\"DAILY_CAP_USD\", \"100\")}\"')
")

REPO="${1:-}"
REQUESTER="${2:-cli}"

if [[ -z "$REPO" ]]; then
    echo "usage: jarvis_claudify.sh <repo> [requester]" >&2
    exit 2
fi

# --- Pre-flight: repo exists? -----------------------------------------------
if ! gh repo view "${GITHUB_ORG}/$REPO" --json name >/dev/null 2>&1; then
    echo ""
    echo "ASTRA_FIX_FAILED=repo '${GITHUB_ORG}/$REPO' not found (typo in repo name?)"
    echo "JARVIS_FIX_FAILED=repo '${GITHUB_ORG}/$REPO' not found (typo in repo name?)"
    exit 3
fi

# --- Paths + audit -----------------------------------------------------------
TS=$(date -u +%Y%m%d-%H%M%S)
TASK_ID="claudify-${TS}-${REPO}"
WORK_DIR="$ROOT_DIR/workspaces/${TASK_ID}"
LOG_FILE="${WORK_DIR}/claudify_run.log"
AUDIT="$ROOT_DIR/logs/claudify_audit.jsonl"
mkdir -p "$WORK_DIR" "$(dirname "$AUDIT")"

CLAUDIFY_ROOT="$ROOT_DIR/scripts/claudify_official"
ORCHESTRATOR="${CLAUDIFY_ROOT}/generate_org_claude_docs.py"
BRANCH_NAME="add-claude-md-docs"

if [[ ! -f "$ORCHESTRATOR" ]]; then
    echo ""
    echo "ASTRA_FIX_FAILED=official orchestrator not found at $ORCHESTRATOR (missing claudify_official/ scripts)"
    echo "JARVIS_FIX_FAILED=official orchestrator not found at $ORCHESTRATOR (missing claudify_official/ scripts)"
    exit 3
fi

audit() {
    local event="$1" extra="${2:-}"
    local payload="{\"task_id\":\"$TASK_ID\",\"event\":\"$event\",\"repo\":\"$REPO\","
    payload+="\"requester\":\"$REQUESTER\",\"branch\":\"$BRANCH_NAME\","
    payload+="\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
    [[ -n "$extra" ]] && payload+=",${extra}"
    payload+="}"
    echo "$payload" >> "$AUDIT"
}

# --- Daily-cap pre-flight (A3 safety net) -----------------------------------
# Sum today's claudify spend from workspace cost JSONs. If we're at or over the
# cap, refuse to start a new run. Configurable via JARVIS_DAILY_CAP_USD env var.
DAILY_CAP_USD="${DAILY_CAP_USD:-100}"
TODAY_IST=$(TZ=Asia/Kolkata date +%Y-%m-%d)
TODAY_SPEND=$("$ROOT_DIR/scripts/indexer/.venv/bin/python" - <<PYEOF
import json, os
from datetime import datetime, timedelta, timezone
IST = timezone(timedelta(hours=5, minutes=30))
today = "${TODAY_IST}"
total = 0.0
audit_path = "$ROOT_DIR/logs/claudify_audit.jsonl"
if os.path.exists(audit_path):
    for line in open(audit_path):
        try:
            e = json.loads(line)
            if e.get("event") != "success": continue
            try:
                d = datetime.fromisoformat(e["ts"].replace("Z","+00:00")).astimezone(IST).date().isoformat()
            except: continue
            if d != today: continue
            cf = f"$ROOT_DIR/workspaces/{e['task_id']}/{e['repo']}_costs.json"
            if os.path.exists(cf):
                try: total += json.load(open(cf)).get("total_cost_usd") or 0
                except: pass
        except: pass
print(f"{total:.2f}")
PYEOF
)
echo "[$(date +%H:%M:%S)] today's claudify spend so far: \$$TODAY_SPEND  (cap: \$$DAILY_CAP_USD)"
if awk "BEGIN{exit !($TODAY_SPEND >= $DAILY_CAP_USD)}"; then
    audit aborted_daily_cap "\"today_spend_usd\":$TODAY_SPEND,\"cap_usd\":$DAILY_CAP_USD"
    echo ""
    echo "ASTRA_FIX_FAILED=Daily claudify budget of \$$DAILY_CAP_USD exhausted (today: \$$TODAY_SPEND). Cap resets at IST midnight."
    echo "JARVIS_FIX_FAILED=Daily claudify budget of \$$DAILY_CAP_USD exhausted (today: \$$TODAY_SPEND). Cap resets at IST midnight."
    exit 1
fi

audit start

echo "[$(date +%H:%M:%S)] task_id=$TASK_ID  repo=$REPO  branch=$BRANCH_NAME"
echo "[$(date +%H:%M:%S)] running official Project Claudify orchestrator..."

audit orchestrator_started
START_EPOCH=$(date +%s)

# --- Run the orchestrator ---------------------------------------------------
# It handles: clone, generate (per-module + root), commit, push, label, PR.
# --skip-existing-pr makes it respect any squad work already in flight.
if ! python3 "$ORCHESTRATOR" jupitermoney \
        --repos "$REPO" \
        --parallel 4 \
        --skip-existing-pr \
        --work-dir "$WORK_DIR" \
        > "$LOG_FILE" 2>&1; then
    EXIT_CODE=$?
    audit orchestrator_failed "\"exit_code\":$EXIT_CODE"
    echo "[$(date +%H:%M:%S)] orchestrator exited non-zero ($EXIT_CODE). Last lines:"
    tail -30 "$LOG_FILE" | sed 's|^|  |'
    echo ""
    echo "JARVIS_FIX_FAILED=orchestrator exit $EXIT_CODE — see $LOG_FILE"
    exit 4
fi

DURATION=$(( $(date +%s) - START_EPOCH ))
echo "[$(date +%H:%M:%S)] orchestrator finished in ${DURATION}s"

# --- Parse outcome from log -------------------------------------------------
PR_URL=$(grep -oE 'https://github\.com/jupitermoney/[^/]+/pull/[0-9]+' "$LOG_FILE" | head -1)
SKIPPED_PR=$(grep -E '\[skip\][[:space:]]+open PR already exists' "$LOG_FILE" | head -1)
# Match the orchestrator's actual skip-message: "branch 'X' already exists remotely"
SKIPPED_BRANCH=$(grep -E "\[skip\].*branch.*already exists remotely" "$LOG_FILE" | head -1)
NO_CHANGES=$(grep -E "(\[skip\].*no changes|No changes\s*:\s*[1-9])" "$LOG_FILE" | head -1)

# Always run spend_monitor post-run for immediate credit-failure detection
# (it's idempotent and fast; doesn't matter what the outcome was)
"$ROOT_DIR/scripts/indexer/.venv/bin/python" \
    "$ROOT_DIR/scripts/spend_monitor.py" >/dev/null 2>&1 &

if [[ -n "$PR_URL" ]]; then
    audit success "\"pr_url\":\"$PR_URL\",\"duration_sec\":$DURATION"
    echo ""
    echo "✅ DONE in ${DURATION}s"
    echo "ASTRA_PR_URL=$PR_URL"
    echo "JARVIS_PR_URL=$PR_URL"
    exit 0
elif [[ -n "$SKIPPED_PR" ]]; then
    audit skipped_existing_pr "\"duration_sec\":$DURATION"
    echo ""
    echo "ASTRA_FIX_FAILED=$REPO already has an open Claudify PR. See https://github.com/${GITHUB_ORG}/$REPO/pulls?q=add-claude-md-docs"
    echo "JARVIS_FIX_FAILED=$REPO already has an open Claudify PR. See https://github.com/${GITHUB_ORG}/$REPO/pulls?q=add-claude-md-docs"
    exit 1
elif [[ -n "$SKIPPED_BRANCH" ]]; then
    audit skipped_existing_branch "\"duration_sec\":$DURATION"
    echo ""
    echo "ASTRA_FIX_FAILED=Skipped safely: $REPO already has the branch '$BRANCH_NAME' on remote. Reopen the existing PR or delete the branch. See https://github.com/${GITHUB_ORG}/$REPO/branches"
    echo "JARVIS_FIX_FAILED=Skipped safely: $REPO already has the branch '$BRANCH_NAME' on remote. Reopen the existing PR or delete the branch. See https://github.com/${GITHUB_ORG}/$REPO/branches"
    exit 1
elif [[ -n "$NO_CHANGES" ]]; then
    audit no_changes "\"duration_sec\":$DURATION"
    echo ""
    echo "ASTRA_FIX_FAILED=$REPO already has up-to-date CLAUDE.md files (orchestrator detected no changes to commit)."
    echo "JARVIS_FIX_FAILED=$REPO already has up-to-date CLAUDE.md files (orchestrator detected no changes to commit)."
    exit 1
else
    audit unclear "\"duration_sec\":$DURATION"
    echo ""
    echo "⚠️  No PR URL or known skip marker. Last log lines:"
    tail -20 "$LOG_FILE" | sed 's|^|  |'
    echo ""
    echo "ASTRA_FIX_FAILED=unclear orchestrator outcome (no PR + no skip marker) — see $LOG_FILE"
    echo "JARVIS_FIX_FAILED=unclear orchestrator outcome (no PR + no skip marker) — see $LOG_FILE"
    exit 1
fi
