#!/usr/bin/env bash
# jarvis_companion_pr.sh — open a draft "companion" PR in an upstream repo
# fixing the architectural root cause of a bug diagnosed at a call-site PR.
#
# Used by jarvis_iterate.sh (when ITERATE_COMPANION_PR=1 and Claude outputs
# JARVIS_NEEDS_COMPANION) and as a manual one-shot tool.
#
# Args:
#   --source-pr <url>       Source call-site PR URL (e.g. https://github.com/jupitermoney/jupiter/pull/14141)
#   --upstream-repo <name>  Short name of upstream repo (must be on JARVIS_WRITE_ALLOWED_REPOS)
#   --diagnosis <file>      Path to a markdown file with the diagnosis from the source-iterate run
#   --budget <usd>          Max Claude budget (default 2.00, hard cap 4.00)
#   --caller <id>           Caller identifier for audit (e.g. operator, iterate-auto)
#
# Exit codes: 0 success, 2 bad-args, 3 prereq failure, 4 claude failure, 5 PR not opened
#
# Outputs on success:
#   JARVIS_COMPANION_PR_DONE=<companion-pr-url>

set -euo pipefail

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
print(f'WRITE_ALLOWED_REPOS=\"{get_env(\"WRITE_ALLOWED_REPOS\")}\"')
")

BOT_LOWER=$(echo "$BOT_NAME" | tr '[:upper:]' '[:lower:]')

# --- Arg parsing -------------------------------------------------------------
SOURCE_PR_URL=""
UPSTREAM_REPO=""
DIAGNOSIS_FILE=""
BUDGET_USD="2.00"
CALLER="manual"

while [ $# -gt 0 ]; do
    case "$1" in
        --source-pr)      SOURCE_PR_URL="$2"; shift 2 ;;
        --upstream-repo)  UPSTREAM_REPO="$2"; shift 2 ;;
        --diagnosis)      DIAGNOSIS_FILE="$2"; shift 2 ;;
        --budget)         BUDGET_USD="$2"; shift 2 ;;
        --caller)         CALLER="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

[ -z "$SOURCE_PR_URL" ] && { echo "ERROR: --source-pr required" >&2; exit 2; }
[ -z "$UPSTREAM_REPO" ] && { echo "ERROR: --upstream-repo required" >&2; exit 2; }
[ -z "$DIAGNOSIS_FILE" ] && { echo "ERROR: --diagnosis required" >&2; exit 2; }
[ ! -f "$DIAGNOSIS_FILE" ] && { echo "ERROR: diagnosis file not found: $DIAGNOSIS_FILE" >&2; exit 2; }

if awk "BEGIN{exit !($BUDGET_USD > 4.00)}"; then
    echo "ERROR: --budget $BUDGET_USD exceeds companion hard cap of 4.00" >&2; exit 2
fi

# Allowlist check (same env as fix + iterate)
ALLOWED="${WRITE_ALLOWED_REPOS:-}"
if ! echo " $ALLOWED " | tr ',' ' ' | grep -q " $UPSTREAM_REPO "; then
    echo "ERROR: upstream repo '$UPSTREAM_REPO' not in write allowlist ($ALLOWED)" >&2
    exit 2
fi

# Parse source PR url → source_repo, source_pr_num
SOURCE_REPO=$(echo "$SOURCE_PR_URL" | sed -nE "s|https://github.com/${GITHUB_ORG}/([^/]+)/pull/[0-9]+|\1|p")
SOURCE_PR_NUM=$(echo "$SOURCE_PR_URL" | sed -nE "s|https://github.com/${GITHUB_ORG}/[^/]+/pull/([0-9]+)|\1|p")
[ -z "$SOURCE_REPO" ] || [ -z "$SOURCE_PR_NUM" ] && {
    echo "ERROR: could not parse source PR URL: $SOURCE_PR_URL" >&2; exit 2
}

echo "[$(date +%H:%M:%S)] companion-PR: source=$SOURCE_REPO#$SOURCE_PR_NUM upstream=$UPSTREAM_REPO budget=$BUDGET_USD"

# --- Workspace + clone -------------------------------------------------------
TS=$(date -u +%Y%m%d-%H%M%S)
WORKSPACE="$ROOT_DIR/workspaces/companion-${UPSTREAM_REPO}-from-${SOURCE_REPO}-${SOURCE_PR_NUM}-${TS}"
mkdir -p "$WORKSPACE"
cd "$WORKSPACE"

BRANCH="${BOT_LOWER}/companion-${SOURCE_REPO}-pr${SOURCE_PR_NUM}-${TS}"
echo "[$(date +%H:%M:%S)] cloning ${GITHUB_ORG}/$UPSTREAM_REPO into $WORKSPACE"
if ! gh repo clone "${GITHUB_ORG}/$UPSTREAM_REPO" 2>&1 | sed 's|^|  clone: |'; then
    echo "ASTRA_COMPANION_FAILED=clone failed"
    echo "JARVIS_COMPANION_FAILED=clone failed"
    exit 3
fi
cd "$UPSTREAM_REPO"

# Detect default branch (main vs master vs develop)
DEFAULT_BRANCH=$(gh api "repos/${GITHUB_ORG}/$UPSTREAM_REPO" -q '.default_branch')
echo "[$(date +%H:%M:%S)] default branch: $DEFAULT_BRANCH"
git checkout -b "$BRANCH" 2>&1 | sed 's|^|  checkout: |'
git config user.email "${BOT_LOWER}-bot@${GITHUB_ORG}.local"
git config user.name "${BOT_NAME} Bot"

# --- Audit -------------------------------------------------------------------
COMP_TASK_ID="companion-${TS}-${UPSTREAM_REPO}-from-pr${SOURCE_PR_NUM}"
AUDIT="$ROOT_DIR/logs/companion_audit.jsonl"
mkdir -p "$(dirname "$AUDIT")"
audit() {
    local event="$1" extra="${2:-}"
    local payload="{\"task_id\":\"$COMP_TASK_ID\",\"event\":\"$event\","
    payload+="\"source_pr\":\"$SOURCE_PR_URL\",\"upstream_repo\":\"$UPSTREAM_REPO\","
    payload+="\"caller\":\"$CALLER\",\"budget_usd\":$BUDGET_USD,"
    payload+="\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
    [ -n "$extra" ] && payload+=",${extra}"
    payload+="}"
    echo "$payload" >> "$AUDIT"
}
audit start

# --- Build Claude prompt -----------------------------------------------------
DIAGNOSIS_CONTENT=$(cat "$DIAGNOSIS_FILE")
read -r -d '' PROMPT <<EOFPROMPT || true
You are Jarvis, opening a "companion" draft PR in jupitermoney/${UPSTREAM_REPO}
to fix the ARCHITECTURAL ROOT CAUSE of a bug that has been worked around at
a call-site in another repo.

Source call-site PR: ${SOURCE_PR_URL}

DIAGNOSIS from the source-iterate run (this is the analysis that identified
the bug as belonging in your repo, not the call-site):

----- BEGIN DIAGNOSIS -----
${DIAGNOSIS_CONTENT}
----- END DIAGNOSIS -----

You are inside a fresh checkout of jupitermoney/${UPSTREAM_REPO} on a new
branch (${BRANCH}). Default branch is ${DEFAULT_BRANCH}.

MANDATORY WORKFLOW:

1. READ the diagnosis above. Identify the exact component / file / line
   where the root-cause fix belongs.

2. LOCATE the relevant file in this repo. Use grep + find liberally.
   Read sibling files for conventions.

3. WRITE THE MINIMAL CORRECT FIX. Architectural fix at source — that is
   the entire point of opening a companion PR.
   - Do NOT change unrelated code.
   - Use existing design tokens / typography variants if applicable.
   - Match the existing code style (component patterns, naming, indentation).

4. VALIDATE: run linters / type checks if there's an obvious script
   (yarn lint, yarn tsc, yarn jest <component>). Skip if no obvious target.

5. SELF-REVIEW: read your diff. Hunt for: hardcoded values that should be
   tokens, missing tests for new exported behaviour, accidentally-changed
   unrelated code.

6. COMMIT all changes as ONE commit. Subject:
     "fix: architectural fix for descender clipping (companion to ${SOURCE_REPO}#${SOURCE_PR_NUM})"
   (or similar — replace the descriptive part with what the actual fix is).
   Body should explain: WHAT the architectural bug was, WHERE the fix lives,
   and WHICH call-site PR triggered the diagnosis.
   Sign with:
     "Co-Authored-By: Jarvis (jupitermoney pilot) <jarvis-bot@jupitermoney.local>"

7. PUSH the branch:
     git push origin ${BRANCH}

8. OPEN A DRAFT PR using gh:
     gh pr create --repo jupitermoney/${UPSTREAM_REPO} \\
         --base ${DEFAULT_BRANCH} \\
         --head ${BRANCH} \\
         --draft \\
         --title "<descriptive title prefixed with fix:>" \\
         --body "\$(cat <<'BODYEOF'
**Companion PR — auto-drafted by Jarvis**

This is a DRAFT companion PR opened in response to call-site review feedback on ${SOURCE_PR_URL}.

A reviewer on the source PR identified that the architectural fix for the bug belongs in this design-system / library repo, not at the call-site. This PR contains Jarvis's first-pass attempt at that upstream fix.

**Recommended review focus:**
- Verify the fix is at the right layer (component / token / theme).
- Verify no regression to other consumers of the changed component / variant.
- Approve or request changes — this is intentionally OPEN AS DRAFT so the design-system team controls whether/when it merges.

**Source call-site PR for context:** ${SOURCE_PR_URL}

🤖 Jarvis (jupitermoney pilot) — companion-PR auto-flow
BODYEOF
)"

9. OUTPUT the PR URL on a NEW LINE, prefix EXACTLY:
     JARVIS_COMPANION_PR_DONE=<companion-pr-url>

HARD CONSTRAINTS — never violate:
- NEVER push to ${DEFAULT_BRANCH} directly — use the new branch ${BRANCH}.
- NEVER mark the PR as ready for review (must be draft).
- NEVER add reviewers programmatically — the design-system team will pick reviewers.
- NEVER touch .github/workflows/, build configs, package.json deps, or *.lock
  unless the architectural fix specifically requires it (and even then, flag in PR body).
- NEVER edit secrets / .env files.
- If you cannot identify the right file or write a clean fix in <=3 attempts,
  output: JARVIS_COMPANION_FAILED=<short reason>  and STOP. Don't open a bad PR.
EOFPROMPT

# --- Run Claude --------------------------------------------------------------
echo "[$(date +%H:%M:%S)] running Claude (budget \$$BUDGET_USD)..."
audit claude_started
LOG_FILE="$WORKSPACE/companion_claude_run.log"
START_EPOCH=$(date +%s)
CLAUDE_EXIT=0

if ! claude -p "$PROMPT" \
        --dangerously-skip-permissions \
        --output-format json \
        --model claude-sonnet-4-6 \
        --max-budget-usd "$BUDGET_USD" > "$LOG_FILE" 2>&1; then
    CLAUDE_EXIT=$?
fi
DURATION=$(($(date +%s) - START_EPOCH))

# --- Parse result ------------------------------------------------------------
COST_USD=$(python3 -c "
import json, sys
try:
    with open('$LOG_FILE') as f:
        data = json.load(f)
    print(data.get('total_cost_usd', 0))
except Exception:
    print(0)
" 2>/dev/null || echo "0")
RESULT_TEXT=$(python3 -c "
import json
try:
    with open('$LOG_FILE') as f:
        data = json.load(f)
    print(data.get('result', '') or data.get('text', ''))
except Exception:
    pass
" 2>/dev/null || echo "")

echo "[$(date +%H:%M:%S)] claude finished in ${DURATION}s, \$$COST_USD"

if [ "$CLAUDE_EXIT" -ne 0 ]; then
    audit claude_failed "\"exit_code\":$CLAUDE_EXIT,\"duration_sec\":$DURATION,\"cost_usd\":$COST_USD"
    echo "JARVIS_COMPANION_FAILED=claude exited $CLAUDE_EXIT"
    exit 4
fi

COMPANION_PR_URL=$(echo "$RESULT_TEXT" | grep -oE 'JARVIS_COMPANION_PR_DONE=https://github.com/jupitermoney/[^ ]+' | head -1 | cut -d= -f2-)
COMPANION_FAILED=$(echo "$RESULT_TEXT" | grep -oE 'JARVIS_COMPANION_FAILED=.+' | head -1 || true)

if [ -n "$COMPANION_FAILED" ]; then
    audit failed "\"duration_sec\":$DURATION,\"cost_usd\":$COST_USD,\"reason\":\"${COMPANION_FAILED}\""
    echo "$COMPANION_FAILED"
    exit 5
fi

if [ -z "$COMPANION_PR_URL" ]; then
    audit no_pr_url "\"duration_sec\":$DURATION,\"cost_usd\":$COST_USD"
    echo "JARVIS_COMPANION_FAILED=claude finished cleanly but did not output JARVIS_COMPANION_PR_DONE"
    exit 5
fi

audit success "\"duration_sec\":$DURATION,\"cost_usd\":$COST_USD,\"companion_pr_url\":\"$COMPANION_PR_URL\""
echo "[$(date +%H:%M:%S)] ✅ companion PR opened: $COMPANION_PR_URL"

# --- Cross-link comments (best-effort; non-fatal) ----------------------------
# Gated behind env so smoke tests can run end-to-end without posting visible
# cross-link comments. Default OFF — caller sets COMPANION_CROSSLINK_COMMENTS=1
# once they've reviewed the companion PR.
if [ "${COMPANION_CROSSLINK_COMMENTS:-0}" = "1" ]; then

echo "[$(date +%H:%M:%S)] cross-linking $SOURCE_PR_URL ↔ $COMPANION_PR_URL"

CROSSLINK_TO_SOURCE="Companion upstream PR opened: ${COMPANION_PR_URL}

Per reviewer guidance, the architectural fix for this bug lives in \`${UPSTREAM_REPO}\`, not at the call-site. The draft PR linked above contains ${BOT_NAME}'s first-pass attempt — design-system team owns review + merge timing.

Once that PR merges, the call-site workaround on this PR can be reverted in favour of using the fixed component directly.

🤖 ${BOT_NAME} (${GITHUB_ORG} pilot) — companion-PR auto-link"

CROSSLINK_TO_COMPANION="Companion to call-site review on ${SOURCE_PR_URL}

For context, that PR's reviewer flagged the workaround as belonging upstream — this draft is ${BOT_NAME}'s attempt at the architectural fix here.

🤖 ${BOT_NAME} (${GITHUB_ORG} pilot) — companion-PR auto-link"

gh api "repos/${GITHUB_ORG}/${SOURCE_REPO}/issues/${SOURCE_PR_NUM}/comments" \
    -f body="$CROSSLINK_TO_SOURCE" >/dev/null 2>&1 \
    && echo "  ✓ comment posted on source PR" \
    || echo "  ✗ failed to post comment on source PR"

COMPANION_PR_NUM=$(echo "$COMPANION_PR_URL" | sed -nE 's|.*/pull/([0-9]+)|\1|p')
if [ -n "$COMPANION_PR_NUM" ]; then
    gh api "repos/${GITHUB_ORG}/${UPSTREAM_REPO}/issues/${COMPANION_PR_NUM}/comments" \
        -f body="$CROSSLINK_TO_COMPANION" >/dev/null 2>&1 \
        && echo "  ✓ comment posted on companion PR" \
        || echo "  ✗ failed to post comment on companion PR"
fi

else
    echo "[$(date +%H:%M:%S)] cross-link comments skipped (set COMPANION_CROSSLINK_COMMENTS=1 to post)"
fi

echo ""
echo "✅ COMPANION DONE in ${DURATION}s, \$${COST_USD} of \$${BUDGET_USD} budget"
echo "ASTRA_COMPANION_PR_DONE=${COMPANION_PR_URL}"
echo "JARVIS_COMPANION_PR_DONE=${COMPANION_PR_URL}"
