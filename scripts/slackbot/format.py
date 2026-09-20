"""Convert markdown answers from Jarvis into Slack Block Kit / mrkdwn payloads.

Slack's mrkdwn is similar to but distinct from CommonMark:
  - Bold uses single asterisks (*X*) not double (**X**)
  - No native headings — use bold lines as visual separators
  - No native tables — render as code blocks for column alignment
  - Links are <url|text> not [text](url)
  - Bullets need to be rendered as • (Slack ignores leading - and *)

Output is a list of Block Kit blocks. Each section block's text has a 3000 char hard
limit — we split long answers into multiple section blocks at paragraph boundaries.
"""
from __future__ import annotations
import re
from typing import Iterable
from agent.config import BOT_NAME, SLASH_COMMAND, GITHUB_ORG, COMPANY_NAME

SECTION_TEXT_MAX = 2900  # leave headroom under the 3000 hard limit


# --- markdown → Slack mrkdwn conversion ----------------------------------------

_FENCE_PLACEHOLDER = "\x00FENCE{}\x00"
_INLINE_PLACEHOLDER = "\x00INLINE{}\x00"


def _protect_code(md: str) -> tuple[str, list[str], list[str]]:
    fences: list[str] = []
    inlines: list[str] = []

    def fence_repl(m: re.Match) -> str:
        fences.append(m.group(0))
        return _FENCE_PLACEHOLDER.format(len(fences) - 1)

    md = re.sub(r"```[\s\S]*?```", fence_repl, md)

    def inline_repl(m: re.Match) -> str:
        inlines.append(m.group(0))
        return _INLINE_PLACEHOLDER.format(len(inlines) - 1)

    md = re.sub(r"`[^`\n]+`", inline_repl, md)
    return md, fences, inlines


def _restore_code(text: str, fences: list[str], inlines: list[str]) -> str:
    for i, c in enumerate(inlines):
        text = text.replace(_INLINE_PLACEHOLDER.format(i), c)
    for i, c in enumerate(fences):
        text = text.replace(_FENCE_PLACEHOLDER.format(i), c)
    return text


def _convert_tables(md: str) -> str:
    """Tables → fenced code block (Slack renders monospace, columns line up)."""
    out_lines: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]):
            # found a table
            header = _split_row(lines[i])
            i += 2
            rows = [header]
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            # column widths
            cols = max(len(r) for r in rows)
            widths = [0] * cols
            for r in rows:
                for j, c in enumerate(r):
                    widths[j] = max(widths[j], len(c))
            sep = "  "
            block = ["```"]
            block.append(sep.join(c.ljust(widths[j]) for j, c in enumerate(rows[0])))
            block.append(sep.join("-" * widths[j] for j in range(cols)))
            for r in rows[1:]:
                block.append(sep.join(c.ljust(widths[j]) for j, c in enumerate(r)))
            block.append("```")
            out_lines.extend(block)
            continue
        out_lines.append(lines[i])
        i += 1
    return "\n".join(out_lines)


def _split_row(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def md_to_slack(md: str) -> str:
    md = _convert_tables(md)
    md, fences, inlines = _protect_code(md)

    # Headings (#, ##, ###) → bold line
    md = re.sub(r"^\s*#{1,6}\s+(.*)$", r"*\1*", md, flags=re.MULTILINE)

    # Bold: **X** → *X* (mrkdwn). Do this AFTER heading conversion (which uses *).
    md = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", md)

    # Italic: __X__ or _X_ → _X_ (already valid)
    # Links [text](url) → <url|text>
    md = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", md)

    # Bullets: -, * at line start → •
    md = re.sub(r"^(\s*)[-*]\s+", r"\1• ", md, flags=re.MULTILINE)

    # Horizontal rules (---) → spacing
    md = re.sub(r"^---+$", "—" * 8, md, flags=re.MULTILINE)

    # Collapse 3+ blank lines
    md = re.sub(r"\n{3,}", "\n\n", md)

    return _restore_code(md, fences, inlines).strip()


# --- chunking into Block Kit sections ------------------------------------------

def _chunks(text: str, limit: int = SECTION_TEXT_MAX) -> Iterable[str]:
    """Split on blank-line paragraph boundaries; if a single paragraph is too big,
    split on lines; if a single line is too big, hard-cut."""
    if len(text) <= limit:
        yield text
        return

    paras = text.split("\n\n")
    buf = ""
    for p in paras:
        if len(p) > limit:
            # paragraph itself too big — flush buf, then split on lines
            if buf:
                yield buf
                buf = ""
            for line in p.split("\n"):
                while len(line) > limit:
                    yield line[:limit]
                    line = line[limit:]
                if buf and len(buf) + len(line) + 1 > limit:
                    yield buf
                    buf = line
                else:
                    buf = (buf + "\n" + line) if buf else line
            continue
        if buf and len(buf) + len(p) + 2 > limit:
            yield buf
            buf = p
        else:
            buf = (buf + "\n\n" + p) if buf else p
    if buf:
        yield buf


# --- Block Kit builders ---------------------------------------------------------

def ack_blocks(question: str, turn_n: int = 1) -> list[dict]:
    q = question.strip()
    if len(q) > 200:
        q = q[:200] + "…"
    if turn_n > 1:
        intro = f":thread: *Turn {turn_n}* · :robot_face: *Astra is thinking…*"
    else:
        intro = ":robot_face: *Astra is thinking…*"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{intro}\n> {q}"}}
    ]


def answer_to_blocks(question: str, result, turn_n: int = 1, qid: str | None = None) -> list[dict]:
    """Build Slack blocks for a finished answer. `result` is a RunResult.

    If `qid` is provided, append 👍/👎 feedback buttons keyed to that question ID.
    """
    q = question.strip()
    if len(q) > 200:
        q = q[:200] + "…"

    header_elements: list[dict] = []
    if turn_n > 1:
        header_elements.append({"type": "mrkdwn",
            "text": f":thread: *Turn {turn_n}* · using your prior conversation as context"})
    else:
        header_elements.append({"type": "mrkdwn", "text": ":robot_face: *Astra* answered:"})
    header_elements.append({"type": "mrkdwn", "text": f"_{q}_"})

    blocks: list[dict] = [
        {"type": "context", "elements": header_elements},
        {"type": "divider"},
    ]

    answer = md_to_slack(result.answer or "_(no answer produced)_")
    for chunk in _chunks(answer):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})

    if turn_n == 1:
        followup_hint = f":bulb: Follow-ups: just keep typing `{SLASH_COMMAND} ...` within 10 minutes — {BOT_NAME} will remember this conversation. Type `{SLASH_COMMAND} -new ...` to start fresh."
    else:
        followup_hint = f":bulb: Continue with another `{SLASH_COMMAND} ...` (within 10 min). `{SLASH_COMMAND} -new ...` resets the conversation."

    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": (
            f"⚙️ {result.iterations} iters · "
            f"{len(result.tool_calls)} tool calls · "
            f"{result.elapsed_sec}s"
        )},
        {"type": "mrkdwn", "text": followup_hint},
    ]})

    if qid:
        blocks.append({
            "type": "actions",
            "block_id": f"fb-{qid}",
            "elements": [
                {
                    "type": "button",
                    "action_id": "feedback_up",
                    "value": qid,
                    "text": {"type": "plain_text", "text": "👍 Helpful"},
                },
                {
                    "type": "button",
                    "action_id": "feedback_down",
                    "value": qid,
                    "text": {"type": "plain_text", "text": "👎 Needs work"},
                    "style": "danger",
                },
            ],
        })
    return blocks


def feedback_thanks_blocks(original_blocks: list[dict], rating: str) -> list[dict]:
    """Return the original answer blocks with buttons replaced by a thank-you context."""
    new_blocks = [b for b in original_blocks if b.get("type") != "actions"]
    icon = "👍 Helpful" if rating == "up" else "👎 Needs work"
    new_blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": f":sparkles: Feedback recorded: *{icon}* — thanks! "
                    f"{'Reply in thread or DM if you want to add more detail.' if rating == 'down' else ''}",
        }],
    })
    return new_blocks


def help_blocks() -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":robot_face: *{BOT_NAME}* — AI engineering platform for your organization\n\n"
            f"*Ask a question:*  `{SLASH_COMMAND} <your question>`\n"
            f"*Follow up:*  just keep typing `{SLASH_COMMAND} ...` within 10 minutes — {BOT_NAME} remembers your last conversation\n"
            f"*Start fresh:*  `{SLASH_COMMAND} -new <new question>` resets the conversation\n\n"
            f"*Open a draft PR (pilot, allowlisted repos only):*\n"
            f"  `{SLASH_COMMAND} fix <repo>: <what needs to change>`\n"
            f"  Example: `{SLASH_COMMAND} fix bff-core: add x-trace-id to standard-headers.ts`\n"
            f"  {BOT_NAME} investigates, commits to a `{BOT_NAME.lower()}/...` branch, opens a *draft* PR for your review.\n\n"
            f"*Claudify a repo (any repo in `{GITHUB_ORG}/`):*\n"
            f"  `{SLASH_COMMAND} claudify <repo>`\n"
            f"  Example: `{SLASH_COMMAND} claudify card-mandates`\n"
            f"  Runs the official Project Claudify orchestrator — generates per-module + root CLAUDE.md, "
            f"opens a PR on `add-claude-md-docs` with the `Claudify` label and cost in the body. "
            f"Skips if there's already an open Claudify PR.\n\n"
            f"*Cross-repo PR review (NEW):*\n"
            f"  `{SLASH_COMMAND} review <repo>#<num>` or `{SLASH_COMMAND} review <full-PR-URL>`\n"
            f"  Example: `{SLASH_COMMAND} review gateway#8027`\n"
            f"  Posts a cross-repo impact analysis comment on the PR — finds consumers of changed symbols "
            f"across all indexed repos. Best for PRs touching shared libraries / OpenAPI specs / "
            f"Kafka topics. ~30-90s, ~$0.20-0.50 per review.\n\n"
            f"*Confluence-scoped Q&A (via Jove):*\n"
            f"  `{SLASH_COMMAND} ask <space>: <question>`  — friendly names work: `Technology`, `Product`, `Data Science`, etc.\n"
            f"  Examples:\n"
            f"    `{SLASH_COMMAND} ask Technology: what's our standard auth pattern for new microservices?`\n"
            f"    `{SLASH_COMMAND} ask product: rollout plan for credit-on-UPI?`\n"
            f"    `{SLASH_COMMAND} ask DS: rNPS calculation for Savings?`\n"
            f"  Jove ({COMPANY_NAME.lower()}-product-expert) answers from Confluence — 108 spaces indexed.\n\n"
            f"*Force a fresh re-pull of a Confluence space:*\n"
            f"  `{SLASH_COMMAND} refresh <space>`\n"
            f"  Examples: `{SLASH_COMMAND} refresh GrowthX` (~24s · inline progress) · `{SLASH_COMMAND} refresh Technology` (~74min · async DMs).\n"
            f"  Triggers Jove to re-fetch live Confluence content. Use when you just edited a page and want answers to reflect it.\n\n"
            f"*Production debugging (NEW):*\n"
            f"  `{SLASH_COMMAND} investigate <alert-name-or-issue-description>`\n"
            f"  Examples:\n"
            f"    `{SLASH_COMMAND} investigate CMS New Card success rate less than 90 percent`\n"
            f"    `{SLASH_COMMAND} investigate why is loan-application-creation p99 spiking`\n"
            f"  Returns a structured runbook: source / code flow / log statements / ranked failure modes. "
            f"Same analysis Sumith's SRE bot already gets via the HTTP API.\n\n"
            f"*Examples (Q&A):*\n"
            f"• `{SLASH_COMMAND} where does Stargate route /accounts requests`\n"
            f"• `{SLASH_COMMAND} why was LENDER_SELECTION renamed to PROGRAM_SELECTION?`\n"
            f"• `{SLASH_COMMAND} explain the CKYC flow end to end`\n\n"
            f"*Indexed:* every active code repo in `{GITHUB_ORG}` + recent PR descriptions.\n\n"
            f"Q&A answers take 30–60s; fix-mode takes 1–3 min. Replies are visible only to you. "
            f"If something looks wrong, ping <@U0837N31T9C> directly so we can tune."
        }}
    ]


def fix_ack_blocks(repo: str, description: str, branch_preview: str) -> list[dict]:
    desc = description if len(description) <= 240 else description[:240] + "…"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":wrench: *{BOT_NAME} is on it.*\n"
            f"*Repo:* `{repo}`  ·  *Branch:* `{branch_preview}`\n"
            f"*Task:* _{desc}_"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            ":hourglass: _starting…_   ETA 1–3 min. I'll keep this message updated."}]},
    ]


def fix_progress_blocks(repo: str, description: str, branch_preview: str,
                         status_emoji: str, status_text: str,
                         elapsed_sec: float) -> list[dict]:
    """Update the in-flight fix message with a live status line."""
    desc = description if len(description) <= 240 else description[:240] + "…"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":wrench: *{BOT_NAME} is on it.*\n"
            f"*Repo:* `{repo}`  ·  *Branch:* `{branch_preview}`\n"
            f"*Task:* _{desc}_"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"{status_emoji} _{status_text}_   ·   {elapsed_sec:.0f}s elapsed"}]},
    ]


def fix_success_blocks(repo: str, description: str, pr_url: str,
                       elapsed_sec: float, cost: float | None = None) -> list[dict]:
    desc = description if len(description) <= 240 else description[:240] + "…"
    pr_num = pr_url.rstrip("/").rsplit("/", 1)[-1]
    cost_str = f" · est ${cost:.2f}" if cost is not None else ""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":white_check_mark: *Draft PR opened* — please review.\n\n"
            f"<{pr_url}|*PR #{pr_num}* in `{repo}`>\n"
            f"_Task:_ {desc}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f":robot_face: Done in {elapsed_sec:.0f}s{cost_str}. "
            f"Review carefully — {BOT_NAME} can't run your test suite. "
            f"Click 👍/👎 below if useful (helps us tune)."}]},
    ]


def fix_failed_blocks(repo: str, description: str, reason: str) -> list[dict]:
    desc = description if len(description) <= 240 else description[:240] + "…"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":warning: *{BOT_NAME} stopped without opening a PR.*\n\n"
            f"*Repo:* `{repo}`\n"
            f"*Task:* _{desc}_\n"
            f"*Reason:* {reason}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            "Try a more specific description, or ping <@U0837N31T9C> for help."}]},
    ]


def fix_rejected_blocks(repo: str, allowed: list[str]) -> list[dict]:
    if allowed:
        allowed_str = ", ".join(f"`{r}`" for r in allowed)
        body = (
            f":lock: Fix mode is enabled only for: {allowed_str}\n"
            f"`{repo}` isn't in the allowlist yet. Ping <@U0837N31T9C> to add it.")
    else:
        body = (":lock: Fix mode is not enabled for any repo right now. "
                "Ping <@U0837N31T9C> to allowlist a pilot repo.")
    return [{"type": "section", "text": {"type": "mrkdwn", "text": body}}]


def fix_busy_blocks() -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text":
        ":hourglass_flowing_sand: You already have a fix in progress. "
        "Wait for it to finish (or fail) before starting another."}}]


def fix_usage_blocks() -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text":
        f":wrench: Fix-mode usage: `{SLASH_COMMAND} fix <repo>: <what needs to change>`\n\n"
        f"Example: `{SLASH_COMMAND} fix bff-core: add x-trace-id to standard-headers.ts`\n\n"
        "The colon between the repo and the description is required — that's how "
        "I tell apart a fix request from a question."}}]


# --- Claudify-mode blocks ---------------------------------------------------

def claudify_ack_blocks(repo: str, branch_preview: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":books: *{BOT_NAME} is claudifying* `{repo}`.\n"
            f"*Branch:* `{branch_preview}`  ·  *PR label:* `Claudify`\n"
            f"_Runs the official Project Claudify orchestrator (per-module + root CLAUDE.md, MCP-parseable tables, cost reported in PR body)._"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            ":hourglass: _starting…_   ETA 3–15 min depending on repo size + module count. I'll keep this message updated."}]},
    ]


def claudify_progress_blocks(repo: str, branch_preview: str,
                              status_emoji: str, status_text: str,
                              elapsed_sec: float) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":books: *{BOT_NAME} is claudifying* `{repo}`.\n"
            f"*Branch:* `{branch_preview}`  ·  *PR label:* `Claudify`"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"{status_emoji} _{status_text}_   ·   {elapsed_sec:.0f}s elapsed"}]},
    ]


def claudify_success_blocks(repo: str, pr_url: str, elapsed_sec: float) -> list[dict]:
    pr_num = pr_url.rstrip("/").rsplit("/", 1)[-1]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":white_check_mark: *PR opened* — please review.\n\n"
            f"<{pr_url}|*PR #{pr_num}* in `{repo}`> · _Project Claudify · label: `Claudify`_"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f":robot_face: Done in {elapsed_sec:.0f}s. PR body has the cost + duration table. "
            f"Please review CLAUDE.md content for accuracy on build commands, network topology, "
            f"and squad-specific conventions before merging."}]},
    ]


def claudify_usage_blocks() -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text":
        f":books: Claudify usage: `{SLASH_COMMAND} claudify <repo>`\n\n"
        f"Example: `{SLASH_COMMAND} claudify card-mandates`\n\n"
        f"Runs the official Project Claudify orchestrator for the repo: generates per-module + root "
        f"CLAUDE.md files, opens a PR on branch `add-claude-md-docs` with the `Claudify` label, "
        f"cost + duration table in the body. Works on any repo in `{GITHUB_ORG}/`. Skips safely "
        f"if there's already an open Claudify PR."}}]


# --- Review-mode blocks ----------------------------------------------------

def review_ack_blocks(repo: str, pr_num: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":mag: *{BOT_NAME} is reviewing* `{repo}#{pr_num}`.\n"
            f"_Cross-repo impact analysis — searches all indexed repos for consumers of "
            f"changed symbols (OpenAPI paths, Kotlin classes, Kafka topics, Stargate routes). "
            f"Posts ONE review comment to the PR with cited file:line references._"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            ":hourglass: _starting…_   ETA 30-90s for typical PRs. I'll keep this message updated."}]},
    ]


def review_progress_blocks(repo: str, pr_num: str, status_emoji: str,
                            status_text: str, elapsed_sec: float) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":mag: *{BOT_NAME} is reviewing* `{repo}#{pr_num}`."}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"{status_emoji} _{status_text}_   ·   {elapsed_sec:.0f}s elapsed"}]},
    ]


def review_success_blocks(repo: str, pr_num: str, comment_url: str,
                            elapsed_sec: float) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":white_check_mark: *Cross-repo review posted* on <https://github.com/{GITHUB_ORG}/{repo}/pull/{pr_num}|`{repo}#{pr_num}`>.\n\n"
            f"<{comment_url}|View {BOT_NAME} comment on PR>"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f":robot_face: Done in {elapsed_sec:.0f}s. The PR author + reviewers will see the comment. "
            f"This is a *cross-repo impact* review only — for intra-repo correctness, use Cursor / "
            f"GitHub Copilot / human review."}]},
    ]


# --- Investigate-mode blocks -----------------------------------------------

def investigate_ack_blocks(description: str) -> list[dict]:
    desc = description if len(description) <= 240 else description[:240] + "…"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":mag_right: *{BOT_NAME} is investigating.*\n*Issue:* _{desc}_\n"
            f"_Structured incident analysis: source / code flow / log statements / "
            f"failure modes ranked by probability._"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            ":hourglass: _starting…_   ETA 30-90s. I'll replace this message with the analysis."}]},
    ]


def investigate_usage_blocks() -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text":
        f":mag_right: Investigate usage: `{SLASH_COMMAND} investigate <alert-name-or-issue-description>`\n\n"
        "Examples:\n"
        f"  `{SLASH_COMMAND} investigate CMS New Card success rate less than 90%`\n"
        f"  `{SLASH_COMMAND} investigate why is the loan-application-creation latency p99 spiking`\n"
        f"  `{SLASH_COMMAND} investigate Redemption: eVoucher Execute API failing for tenant X`\n\n"
        "Returns a structured runbook: *source* (which repo + file emits the metric), "
        "*complete code flow* (entry → business logic → downstream → metric emission), "
        "*log statements* in that flow (with format strings for Kibana grep), and "
        "*ranked failure modes* with concrete investigation commands.\n\n"
        "Best for: Prometheus alerts, Grafana panel anomalies, customer-reported issues, "
        "production debugging starts. Same Jarvis-quality reasoning Sumith's SRE bot already "
        "gets via the HTTP API."}}]


# --- Ask-Jove mode blocks --------------------------------------------------

def ask_ack_blocks(space: str, question: str, refresh: bool = False) -> list[dict]:
    q = question if len(question) <= 240 else question[:240] + "…"
    refresh_note = " · *refresh requested* (Jove will re-fetch live Confluence)" if refresh else ""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":telescope: *Asking Jove about Confluence space* `{space}`{refresh_note}\n"
            f"*Question:* _{q}_"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            ":hourglass: _querying Jove…_   ETA 20-90s. Jove searches the indexed pages and synthesises an answer."}]},
    ]


def ask_success_blocks(space: str, question: str, answer_md: str,
                        elapsed_sec: float, refresh: bool = False) -> list[dict]:
    q = question if len(question) <= 200 else question[:200] + "…"
    # Add a copy-friendly separator at the top — the divider block doesn't
    # survive copy/paste, so a literal "---" in the text helps when engineers
    # quote chunks of the answer.
    answer_md_with_sep = "---\n\n" + answer_md.lstrip()
    # Slack section text limit ~3000 chars; chunk the answer
    answer_chunks: list[str] = []
    remaining = answer_md_with_sep
    CHUNK = 2800
    while remaining:
        answer_chunks.append(remaining[:CHUNK])
        remaining = remaining[CHUNK:]
    refresh_note = " (refreshed)" if refresh else ""
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":telescope: *Jove answer* — Confluence space `{space}`{refresh_note}\n"
            f"*Q:* _{q}_"}},
        {"type": "divider"},
    ]
    for chunk in answer_chunks[:5]:  # cap at 5 chunks (~14k chars) — safety
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
    if len(answer_chunks) > 5:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
            f":scissors: _answer truncated ({len(answer_chunks) - 5} more chunks suppressed for Slack length limit)_"}]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
        f":robot_face: Done in {elapsed_sec:.0f}s. Source: Jove (jupiter-product-expert) · "
        f"scoped to space `{space}`. To follow up on this thread, just send another `{SLASH_COMMAND} ask {space}: ...` "
        f"within 10 minutes — {BOT_NAME} will remember this context. Type `{SLASH_COMMAND} -new ...` to start fresh."}]})
    return blocks


def ask_usage_blocks() -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text":
        ":telescope: Ask-Jove usage:\n"
        f"  `{SLASH_COMMAND} ask <space-key>: <your question>`\n"
        f"  `{SLASH_COMMAND} ask <space-key> refresh: <your question>` (force a fresh fetch from Confluence)\n\n"
        "Examples:\n"
        f"  `{SLASH_COMMAND} ask TECH: what's our standard auth pattern for new microservices?`\n"
        f"  `{SLASH_COMMAND} ask PROD: what's the rollout plan for the credit-on-UPI feature?`\n"
        f"  `{SLASH_COMMAND} ask DS refresh: what's the rNPS calculation for Savings, latest version?`\n\n"
        "Jove (jupiter-product-expert) answers using its indexed Confluence corpus, *scoped to the space you specify*. "
        "Top spaces by volume: `TECH` (4.4k pages), `PROD` (4k), `DS` (3.9k). 108 spaces indexed total. "
        "If you give an unknown space-key I'll suggest the closest matches."}}]


def ask_unknown_space_blocks(input_key: str, suggestions: list[str]) -> list[dict]:
    sug = ", ".join(f"`{k}`" for k in suggestions) if suggestions else "(no close matches)"
    return [{"type": "section", "text": {"type": "mrkdwn", "text":
        f":warning: Confluence space `{input_key}` is not indexed by Jove.\n\n"
        f"*Did you mean one of these?*  {sug}\n\n"
        f"_Top spaces by page-count:_ `Technology` · `Product` · `Data Science` · `Growth` · `Customer Experience`. "
        f"Run `{SLASH_COMMAND} ask` (no args) for full usage. If you think the space should be indexed, ping <@U0837N31T9C>."}}]


def ask_disambiguation_blocks(input_key: str, candidates: list[str]) -> list[dict]:
    """When user input matches multiple spaces, show options."""
    lines = []
    from agent.jove_client import space_label, list_spaces
    spaces = list_spaces()
    for k in candidates[:8]:
        s = spaces.get(k, {})
        lines.append(f"   • `{space_label(k)}` — {s.get('page_count', '?')} pages")
    return [{"type": "section", "text": {"type": "mrkdwn", "text":
        f":mag: `{input_key}` matched multiple spaces. Pick one and try again:\n\n"
        + "\n".join(lines) + "\n\n"
        f"_Tip: use the canonical `space_key` (in parens) for a deterministic pick. "
        f"Or be more specific: `Customer Experience` or `Customer Success` instead of just `customer`._"
        }}]


# --- Refresh-Jove blocks ---------------------------------------------------

def _fmt_eta(sec: int) -> str:
    if sec < 60:
        return f"{sec}s"
    m = sec // 60
    s = sec % 60
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h = m // 60
    m = m % 60
    return f"{h}h {m}m"


def refresh_inline_progress_blocks(space_label: str, run_id: str, pages_done: int,
                                    eta_sec: int, elapsed_sec: int,
                                    pages_total: int = 0) -> list[dict]:
    bar_len = 20
    if pages_total > 0:
        pct = min(100, int(100 * pages_done / pages_total))
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        progress = f"`{bar}`  {pct}%  ({pages_done:,} / {pages_total:,} pages)"
    else:
        progress = "_(discovering pages…)_"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":arrows_counterclockwise: *Refreshing* `{space_label}` _(via Jove)_\n{progress}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f":hourglass: {_fmt_eta(elapsed_sec)} elapsed  ·  ETA ~{_fmt_eta(max(eta_sec - elapsed_sec, 5))}  ·  run_id `{run_id[:8]}`"}]},
    ]


def refresh_inline_complete_blocks(space_label: str, run_id: str,
                                    status_payload: dict, elapsed_sec: int) -> list[dict]:
    status = status_payload.get("status", "?")
    pages_indexed = status_payload.get("pages_indexed", 0)
    pages_total = status_payload.get("pages_discovered", 0)
    pages_failed = status_payload.get("pages_failed", 0)
    pages_skipped = status_payload.get("pages_skipped", 0)
    if status == "completed":
        head = f":white_check_mark: *Refreshed* `{space_label}` in {_fmt_eta(elapsed_sec)}"
        body = (f"   • {pages_indexed:,} pages re-indexed  ({pages_total:,} discovered)\n"
                f"   • {pages_failed} failed  ·  {pages_skipped} skipped\n\n"
                f"_Try:_ `{SLASH_COMMAND} ask {space_label.split('(')[0].strip()}: <your question>`")
    else:
        head = f":x: *Refresh of* `{space_label}` *failed* after {_fmt_eta(elapsed_sec)}"
        body = (f"   • {pages_indexed:,} pages indexed before failure ({pages_total:,} discovered)\n"
                f"   • {pages_failed} failed  ·  {pages_skipped} skipped\n\n"
                f"Ping <@U0837N31T9C> to investigate.")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{head}\n\n{body}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"run_id `{run_id[:8]}`"}]},
    ]


def refresh_async_started_blocks(space_label: str, run_id: str, eta_sec: int) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":arrows_counterclockwise: *Refresh of* `{space_label}` *started* "
            f"_(estimated {_fmt_eta(eta_sec)})_\n\n"
            f"This is a long-running job — I'll *DM you* on milestones (25/50/75%) and on completion. "
            f"Feel free to do other things in the meantime."}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f":hourglass: ETA ~{_fmt_eta(eta_sec)}  ·  run_id `{run_id[:8]}`"}]},
    ]


def refresh_dm_milestone_blocks(space_label: str, run_id: str, pages_done: int,
                                  pages_total: int, elapsed_sec: int, pct: int) -> list[dict]:
    rate = pages_done / max(elapsed_sec, 1)
    eta_remaining = int((pages_total - pages_done) / max(rate, 0.1))
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":arrows_counterclockwise: *Refresh of* `{space_label}`: {pct}% done\n"
            f"   • {pages_done:,} / {pages_total:,} pages re-indexed\n"
            f"   • {_fmt_eta(elapsed_sec)} elapsed  ·  ETA remaining ~{_fmt_eta(eta_remaining)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"run_id `{run_id[:8]}` — I'll DM you again on the next milestone."}]},
    ]


def refresh_dm_complete_blocks(space_label: str, run_id: str,
                                 status_payload: dict, elapsed_sec: int) -> list[dict]:
    status = status_payload.get("status", "?")
    pages_indexed = status_payload.get("pages_indexed", 0)
    pages_total = status_payload.get("pages_discovered", 0)
    pages_failed = status_payload.get("pages_failed", 0)
    if status == "completed":
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text":
                f":white_check_mark: *Refresh of* `{space_label}` *complete* in {_fmt_eta(elapsed_sec)}\n"
                f"   • {pages_indexed:,} pages re-indexed  ({pages_total:,} discovered)\n"
                f"   • {pages_failed} failed\n\n"
                f"_Try:_ `{SLASH_COMMAND} ask {space_label.split('(')[0].strip()}: <your question>` — fresh data ready."}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text":
                f"run_id `{run_id[:8]}`"}]},
        ]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":x: *Refresh of* `{space_label}` *failed* after {_fmt_eta(elapsed_sec)}\n"
            f"   • {pages_indexed:,} pages indexed before failure  ·  {pages_failed} failed\n\n"
            f"Ping <@U0837N31T9C> to investigate."}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"run_id `{run_id[:8]}`"}]},
    ]


def refresh_busy_blocks(requested_space_label: str, busy_payload: dict) -> list[dict]:
    msg = busy_payload.get("message") or "(no detail provided)"
    current = busy_payload.get("current_space_key") or busy_payload.get("space_key") or "another space"
    return [{"type": "section", "text": {"type": "mrkdwn", "text":
        f":no_entry_sign: *Indexer busy* — can't start refresh of `{requested_space_label}` right now.\n\n"
        f"Currently re-indexing: `{current}`\n"
        f"_Detail:_ {msg}\n\n"
        f"Try again in a few minutes. The indexer runs one job at a time."}}]


def refresh_usage_blocks() -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text":
        f":arrows_counterclockwise: Refresh usage: `{SLASH_COMMAND} refresh <space>`\n\n"
        "Examples:\n"
        f"  `{SLASH_COMMAND} refresh GrowthX` (small — ~24s, inline progress)\n"
        f"  `{SLASH_COMMAND} refresh Technology` (big — ~74min, async with DM milestones)\n"
        f"  `{SLASH_COMMAND} refresh Product`   (big — ~67min)\n\n"
        "Triggers Jove to re-pull the latest content for that Confluence space. "
        "Friendly names work; case-insensitive. Indexer runs *one job at a time*; "
        "if busy you'll be told who's blocking. Need to also ask a question? Use "
        f"`{SLASH_COMMAND} ask <space> refresh: <question>` — I'll fire the refresh in the background "
        "and answer with current data immediately, then DM you when fresh data is ready."}}]


def review_usage_blocks() -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text":
        ":mag: Review usage:\n"
        f"  `{SLASH_COMMAND} review https://github.com/{GITHUB_ORG}/<repo>/pull/<num>`\n"
        f"  `{SLASH_COMMAND} review <repo>#<num>`  (shorthand)\n\n"
        "Examples:\n"
        f"  `{SLASH_COMMAND} review https://github.com/{GITHUB_ORG}/gateway/pull/8027`\n"
        f"  `{SLASH_COMMAND} review gateway#8027`\n\n"
        f"Posts a *cross-repo impact analysis* comment on the PR — searches all indexed "
        f"repos for consumers of symbols changed in the diff. Best for PRs touching shared "
        f"libraries, OpenAPI specs, Kafka topics, or Stargate routes. ~$0.20-0.50 per review."}}]


# --- Nitpick-mode blocks (intra-repo Kotlin/Java correctness review) ----------

def nitpick_ack_blocks(repo: str, pr_num: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":memo: *{BOT_NAME} is nitpicking* `{repo}#{pr_num}`.\n"
            f"_Intra-repo Kotlin/Java review: null safety, JOOQ/JPA patterns, Temporal rules, "
            f"BOM overrides, Java 21 conventions. Complements `{SLASH_COMMAND} review` (cross-repo impact)._"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            ":hourglass: _starting…_   ETA 30-90s. I'll keep this message updated."}]},
    ]


def nitpick_progress_blocks(repo: str, pr_num: str, status_emoji: str,
                              status_text: str, elapsed_sec: float) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":memo: *{BOT_NAME} is nitpicking* `{repo}#{pr_num}`."}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"{status_emoji} _{status_text}_   ·   {elapsed_sec:.0f}s elapsed"}]},
    ]


def nitpick_success_blocks(repo: str, pr_num: str, review_url: str,
                             elapsed_sec: float) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":white_check_mark: *Kotlin review posted* on "
            f"<https://github.com/{GITHUB_ORG}/{repo}/pull/{pr_num}|`{repo}#{pr_num}`>.\n\n"
            f"<{review_url}|View review on GitHub>"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f":robot_face: Done in {elapsed_sec:.0f}s. "
            f"This is an *intra-repo correctness* review — "
            f"pair with `{SLASH_COMMAND} review` for cross-repo impact."}]},
    ]


def nitpick_usage_blocks() -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text":
        f":memo: Nitpick usage: `{SLASH_COMMAND} nitpick <GitHub PR URL>`\n\n"
        f"Example: `{SLASH_COMMAND} nitpick https://github.com/{GITHUB_ORG}/p2p-custodian/pull/316`\n\n"
        f"Posts a GitHub review (with inline comments) applying the checklist: "
        "null safety, JOOQ/JPA, Temporal rules, BOM overrides, Java 21 migration. ~$0.05-0.25."}}]


def error_blocks(question: str, err: Exception) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f":warning: Jarvis hit an error answering:\n> {question[:200]}\n\n"
            f"`{type(err).__name__}: {err}`"
        }}
    ]
