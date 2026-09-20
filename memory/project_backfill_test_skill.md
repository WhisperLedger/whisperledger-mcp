---
name: Regression-test backfill skill — `scripts/jarvis_backfill_test.sh`
description: Retro-adds a regression test to an existing Jarvis-fired PR on the SAME branch (no new PR). Built 2026-06-06 after the Jove filter-19028 batch shipped 20 PRs with regression_test silently dropped client-side.
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
## What it is

`scripts/jarvis_backfill_test.sh <repo> <pr_number> [--budget 1.50]` — retro-adds a regression test to an existing Jarvis-fired PR. Different from `regression_test=true` mode (which is for fresh PRs); backfill is for PRs that already shipped.

How it works:
1. Fresh-clones the PR's head branch
2. Pre-check: if PR already has a `test:` prefix commit, exits cheap (`JARVIS_BACKFILL_ALREADY_HAS_TEST`) with no Claude run
3. Runs Claude with a regression-backfill prompt — read pre-fix (HEAD~1) vs post-fix (HEAD), identify the bug, write a test that fails pre-fix and passes post-fix
4. Verifies the test would have failed pre-fix (Claude does this with `git checkout HEAD~1 -- <files>` + run test + revert)
5. Commits as `test: regression for #N — <title>` and pushes

Outcome markers it emits:
- `JARVIS_BACKFILL_DONE=<sha>` — test added + pushed
- `JARVIS_BACKFILL_ALREADY_HAS_TEST=<sha>` — pre-check matched, no work needed
- `JARVIS_BACKFILL_SKIPPED=<reason>` — honest refusal (no test infra, trivial 1-line fix, UI-only with no test path)
- `JARVIS_BACKFILL_NO_OP=<reason>` — Claude said DONE but HEAD didn't move
- `JARVIS_BACKFILL_FAILED=<reason>` — error

## When to use it

- A `/api/v1/fix` batch shipped without `regression_test=true` and the gap was caught later
- A historical PR is being re-reviewed and the reviewer would benefit from seeing a regression test
- Any time you find a Jarvis-fired bug PR without a `test:` commit and want one

## When NOT to use it

- The PR is already merged AND the branch is gone — backfill needs an active branch
- The PR is trivial (1-line typo, copy edit, dependency bump) — the wrapper will honestly skip
- The fix touches CI YAML / .github / config-only — no test path; skip

## Validated batch (2026-06-06)

Ran on the 20-PR Jove filter-19028 batch (jupiter#14232-14251):
- 17 backfilled cleanly (`DONE`)
- 1 already had a test (`ALREADY_HAS_TEST` — #14245)
- 3 honest skips (`SKIPPED` — no `@testing-library/react-native`, useMemo-internal fix, purely visual migration)
- 0 failures
- Total cost ~$15-25

Audit log: `~/jarvis/logs/backfill_test_audit.jsonl`. Workspaces at `~/jarvis/workspaces/backfill-<repo>-<pr>-<ts>/`.

## Known limitations

1. **Stdout buffering bug**: the wrapper's terminal output markers sometimes don't flush to the parent log file (race with nohup + `>` redirection). Ground truth is the git state of the PR branch — check via `gh api repos/<owner>/<repo>/pulls/<num>/commits`.
2. **No semaphore**: backfills run as independent bash subprocesses, no concurrency limit. Fired 18 in parallel during the batch; no Anthropic-side rate-limit hits, but watch budget if you fire >30.
3. **Audit log cost field bug**: the `cost_usd` field in `backfill_test_audit.jsonl` sometimes reads $0 instead of the real cost. The Claude `total_cost_usd` in the workspace's `claude_run.log` is authoritative.
