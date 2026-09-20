#!/usr/bin/env bash
# Jarvis iterate runner — address review comments on a Jarvis-opened PR by
# running Claude on the existing workspace + branch, then pushing back.
#
#   jarvis_iterate.sh <repo> <pr_number> [requester] \
#                     [--source slack|http_api] [--caller <name>] [--budget <usd>]
#
# Behaviour:
# - Fetches PR details + review comments + reviews via gh api
# - Locates the workspace dir for the PR's branch (or fresh-clones if GC'd)
# - Checks out the branch (resets hard to remote in case of stale state)
# - Runs Claude with an iteration prompt that includes the comments verbatim
# - Commits any changes as ONE new commit, pushes (NEVER force-pushes)
# - PR auto-updates with the new commit
#
# Outputs:
#   JARVIS_ITERATE_DONE=<pr_url>    (success)
#   JARVIS_ITERATE_FAILED=<reason>  (cannot complete safely)
#
# Exit codes: 0 success, 2 bad-args / not-allowed, 3 prereq failure, 4 claude failure
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
")

BOT_LOWER=$(echo "$BOT_NAME" | tr '[:upper:]' '[:lower:]')

REPO="${1:-}"; shift || true
PR_NUMBER="${1:-}"; shift || true

REQUESTER="cli"
if [[ $# -gt 0 && "$1" != --* ]]; then
    REQUESTER="$1"; shift
fi

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

if [[ -z "$REPO" || -z "$PR_NUMBER" ]]; then
    echo "usage: jarvis_iterate.sh <repo> <pr_number> [requester] [--source ...] [--caller ...] [--budget ...]" >&2
    exit 2
fi
if awk "BEGIN{exit !($BUDGET_USD > 5.00)}"; then
    echo "ERROR: --budget $BUDGET_USD exceeds hard cap of 5.00" >&2; exit 2
fi

# --- Allowlist check ---------------------------------------------------------
ALLOWED="${JARVIS_WRITE_ALLOWED_REPOS:-}"
ALLOWED_NORM=$(tr ',' ' ' <<< "$ALLOWED")
if ! grep -qw -- "$REPO" <<< " $ALLOWED_NORM "; then
    echo "ERROR: repo '$REPO' not in JARVIS_WRITE_ALLOWED_REPOS." >&2
    exit 2
fi

# --- Fetch PR + comments via gh api -----------------------------------------
echo "[$(date +%H:%M:%S)] fetching PR #$PR_NUMBER on jupitermoney/$REPO"
PR_JSON=$(gh api "repos/jupitermoney/$REPO/pulls/$PR_NUMBER" 2>&1) || {
    echo "ERROR: cannot fetch PR: $PR_JSON" >&2
    echo "JARVIS_ITERATE_FAILED=PR fetch failed (404? not authed?)"
    exit 3
}
PR_STATE=$(jq -r '.state' <<< "$PR_JSON")
BRANCH=$(jq -r '.head.ref' <<< "$PR_JSON")
HEAD_SHA=$(jq -r '.head.sha[:10]' <<< "$PR_JSON")
PR_TITLE=$(jq -r '.title' <<< "$PR_JSON")
PR_URL=$(jq -r '.html_url' <<< "$PR_JSON")
HEAD_REPO_FULL=$(jq -r '.head.repo.full_name' <<< "$PR_JSON")

if [[ "$PR_STATE" != "open" ]]; then
    echo "JARVIS_ITERATE_FAILED=PR is $PR_STATE, not open"
    exit 1
fi
if [[ "$HEAD_REPO_FULL" != "jupitermoney/$REPO" ]]; then
    echo "JARVIS_ITERATE_FAILED=PR head is from $HEAD_REPO_FULL (fork?), iterate only supports same-repo branches"
    exit 1
fi

echo "[$(date +%H:%M:%S)] PR title: $PR_TITLE"
echo "[$(date +%H:%M:%S)] branch: $BRANCH  head: $HEAD_SHA"

# --- Fetch comments + reviews ------------------------------------------------
INLINE_COMMENTS=$(gh api --paginate "repos/jupitermoney/$REPO/pulls/$PR_NUMBER/comments" 2>/dev/null || echo "[]")
ISSUE_COMMENTS=$(gh api --paginate "repos/jupitermoney/$REPO/issues/$PR_NUMBER/comments" 2>/dev/null || echo "[]")
REVIEWS=$(gh api "repos/jupitermoney/$REPO/pulls/$PR_NUMBER/reviews" 2>/dev/null || echo "[]")

N_INLINE=$(jq 'length' <<< "$INLINE_COMMENTS")
N_ISSUE=$(jq 'length' <<< "$ISSUE_COMMENTS")
N_REVIEWS=$(jq 'length' <<< "$REVIEWS")
echo "[$(date +%H:%M:%S)] comments: $N_INLINE inline, $N_ISSUE top-level, $N_REVIEWS reviews"

if [[ "$N_INLINE" -eq 0 && "$N_ISSUE" -eq 0 && "$N_REVIEWS" -eq 0 ]]; then
    echo "JARVIS_ITERATE_FAILED=no comments or reviews on PR #$PR_NUMBER — nothing to iterate"
    exit 1
fi

# --- Workspace setup (reuse existing if branch matches; else fresh clone) ----
# Workspace dirs are named by the original TASK_ID; branch is "bot/<task_id>".
# Strip the prefixes to derive task_id.
TASK_ID="${BRANCH#jarvis/}"
TASK_ID="${TASK_ID#astra/}"
TASK_ID="${TASK_ID#$BOT_LOWER/}"
WORKSPACE="$ROOT_DIR/workspaces/iterate-${TASK_ID}-$(date -u +%H%M%S)"
mkdir -p "$WORKSPACE"
cd "$WORKSPACE"

echo "[$(date +%H:%M:%S)] cloning fresh into $WORKSPACE (iterate always starts from remote state to pick up any human pushes)"
if ! gh repo clone "${GITHUB_ORG}/$REPO" 2>&1 | sed 's|^|  clone: |'; then
    echo "ASTRA_ITERATE_FAILED=clone failed"
    echo "JARVIS_ITERATE_FAILED=clone failed"
    exit 3
fi
cd "$REPO"
git fetch origin "$BRANCH" 2>&1 | sed 's|^|  fetch: |'
git checkout "$BRANCH" 2>&1 | sed 's|^|  checkout: |' || {
    echo "ASTRA_ITERATE_FAILED=cannot checkout branch $BRANCH"
    echo "JARVIS_ITERATE_FAILED=cannot checkout branch $BRANCH"
    exit 3
}
git config user.email "${BOT_LOWER}-bot@${GITHUB_ORG}.local"
git config user.name "${BOT_NAME} Bot"

# --- Audit -------------------------------------------------------------------
TS=$(date -u +%Y%m%d-%H%M%S)
ITER_TASK_ID="iterate-${TS}-pr-${PR_NUMBER}"
AUDIT="$ROOT_DIR/logs/iterate_audit.jsonl"
mkdir -p "$(dirname "$AUDIT")"

audit() {
    local event="$1" extra="${2:-}"
    local payload="{\"task_id\":\"$ITER_TASK_ID\",\"event\":\"$event\",\"repo\":\"$REPO\","
    payload+="\"pr_number\":$PR_NUMBER,\"branch\":\"$BRANCH\","
    payload+="\"requester\":\"$REQUESTER\",\"source\":\"$SOURCE\",\"budget_usd\":$BUDGET_USD,"
    [[ -n "$CALLER" ]] && payload+="\"caller\":\"$CALLER\","
    payload+="\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
    [[ -n "$extra" ]] && payload+=",${extra}"
    payload+="}"
    echo "$payload" >> "$AUDIT"
}

audit start "\"head_sha\":\"$HEAD_SHA\",\"inline_comments\":$N_INLINE,\"issue_comments\":$N_ISSUE,\"reviews\":$N_REVIEWS"

# --- Guard B: loop-detection cap (rule established 2026-05-19) ---------------
# Refuse a 3rd+ iterate on the same PR if no new reviewer activity or
# attachments have arrived since the last iterate commit. Compounding
# speculative fixes burns money and frustrates reviewers; manual debug is
# the right move once the agent is stuck in a loop.
# Override: JARVIS_FORCE_ITERATE=1 (when you have evidence not in PR data).
PRIOR_ITERATE_COUNT=$(git log --oneline --grep="address review comments on PR #${PR_NUMBER}" 2>/dev/null | wc -l | tr -d ' ')
if [ "$PRIOR_ITERATE_COUNT" -ge 3 ] && [ "${JARVIS_FORCE_ITERATE:-0}" != "1" ]; then
    LAST_ITERATE_EPOCH=$(git log -1 --format=%ct --grep="address review comments on PR #${PR_NUMBER}" 2>/dev/null || echo "0")
    LAST_ITERATE_EPOCH=${LAST_ITERATE_EPOCH:-0}
    LATEST_COMMENT_ISO=$(jq -s -r '[.[][]? | (.created_at // .submitted_at // empty)] | sort | last // ""' \
        <(echo "$INLINE_COMMENTS") <(echo "$ISSUE_COMMENTS") <(echo "$REVIEWS") 2>/dev/null || echo "")
    LATEST_COMMENT_EPOCH=0
    [ -n "$LATEST_COMMENT_ISO" ] && LATEST_COMMENT_EPOCH=$(date -d "$LATEST_COMMENT_ISO" +%s 2>/dev/null || echo 0)
    NEW_EVIDENCE=0
    [ "$LATEST_COMMENT_EPOCH" -gt "$LAST_ITERATE_EPOCH" ] && NEW_EVIDENCE=1
    [ -n "${ITERATE_EXTRA_ATTACHMENTS:-}" ] && NEW_EVIDENCE=1
    if [ "$NEW_EVIDENCE" -eq 0 ]; then
        echo "[$(date +%H:%M:%S)] GUARD B refused: $PRIOR_ITERATE_COUNT prior iterations on PR #$PR_NUMBER, no new evidence since last attempt (last_iterate=$LAST_ITERATE_EPOCH, latest_comment=$LATEST_COMMENT_EPOCH)"
        audit refused "\"reason\":\"loop_detected\",\"prior_iterate_count\":$PRIOR_ITERATE_COUNT,\"last_iterate_epoch\":$LAST_ITERATE_EPOCH,\"latest_comment_epoch\":$LATEST_COMMENT_EPOCH"
        echo "JARVIS_ITERATE_REFUSED=loop_detected ($PRIOR_ITERATE_COUNT prior iterations on PR #$PR_NUMBER, no new reviewer activity or attachments since last commit; manual debug recommended; override with JARVIS_FORCE_ITERATE=1)"
        exit 5
    fi
    echo "[$(date +%H:%M:%S)] GUARD B: $PRIOR_ITERATE_COUNT prior iterations but new evidence detected; proceeding"
fi


# --- Build comments block for the prompt -------------------------------------
COMMENTS_FILE="$WORKSPACE/comments.md"
{
    echo "# Review comments on PR #$PR_NUMBER"
    echo ""
    echo "## Inline comments ($N_INLINE)"
    echo ""
    jq -r '.[] | "### [\(.user.login) on \(.path):\(.line // .original_line)]\n\(.body)\n"' <<< "$INLINE_COMMENTS"
    echo ""
    echo "## Top-level / issue comments ($N_ISSUE)"
    echo ""
    jq -r '.[] | "### [\(.user.login) @ \(.created_at)]\n\(.body)\n"' <<< "$ISSUE_COMMENTS"
    echo ""
    echo "## Reviews ($N_REVIEWS)"
    echo ""
    jq -r '.[] | "### [\(.user.login) — \(.state) — \(.submitted_at)]\n\(.body // "(no body, comments inline)")\n"' <<< "$REVIEWS"
} > "$COMMENTS_FILE"

# --- Auto-extract any image / video attachments from comments/reviews -------
# Reviewers paste screenshots via GitHub's drag-and-drop attachment flow,
# producing <img src="https://github.com/user-attachments/assets/..."> tags;
# bug reports often include MP4 screen recordings. Both unlock visual / timing
# context that pure-text reasoning misses. Fetch + (for video) extract keyframes
# via ffmpeg, then pass each as a Claude image content block.
#
# Plus: caller-supplied attachments via ITERATE_EXTRA_ATTACHMENTS env var
# (comma-separated URLs) — e.g. the Jira screen recording URL referenced in
# the original ticket but not in the GitHub PR.
IMG_DIR="$WORKSPACE/attachments"
mkdir -p "$IMG_DIR"
GH_TOKEN=$(gh auth token 2>/dev/null || echo "")

# Collect URLs: from PR comments + any caller-supplied extras
COMMENT_URLS=$(grep -oE 'https?://[^"<> )]+(\.png|\.jpg|\.jpeg|\.gif|\.mp4|\.mov|\.webm|/user-attachments/assets/[a-z0-9-]+|atlassian\.net/rest/api/[0-9]/attachment/content/[0-9]+)' "$COMMENTS_FILE" | sort -u)
EXTRA_URLS=$(echo "${ITERATE_EXTRA_ATTACHMENTS:-}" | tr ',' '\n' | grep -v '^$' || true)
ALL_URLS=$(printf '%s\n%s\n' "$COMMENT_URLS" "$EXTRA_URLS" | grep -v '^$' | sort -u)

IMG_COUNT=0
IMG_PATHS=()
FRAMES_PER_VIDEO_CAP=${ITERATE_FRAMES_PER_VIDEO:-10}  # cap keyframes per video

for url in $ALL_URLS; do
    IMG_COUNT=$((IMG_COUNT + 1))
    out="$IMG_DIR/raw_${IMG_COUNT}.bin"

    # Choose auth header based on hostname (atlassian.net needs Basic auth;
    # GitHub user-attachments need GH token; everything else: anonymous).
    AUTH_ARGS=()
    case "$url" in
        *atlassian.net*)
            if [ -n "${CONFLUENCE_EMAIL:-}" ] && [ -n "${CONFLUENCE_API_TOKEN:-}" ]; then
                AUTH_ARGS=("-u" "$CONFLUENCE_EMAIL:$CONFLUENCE_API_TOKEN")
            fi
            ;;
        *github.com*|*githubusercontent.com*)
            if [ -n "$GH_TOKEN" ]; then
                AUTH_ARGS=("-H" "Authorization: token $GH_TOKEN")
            fi
            ;;
    esac

    if ! curl -sSL -L -A "Mozilla/5.0" "${AUTH_ARGS[@]}" "$url" -o "$out" 2>/dev/null; then
        echo "[$(date +%H:%M:%S)] fetch FAILED: $url"
        rm -f "$out"
        continue
    fi
    SIZE=$(stat -c "%s" "$out" 2>/dev/null || echo 0)
    if [ "$SIZE" -le 1024 ]; then
        echo "[$(date +%H:%M:%S)] skipped (too small, $SIZE bytes): $url"
        rm -f "$out"
        continue
    fi

    FILE_DESC=$(file -b "$out")
    case "$FILE_DESC" in
        *PNG*|*JPEG*|*GIF*|*image*)
            # Image: rename + add directly
            ext="png"
            case "$FILE_DESC" in *JPEG*) ext="jpg" ;; *GIF*) ext="gif" ;; esac
            mv "$out" "$IMG_DIR/img_${IMG_COUNT}.${ext}"
            IMG_PATHS+=("$IMG_DIR/img_${IMG_COUNT}.${ext}")
            echo "[$(date +%H:%M:%S)] image attached (#${IMG_COUNT}, $SIZE bytes): $url"
            ;;
        *MP4*|*ISO\ Media*|*QuickTime*|*WebM*|*Matroska*|*video*)
            # Video: extract keyframes via ffmpeg, cap to FRAMES_PER_VIDEO_CAP.
            # Probe duration; sample evenly across the timeline.
            DUR=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$out" 2>/dev/null || echo "0")
            DUR_INT=${DUR%.*}
            [ -z "$DUR_INT" ] && DUR_INT=0
            if [ "$DUR_INT" -le 0 ]; then
                echo "[$(date +%H:%M:%S)] video has no probe-able duration, skipping: $url"
                rm -f "$out"
                continue
            fi
            # fps such that we get ~FRAMES_PER_VIDEO_CAP frames total
            FPS=$(awk "BEGIN { printf \"%.4f\", $FRAMES_PER_VIDEO_CAP / $DUR }")
            VID_FRAME_DIR="$IMG_DIR/video_${IMG_COUNT}_frames"
            mkdir -p "$VID_FRAME_DIR"
            if ffmpeg -nostdin -v error -i "$out" -vf "fps=${FPS}" -frames:v "$FRAMES_PER_VIDEO_CAP" \
                "$VID_FRAME_DIR/frame_%03d.png" 2>&1 | sed 's|^|  ffmpeg: |'; then
                FRAME_COUNT=0
                for f in "$VID_FRAME_DIR"/frame_*.png; do
                    [ -f "$f" ] || continue
                    IMG_PATHS+=("$f")
                    FRAME_COUNT=$((FRAME_COUNT + 1))
                done
                echo "[$(date +%H:%M:%S)] video attached (#${IMG_COUNT}, ${SIZE} bytes, ${DUR}s) -> ${FRAME_COUNT} keyframes: $url"
                rm -f "$out"  # raw video no longer needed once frames extracted
            else
                echo "[$(date +%H:%M:%S)] ffmpeg failed for: $url"
                rm -f "$out"
            fi
            ;;
        *)
            echo "[$(date +%H:%M:%S)] skipped (unrecognized type '$FILE_DESC'): $url"
            rm -f "$out"
            ;;
    esac
done
echo "[$(date +%H:%M:%S)] total content blocks for Claude: ${#IMG_PATHS[@]} (images + extracted video keyframes)"


# --- Guard A: refuse FE iteration without visuals (rule established 2026-05-19) ---
# Frontend bugs (layout, animation, gesture, keyboard interaction) require
# images or video keyframes for reliable diagnosis. Text-only reasoning on
# visual bugs produces confidently-wrong fixes that waste reviewer cycles.
# Override: JARVIS_ALLOW_FE_NO_VISUALS=1.
if [ "${#IMG_PATHS[@]}" -eq 0 ] && [ "${JARVIS_ALLOW_FE_NO_VISUALS:-0}" != "1" ]; then
    TOUCHED_FILES=$(git diff --name-only origin/main...HEAD 2>/dev/null || git diff --name-only HEAD~5..HEAD 2>/dev/null || true)
    FE_FILES=$(echo "$TOUCHED_FILES" | grep -iE '\.(tsx|jsx|css|scss|sass|less|html|vue|svelte)$' | head -5 || true)
    if [ -n "$FE_FILES" ]; then
        echo "[$(date +%H:%M:%S)] GUARD A refused: PR touches frontend files but no images/videos available"
        echo "[$(date +%H:%M:%S)]   touched FE files: $(echo "$FE_FILES" | tr '\n' ' ')"
        audit refused "\"reason\":\"fe_no_visuals\",\"touched_fe_count\":$(echo "$FE_FILES" | wc -l | tr -d ' ')"
        echo "JARVIS_ITERATE_REFUSED=fe_no_visuals (PR touches frontend files but no images/videos available. Add visuals via reviewer screenshot, Jira attachment, or ITERATE_EXTRA_ATTACHMENTS env; or override with JARVIS_ALLOW_FE_NO_VISUALS=1)"
        exit 6
    fi
fi

COMPANION_MODE="${ITERATE_COMPANION_PR:-0}"
if [ "$COMPANION_MODE" = "1" ]; then
    echo "[$(date +%H:%M:%S)] companion mode ON — will defer to upstream-repo PR if Claude requests it"
fi

# --- Build Claude prompt -----------------------------------------------------
if [ "$COMPANION_MODE" = "1" ]; then
    COMPANION_PROMPT_BLOCK=$(cat <<'CMPEOF'

===== COMPANION MODE — IMPORTANT =====

If you determine that the architectural root cause of the bug lives in an
UPSTREAM / LIBRARY / DESIGN-SYSTEM repo (not at the call-site you're
reviewing), DO NOT modify any files in this repo and DO NOT commit.

Instead:
1. Write a diagnosis markdown file to ${WORKSPACE_PLACEHOLDER}/companion_diagnosis.md
   containing:
   - WHAT the bug actually is (symptom + root cause)
   - WHERE the architectural fix belongs (specific file / component / line in the upstream repo)
   - WHY the call-site workaround is wrong
   - HOW the fix should be implemented (concrete code change suggested)
2. Output a SINGLE LINE on stdout:
     JARVIS_NEEDS_COMPANION=<upstream-repo-short-name>
   (e.g. JARVIS_NEEDS_COMPANION=jupiter-design-system)
3. STOP. Do not commit, do not push, do not create files anywhere else.

A companion draft PR will be opened in the upstream repo automatically.

If the bug genuinely belongs at the call-site, proceed with the normal
MANDATORY WORKFLOW below (commit + push to this PR's branch).

===== END COMPANION MODE =====

CMPEOF
)
    COMPANION_PROMPT_BLOCK="${COMPANION_PROMPT_BLOCK//\${WORKSPACE_PLACEHOLDER}/$WORKSPACE}"
else
    COMPANION_PROMPT_BLOCK=""
fi

read -r -d '' PROMPT <<EOF || true
${COMPANION_PROMPT_BLOCK}
You are Jarvis, iterating on PR #${PR_NUMBER} in jupitermoney/${REPO} that you
previously authored. You are inside a fresh checkout of that PR's branch
(${BRANCH}). The current HEAD is ${HEAD_SHA}.

PR title: ${PR_TITLE}
PR URL: ${PR_URL}

The PR has received review comments. Address each one by making the changes
the reviewers asked for. The full comment dump (with file:line context for
each inline comment) is at:

    ${COMMENTS_FILE}

READ that file first. It has $N_INLINE inline comments, $N_ISSUE top-level
comments, and $N_REVIEWS reviews.


PRE-FLIGHT CHECK — before doing ANY work, verify both of these:

(i) Run: git log --oneline --grep="address review comments on PR #${PR_NUMBER}"
    If 2 or more prior iterate commits exist AND you cannot articulate what
    NEW information is in THIS iteration vs. those (new reviewer comment with
    a specific file:line pointer, new visual evidence, a new code path
    discovered), output:
        JARVIS_ITERATE_REFUSED=insufficient_evidence
    and STOP. Don't compound speculative fixes — a 3rd guess on top of two
    failed guesses is the wrong tool; manual human debug is.

(ii) If the bug being reviewed is visual / UI (component layout, animation,
    gesture, keyboard interaction, anything where what the user SEES matters),
    AND no images or video frames are attached to your inputs: output:
        JARVIS_ITERATE_REFUSED=insufficient_evidence
    and STOP. Code-only reasoning on visual bugs produces confident-but-wrong
    fixes (cf. PR #14140 RECO-1259 history: 3 text-only iterations all wrong;
    1st image+video iteration found root cause).

MANDATORY WORKFLOW:

1. READ ${COMMENTS_FILE}. Note each distinct change requested.

2. INVESTIGATE each comment: open the referenced file:line, understand the
   reviewer's intent, look at sibling code for the existing convention.

3. MAKE THE CHANGES. Default to the architecturally-correct fix, NOT a
   call-site workaround. If a reviewer's comment hints that the real bug
   is in an upstream library / design-system component, do the upstream
   fix as the primary recommendation — even if it means a larger diff or
   a cross-repo PR. Only ship a call-site patch as the primary fix if a
   reviewer explicitly asks for an interim / hotfix.

   Common anti-patterns to AVOID this round:
   - Patching one consumer of a buggy design-system component instead of
     fixing the component itself.
   - Adding a guard at a single call-site to band-aid around a buggy
     library prop combination, instead of changing the prop combination
     correctly.
   - Mutating state to "work around" a re-render race, instead of
     restructuring the state ownership so the race can't happen.

   If the architecturally-correct fix requires touching a different repo,
   say so EXPLICITLY in the commit message and PR body: "this is a partial
   fix; the architectural fix is a follow-up PR in <other-repo>".

   Group related changes if a single edit addresses multiple comments. Use
   design tokens / named constants if the comment was a hardcode complaint
   (search the design system for the right token before writing literal
   values — Jupiter's tokens live in
   jupitermoney/jupiter-design-system, theme at packages/design-system/src/theme/index.ts).

4. VALIDATE: run linters / type checks. RUN UNIT TESTS for touched modules
   (yarn jest <touched-test>, ./gradlew :module:test, pytest path/to/test).
   If a test fails: fix and re-run. Max 3 fix-and-rerun rounds.

5. SELF-REVIEW your aggregate diff vs the previous HEAD ($HEAD_SHA) — re-read
   as a senior reviewer. Hunt for the same patterns the standards-bot will
   flag: hardcoded design values, leftover TODOs / console.log, nested
   ternaries, inline arrow handlers >1 statement, magic numbers, missing
   tests.

6. COMMIT all changes as ONE commit. Subject:
     "fix: address review comments on PR #${PR_NUMBER} (\${N} items)"
   Body should list each comment addressed (one line per comment, brief).
   If you couldn't address a comment (e.g. it requires major refactor
   beyond iterate scope), say so in the body — don't pretend you did.
   Sign with:
     "Co-Authored-By: Jarvis (jupitermoney pilot) <jarvis-bot@jupitermoney.local>"

7. PUSH (regular push, NEVER force-push):
     git push origin $BRANCH

8. OUTPUT THE PR URL on a new line, prefix exactly:
     JARVIS_ITERATE_DONE=${PR_URL}

HARD CONSTRAINTS — never violate:
- NEVER force-push or rebase (would clobber reviewer's diff history)
- NEVER close the PR or change its title/description
- NEVER add reviewers programmatically
- NEVER delete files
- NEVER touch .github/workflows/, build.gradle, package.json's deps, *.lock
  unless the comment specifically asks for it
- NEVER add external dependencies
- NEVER edit secrets / .env files

If you cannot complete safely, output:
   JARVIS_ITERATE_FAILED=<short reason>
and STOP. Don't half-do it.
EOF

# --- Run Claude --------------------------------------------------------------
echo "[$(date +%H:%M:%S)] running Claude Code (max budget \$$BUDGET_USD, images=${#IMG_PATHS[@]})..."
audit claude_started "\"image_attachments\":${#IMG_PATHS[@]}"
LOG_FILE="$WORKSPACE/claude_run.log"
START_EPOCH=$(date +%s)

CLAUDE_EXIT=0
if [ "${#IMG_PATHS[@]}" -gt 0 ]; then
    # Image path: build stream-json input with text + image content blocks.
    # claude requires --verbose when --print + --output-format=stream-json.
    INPUT_FILE="$WORKSPACE/stream_input.jsonl"
    PROMPT_TEXT_FILE="$WORKSPACE/prompt.txt"
    printf '%s' "$PROMPT" > "$PROMPT_TEXT_FILE"

    # Use Python to build the stream-json user message (cleaner than bash JSON).
    "$ROOT_DIR/scripts/indexer/.venv/bin/python" - "$PROMPT_TEXT_FILE" "$INPUT_FILE" "${IMG_PATHS[@]}" <<'PYEOF'
import base64, json, mimetypes, sys
prompt_file, input_file, *img_paths = sys.argv[1:]
with open(prompt_file) as f:
    prompt = f.read()
content = [{"type": "text", "text": prompt}]
for p in img_paths:
    mime = mimetypes.guess_type(p)[0] or "image/png"
    if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        mime = "image/png"
    with open(p, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
msg = {"type": "user", "message": {"role": "user", "content": content}}
with open(input_file, "w") as f:
    f.write(json.dumps(msg) + "\n")
print(f"  stream-json input: {len(content)} content blocks (1 text + {len(content)-1} image)", file=sys.stderr)
PYEOF

    if ! claude -p --verbose \
            --max-budget-usd "$BUDGET_USD" \
            --dangerously-skip-permissions \
            --input-format stream-json \
            --output-format stream-json \
            --model claude-sonnet-4-6 \
            < "$INPUT_FILE" > "$LOG_FILE" 2>&1; then
        CLAUDE_EXIT=$?
    fi
else
    # No images: existing simple json mode.
    if ! claude -p "$PROMPT" \
            --max-budget-usd "$BUDGET_USD" \
            --dangerously-skip-permissions \
            --output-format json \
            --model claude-sonnet-4-6 \
            > "$LOG_FILE" 2>&1; then
        CLAUDE_EXIT=$?
    fi
fi

if [ "$CLAUDE_EXIT" -ne 0 ]; then
    EXIT_CODE=$CLAUDE_EXIT
    # Cost extraction differs between json and stream-json modes.
    if [ "${#IMG_PATHS[@]}" -gt 0 ]; then
        PARTIAL_COST=$(jq -rs '[.[] | select(.type == "result") | .total_cost_usd // 0] | .[-1] // 0' "$LOG_FILE" 2>/dev/null || echo "0")
    else
        PARTIAL_COST=$(jq -r '.total_cost_usd // 0' "$LOG_FILE" 2>/dev/null || echo "0")
    fi
    audit claude_exited "\"exit_code\":$EXIT_CODE,\"cost_usd\":${PARTIAL_COST:-0}"
    echo "[$(date +%H:%M:%S)] claude exited non-zero ($EXIT_CODE). Last lines:"
    tail -30 "$LOG_FILE" | sed 's|^|  |'
    exit 4
fi

DURATION=$(( $(date +%s) - START_EPOCH ))
# Cost + output extraction differs between json (single object) and
# stream-json (multiple newline-delimited objects).
if [ "${#IMG_PATHS[@]}" -gt 0 ]; then
    COST_USD=$(jq -rs '[.[] | select(.type == "result") | .total_cost_usd // 0] | .[-1] // 0' "$LOG_FILE" 2>/dev/null || echo "0")
    AGENT_OUTPUT=$(jq -rs '[.[] | select(.type == "assistant") | .message.content[] | select(.type == "text") | .text] | join("\n")' "$LOG_FILE" 2>/dev/null || cat "$LOG_FILE")
else
    COST_USD=$(jq -r '.total_cost_usd // 0' "$LOG_FILE" 2>/dev/null || echo "0")
    AGENT_OUTPUT=$(jq -r '.result // ""' "$LOG_FILE" 2>/dev/null || cat "$LOG_FILE")
fi
echo "[$(date +%H:%M:%S)] claude finished in ${DURATION}s, \$$COST_USD (images_in: ${#IMG_PATHS[@]})"

# --- Companion-mode: defer to upstream-repo PR if Claude requested it -------
if [ "$COMPANION_MODE" = "1" ]; then
    NEEDS_UPSTREAM=$(echo "$AGENT_OUTPUT" | grep -oE 'JARVIS_NEEDS_COMPANION=[a-zA-Z0-9_-]+' | head -1 | cut -d= -f2)
    DIAGNOSIS_FILE_PATH="$WORKSPACE/companion_diagnosis.md"
    if [ -n "$NEEDS_UPSTREAM" ] && [ -f "$DIAGNOSIS_FILE_PATH" ]; then
        audit needs_companion "\"upstream_repo\":\"$NEEDS_UPSTREAM\",\"diagnosis_bytes\":$(stat -c %s "$DIAGNOSIS_FILE_PATH")"
        echo "[$(date +%H:%M:%S)] companion mode: Claude requested deferral to $NEEDS_UPSTREAM"
        echo "[$(date +%H:%M:%S)] spawning astra_companion_pr.sh ..."
        COMPANION_OUT="$WORKSPACE/companion_run.log"
        if "$ROOT_DIR/scripts/astra_companion_pr.sh" \
                --source-pr "$PR_URL" \
                --upstream-repo "$NEEDS_UPSTREAM" \
                --diagnosis "$DIAGNOSIS_FILE_PATH" \
                --caller "iterate-auto-${CALLER:-${REQUESTER:-unknown}}" \
                --budget "${COMPANION_BUDGET_USD:-2.00}" 2>&1 | tee "$COMPANION_OUT"; then
            COMPANION_URL=$(grep -oE 'JARVIS_COMPANION_PR_DONE=https://[^ ]+' "$COMPANION_OUT" | tail -1 | cut -d= -f2-)
            audit companion_done "\"companion_pr_url\":\"$COMPANION_URL\""
            echo ""
            echo "✅ ITERATE DEFERRED TO COMPANION in ${DURATION}s, \$$COST_USD (iterate side)"
            echo "ASTRA_ITERATE_DEFERRED_TO_COMPANION=$COMPANION_URL"
            echo "JARVIS_ITERATE_DEFERRED_TO_COMPANION=$COMPANION_URL"
            exit 0
        else
            COMPANION_EXIT=$?
            audit companion_failed "\"exit_code\":$COMPANION_EXIT"
            echo "ASTRA_ITERATE_FAILED=companion_spawn_failed (exit $COMPANION_EXIT)"
            echo "JARVIS_ITERATE_FAILED=companion_spawn_failed (exit $COMPANION_EXIT)"
            exit 1
        fi
    elif [ -n "$NEEDS_UPSTREAM" ] && [ ! -f "$DIAGNOSIS_FILE_PATH" ]; then
        audit companion_no_diagnosis "\"upstream_repo\":\"$NEEDS_UPSTREAM\""
        echo "[$(date +%H:%M:%S)] companion marker found but no diagnosis file at $DIAGNOSIS_FILE_PATH — falling through to normal iterate"
    fi
    # If no companion marker, fall through to normal iterate commit/push.
fi


# --- Parse outcome -----------------------------------------------------------
DONE_URL=$(echo "$AGENT_OUTPUT" | grep -oE 'JARVIS_ITERATE_DONE=https?://[^ ]+' | tail -1 | cut -d= -f2-)
FAIL_REASON=$(echo "$AGENT_OUTPUT" | grep -oE 'JARVIS_ITERATE_FAILED=.+' | tail -1 | cut -d= -f2-)

if [[ -n "$DONE_URL" ]]; then
    audit success "\"pr_url\":\"$DONE_URL\",\"duration_sec\":$DURATION,\"cost_usd\":${COST_USD:-0}"
    echo ""
    echo "✅ ITERATE DONE in ${DURATION}s, \$${COST_USD} of \$${BUDGET_USD} budget"
    echo "ASTRA_ITERATE_DONE=$DONE_URL"
    echo "JARVIS_ITERATE_DONE=$DONE_URL"
    # For uniformity with /api/v1/fix payload shape, ALSO emit ASTRA/JARVIS_PR_URL so
    # the api/jobs.py parser (which currently expects JARVIS_PR_URL) keeps working.
    echo "ASTRA_PR_URL=$DONE_URL"
    echo "JARVIS_PR_URL=$DONE_URL"
    exit 0
elif [[ -n "$FAIL_REASON" ]]; then
    audit failed "\"reason\":$(jq -Rs <<< "$FAIL_REASON"),\"duration_sec\":$DURATION,\"cost_usd\":${COST_USD:-0}"
    echo "⚠️  Claude reported failure: $FAIL_REASON"
    exit 1
else
    audit unclear "\"duration_sec\":$DURATION,\"cost_usd\":${COST_USD:-0}"
    echo "⚠️  No DONE / FAILED marker. Last lines:"
    tail -20 "$LOG_FILE" | sed 's| |  |'
    exit 1
fi
