#!/usr/bin/env bash
# Garbage-collect old fix/claudify workspaces (default: older than 7 days).
# Each workspace is a one-shot full clone created by jarvis_fix.sh or claudify;
# once the PR is opened the workspace serves no further purpose.
#
# Before deleting a workspace, any *_costs.json files (written by the claudify
# orchestrator) are extracted and appended to ~/jarvis/logs/claudify_cost_history.jsonl
# so cost data survives GC.
#
# Run manually or wire into cron / a systemd timer. Safe to re-run; idempotent.
set -uo pipefail
WORKSPACES=/home/ubuntu/jarvis/workspaces
KEEP_DAYS="${KEEP_DAYS:-7}"
COST_HISTORY=/home/ubuntu/jarvis/logs/claudify_cost_history.jsonl

ts() { date -u +"%Y-%m-%d %H:%M:%S UTC"; }

if [[ ! -d "$WORKSPACES" ]]; then
    echo "[$(ts)] $WORKSPACES does not exist; nothing to GC"
    exit 0
fi

mkdir -p "$(dirname "$COST_HISTORY")"

echo "[$(ts)] gc starting — deleting workspaces older than ${KEEP_DAYS} days under $WORKSPACES"
before_count=$(ls -1 "$WORKSPACES" 2>/dev/null | wc -l)
before_du=$(du -sh "$WORKSPACES" 2>/dev/null | cut -f1)

deleted=0
cost_records_preserved=0
while IFS= read -r dir; do
    workspace_name=$(basename "$dir")

    # Persist any cost JSONs in this workspace before deletion. The claudify
    # orchestrator writes <repo>_costs.json at the workspace root after a run;
    # surface those + a deletion timestamp into the durable history log so
    # the data outlives GC.
    while IFS= read -r cost_file; do
        [[ -z "$cost_file" ]] && continue
        # Wrap each cost JSON with workspace + deletion timestamp for context.
        wrapped=$(jq -c \
            --arg ws "$workspace_name" \
            --arg src "$(basename "$cost_file")" \
            --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            '. + {workspace: $ws, source_file: $src, gc_deleted_at: $ts}' \
            "$cost_file" 2>/dev/null) || continue
        echo "$wrapped" >> "$COST_HISTORY"
        cost_records_preserved=$((cost_records_preserved + 1))
    done < <(find "$dir" -maxdepth 3 -name "*_costs.json" -type f 2>/dev/null)

    echo "  rm $dir ($(du -sh "$dir" 2>/dev/null | cut -f1))"
    rm -rf -- "$dir"
    deleted=$((deleted + 1))
done < <(find "$WORKSPACES" -mindepth 1 -maxdepth 1 -type d -mtime "+${KEEP_DAYS}")

after_count=$(ls -1 "$WORKSPACES" 2>/dev/null | wc -l)
after_du=$(du -sh "$WORKSPACES" 2>/dev/null | cut -f1)

echo "[$(ts)] gc done — deleted $deleted, kept $after_count workspace(s)"
echo "[$(ts)] workspaces dir: $before_du → $after_du"
echo "[$(ts)] cost records preserved to $COST_HISTORY: $cost_records_preserved"
