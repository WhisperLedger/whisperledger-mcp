"""Pre-push local PR review — reads a local diff, runs cross-repo + standards analysis,
emits STRUCTURED JSON findings to stdout. Designed to be called by the HTTP endpoint
POST /api/v1/preflight (which is in turn called by the CLI `jarvis preflight` that
engineers run locally before `git push`).

Output contract (stdout):
    Last line is exactly: JARVIS_PREFLIGHT_RESULT=<single-line-JSON>

The JSON has shape:
    {
      "ok": true|false,
      "summary": {"critical": N, "high": N, "medium": N, "low": N, "info": N},
      "findings": [
        {
          "severity": "critical|high|medium|low|info",
          "category": "cross_repo_breakage|parallel_drift|ci_security|standards|deploy_risk|...",
          "file": "path/to/file.tsx",
          "line": 42,
          "title": "short headline",
          "body": "detailed explanation + code reference"
        },
        ...
      ],
      "risk_assessment": "LOW|MEDIUM|HIGH",
      "duration_sec": 12.3,
      "cost_usd": 0.18
    }

Same Claude agent as /jarvis review (cross-repo search + Mithun-style severity-graded
inline findings). NO writes to GitHub — output is just for the CLI to render in the
engineer's terminal.
"""
from __future__ import annotations
import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

from agent.agent import ask
from agent.config import GITHUB_ORG, COMPANY_NAME, BOT_NAME, SLASH_COMMAND, ROOT_DIR

PREFLIGHT_INSTRUCTIONS = f"""PREFLIGHT MODE: an engineer is about to `git push` in {GITHUB_ORG}/{{repo}}.
You're running on their LOCAL diff (NO PR exists yet). Your job: surface issues NOW so they
fix before push instead of after review.

Your unique value (vs Cursor/Copilot already running in their IDE):
- ORG-WIDE cross-repo impact via `search_code` across all 261 indexed repos
- PARALLEL-IMPLEMENTATION DRIFT: when the diff changes constants / error codes / routing /
  enum-like classes, search for structurally similar definitions in OTHER repos and flag
  divergence (e.g., StandardPayUErrorCodes in one repo diverging from another)
- BREAKING-CHANGE validation against KNOWN consumers (not just "found consumer X" — verify
  whether the changed signature / model / schema actually breaks X)
- CI/SECURITY workflow audit: if .github/workflows/* changed, flag removed/disabled security
  steps (Trivy, Semgrep, Snyk) explicitly
- CONFIG VALUE PROPAGATION: when a config default changes (poll size, timeout, limit),
  grep cross-repo consumers for overrides; if none override, the change silently affects them

WORKFLOW:
1. Read the diff. Extract:
   - OpenAPI paths, schema names, gRPC service+method, Kafka topic names, Stargate routes
   - Kotlin/TS classes/enums/sealed types (especially in *-models, *-spi, *-commons, design-system)
   - Constants that look like error codes, routing keys, feature flags
   - Config defaults (numeric literals named like *Limit, *Timeout, *Size, *Records)
   - `.github/workflows/*` security-relevant changes
2. For each identifier: call `search_code` across all repos (no `repo` filter), skipping the
   source repo. Call `read_file` on hits to confirm real usage (not string-match coincidences).
3. Categorize each issue into severity:
   - critical: deploy-time silent failures, security-step removed, breaks a known consumer
   - high: breaking change for indexed consumers, parallel-implementation diverged, CI security weakened
   - medium: additive but consumers should be aware, config-default change with no consumer overrides, missing tests for new exported behavior
   - low: standards / style-bot likely to flag, hardcoded design value where token exists
   - info: cross-repo consumers found but unchanged; for engineer awareness

OUTPUT FORMAT — single JSON object, ONE line, prefixed exactly:

    ASTRA_PREFLIGHT_RESULT=<json>

JSON schema:
{{
  "ok": true,
  "summary": {{"critical": <n>, "high": <n>, "medium": <n>, "low": <n>, "info": <n>}},
  "findings": [
    {{
      "severity": "critical|high|medium|low|info",
      "category": "cross_repo_breakage|parallel_drift|ci_security|breaking_change|config_propagation|standards|deploy_risk|missing_tests|other",
      "file": "<path/from/diff>",
      "line": <line-number-in-diff or null if file-level>,
      "title": "<one-line headline, <=80 chars>",
      "body": "<2-4 sentence explanation with file:line citations of evidence in OTHER repos>"
    }}
  ],
  "risk_assessment": "LOW|MEDIUM|HIGH"
}}

CALIBRATION:
- HIGH risk: any critical findings, OR 2+ high findings, OR a parallel-drift or breaking-change finding
- MEDIUM risk: 1 high, OR 3+ medium
- LOW risk: only low/info findings, OR clean diff

Be HONEST about LOW. If the diff is self-contained / pure intra-repo refactor / typo fix,
return zero findings + LOW risk + a short note in the summary. Don't pad with hypotheticals.

If you cannot confidently analyze (diff malformed, repo unknown, etc.), output:

    JARVIS_PREFLIGHT_FAILED=<short reason>

DIFF (truncated to {diff_chars} chars if longer):
```diff
{diff_truncated}
```
"""


def fail(reason: str, code: int = 1) -> None:
    print(f"\nJARVIS_PREFLIGHT_FAILED={reason}")
    sys.exit(code)


def main():
    if len(sys.argv) < 3:
        fail("usage: jarvis_preflight.py <repo> <diff-file> [requester]", 2)
    repo = sys.argv[1].strip()
    diff_file = sys.argv[2].strip()
    requester = sys.argv[3] if len(sys.argv) > 3 else "cli"

    if not Path(diff_file).exists():
        fail(f"diff file not found: {diff_file}", 2)
    diff_text = Path(diff_file).read_text()
    if not diff_text.strip():
        fail("diff is empty — nothing to preflight", 2)

    # --- Audit setup ---
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    task_id = f"preflight-{ts}-{repo}"
    work_dir = ROOT_DIR / "workspaces" / task_id
    work_dir.mkdir(parents=True, exist_ok=True)
    audit_path = ROOT_DIR / "logs" / "preflight_audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    def audit(event: str, **kw):
        rec = {"task_id": task_id, "event": event, "repo": repo,
               "requester": requester,
               "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
               **kw}
        with audit_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    audit("start", diff_chars=len(diff_text))
    print(f"[{datetime.now().strftime('%H:%M:%S')}] task_id={task_id}  repo={repo}  diff={len(diff_text)} chars", file=sys.stderr)

    DIFF_BUDGET = 6000
    diff_truncated = diff_text[:DIFF_BUDGET]
    if len(diff_text) > DIFF_BUDGET:
        diff_truncated += f"\n... [truncated, {len(diff_text) - DIFF_BUDGET} more chars]"

    prompt = PREFLIGHT_INSTRUCTIONS.format(
        repo=repo, diff_chars=DIFF_BUDGET, diff_truncated=diff_truncated,
    )
    (work_dir / "diff.patch").write_text(diff_text)
    (work_dir / "prompt.txt").write_text(prompt)

    # --- Run agent ---
    print(f"[{datetime.now().strftime('%H:%M:%S')}] running agent...", file=sys.stderr)
    audit("agent_started")
    started = time.time()
    try:
        res = ask(prompt)
    except Exception as e:
        audit("agent_failed", error=f"{type(e).__name__}: {e}")
        fail(f"agent crashed: {type(e).__name__}: {e}", 4)
    duration_s = round(time.time() - started, 1)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] agent finished in {duration_s}s "
          f"(iterations={res.iterations}, tool_calls={len(res.tool_calls)})", file=sys.stderr)

    cost = round(
        (res.input_tokens * 3 + res.cache_read_tokens * 0.30
         + res.cache_creation_tokens * 3.75 + res.output_tokens * 15) / 1_000_000, 4)

    raw = res.answer.strip()
    (work_dir / "agent_output.txt").write_text(raw)

    # Extract the ASTRA_PREFLIGHT_RESULT= line
    result_json = None
    for line in raw.splitlines():
        if line.startswith("ASTRA_PREFLIGHT_RESULT="):
            try:
                result_json = json.loads(line[len("ASTRA_PREFLIGHT_RESULT="):])
                break
            except json.JSONDecodeError as e:
                audit("parse_failed", error=str(e), preview=line[:300])
                fail(f"agent returned malformed JSON: {e}", 5)
        if line.startswith("ASTRA_PREFLIGHT_FAILED="):
            reason = line[len("ASTRA_PREFLIGHT_FAILED="):]
            audit("agent_refused", reason=reason, cost_usd=cost, duration_sec=duration_s)
            fail(reason, 6)

    if result_json is None:
        audit("no_result_marker", cost_usd=cost, duration_sec=duration_s)
        fail("agent produced no ASTRA_PREFLIGHT_RESULT line", 5)

    # Inject metadata
    result_json["duration_sec"] = duration_s
    result_json["cost_usd"] = cost
    result_json["task_id"] = task_id

    audit("success", cost_usd=cost, duration_sec=duration_s,
          findings_count=len(result_json.get("findings", [])),
          risk=result_json.get("risk_assessment", "?"))

    # Final line — what the HTTP endpoint / CLI reads
    print(f"ASTRA_PREFLIGHT_RESULT={json.dumps(result_json, separators=(',', ':'))}")
    sys.exit(0)


if __name__ == "__main__":
    main()
