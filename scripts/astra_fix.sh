#!/usr/bin/env bash
# Jarvis fix runner — investigate + fix + open draft PR for a task.
#
#   jarvis_fix.sh <repo> "<task description>" [requester] \
#                 [--source slack|http_api] [--caller <name>] [--budget <usd>]
#
# Positional args (Slack flow, back-compat):
#   repo         must appear in JARVIS_WRITE_ALLOWED_REPOS (space/comma-separated)
#   task         free-form description; longer is fine (HTTP can send 3-4 KB)
#   requester    audit identifier (Slack user ID like "U... (Name)" or "cli")
#
# Optional flags (added for HTTP /api/v1/fix surface):
#   --source     audit field; "slack" (default) or "http_api"
#   --caller     audit field; free-form caller identity (e.g. "jove")
#   --budget     max Claude spend in USD; default 2.00, hard cap 5.00
#
# Safety:
# - Branch forced to jarvis/<task-slug>; PR opened as DRAFT
# - Audit log at ~/jarvis/logs/fix_audit.jsonl
# - Workspace at ~/jarvis/workspaces/<task-id>/
#
# Outputs on success:    JARVIS_PR_URL=https://github.com/jupitermoney/<repo>/pull/<n>
# Exit codes: 0 success, 2 not-allowed/bad-args, 3 prereq failure, 4 claude failure
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
print(f'WRITE_ALLOWED_REPOS=\"{get_env(\"WRITE_ALLOWED_REPOS\")}\"')
print(f'SKIP_BRIEF_GATE=\"{get_env(\"SKIP_BRIEF_GATE\")}\"')
")

BOT_LOWER=$(echo "$BOT_NAME" | tr '[:upper:]' '[:lower:]')

REPO="${1:-}"; shift || true
TASK="${1:-}"; shift || true

# Requester is positional arg 3 IF present AND not a flag
REQUESTER="cli"
if [[ $# -gt 0 && "$1" != --* ]]; then
    REQUESTER="$1"
    shift
fi

# Parse optional flags
SOURCE="slack"
CALLER=""
BUDGET_USD="2.00"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source) SOURCE="$2"; shift 2 ;;
        --caller) CALLER="$2"; shift 2 ;;
        --budget) BUDGET_USD="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$REPO" || -z "$TASK" ]]; then
    echo "usage: jarvis_fix.sh <repo> \"<task description>\" [requester] [--source ...] [--caller ...] [--budget ...]" >&2
    exit 2
fi

# Hard cap on budget (any source)
if awk "BEGIN{exit !($BUDGET_USD > 5.00)}"; then
    echo "ERROR: --budget $BUDGET_USD exceeds hard cap of 5.00" >&2
    exit 2
fi
if awk "BEGIN{exit !($BUDGET_USD <= 0)}"; then
    echo "ERROR: --budget must be positive" >&2
    exit 2
fi

# --- Allowlist check ---------------------------------------------------------
ALLOWED="${WRITE_ALLOWED_REPOS:-}"
ALLOWED_NORM=$(tr ',' ' ' <<< "$ALLOWED")
if ! grep -qw -- "$REPO" <<< " $ALLOWED_NORM "; then
    echo "ERROR: repo '$REPO' not in write allowlist." >&2
    echo "       Currently allowed: '${ALLOWED:-(none)}'" >&2
    echo "       Set the env var to allow specific repos for write access." >&2
    exit 2
fi


# --- Phase 3 (2026-05-19): multimodal + companion-PR env handling -----------
FIX_ATTACHMENTS_CSV="${FIX_ATTACHMENTS:-}"
FIX_COMPANION_MODE="${FIX_COMPANION_PR:-0}"
FIX_REGRESSION_MODE="${FIX_REGRESSION_TEST:-0}"
FIX_JIRA_TICKET_KEY="${FIX_JIRA_TICKET:-}"

# --- Jira auto-fetch (Phase B+, 2026-05-20) -----------------------------------
# When FIX_JIRA_TICKET=RECO-1259 is set, fetch the ticket's summary + description
# + media attachments from Atlassian and merge into TASK + FIX_ATTACHMENTS_CSV.
if [ -n "$FIX_JIRA_TICKET_KEY" ]; then
    echo "[$(date +%H:%M:%S)] fetching Jira ticket $FIX_JIRA_TICKET_KEY ..."
    JIRA_RESULT=$(python3 "$ROOT_DIR/scripts/jira_fetch.py" "$FIX_JIRA_TICKET_KEY" 2>&1) || true
    JIRA_OK=$(echo "$JIRA_RESULT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('ok', False))" 2>/dev/null || echo "False")
    # Status guard: refuse fast if the Jira ticket is already in a resolved/done state.
    # Avoids the misroute pattern caught by Chirag on jupiter#14239 (2026-06-08) and
    # Prasanna on jupiter#14248/#14251 — Jove fired PRs for tickets that were already
    # resolved or being fixed elsewhere. Catches the class without per-ticket triage.
    if [ "$JIRA_OK" = "True" ]; then
        JIRA_STATUS=$(echo "$JIRA_RESULT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('status',''))" 2>/dev/null || echo "")
        JIRA_STATUS_CAT=$(echo "$JIRA_RESULT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('status_category',''))" 2>/dev/null || echo "")
        # Jira status category "done" covers Done / Resolved / Closed / Won't Fix / etc.
        # Also pattern-match name for safety in case category is missing.
        case "$JIRA_STATUS_CAT" in done) IS_DONE=1 ;; *) IS_DONE=0 ;; esac
        case "$JIRA_STATUS" in 
            "Done"|"Resolved"|"Closed"|"Won't Fix"|"Won't Do"|"Duplicate"|"Cannot Reproduce") IS_DONE=1 ;;
        esac
        if [ "$IS_DONE" = "1" ]; then
            echo "[$(date +%H:%M:%S)] REFUSED: ticket $FIX_JIRA_TICKET_KEY is in resolved state \"$JIRA_STATUS\" (category=$JIRA_STATUS_CAT) — no fix needed"
            audit refused "\"reason\":\"ticket_already_resolved\",\"ticket\":\"$FIX_JIRA_TICKET_KEY\",\"status\":\"$JIRA_STATUS\""
            echo "JARVIS_FIX_REFUSED=ticket_already_resolved (jira status: $JIRA_STATUS, category: $JIRA_STATUS_CAT)"
            exit 0
        fi

        # Brief sufficiency gate (Haiku) — refuse fast when the Jira description
        # is too thin for the fix loop to attempt without guessing. Catches the
        # "engineers complain about junk PRs from underspecified tickets" class.
        # Override: set JARVIS_SKIP_BRIEF_GATE=1 to bypass (rare, intentional).
        # Fails OPEN — a Haiku outage cannot block production fixes.
        if [ -z "$JARVIS_SKIP_BRIEF_GATE" ]; then
            GATE_RESULT=$(echo "$JIRA_RESULT" | "$ROOT_DIR/scripts/indexer/.venv/bin/python" "$ROOT_DIR/scripts/agent/brief_gate.py" 2>/dev/null || echo '{"sufficient":true,"missing":[],"reason":"gate_invoke_error"}')
            GATE_SUFFICIENT=$(echo "$GATE_RESULT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('sufficient', True))" 2>/dev/null || echo "True")
            if [ "$GATE_SUFFICIENT" = "False" ]; then
                GATE_MISSING=$(echo "$GATE_RESULT" | python3 -c "import json,sys; print(', '.join(json.loads(sys.stdin.read()).get('missing', [])))" 2>/dev/null || echo "")
                GATE_REASON=$(echo "$GATE_RESULT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('reason',''))" 2>/dev/null || echo "")
                echo "[$(date +%H:%M:%S)] REFUSED: brief insufficient — $GATE_REASON"
                echo "  Missing: $GATE_MISSING"
                audit refused "\"reason\":\"insufficient_brief\",\"ticket\":\"$FIX_JIRA_TICKET_KEY\",\"missing\":\"$GATE_MISSING\""
                echo "JARVIS_FIX_REFUSED=insufficient_brief (missing: $GATE_MISSING)"
                # Phase 2: post a friendly Jira comment so the reporter knows what to add.
                # Best-effort — failures here never block the refusal flow.
                JIRA_COMMENT_RES=$(python3 "$ROOT_DIR/scripts/jira_comment.py" "$FIX_JIRA_TICKET_KEY" "$GATE_MISSING" "$GATE_REASON" 2>/dev/null || echo '{"ok":false}')
                JIRA_COMMENT_OK=$(echo "$JIRA_COMMENT_RES" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('ok', False))" 2>/dev/null || echo "False")
                JIRA_COMMENT_ID=$(echo "$JIRA_COMMENT_RES" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('comment_id',''))" 2>/dev/null || echo "")
                if [ "$JIRA_COMMENT_OK" = "True" ]; then
                    echo "  posted Jira comment ($JIRA_COMMENT_ID) explaining what is missing"
                    audit refused "\"jira_comment_posted\":true,\"comment_id\":\"$JIRA_COMMENT_ID\""
                fi
                exit 0
            fi
        fi
    fi
    if [ "$JIRA_OK" = "True" ]; then
        JIRA_SUMMARY=$(echo "$JIRA_RESULT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('summary',''))")
        JIRA_DESC=$(echo "$JIRA_RESULT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('description',''))")
        JIRA_ATTS=$(echo "$JIRA_RESULT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('attachments_csv',''))")
        JIRA_ATT_COUNT=$(echo "$JIRA_RESULT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('attachments_count',0))")
        if [ -n "$TASK" ]; then
            TASK="Jira ticket: ${FIX_JIRA_TICKET_KEY}
Summary: ${JIRA_SUMMARY}

${JIRA_DESC}

---
Additional context from caller:
${TASK}"
        else
            TASK="Jira ticket: ${FIX_JIRA_TICKET_KEY}
Summary: ${JIRA_SUMMARY}

${JIRA_DESC}"
        fi
        if [ -n "$JIRA_ATTS" ]; then
            if [ -n "$FIX_ATTACHMENTS_CSV" ]; then
                FIX_ATTACHMENTS_CSV="${FIX_ATTACHMENTS_CSV},${JIRA_ATTS}"
            else
                FIX_ATTACHMENTS_CSV="$JIRA_ATTS"
            fi
        fi
        echo "[$(date +%H:%M:%S)] jira fetch OK: '${JIRA_SUMMARY}' + ${JIRA_ATT_COUNT} media attachment(s) merged into TASK + FIX_ATTACHMENTS"
    else
        JIRA_ERR=$(echo "$JIRA_RESULT" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('error','unknown'))" 2>/dev/null || echo "parse-failed")
        echo "[$(date +%H:%M:%S)] WARN: jira fetch failed: ${JIRA_ERR} — proceeding with caller-supplied description only"
    fi
fi

# --- Identifiers + paths -----------------------------------------------------
TS=$(date -u +%Y%m%d-%H%M%S)
SLUG=$(tr -cs '[:alnum:]' '-' <<< "$TASK" | cut -c1-40 | tr -s '-' | sed 's/^-//;s/-$//' | tr '[:upper:]' '[:lower:]')
# Append a short random suffix so two requests with the same TS (second-resolution)
# and same task body cannot collide on workspace/branch names. Discovered 2026-05-18
# during idempotency-feature smoke test: two same-second same-body POSTs spawned
# bash subprocs with identical TASK_IDs; the second one's `mkdir -p` succeeded but
# `gh repo clone` failed when the workspace's nested repo dir was already present.
SUFFIX=$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')
TASK_ID="${TS}-${SLUG}-${SUFFIX}"
BRANCH="${BOT_LOWER}/${TASK_ID}"
WORKSPACE="$ROOT_DIR/workspaces/${TASK_ID}"
AUDIT="$ROOT_DIR/logs/fix_audit.jsonl"
mkdir -p "$WORKSPACE" "$(dirname "$AUDIT")"

audit() {
    local event="$1"
    local extra="${2:-}"
    local payload="{\"task_id\":\"$TASK_ID\",\"event\":\"$event\",\"repo\":\"$REPO\","
    payload+="\"requester\":\"$REQUESTER\",\"branch\":\"$BRANCH\","
    payload+="\"source\":\"$SOURCE\",\"budget_usd\":$BUDGET_USD,"
    [[ -n "$CALLER" ]] && payload+="\"caller\":\"$CALLER\","
    payload+="\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
    [[ -n "$extra" ]] && payload+=",${extra}"
    payload+="}"
    echo "$payload" >> "$AUDIT"
}

audit start "\"task\":$(jq -Rs <<< "$TASK")"

echo "[$(date +%H:%M:%S)] task_id=$TASK_ID  repo=$REPO  branch=$BRANCH  source=$SOURCE  budget=\$$BUDGET_USD"
echo "[$(date +%H:%M:%S)] cloning $REPO into $WORKSPACE..."
cd "$WORKSPACE"
if ! gh repo clone "jupitermoney/$REPO" 2>&1 | sed 's|^|  clone: |'; then
    audit clone_failed
    exit 3
fi
cd "$REPO"

# Configure git identity (gh auth handles credentials, but commits need an author)
git config user.email "${BOT_LOWER}-bot@${GITHUB_ORG}.local"
git config user.name "${BOT_NAME} Bot"


# --- Multimodal extraction (Phase 3, 2026-05-19) ----------------------------
IMG_DIR="$WORKSPACE/attachments"
mkdir -p "$IMG_DIR"
GH_TOKEN_RAW=$(gh auth token 2>/dev/null || echo "")
IMG_COUNT=0
IMG_PATHS=()
FRAMES_PER_VIDEO_CAP=${FIX_FRAMES_PER_VIDEO:-10}

if [ -n "$FIX_ATTACHMENTS_CSV" ]; then
    echo "[$(date +%H:%M:%S)] processing $(echo "$FIX_ATTACHMENTS_CSV" | tr ',' '\n' | wc -l) attachment URL(s)"
    for url in $(echo "$FIX_ATTACHMENTS_CSV" | tr ',' '\n' | grep -v '^$'); do
        IMG_COUNT=$((IMG_COUNT + 1))
        out="$IMG_DIR/raw_${IMG_COUNT}.bin"
        AUTH_ARGS=()
        case "$url" in
            *atlassian.net*)
                if [ -n "${CONFLUENCE_EMAIL:-}" ] && [ -n "${CONFLUENCE_API_TOKEN:-}" ]; then
                    AUTH_ARGS=("-u" "$CONFLUENCE_EMAIL:$CONFLUENCE_API_TOKEN")
                fi
                ;;
            *github.com*|*githubusercontent.com*)
                if [ -n "$GH_TOKEN_RAW" ]; then
                    AUTH_ARGS=("-H" "Authorization: token $GH_TOKEN_RAW")
                fi
                ;;
        esac
        if ! curl -sSL -L -A "Mozilla/5.0" "${AUTH_ARGS[@]}" "$url" -o "$out" 2>/dev/null; then
            echo "[$(date +%H:%M:%S)] fetch FAILED: $url"
            rm -f "$out"; continue
        fi
        SIZE=$(stat -c "%s" "$out" 2>/dev/null || echo 0)
        if [ "$SIZE" -le 1024 ]; then
            echo "[$(date +%H:%M:%S)] skipped (too small, $SIZE bytes): $url"
            rm -f "$out"; continue
        fi
        FILE_DESC=$(file -b "$out")
        case "$FILE_DESC" in
            *PNG*|*JPEG*|*GIF*|*image*)
                ext="png"
                case "$FILE_DESC" in *JPEG*) ext="jpg" ;; *GIF*) ext="gif" ;; esac
                mv "$out" "$IMG_DIR/img_${IMG_COUNT}.${ext}"
                IMG_PATHS+=("$IMG_DIR/img_${IMG_COUNT}.${ext}")
                echo "[$(date +%H:%M:%S)] image attached (#${IMG_COUNT}, $SIZE bytes): $url"
                ;;
            *MP4*|*ISO\ Media*|*QuickTime*|*WebM*|*Matroska*|*video*)
                DUR=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$out" 2>/dev/null || echo "0")
                DUR_INT=${DUR%.*}
                [ -z "$DUR_INT" ] && DUR_INT=0
                if [ "$DUR_INT" -le 0 ]; then
                    echo "[$(date +%H:%M:%S)] video has no probe-able duration, skipping: $url"
                    rm -f "$out"; continue
                fi
                FPS=$(awk "BEGIN { printf \"%.4f\", $FRAMES_PER_VIDEO_CAP / $DUR }")
                VID_FRAME_DIR="$IMG_DIR/video_${IMG_COUNT}_frames"
                mkdir -p "$VID_FRAME_DIR"
                if ffmpeg -nostdin -v error -i "$out" -vf "fps=${FPS}" -frames:v "$FRAMES_PER_VIDEO_CAP" \
                    "$VID_FRAME_DIR/frame_%03d.png" 2>&1 | sed 's|^|  ffmpeg: |'; then
                    for f in "$VID_FRAME_DIR"/frame_*.png; do
                        [ -f "$f" ] && IMG_PATHS+=("$f")
                    done
                    echo "[$(date +%H:%M:%S)] video attached (#${IMG_COUNT}, ${SIZE} bytes, ${DUR}s) -> ${#IMG_PATHS[@]} keyframes (cumulative): $url"
                    rm -f "$out"
                else
                    echo "[$(date +%H:%M:%S)] ffmpeg failed for: $url"; rm -f "$out"
                fi
                ;;
            *)
                echo "[$(date +%H:%M:%S)] skipped (unrecognized type): $url"; rm -f "$out"
                ;;
        esac
    done
fi
echo "[$(date +%H:%M:%S)] total content blocks for Claude: ${#IMG_PATHS[@]} (images + extracted video keyframes)"

# Companion-aware prompt block
if [ "$FIX_COMPANION_MODE" = "1" ]; then
    COMPANION_PROMPT_BLOCK=$(cat <<'CMPEOF'

===== COMPANION MODE — IMPORTANT =====

After you open the fix's call-site PR, evaluate whether the architectural
root cause lives in an upstream / library / design-system repo (not just
at the call-site). Symptoms that suggest yes: the bug would resurface at
any other caller of the same component; the call-site fix is a workaround
for a library prop combination; you had to hand-roll a replacement for a
design-system component.

If yes:
1. Write a diagnosis markdown file to ${WORKSPACE_PLACEHOLDER}/companion_diagnosis.md
   containing: WHAT the bug is, WHERE the architectural fix belongs (file,
   component, line in the upstream repo), WHY the call-site workaround is
   incomplete, HOW the fix should be implemented (concrete code change).
2. Output an ADDITIONAL line on stdout (in ADDITION to JARVIS_PR_URL):
     JARVIS_NEEDS_COMPANION=<upstream-repo-short-name>
3. The fix's call-site PR remains open; a companion draft PR will be
   opened automatically in the upstream repo.

If the bug is purely at the call-site, do not emit the marker — proceed normally.

===== END COMPANION MODE =====

CMPEOF
)
    COMPANION_PROMPT_BLOCK="${COMPANION_PROMPT_BLOCK//\${WORKSPACE_PLACEHOLDER}/$WORKSPACE}"
else
    COMPANION_PROMPT_BLOCK=""
fi

# Phase B (regression-capture mode) prompt block — set REGRESSION_PROMPT_BLOCK
if [ "$FIX_REGRESSION_MODE" = "1" ]; then
    REGRESSION_PROMPT_BLOCK=$(cat << 'REGEOF'
===== REGRESSION-CAPTURE MODE — MANDATORY WORKFLOW =====

This run is in TDD / regression-capture mode. Instead of "write fix, open PR",
the workflow is:

(1) READ the bug description above. Extract a <bug-id> from any explicit ticket
    identifier (RECO-1259, JIRA-style) or use a short slug from the description.

(2) LOCATE the right test file. Conventions per language:
      - TypeScript / RN: nearest __tests__/<Component>.test.ts(x) or .spec.ts
      - Kotlin: src/test/kotlin/... mirroring the main path
      - Python: tests/test_<module>.py mirroring the main path

    If NO test infrastructure exists for the affected module (no test runner
    installed, no sibling test files, no test config), DO NOT REFUSE. Instead:
      (a) SKIP steps 3-5 (the failing-test-first flow). Proceed straight to
          step 6 (write the implementation fix).
      (b) When you reach step 9 (PR body), ADD a clearly-labelled section:

            ## :warning: Regression test NOT added
            The `<module-name>` module has no test infrastructure (no
            `@testing-library/react-native` / no jest config / no sibling
            test files). This PR ships the fix WITHOUT a regression test
            for the bug. Follow-up to be tracked separately: either add
            test infrastructure to this module, or capture the regression
            at an integration level.

      (c) Commit the fix as ONE commit (not two — no separate test commit).
      (d) PROCEED with the rest of the workflow as normal (push, open PR, etc).

    DO NOT skip the test step for any other reason — only when the test
    infrastructure is genuinely missing. If infrastructure exists but you
    just don't know how to use it, STOP and ask for help with
    JARVIS_FIX_REFUSED=test_runner_unknown instead.

(3) WRITE A FAILING TEST that reproduces the bug. The test must match the exact
    symptom + use the project's existing test conventions + assert something that
    FAILS on the current (unpatched) code.

(4) RUN THE TEST. Use the project's test runner (yarn jest <path>, ./gradlew
    :module:test --tests "<class>", pytest <path>, etc.). The test MUST FAIL
    with the bug symptom. Capture the failure output.
    If the test PASSES on unpatched code, output:
        JARVIS_FIX_REFUSED=test_not_failing_before_fix
    and STOP.

(5) COMMIT the test ALONE as the FIRST commit:
        Subject: "test: add failing regression for <bug-id>"
        Body: brief description of what the test exercises + the failure output
              captured in step 4 (as evidence the test does catch the bug)

(6) NOW WRITE THE IMPLEMENTATION FIX. Same root-cause / minimal-change rules as
    the rest of this prompt apply.

(7) RUN THE TEST AGAIN. The test from step 3 MUST now PASS. Run any other tests
    in the same file/module too (don't regress).
    If the test STILL FAILS, output:
        JARVIS_FIX_REFUSED=test_not_passing_after_fix
    and STOP. Do NOT open the PR.

(8) COMMIT the implementation fix as the SECOND commit (separate from the test):
        Subject: "fix: <root-cause description>"
        Body: explains the root cause + cites the failing test (HEAD~1) as
              evidence the fix is correctly scoped

(9) OPEN THE DRAFT PR explaining: the bug, the failing test (HEAD~1), the fix
    (HEAD), and that reviewers can verify by checking out HEAD~1 (test fails)
    then HEAD (test passes).

CRITICAL: do NOT skip steps 4 or 7 — running the tests is what makes this a
real regression-capture vs a hopeful guess. Both verifications MUST pass.

===== END REGRESSION-CAPTURE MODE =====
REGEOF
)
else
    REGRESSION_PROMPT_BLOCK=""
fi

# Soft Guard-A: visual-bug + no-attachments nudge (prompt-only, not a hard refuse)
if [ "${#IMG_PATHS[@]}" -eq 0 ]; then
    SOFT_GUARD_A_BLOCK=$(cat <<'GAEOF'

===== ATTACHMENT NOTICE =====

You received ZERO image / video attachments with this brief. If the brief
describes a VISUAL bug (component layout, animation, gesture, keyboard
interaction, anything where what the user SEES matters), and the textual
description alone is insufficient to determine the root cause WITHOUT
running the app, output:
    JARVIS_FIX_REFUSED=insufficient_evidence
and STOP. Code-only reasoning on visual bugs produces confidently-wrong
fixes (cf. PR #14140 RECO-1259: 3 text-only iterations all wrong; the
fix only landed once image+video were attached).

If the bug is backend / API / data-shape / business-logic, ignore this
notice and proceed normally — text-only is fine for those.

===== END ATTACHMENT NOTICE =====

GAEOF
)
else
    SOFT_GUARD_A_BLOCK=""
fi

# --- Build Claude prompt -----------------------------------------------------
read -r -d '' PROMPT <<EOF || true
${COMPANION_PROMPT_BLOCK}${SOFT_GUARD_A_BLOCK}${REGRESSION_PROMPT_BLOCK}You are Jarvis, an automated AI code-fix engineer running headless. You are
inside a clean checkout of the $REPO repo (current working directory).

TASK FROM A HUMAN ENGINEER:
${TASK}

(Requested by: ${REQUESTER})

MANDATORY WORKFLOW — follow in order:

1. INVESTIGATE the codebase to understand what needs to change.
   - Use rg/grep to search, read files, check git log if relevant.
   - If the task is too vague to act on safely, output:
     JARVIS_FIX_FAILED=task too vague: <what specifically would unblock you>
     and stop.

2. FIND RELATED TESTS for any file you plan to modify. Tests encode invariants
   the PRD often doesn't mention — missing them is the #1 cause of "looks right
   but breaks something subtle" fixes.
   - Look in obvious locations: src/test/, __tests__/, tests/, test/,
     <file>Test.kt, <file>.test.ts, test_<file>.py.
   - Use rg to find tests that import or reference the symbol(s) you'll change:
     rg -l "<class or function name>" --type-add 'test:*Test*' -t test
   - READ those tests. Note what behaviors / invariants they enforce.

3. STATE-MACHINE / ENUM AWARENESS: if the file you're changing references
   state enums (LoanState, KycState, ApplicationState, etc.), find the enum
   definition (look in domain/, enums/, models/, state/ directories) and read
   it. Note any state-transition logic. Don't change a state name or order
   without understanding the transitions it participates in.

4. ROOT CAUSE: identify the architecturally-correct fix, not the smallest patch.
   - Default: fix at the source — even if it means a larger diff, touching more
     files, or proposing a fix in an upstream library/repo.
   - Common anti-pattern to AVOID: a call-site workaround when the real bug is
     in a design-system component (e.g. patching one Button consumer instead of
     fixing the Button's tertiary variant in @jupitermoney/sense-ui).
   - If the architecturally-correct fix requires touching a DIFFERENT repo than
     the one you're in (e.g. you're in jupiter but the fix belongs in
     jupiter-design-system), say so EXPLICITLY in the PR body: "this is a
     partial fix at the call-site; the architectural fix is a follow-up PR in
     <other-repo>". Do not silently ship a workaround as if it were the real fix.
   - The ONLY time to default to a call-site workaround is when the task
     description explicitly says "hotfix" / "interim" / "patch for now". In that
     case, ship the workaround AND name the followup ticket required for the
     real fix.

5. MAKE THE CHANGE in the cleanest way possible.

6. VALIDATE if you can:
   - Look for and run obvious linters / type checks for this stack
     (e.g. \`npm run lint\`, \`tsc --noEmit\`, \`gradle ktlintCheck\`,
     \`./gradlew compileKotlin\`).
   - RUN UNIT TESTS for the touched modules — not the full suite (too slow),
     just the tests that exercise the files you changed:
       - JS/TS:  \`yarn jest <touched-test-file>\` or \`yarn test <touched-module>\`
       - Kotlin: \`./gradlew :<touched-module>:test --tests "*<RelevantClass>*"\`
       - Python: \`pytest path/to/touched/tests/test_x.py -x\`
     Look for the actual test runner the project uses (check package.json
     scripts, build.gradle, pyproject.toml) — match its conventions.
   - If a test fails: fix the cause (either the test, if you misunderstood
     intended behaviour, OR the code, if you broke something). Re-run.
     Max 3 fix-and-re-run rounds before you stop.
   - If validation fails persistently AND you cannot determine cause, output:
     JARVIS_FIX_FAILED=tests failing after 3 rounds: <one-line summary>
     and STOP. Don't push code that fails its own tests.

7. SELF-REVIEW: re-read your full diff (\`git diff\` for unstaged + staged
   changes) AS IF you were a senior reviewer who will reject sloppy work.
   You are looking for things that an automated standards-bot or human
   reviewer will flag. Hunt for:
   - **Hardcoded design values** where the project's design system has
     a token. Hex colors ('#FFFFFF', '#E8E8E8'), magic numbers for
     borderRadius / lineHeight / spacing / font-size when named constants
     exist. For Jupiter mobile/web (jupitermoney/jupiter and friends),
     tokens are exposed via \`@jupitermoney/sense-ui\` (source repo:
     \`jupitermoney/jupiter-design-system\`). The canonical palette lives at:
       packages/design-system/src/theme/index.ts
     which exports structured tokens — examples (not exhaustive):
       colors.lightBgPrimaryA          (was '#FFFFFF')
       colors.lightBorderPrimary       (was '#E8E8E8' / '#EEE')
       colors.lightContentPrimary      (text default)
       colors.lightContentAccent       (text accent)
       colors.lightBgAccentPrimary     (accent backgrounds)
       colors.lightBgSuccessPrimary / lightBgErrorPrimary
     Access in a component:
       import { useTheme } from '@shopify/restyle';
       import type { Theme } from '@jupitermoney/sense-ui';
       const { colors, borderRadii, spacing } = useTheme<Theme>();
       ...style={{ backgroundColor: colors.lightBgPrimaryA }}
     If you wrote a hex literal or magic number for color/border/spacing:
       1. \`rg "lightBg|lightBorder|lightContent|borderRadii|spacing" <touched-file>\`
          OR check sibling components in the same directory for the convention.
       2. If you cannot find a matching token after a reasonable search,
          state that explicitly in the PR body — don't silently ship a
          hardcode and hope the reviewer doesn't notice.
   - **Leftover debug artefacts**: \`console.log\`, \`console.error\` (unless
     part of legitimate logging), \`debugger;\` statements, \`// TODO\` comments
     that should be addressed before merge or converted to tracking-issue
     references.
   - **Nested ternaries more than one level deep** — extract to a named
     variable or helper function. Three levels is always too many.
   - **Inline arrow handlers with >1 statement** — extract to named handlers
     (\`const handleX = () => { ... }\`). Easier to read, easier to test.
   - **Magic numbers** (raw integers in code) where named constants would
     be clearer. \`setTimeout(..., 250)\` → why 250? Make it a constant.
   - **Wrong directory layer**: mutations / API-write hooks in \`hooks/\`
     when project convention is \`services/\`. Stateful screens in
     \`components/\` instead of \`screens/\`. Check sibling files for the
     project's actual convention.
   - **Missing tests** for any new exported function / component you added.
   - **Repeated logic / dead code paths** in your own diff.
   - **Workaround at wrong layer**: ask "if I shipped this and the bug
     resurfaced elsewhere tomorrow, would I have to patch each new call-site
     again?" If yes, the fix is at the wrong layer — go upstream. Examples:
     patching one consumer of a buggy design-system component instead of
     fixing the component; adding a guard at one call-site instead of fixing
     the underlying state machine; band-aiding around a buggy library prop
     instead of using the right prop combination at source.

   If you find issues, fix them, re-run VALIDATE (incl. tests), then
   re-self-review. Up to 2 self-review passes before you ship. Don't ship
   something a reviewer is going to reject in 30 seconds — fix it now.

8. CREATE BRANCH (this exact name, do not deviate):
   git checkout -b $BRANCH

9. COMMIT with a clear conventional-commits subject + 1-3 sentence body
   explaining the WHY. Sign with:
   "Co-Authored-By: Jarvis (jupitermoney pilot) <jarvis-bot@jupitermoney.local>"

10. PUSH:
   git push -u origin $BRANCH

11. OPEN DRAFT PR via gh (mandatory --draft flag):

    FIRST: check if this repo has its own PR template. GitHub supports the
    template at the REPO ROOT, in docs/, or in .github/, with case-insensitive
    filename. Use this exact command (case-insensitive, .md or .txt):

      find . -maxdepth 3 \\
        \\( -ipath "./pull_request_template*" \\
           -o -ipath "./.github/pull_request_template*" \\
           -o -ipath "./.github/PULL_REQUEST_TEMPLATE/*" \\
           -o -ipath "./docs/pull_request_template*" \\
        \\) -type f 2>/dev/null

    The template is the FIRST match. If the search returns multiple files
    (e.g. .github/PULL_REQUEST_TEMPLATE/ has many), prefer the most generic
    one (named "default", "feature", or simply pick the shortest filename).

    IF a template is found, USE IT:
      - Fill in every section of the template based on the change you made.
      - Leave nothing as placeholder text (no "TODO" or "<fill this in>").
      - If the template asks for a JIRA ticket / task ID and the user gave
        none, write "N/A (Jarvis-opened — no linked ticket)".
      - Keep the template's structure / headings intact so reviewers see
        familiar formatting.
      - APPEND the Jarvis self-audit block below at the end of the body
        (after the template content).

    IF no template found, use this default body structure:
      ## Problem
      <1-3 sentence description of what was wrong>

      ## Fix
      <what you changed, file by file>

      ## Test plan
      <how a reviewer can verify this works>

      ## Risk
      <LOW/MEDIUM/HIGH + 1 sentence on side effects>

    EITHER WAY, append this Jarvis self-audit block at the very end of the body:

      ---
      ### 🤖 Jarvis self-audit
      *Invariants checked:* for each test file read in step 2, name it and the
      invariant it enforces, marked *PRESERVED* or *INTENTIONALLY RELAXED* (with
      reasoning). If state enums from step 3 are relevant, list them too.

      *Caveats:* what Jarvis could NOT verify (e.g. "couldn't run integration
      test suite", "behavior under high load not validated").

      *Opened by Jarvis (Phase 2 pilot)*. Requested by: ${REQUESTER}.

    Compose the full body, write it to a temp file (gh handles multi-line via
    --body-file), and run:
      gh pr create --draft \\
        --title "<imperative subject under 72 chars>" \\
        --body-file <path-to-temp-body-file>

12. OUTPUT THE PR URL on a NEW LINE prefixed exactly:
    JARVIS_PR_URL=<url>

HARD CONSTRAINTS — never violate:
- NEVER commit on main, NEVER push to main, NEVER force-push
- NEVER delete files
- NEVER touch .github/workflows/, build.gradle, package.json's deps section,
  or *.lock files unless the task explicitly requires it
- NEVER add new external dependencies
- NEVER edit secrets or .env files

If you cannot complete safely, output:
   JARVIS_FIX_FAILED=<short reason>
and STOP. Don't half-do it.
EOF

# --- Run Claude -------------------------------------------------------------
echo "[$(date +%H:%M:%S)] running Claude Code (max budget \$$BUDGET_USD)..."
audit claude_started
LOG_FILE="$WORKSPACE/claude_run.log"
START_EPOCH=$(date +%s)

if [ "${#IMG_PATHS[@]}" -gt 0 ]; then
    PROMPT_TEXT_FILE="$WORKSPACE/prompt.txt"
    printf '%s' "$PROMPT" > "$PROMPT_TEXT_FILE"
    INPUT_FILE="$WORKSPACE/claude_input.jsonl"
    python3 - "$PROMPT_TEXT_FILE" "$INPUT_FILE" "${IMG_PATHS[@]}" <<'PYSCRIPT' 2>>"$LOG_FILE"
import base64, json, mimetypes, sys
prompt_file, input_file, *img_paths = sys.argv[1:]
with open(prompt_file) as f:
    prompt = f.read()
content = [{"type": "text", "text": prompt}]
for ip in img_paths:
    mime, _ = mimetypes.guess_type(ip)
    if not mime: mime = "image/png"
    with open(ip, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
msg = {"type": "user", "message": {"role": "user", "content": content}}
with open(input_file, "w") as f:
    f.write(json.dumps(msg) + "\n")
print(f"  stream-json input: {len(content)} content blocks (1 text + {len(content)-1} image)", file=sys.stderr)
PYSCRIPT
    CLAUDE_RC=0
    if ! claude -p --verbose \
            --max-budget-usd "$BUDGET_USD" \
            --dangerously-skip-permissions \
            --input-format stream-json \
            --output-format stream-json \
            --model claude-sonnet-4-6 \
            < "$INPUT_FILE" > "$LOG_FILE" 2>&1; then
        CLAUDE_RC=$?
    fi
    if [ "$CLAUDE_RC" -ne 0 ]; then
        EXIT_CODE=$CLAUDE_RC
        PARTIAL_COST=$(jq -r '.total_cost_usd // 0' "$LOG_FILE" 2>/dev/null || echo "0")
        audit claude_exited "\"exit_code\":$EXIT_CODE,\"cost_usd\":${PARTIAL_COST:-0},\"images_in\":${#IMG_PATHS[@]}"
        echo "[$(date +%H:%M:%S)] claude (multimodal) exited non-zero ($EXIT_CODE), partial cost \$$PARTIAL_COST. Last lines:"
        tail -30 "$LOG_FILE" | sed 's|^|  |'
        exit 4
    fi
elif ! claude -p "$PROMPT" \
        --max-budget-usd "$BUDGET_USD" \
        --dangerously-skip-permissions \
        --output-format json \
        --model claude-sonnet-4-6 \
        > "$LOG_FILE" 2>&1; then
    EXIT_CODE=$?
    # Best-effort cost extraction even on non-zero exit (budget cap, etc.)
    PARTIAL_COST=$(jq -r '.total_cost_usd // 0' "$LOG_FILE" 2>/dev/null || echo "0")
    audit claude_exited "\"exit_code\":$EXIT_CODE,\"cost_usd\":${PARTIAL_COST:-0}"
    echo "[$(date +%H:%M:%S)] claude exited non-zero ($EXIT_CODE), partial cost \$$PARTIAL_COST. Last lines:"
    tail -30 "$LOG_FILE" | sed 's|^|  |'
    exit 4
fi

DURATION=$(( $(date +%s) - START_EPOCH ))
echo "[$(date +%H:%M:%S)] claude finished in ${DURATION}s"

# --- Parse outcome ----------------------------------------------------------
# Two output formats in play:
#   - text-only path: --output-format json → single JSON envelope, extract via jq -r '.total_cost_usd'
#   - multimodal path: --output-format stream-json → JSONL events, slurp + filter for the final result
if [ "${#IMG_PATHS[@]}" -gt 0 ]; then
    COST_USD=$(jq -rs '[.[] | select(.type == "result") | .total_cost_usd // 0] | .[-1] // 0' "$LOG_FILE" 2>/dev/null || echo "0")
    AGENT_OUTPUT=$(jq -rs '[.[] | select(.type == "assistant") | .message.content[]? | select(.type == "text") | .text] | join("\n")' "$LOG_FILE" 2>/dev/null || cat "$LOG_FILE")
else
    COST_USD=$(jq -r '.total_cost_usd // 0' "$LOG_FILE" 2>/dev/null || echo "0")
    AGENT_OUTPUT=$(jq -r '.result // ""' "$LOG_FILE" 2>/dev/null || cat "$LOG_FILE")
fi
PR_URL=$(echo "$AGENT_OUTPUT" | grep -oE 'JARVIS_PR_URL=https?://[^ ]+' | tail -1 | cut -d= -f2-)
FAIL_REASON=$(echo "$AGENT_OUTPUT" | grep -oE 'JARVIS_FIX_FAILED=.+' | tail -1 | cut -d= -f2-)
REFUSE_REASON=$(echo "$AGENT_OUTPUT" | grep -oE 'JARVIS_FIX_REFUSED=[a-zA-Z0-9_]+' | tail -1 | cut -d= -f2-)

echo "[$(date +%H:%M:%S)] cost: \$$COST_USD  (budget: \$$BUDGET_USD)"

if [[ -n "$PR_URL" ]]; then
    audit success "\"pr_url\":\"$PR_URL\",\"duration_sec\":$DURATION,\"cost_usd\":${COST_USD:-0},\"images_in\":${#IMG_PATHS[@]}"
    echo ""
    echo "✅ DONE in ${DURATION}s, \$${COST_USD} of \$${BUDGET_USD} budget"
    echo "ASTRA_PR_URL=$PR_URL"
    echo "JARVIS_PR_URL=$PR_URL"

    if [ "$FIX_COMPANION_MODE" = "1" ]; then
        NEEDS_UPSTREAM=$(echo "$AGENT_OUTPUT" | grep -oE 'JARVIS_NEEDS_COMPANION=[a-zA-Z0-9_-]+' | head -1 | cut -d= -f2)
        COMPANION_DIAG="$WORKSPACE/companion_diagnosis.md"
        if [ -n "$NEEDS_UPSTREAM" ] && [ -f "$COMPANION_DIAG" ]; then
            audit needs_companion "\"upstream_repo\":\"$NEEDS_UPSTREAM\",\"source_pr\":\"$PR_URL\""
            echo "[$(date +%H:%M:%S)] companion mode: Claude requested companion PR in $NEEDS_UPSTREAM"
            COMPANION_OUT="$WORKSPACE/companion_run.log"
            if "$ROOT_DIR/scripts/astra_companion_pr.sh" \
                    --source-pr "$PR_URL" \
                    --upstream-repo "$NEEDS_UPSTREAM" \
                    --diagnosis "$COMPANION_DIAG" \
                    --caller "fix-auto-${CALLER:-${REQUESTER:-unknown}}" \
                    --budget "${COMPANION_BUDGET_USD:-2.00}" 2>&1 | tee "$COMPANION_OUT"; then
                COMPANION_URL=$(grep -oE 'JARVIS_COMPANION_PR_DONE=https://[^ ]+' "$COMPANION_OUT" | tail -1 | cut -d= -f2-)
                audit companion_done "\"companion_pr_url\":\"$COMPANION_URL\""
                echo "ASTRA_COMPANION_PR_URL=$COMPANION_URL"
                echo "JARVIS_COMPANION_PR_URL=$COMPANION_URL"
            else
                COMPANION_EXIT=$?
                audit companion_failed "\"exit_code\":$COMPANION_EXIT"
                echo "[$(date +%H:%M:%S)] companion spawn failed (exit $COMPANION_EXIT) — fix PR is still good; companion is best-effort"
            fi
        elif [ -n "$NEEDS_UPSTREAM" ]; then
            echo "[$(date +%H:%M:%S)] companion marker found but no diagnosis file at $COMPANION_DIAG — skipping companion"
        fi
    fi
    exit 0
elif [[ -n "$REFUSE_REASON" ]]; then
    # Phase B refusal codes (no_test_infrastructure, test_not_failing_before_fix, etc.)
    # are GRACEFUL — distinct from real failures. Exit 7 so callers (jobs.py, Jove)
    # can disambiguate "agent decided not to ship" from "agent crashed mid-run".
    audit refused "\"reason\":\"$REFUSE_REASON\",\"duration_sec\":$DURATION,\"cost_usd\":${COST_USD:-0}"
    echo ""
    echo "✋ Refused (graceful): $REFUSE_REASON  (cost \$$COST_USD)"
    echo "ASTRA_FIX_REFUSED=$REFUSE_REASON"
    echo "JARVIS_FIX_REFUSED=$REFUSE_REASON"
    exit 7
elif [[ -n "$FAIL_REASON" ]]; then
    audit failed "\"reason\":$(jq -Rs <<< "$FAIL_REASON"),\"duration_sec\":$DURATION,\"cost_usd\":${COST_USD:-0}"
    echo ""
    echo "⚠️  Claude reported failure: $FAIL_REASON  (cost \$$COST_USD)"
    exit 1
else
    audit unclear "\"duration_sec\":$DURATION,\"cost_usd\":${COST_USD:-0}"
    echo ""
    echo "⚠️  No PR URL or failure marker in output (cost \$$COST_USD). Last lines:"
    tail -20 "$LOG_FILE" | sed 's|^|  |'
    exit 1
fi
