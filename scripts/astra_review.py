"""Cross-repo PR review — fetches PR diff, runs Jarvis agent with review-focused prompt,
posts ONE review comment back to the PR with cited cross-repo impact.

Usage:
    python -m jarvis_review <pr-url> [requester]

Outputs (so the Slack handler can parse):
    JARVIS_PR_URL=<comment URL>   on success
    JARVIS_FIX_FAILED=<reason>    on failure
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from agent.agent import ask  # uses existing search_code/read_file/etc. tools
from agent.config import GITHUB_ORG, COMPANY_NAME, BOT_NAME, SLASH_COMMAND, ROOT_DIR

PR_URL_RE = re.compile(rf'github\.com/{re.escape(GITHUB_ORG)}/([^/]+)/pull/(\d+)')

REVIEW_INSTRUCTIONS = f"""REVIEW MODE: you're reviewing PR #{{pr_num}} in {GITHUB_ORG}/{{repo}}.

Your *single* task: identify CROSS-REPO IMPACT of the diff below. Other tools
(Cursor, GitHub Copilot, etc.) already handle intra-repo style/logic review well.
Your unique value is the org-wide view via `search_code` across all indexed
repositories. Do not duplicate what stock tools do.

WORKFLOW:
1. Read the diff carefully. Extract identifiers that could have cross-repo impact:
   - OpenAPI paths and schema names
   - Kotlin classes / enums / sealed types (especially in *-models, *-spi, *-commons)
   - gRPC services + RPC method names
   - Kafka topic names / queue names
   - Stargate routes / public API paths
   - Shared-plugin keys (kotlin-dependency-management plugin IDs, etc.)
   - Database table/column renames (if migrations changed)
2. For each identifier, call `search_code` across ALL repos (no `repo` filter).
   Skip the source repo itself in your impact analysis.
3. For confirmed consumer hits, call `read_file` to verify it's a real reference
   (not a string-match coincidence).
4. Compose ONE review comment as markdown, in this exact structure:

   ## :robot_face: {BOT_NAME} cross-repo impact review

   <2-3 sentence summary of what the diff changes, focusing on the contract surface>

   ### Cross-repo consumers found
   <Markdown table or bulleted list with `repo/path:line` + 1-line description per hit.
   If you found NONE, say so honestly: "No cross-repo consumers found in indexed repos.">

   ### Risk assessment
   <**LOW** / **MEDIUM** / **HIGH** + 1-2 sentences. HIGH = breaking-change semantics
   for consumers; MEDIUM = additive but consumers should be aware; LOW = self-contained
   or backward-compatible additive.>

   ### Caveats
   - I only see indexed repos. Some consumers may live
     in unindexed repos (e.g. `*-prod.internal`, `claude-plugins`, `github-metadata`).
   - I did NOT build, run tests, or validate at runtime.
   - This is a *cross-repo* review only — for intra-repo correctness use Cursor /
     GitHub Copilot / human review.

   ---
   _Triggered by_ {{requester}} _via `{SLASH_COMMAND} review`_

5. If the PR is purely intra-repo (no symbols cross repo boundaries), the comment
   should be SHORT — just the summary + "No cross-repo consumers found" + LOW risk.
   Don't pad with hypotheticals. Honest "this PR is self-contained" is the right answer
   when true.

Output ONLY the markdown review comment, ready to post verbatim. No JSON wrapper, no
preamble like "here's the review:". Just the comment.

PR DIFF (truncated to {{diff_chars}} chars if longer):
```diff
{{diff_truncated}}
```
"""


def fail(reason: str, code: int = 1) -> None:
    print(f"\nASTRA_FIX_FAILED={reason}")
    sys.exit(code)


def main():
    if len(sys.argv) < 2:
        fail("astra_review.py <pr-url> [requester]", 2)
    pr_url = sys.argv[1].strip()
    requester = sys.argv[2] if len(sys.argv) > 2 else "cli"

    m = PR_URL_RE.search(pr_url)
    if not m:
        fail(f"invalid PR URL — expected https://github.com/{GITHUB_ORG}/<repo>/pull/<num>", 2)
    repo, pr_num = m.group(1), m.group(2)

    # --- Audit setup ---
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    task_id = f"review-{ts}-{repo}-{pr_num}"
    work_dir = ROOT_DIR / "workspaces" / task_id
    work_dir.mkdir(parents=True, exist_ok=True)
    audit_path = ROOT_DIR / "logs" / "review_audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    def audit(event: str, **kw):
        rec = {"task_id": task_id, "event": event, "repo": repo, "pr_num": int(pr_num),
               "requester": requester,
               "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
               **kw}
        with audit_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    audit("start")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] task_id={task_id}  pr={GITHUB_ORG}/{repo}#{pr_num}")

    # --- Fetch diff ---
    print(f"[{datetime.now().strftime('%H:%M:%S')}] fetching PR diff...")
    diff_proc = subprocess.run(
        ["gh", "pr", "diff", pr_num, "--repo", f"{GITHUB_ORG}/{repo}"],
        capture_output=True, text=True, timeout=30,
    )
    if diff_proc.returncode != 0:
        audit("fetch_failed", stderr=diff_proc.stderr[:500])
        fail(f"could not fetch PR diff (exit {diff_proc.returncode}): {diff_proc.stderr[:200]}", 3)
    diff_text = diff_proc.stdout
    diff_chars_total = len(diff_text)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] diff: {diff_chars_total} chars")
    (work_dir / "diff.patch").write_text(diff_text)

    DIFF_BUDGET = 6000  # truncate very large diffs
    diff_truncated = diff_text[:DIFF_BUDGET]
    if diff_chars_total > DIFF_BUDGET:
        diff_truncated += f"\n... [truncated, {diff_chars_total - DIFF_BUDGET} more chars]"

    prompt = REVIEW_INSTRUCTIONS.format(
        pr_num=pr_num, repo=repo, requester=requester,
        diff_chars=DIFF_BUDGET, diff_truncated=diff_truncated,
    )
    (work_dir / "prompt.txt").write_text(prompt)

    # --- Run agent ---
    print(f"[{datetime.now().strftime('%H:%M:%S')}] running agent (cross-repo search + synthesis)...")
    audit("agent_started")
    started = time.time()
    try:
        res = ask(prompt)
    except Exception as e:
        audit("agent_failed", error=f"{type(e).__name__}: {e}")
        fail(f"agent crashed: {type(e).__name__}: {e}", 4)
    duration_s = round(time.time() - started, 1)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] agent finished in {duration_s}s "
          f"(iterations={res.iterations}, tool_calls={len(res.tool_calls)})")

    comment_md = res.answer.strip()
    (work_dir / "review_comment.md").write_text(comment_md)

    # Cost estimate (Sonnet 4.6)
    cost = round(
        (res.input_tokens * 3 + res.cache_read_tokens * 0.30
         + res.cache_creation_tokens * 3.75 + res.output_tokens * 15) / 1_000_000, 4)

    # --- Post comment to PR ---
    print(f"[{datetime.now().strftime('%H:%M:%S')}] posting review comment to PR...")
    body_file = str(work_dir / "review_comment.md")
    post_proc = subprocess.run(
        ["gh", "pr", "comment", pr_num, "--repo", f"{GITHUB_ORG}/{repo}", "--body-file", body_file],
        capture_output=True, text=True, timeout=30,
    )
    if post_proc.returncode != 0:
        audit("post_failed", stderr=post_proc.stderr[:500], cost_usd=cost)
        fail(f"could not post review comment: {post_proc.stderr[:200]}", 5)

    # gh prints the URL of the new comment on success
    comment_url = post_proc.stdout.strip()
    audit("success", comment_url=comment_url, duration_sec=duration_s,
          cost_usd=cost, iterations=res.iterations,
          tool_calls=len(res.tool_calls))
    print(f"\n✅ DONE in {duration_s}s — cost ${cost}")
    print(f"ASTRA_PR_URL={comment_url}")
    sys.exit(0)


if __name__ == "__main__":
    main()
