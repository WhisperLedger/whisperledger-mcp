"""Kotlin/Java PR nitpick — fetches PR context, applies the Jupiter Kotlin review
checklist via a direct Anthropic API call, and posts a GitHub review with inline
comments (REQUEST_CHANGES or COMMENT).

Complements `/jarvis review` (which does cross-repo impact analysis). This command
focuses on intra-repo correctness: Kotlin standards, JOOQ/JPA patterns, Temporal
rules, Java 21 migration, BOM overrides, architecture conventions.

Usage:
    python jarvis_nitpick.py <pr-url> [requester]

Outputs (parsed by the Slack handler in app.py):
    JARVIS_PR_URL=<review HTML URL>   on success
    JARVIS_FIX_FAILED=<reason>        on failure
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from agent.config import GITHUB_ORG, COMPANY_NAME, BOT_NAME, SLASH_COMMAND, ROOT_DIR

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PR_URL_RE = re.compile(rf"github\.com/{re.escape(GITHUB_ORG)}/([^/]+)/pull/(\d+)")
DIFF_BUDGET = 10_000  # chars; large diffs are truncated
_ALLOWED_CMDS: frozenset[str] = frozenset({"gh", "git"})

# Progress keywords polled by app.py _run_nitpick_in_background
PROG_FETCHING = "fetching pr context"
PROG_REVIEWING = "running kotlin review"
PROG_POSTING = "posting review"


# ---------------------------------------------------------------------------
# Review system prompt — sections A–G + output format from p2p-coo-review
# ---------------------------------------------------------------------------

REVIEW_SYSTEM_PROMPT = f"""\
You are a thorough, senior engineer performing a GitHub PR review for {COMPANY_NAME} ({GITHUB_ORG}).

You will be given:
1. PR metadata and diff
2. CLAUDE.md content for the touched modules (repo conventions)
3. Service contract snippets (OpenAPI / proto) for any new external integrations
4. Dependency check result (BOM override output or skip reason)
5. Existing review threads and author comments (if any)

Apply every rule below. Classify each finding as either **[BLOCKER]** or **[Suggestion]**.

---

## How to apply checks

All checks are driven by what you learned from the CLAUDE.md content provided.
Before flagging anything, ask: "does CLAUDE.md establish this convention?" If yes,
enforce it. If not, apply only the universal rules below.

1. **CLAUDE.md rule** — explicit convention: treat as a hard requirement.
2. **Inferred pattern** — something consistently done in the codebase: flag divergence as [Suggestion].
3. **Universal Jupiter rule** — applies to all repos regardless of CLAUDE.md.

---

## A. Architecture & Request Flow

From CLAUDE.md, identify the expected flow (e.g. `Controller → Workflow → Activity`,
`Controller → Service → Repository`). Flag deviations as **[BLOCKER]**.
- Business logic must not live in controllers/handlers.

**Temporal repos:**
- DB state updates must be their own separate Temporal activity — never bundled with an external service call.
- Poll activities must check DB state first before calling the external service.
- Temporal retry/timeout parameters must come from config, never hardcoded.
- Workflows must validate current state before each step and update state after.

**State machine repos (stateless4j):**
- State transitions must go through the FSM — direct status field updates that bypass it → **[BLOCKER]**.

**Spring Boot repos:**
- `@Transactional` on service layer only, not controllers or repositories.

---

## B. Code Styling & Existing Utilities

- Check whether a utility already exists before accepting new code that duplicates it.
- New Gradle dependencies must use the repo's version-catalogue pattern (`Dependency.X` or `libs.X`). Inline version strings → **[Suggestion]**.
- Do not create a new `ObjectMapper` / `Gson` / JSON parser inside a mapper, data class, or service — use a shared/injected instance.
- No commented-out code.
- Comments must accurately describe the code — flag stale or misleading ones.
- Apply CLAUDE.md money unit standard (paisa vs rupees, BigDecimal scale).
- If CLAUDE.md specifies multi-tenancy keys (e.g. `partner_id`), verify all new DB queries include them.
- General-purpose helpers belong in the shared/common module, not duplicated per module.

---

## C. Database & Query Patterns

**JOOQ:**
- `insertInto(...).values(record)` must not include auto-generated PK columns — **[BLOCKER]**.
- Use the repo's established update utility for nullable field comparisons (e.g. `JooqUtils.compareOrDefault()`).
- Unnecessary multi-step mapping chains (DB record → domain → proto) when a single pass suffices → **[Suggestion]**.
- Query methods filtering by an enum type should accept it as optional, not hardcode a specific value.

**JPA/Spring Data:**
- `data class` for a JPA entity → **[BLOCKER]** — use a regular `class`.
- `equals`/`hashCode` must be ID-based only. `hashCode` must return a class-based constant. All-field `equals`/`hashCode` on an entity → **[BLOCKER]**.
- `toString` must not reference lazy collections.
- ID-based `equals` must guard against transient state: `id != 0L && id == other.id`.
- Idempotent operations: enforce uniqueness at BOTH `@Table(uniqueConstraints=[...])` AND application layer. Missing the DB constraint alone → **[BLOCKER]**.
- N+1 fetch inside a loop → **[BLOCKER]**. Suggest `@EntityGraph`, `JOIN FETCH`, or DTO projection. Never recommend `FetchType.EAGER`.
- Bidirectional associations: flag if only one side is maintained → **[Suggestion]**.
- Bulk updates/deletes bypass persistence context → flag stale reads risk → **[Suggestion]**.
- If `open-in-view` is disabled, lazy field access outside a transaction boundary → **[BLOCKER]**.

**Migration files:**
- Use `TIMESTAMP WITH TIME ZONE`, not plain `TIMESTAMP`.
- Do not add `DEFAULT NOW()` on `updated_at` — must be set explicitly.
- Do not add redundant indices.
- If a column is used in `ORDER BY` or `WHERE`, verify an index exists.

---

## D. Error Handling & Null Safety

- No silent defaults for mandatory fields — throw; never use `?: ""` or `?: null` on always-present fields.
- After every external service call, validate the response before using it.
- Do not put error-throwing checks inside a `map` — pre-validate the entire input before the batch operation.
- If a DB lookup returns null for a record that must exist at this stage, throw.

---

## E. Batch Operations & Data Consistency

- Multi-record DB inserts must use batch insert, not sequential individual inserts.
- Prefer batch/bulk fetches over looping individual fetches (N+1 → **[Suggestion]** unless volume is provably tiny).
- Duplicate check before insert must throw on duplicate, not silently skip.

---

## F. Configuration

- All tuneable values (timeouts, retry counts, queue names, service URLs, thresholds) must come from config files. Never hardcoded.
- New config keys in the config file must be bound in the corresponding config data class.

---

## G. Kotlin Language Standards

**Null safety:**
- `!!` operator → **[BLOCKER]** unless the compiler guarantees non-null via a smart cast at that exact site. Replace with `?: throw IllegalStateException("...")`.
- `!= null` checks where idiomatic safe-call (`?.`) or `let`-binding is clearer → **[Suggestion]**.

**Mutability:**
- `var` assigned exactly once and never reassigned must be `val` → **[Suggestion]**.
- Prefer immutable collection types (`List`, `Set`, `Map`) in signatures → **[Suggestion]**.

**Kotlin idioms:**
- `data class` for DTOs only — never for JPA entities.
- `sealed class`/`sealed interface` for closed hierarchies.
- `when` over sealed types must not use a trailing `else` that swallows unhandled variants → **[Suggestion]**.
- Prefer string templates over concatenation → **[Suggestion]**.
- Named arguments when a function has ≥ 3 parameters of the same or similar type → **[Suggestion]**.
- Prefer Kotlin collection functions (`map`, `filter`, `flatMap`, etc.) over imperative `for` loops that build a result → **[Suggestion]**.

**Java-isms to flag:**
- `Optional<T>` → use `T?` **[Suggestion]**
- `Collections.emptyList()` / `singletonList()` → use `emptyList()` / `listOf()` **[Suggestion]**
- `Arrays.asList(...)` → use `listOf(...)` **[Suggestion]**
- `x instanceof Foo` → use `x is Foo` **[Suggestion]**

**Scope functions:**
- `let` — null-safe ops or scoping; `apply` — object init; `also` — side effects; `run`/`with` — grouping.
- Nesting > 2 levels deep → **[Suggestion]** to extract a named function.

**Exception handling:**
- `catch (e: Exception)` with empty or log-only body → **[BLOCKER]**.
- Catch only the specific exception type(s) expected at that boundary.

**Coroutines:**
- `GlobalScope` usage → **[BLOCKER]**.
- Blocking calls inside a coroutine without `Dispatchers.IO` → **[BLOCKER]**.

---

## Re-review (when prior human reviews exist)

For each prior inline thread:
- **Resolved** — author confirmed fix, change visible in diff, or reviewer accepted.
- **Still open** — not addressed or author's response is unconvincing.

For each **still open** thread: add a `[Prior review — still open]` inline comment with the concern and a concrete resolution path.

For each author comment on their own PR: evaluate technical soundness. If not convincing, quote the claim, state why it's insufficient, describe the concrete impact if left unattended, and propose the fix.

---

## Output format

Return a JSON object with exactly these keys — no text outside the JSON:

```json
{
  "event": "REQUEST_CHANGES" | "COMMENT",
  "body": "<full overall review summary in GitHub Markdown>",
  "comments": [
    {"path": "<file>", "line": <new-file line number>, "side": "RIGHT", "body": "<comment>"}
  ]
}
```

`event` rules:
- `"REQUEST_CHANGES"` if any [BLOCKER] exists or prior review threads are still open.
- `"COMMENT"` in all other cases. **Never use "APPROVE".**

The `### What I Checked` section is **mandatory** in `body`:

```
## PR Review

**Verdict: [Request Changes | Comment]**

### What I Checked
- **Dependency check**: [passed / failed / skipped — reason]
- **CLAUDE.md**: [root + N modules read / not found]
- **Architecture & request flow**: checked against [pattern]
- **External service contracts**: [N snippets provided / none]
- **Kotlin language standards**: [N issues found / no issues]
- **Database patterns**: [JOOQ / JPA / not applicable]
- **Re-review threads**: [N threads / not a re-review]
- **Author comments**: [N evaluated / none]

### What looks good
...

### Blockers
...

### Suggestions
...
```

Line numbers in comments: derive from the diff hunk headers (`@@ -old +new,count @@`).
Walk the hunk: advance the new-file counter for context lines and `+` lines; do NOT advance for `-` lines.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _popen(cmd: list[str]) -> subprocess.Popen:
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def _run(cmd: list[str], timeout: int = 15) -> str:
    if not cmd or cmd[0] not in _ALLOWED_CMDS:
        raise ValueError(f"Command not in allowlist: {cmd[0]!r}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _gh_read_file(repo: str, path: str, branch: str) -> Optional[str]:
    raw = _run(["gh", "api", f"repos/{repo}/contents/{path}?ref={branch}",
                "--jq", ".content"], timeout=12)
    if not raw:
        return None
    try:
        return base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception:
        return None


def fail(reason: str, code: int = 1) -> None:
    print(f"\nJARVIS_FIX_FAILED={reason}")
    sys.exit(code)


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------

def _gather_context(repo: str, pr_num: str) -> dict:
    print(f"[{_ts()}] {PROG_FETCHING}…")

    # Fire parallel gh fetches
    procs = {
        "diff":     _popen(["gh", "pr", "diff", pr_num, "--repo", f"{GITHUB_ORG}/{repo}"]),
        "meta":     _popen(["gh", "pr", "view", pr_num, "--repo", f"{GITHUB_ORG}/{repo}",
                             "--json", "number,title,body,headRefName,baseRefName,"
                                       "files,additions,deletions,state,reviews,reviewDecision"]),
        "reviews":  _popen(["gh", "api",
                             f"repos/{GITHUB_ORG}/{repo}/pulls/{pr_num}/reviews",
                             "--jq", "[.[] | {id:.id, user:.user.login, state:.state, body:.body}]"]),
        "threads":  _popen(["gh", "api",
                             f"repos/{GITHUB_ORG}/{repo}/pulls/{pr_num}/comments",
                             "--jq", "[.[] | {id:.id, path:.path, line:.line, "
                                     "user:.user.login, body:.body, in_reply_to_id:.in_reply_to_id}]"]),
        "pr_cmts":  _popen(["gh", "api",
                             f"repos/{GITHUB_ORG}/{repo}/issues/{pr_num}/comments",
                             "--jq", "[.[] | {id:.id, user:.user.login, body:.body}]"]),
        "def_br":   _popen(["gh", "api", f"repos/{GITHUB_ORG}/{repo}", "--jq", ".default_branch"]),
    }
    ctx = {k: p.communicate()[0].decode("utf-8", errors="replace").strip()
           for k, p in procs.items()}

    diff = ctx.get("diff", "")
    branch = ctx.get("def_br", "main")
    diff_len = len(diff)
    diff_truncated = diff[:DIFF_BUDGET]
    if diff_len > DIFF_BUDGET:
        diff_truncated += f"\n... [truncated — {diff_len - DIFF_BUDGET} more chars]"
    ctx["diff_truncated"] = diff_truncated

    print(f"[{_ts()}] diff {diff_len} chars; branch={branch}")

    # CLAUDE.md files for touched modules
    modules = _extract_modules(diff)
    claude_mds: dict[str, str] = {}
    for module in [""] + modules:
        path = f"{module}/CLAUDE.md" if module else "CLAUDE.md"
        content = _gh_read_file(f"{GITHUB_ORG}/{repo}", path, branch)
        if content:
            claude_mds[path] = content
    ctx["claude_mds"] = claude_mds
    print(f"[{_ts()}] CLAUDE.md found: {list(claude_mds.keys()) or 'none'}")

    # Service contract snippets via Jarvis search_code
    ctx["contracts"] = _search_contracts(diff)

    # Optional Gradle dep check
    ctx["dep_check"] = _run_dep_check(repo, ctx)

    return ctx


def _extract_modules(diff: str) -> list[str]:
    modules: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            parts = line.split(" b/")
            if len(parts) == 2:
                top = parts[1].split("/")[0]
                if "." not in top:
                    modules.add(top)
    return sorted(modules)


def _search_contracts(diff: str) -> str:
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from agent.tools import search_code  # type: ignore
    except ImportError:
        return ""

    hints = [ln[1:].strip() for ln in diff.splitlines()
             if ln.startswith("+") and ("import" in ln or ".yaml" in ln.lower())]
    if not hints:
        return ""

    query = "OpenAPI spec OR proto definition: " + " ".join(hints[:6])
    try:
        result = search_code(query, k=4)
        hits = result.get("hits", [])
        if not hits:
            return ""
        parts = ["### Service contract snippets (Jarvis index)"]
        for h in hits:
            parts.append(f"**{h['repo']}:{h['path']}** (score {h['score']:.2f})")
            parts.append(h["snippet"])
        return "\n".join(parts)
    except Exception:
        return ""


def _run_dep_check(repo: str, ctx: dict) -> str:
    art_user = os.environ.get("ARTIFACTORY_USER", "")
    art_pass = os.environ.get("ARTIFACTORY_PASSWORD", "")
    if not art_user or not art_pass:
        return "Dependency check skipped — ARTIFACTORY_USER / ARTIFACTORY_PASSWORD not set"

    try:
        meta = json.loads(ctx.get("meta", "{}"))
        branch = meta.get("headRefName", "")
        if not branch:
            return "Dependency check skipped — could not determine PR branch"

        gh_token = subprocess.check_output(["gh", "auth", "token"],
                                           text=True, timeout=10).strip()
        with tempfile.TemporaryDirectory(prefix="jarvis-kr-") as tmpdir:
            clone_url = f"https://x-access-token:{gh_token}@github.com/{GITHUB_ORG}/{repo}"
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", branch, clone_url, tmpdir],
                check=True, capture_output=True, timeout=120,
            )
            env = {**os.environ, "ARTIFACTORY_USER": art_user,
                   "ARTIFACTORY_PASSWORD": art_pass, "GITHUB_TOKEN": gh_token}
            result = subprocess.run(
                ["./gradlew", "dependencies", "--configuration", "compileClasspath"],
                cwd=tmpdir, capture_output=True, text=True, timeout=180, env=env,
            )
            output = result.stdout + result.stderr
            bom_idx = output.find("BOM Version Override Detected")
            if bom_idx != -1:
                return output[bom_idx:bom_idx + 2000]
            if result.returncode != 0:
                return "Dependency resolution FAILED:\n" + output[-500:]
            return "Dependency resolution passed. No BOM overrides detected."
    except Exception as e:
        return f"Dependency check error: {e}"


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _build_user_message(repo: str, pr_num: str, ctx: dict, requester: str) -> str:
    parts = [
        f"## PR: {GITHUB_ORG}/{repo}#{pr_num}",
        "## Metadata\n" + ctx.get("meta", "(none)"),
        "## Diff\n```diff\n" + ctx.get("diff_truncated", "(empty)") + "\n```",
    ]

    reviews = ctx.get("reviews", "[]")
    if reviews and reviews != "[]":
        parts.append("## Prior Reviews\n" + reviews)

    threads = ctx.get("threads", "[]")
    if threads and threads != "[]":
        parts.append("## Inline Threads\n" + threads)

    pr_cmts = ctx.get("pr_cmts", "[]")
    if pr_cmts and pr_cmts != "[]":
        parts.append("## PR-Level Comments\n" + pr_cmts)

    for path, content in ctx.get("claude_mds", {}).items():
        parts.append(f"## CLAUDE.md: {path}\n{content}")

    if ctx.get("contracts"):
        parts.append(ctx["contracts"])

    parts.append("## Dependency Check\n" + ctx.get("dep_check", "(not run)"))
    parts.append(f"\n_Triggered by {requester} via `{SLASH_COMMAND} nitpick`_")

    return "\n\n---\n\n".join(parts)


def _run_review(user_message: str) -> Optional[dict]:
    client = Anthropic()
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=REVIEW_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw), response
    except Exception as e:
        print(f"[{_ts()}] LLM call failed: {e}")
        return None, None


# ---------------------------------------------------------------------------
# Post review
# ---------------------------------------------------------------------------

def _post_review(repo: str, pr_num: str, review: dict) -> Optional[str]:
    body = review.get("body", "")
    comments = review.get("comments", [])
    event = review.get("event", "REQUEST_CHANGES" if "[BLOCKER]" in body else "COMMENT")

    cmd = [
        "gh", "api", f"repos/{GITHUB_ORG}/{repo}/pulls/{pr_num}/reviews",
        "--method", "POST",
        "--field", f"body={body}",
        "--field", f"event={event}",
    ]
    for c in comments:
        cmd += [
            "--field", f"comments[][path]={c['path']}",
            "--field", f"comments[][line]={c['line']}",
            "--field", f"comments[][side]={c.get('side', 'RIGHT')}",
            "--field", f"comments[][body]={c['body']}",
        ]

    assert cmd and cmd[0] in _ALLOWED_CMDS, f"Unexpected command: {cmd[0]!r}"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"[{_ts()}] gh post failed: {result.stderr[:300]}")
            return None
        data = json.loads(result.stdout)
        return data.get("html_url")
    except Exception as e:
        print(f"[{_ts()}] post error: {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        fail("usage: jarvis_nitpick.py <pr-url> [requester]", 2)

    pr_url = sys.argv[1].strip()
    requester = sys.argv[2] if len(sys.argv) > 2 else "cli"

    m = PR_URL_RE.search(pr_url)
    if not m:
        fail(f"invalid PR URL — expected https://github.com/{GITHUB_ORG}/<repo>/pull/<num>", 2)
    repo, pr_num = m.group(1), m.group(2)

    # Audit setup
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    task_id = f"nitpick-{ts}-{repo}-{pr_num}"
    audit_path = ROOT_DIR / "logs" / "nitpick_audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    def audit(event: str, **kw):
        rec = {"task_id": task_id, "event": event, "repo": repo, "pr_num": int(pr_num),
               "requester": requester,
               "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
               **kw}
        with audit_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    audit("start")
    print(f"[{_ts()}] task_id={task_id}  pr={GITHUB_ORG}/{repo}#{pr_num}")

    started = time.time()

    # Phase 1: Gather context
    ctx = _gather_context(repo, pr_num)

    # Phase 2: Analyze
    print(f"[{_ts()}] {PROG_REVIEWING}…")
    audit("llm_started")
    user_msg = _build_user_message(repo, pr_num, ctx, requester)
    review, llm_response = _run_review(user_msg)
    if review is None:
        audit("llm_failed")
        fail("LLM analysis failed — check jarvis-slack logs")

    duration_s = round(time.time() - started, 1)
    n_comments = len(review.get("comments", []))
    event = review.get("event", "COMMENT")
    print(f"[{_ts()}] review generated in {duration_s}s: event={event} inline_comments={n_comments}")

    # Cost estimate (Sonnet 4.6)
    if llm_response:
        u = llm_response.usage
        cost = round(
            (u.input_tokens * 3 + getattr(u, "cache_read_input_tokens", 0) * 0.30
             + getattr(u, "cache_creation_input_tokens", 0) * 3.75
             + u.output_tokens * 15) / 1_000_000, 4)
    else:
        cost = 0.0

    # Phase 3: Post
    print(f"[{_ts()}] {PROG_POSTING}…")
    review_url = _post_review(repo, pr_num, review)
    if review_url is None:
        audit("post_failed", duration_sec=duration_s, cost_usd=cost)
        fail("could not post review to GitHub — check jarvis-slack logs")

    audit("success", review_url=review_url, duration_sec=duration_s, cost_usd=cost,
          event=event, inline_comments=n_comments)
    print(f"\n✅ DONE in {duration_s}s — cost ${cost}")
    print(f"JARVIS_PR_URL={review_url}")
    sys.exit(0)


if __name__ == "__main__":
    main()
