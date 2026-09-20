#!/usr/bin/env bash
# Jarvis backfill-test runner — retro-add a regression test to an existing
# Jarvis-fired PR, on the SAME branch (no new PR).
#
#   jarvis_backfill_test.sh <repo> <pr_number> [--budget USD]
#
# What it does:
#   1. Fresh-clones the PR's head branch
#   2. Runs Claude with a regression-backfill prompt — Claude writes a failing
#      test, verifies it FAILS on HEAD~1 (pre-fix) and PASSES on HEAD (post-fix)
#   3. If test is valid, commits as `test: regression for <pr-title-slug>` and
#      pushes; if not, refuses with a skip reason
#   4. Emits machine-readable markers for the orchestrator:
#        JARVIS_BACKFILL_DONE=<sha>
#        JARVIS_BACKFILL_SKIPPED=<reason>
#        JARVIS_BACKFILL_FAILED=<reason>
#
# Audit log: ~/jarvis/logs/backfill_test_audit.jsonl
# Workspace: ~/jarvis/workspaces/backfill-<repo>-<pr>-<ts>/
#
# Exit codes: 0 success or clean skip, 2 bad args, 3 prereq failure, 4 claude failure
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
")

BOT_LOWER=$(echo "$BOT_NAME" | tr '[:upper:]' '[:lower:]')

REPO="${1:-}"; shift || true
PR_NUMBER="${1:-}"; shift || true

BUDGET_USD="1.50"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --budget) BUDGET_USD="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$REPO" || -z "$PR_NUMBER" ]]; then
    echo "usage: jarvis_backfill_test.sh <repo> <pr_number> [--budget USD]" >&2
    exit 2
fi

# Hard cap mirror of fix mode
if awk "BEGIN{exit !($BUDGET_USD > 5.00)}"; then
    echo "ERROR: --budget exceeds hard cap 5.00" >&2; exit 2
fi

# --- Identifiers ---
TS=$(date -u +%Y%m%d-%H%M%S)
SUFFIX=$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')
WORKSPACE="$ROOT_DIR/workspaces/backfill-${REPO}-${PR_NUMBER}-${TS}-${SUFFIX}"
AUDIT="$ROOT_DIR/logs/backfill_test_audit.jsonl"
LOG_FILE="${WORKSPACE}/claude_run.log"
mkdir -p "$WORKSPACE" "$(dirname "$AUDIT")"

audit() {
    local event="$1"
    local extra="${2:-}"
    local payload="{\"repo\":\"$REPO\",\"pr_number\":$PR_NUMBER,\"event\":\"$event\","
    payload+="\"workspace\":\"$WORKSPACE\","
    payload+="\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
    [[ -n "$extra" ]] && payload+=",${extra}"
    payload+="}"
    echo "$payload" >> "$AUDIT"
}

# --- Fetch PR metadata ---
echo "[$(date +%H:%M:%S)] backfill: repo=$REPO PR=#$PR_NUMBER budget=\$$BUDGET_USD"
META=$(gh pr view "$PR_NUMBER" --repo "jupitermoney/$REPO" \
    --json title,headRefName,body,headRefOid 2>/dev/null) || {
    echo "ERROR: could not fetch PR #$PR_NUMBER" >&2
    audit fetch_failed
    exit 3
}
BRANCH=$(echo "$META" | jq -r .headRefName)
TITLE=$(echo "$META" | jq -r .title)
HEAD_SHA=$(echo "$META" | jq -r .headRefOid)
BODY=$(echo "$META" | jq -r '.body // ""')

audit start "\"branch\":\"$BRANCH\",\"head_sha\":\"$HEAD_SHA\",\"title\":$(jq -Rs <<< "$TITLE")"
echo "[$(date +%H:%M:%S)] branch=$BRANCH head=$HEAD_SHA"

# Pre-check: if the PR already has a `test:` prefix commit, exit cheap (no claude run).
EXISTING_TEST_COMMIT=$(gh api "repos/jupitermoney/$REPO/pulls/$PR_NUMBER/commits" \
    --jq '.[] | select(.commit.message | startswith("test:")) | .sha' 2>/dev/null | head -1)
if [[ -n "$EXISTING_TEST_COMMIT" ]]; then
    audit already_has_test "\"existing_test_sha\":\"$EXISTING_TEST_COMMIT\""
    echo "  PR already has a test: commit ($EXISTING_TEST_COMMIT) — skipping cheap"
    echo "ASTRA_BACKFILL_ALREADY_HAS_TEST=$EXISTING_TEST_COMMIT"
    echo "JARVIS_BACKFILL_ALREADY_HAS_TEST=$EXISTING_TEST_COMMIT"
    exit 0
fi

# --- Clone + checkout the PR branch ---
cd "$WORKSPACE"
if ! gh repo clone "${GITHUB_ORG}/$REPO" "$REPO" -- --quiet 2>&1 | sed 's|^|  clone: |'; then
    audit clone_failed
    exit 3
fi
cd "$REPO"
git fetch origin "$BRANCH" --quiet 2>&1 | sed 's|^|  fetch: |' || true
git checkout "$BRANCH" --quiet 2>&1 | sed 's|^|  checkout: |' || {
    audit checkout_failed
    exit 3
}
git config user.email "${BOT_LOWER}-bot@${GITHUB_ORG}.local"
git config user.name "${BOT_NAME} Bot"

# Sanity: HEAD~1 must exist
if ! git rev-parse HEAD~1 >/dev/null 2>&1; then
    echo "  cannot diff HEAD~1 — branch has only one commit"
    audit skipped "\"reason\":\"single_commit_branch\""
    echo "ASTRA_BACKFILL_SKIPPED=single_commit_branch (cannot diff pre-fix state)"
    echo "JARVIS_BACKFILL_SKIPPED=single_commit_branch (cannot diff pre-fix state)"
    exit 0
fi

CHANGED_FILES=$(git diff --name-only HEAD~1..HEAD | head -20 | tr '\n' ' ')
echo "  files changed in the fix commit: $CHANGED_FILES"

# --- Build the regression-backfill prompt ---
PROMPT_FILE="${WORKSPACE}/backfill_prompt.txt"
cat > "$PROMPT_FILE" <<PROMPT
# Regression test backfill for an existing Jarvis-fired PR

You are working on **jupitermoney/${REPO} PR #${PR_NUMBER}**, which has already been opened by Jarvis with a fix for a real user bug. The fix is the most recent commit on this branch.

**Your job:** add a failing regression test on this same branch that catches the bug the fix addresses. DO NOT modify, refactor, or improve the fix. Only add a test file (or extend an existing test file) that proves the bug existed before this commit.

## PR context
- Title: ${TITLE}
- Head SHA: ${HEAD_SHA}
- Files changed in the fix commit: ${CHANGED_FILES}

## PR body (first 3KB)
${BODY:0:3000}

## How to proceed

1. Run \`git log --oneline -5\` and \`git show HEAD --stat\` to understand the fix commit.
2. Read the changed files at their CURRENT state (post-fix) using your file-read tool.
3. Read the same files at HEAD~1 (pre-fix) — use \`git show HEAD~1:<path>\` to see what was broken.
4. Identify the test framework used in this repo (look for \`jest.config.*\`, \`vitest.config.*\`, \`pytest\`, \`build.gradle\` with Spek/JUnit, etc.).
5. Write ONE small test that:
   - Asserts the buggy behavior is gone (PASSES on current HEAD, post-fix)
   - Would have caught the bug (FAILS if the fix is reverted to HEAD~1)
6. Run the test on the current state — verify it passes.
7. Verify it would have failed pre-fix: \`git stash\` your test, \`git checkout HEAD~1 -- <fix-files>\`, restore the test from stash, run the test, observe failure. Then \`git checkout HEAD -- <fix-files>\` to restore the fix.
8. Commit the test as \`test: regression for #${PR_NUMBER} — ${TITLE:0:60}\` and push.
9. Emit \`JARVIS_BACKFILL_DONE=\$(git rev-parse HEAD)\` on success.

## When to refuse (and how)

Emit \`JARVIS_BACKFILL_SKIPPED=<reason>\` and exit without committing if:
- The fix is a trivial textual change (typo, copy edit, dependency version bump) with no behavioral test path
- The fix is purely UI (visual layout, styling, image asset) with no automated test infrastructure
- The fix touches CI config / build YAML / .github files, not application code
- The repo has no test framework you can identify
- You cannot construct a meaningful failing test in the remaining budget

Be honest. A skip with reason is better than a tautological assertion test that doesn't actually catch anything.

## Budget guardrails

- Hard budget: \$$BUDGET_USD
- Don't refactor the fix
- Don't add docs / READMEs
- Don't run the full test suite — only the new test
- If the existing test file has 200+ tests, append yours; don't create a new file unless none exists
PROMPT

# --- Run Claude ---
echo "[$(date +%H:%M:%S)] running claude (budget=\$$BUDGET_USD)..."
audit claude_started
START_EPOCH=$(date +%s)
set +e
claude -p --max-budget-usd "$BUDGET_USD" \
    --dangerously-skip-permissions \
    --output-format json \
    --model claude-sonnet-4-6 \
    < "$PROMPT_FILE" > "$LOG_FILE" 2>&1
CLAUDE_RC=$?
set -e
DURATION=$(( $(date +%s) - START_EPOCH ))

if [[ $CLAUDE_RC -ne 0 ]]; then
    PARTIAL_COST=$(jq -r '.total_cost_usd // 0' "$LOG_FILE" 2>/dev/null || echo "0")
    echo "[$(date +%H:%M:%S)] claude exited non-zero ($CLAUDE_RC), partial cost \$${PARTIAL_COST}"
    audit claude_failed "\"rc\":$CLAUDE_RC,\"cost_usd\":${PARTIAL_COST}"
    echo "JARVIS_BACKFILL_FAILED=claude_rc=$CLAUDE_RC"
    exit 4
fi

COST_USD=$(jq -r '.total_cost_usd // 0' "$LOG_FILE" 2>/dev/null || echo "0")
AGENT_OUTPUT=$(jq -r '.result // ""' "$LOG_FILE" 2>/dev/null || cat "$LOG_FILE")

DONE_SHA=$(echo "$AGENT_OUTPUT" | grep -oE 'JARVIS_BACKFILL_DONE=[a-f0-9]+' | tail -1 | cut -d= -f2)
SKIP_REASON=$(echo "$AGENT_OUTPUT" | grep -oE 'JARVIS_BACKFILL_SKIPPED=.+' | tail -1 | cut -d= -f2-)
FAIL_REASON=$(echo "$AGENT_OUTPUT" | grep -oE 'JARVIS_BACKFILL_FAILED=.+' | tail -1 | cut -d= -f2-)

echo "[$(date +%H:%M:%S)] claude finished in ${DURATION}s, cost \$${COST_USD}"

if [[ -n "$DONE_SHA" ]]; then
    NEW_HEAD=$(git rev-parse HEAD)
    if [[ "$NEW_HEAD" == "$HEAD_SHA" ]]; then
        # Claude emitted DONE but didn't actually add a commit. Two cases:
        #   (a) Claude analyzed the PR and found the test was already there (race
        #       with the pre-check, or test commit message didn't start with "test:")
        #   (b) Claude wrote a test file but forgot to commit
        # Either way, NOT a failure — log it and exit clean.
        audit no_new_commit "\"cost_usd\":${COST_USD},\"note\":\"DONE marker emitted but HEAD unchanged — likely already has test or commit step skipped\""
        echo "⊘ NO-OP: claude said DONE but HEAD unchanged (likely already has equivalent test)"
        echo "ASTRA_BACKFILL_NO_OP=head_unchanged"
        echo "JARVIS_BACKFILL_NO_OP=head_unchanged"
        exit 0
    fi
    if ! git push origin "$BRANCH" 2>&1 | sed 's|^|  push: |'; then
        echo "  push failed"
        audit push_failed "\"cost_usd\":${COST_USD}"
        echo "ASTRA_BACKFILL_FAILED=push_failed"
        echo "JARVIS_BACKFILL_FAILED=push_failed"
        exit 4
    fi
    audit success "\"new_sha\":\"$NEW_HEAD\",\"duration_sec\":$DURATION,\"cost_usd\":${COST_USD}"
    echo "✅ DONE in ${DURATION}s, \$${COST_USD}"
    echo "ASTRA_BACKFILL_DONE=$NEW_HEAD"
    echo "JARVIS_BACKFILL_DONE=$NEW_HEAD"
    exit 0
elif [[ -n "$SKIP_REASON" ]]; then
    audit skipped "\"reason\":$(jq -Rs <<< "$SKIP_REASON"),\"duration_sec\":$DURATION,\"cost_usd\":${COST_USD}"
    echo "⊘ SKIPPED: $SKIP_REASON  (\$${COST_USD} sunk)"
    echo "ASTRA_BACKFILL_SKIPPED=$SKIP_REASON"
    echo "JARVIS_BACKFILL_SKIPPED=$SKIP_REASON"
    exit 0
elif [[ -n "$FAIL_REASON" ]]; then
    audit failed "\"reason\":$(jq -Rs <<< "$FAIL_REASON"),\"duration_sec\":$DURATION,\"cost_usd\":${COST_USD}"
    echo "❌ FAILED: $FAIL_REASON"
    echo "ASTRA_BACKFILL_FAILED=$FAIL_REASON"
    echo "JARVIS_BACKFILL_FAILED=$FAIL_REASON"
    exit 4
else
    audit unclear_outcome "\"duration_sec\":$DURATION,\"cost_usd\":${COST_USD}"
    echo "❌ FAILED: no clear outcome marker in claude output"
    echo "ASTRA_BACKFILL_FAILED=unclear_outcome"
    echo "JARVIS_BACKFILL_FAILED=unclear_outcome"
    tail -15 "$LOG_FILE" | sed 's|^|  log: |'
    exit 4
fi
