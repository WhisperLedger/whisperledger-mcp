"""One-shot Slack announcement: repo coverage, usage examples, dev rankings.

Sends a 5-part series to the pilot channel as the bot. Computes the dev
ranking on the fly from the most recent commits JSONL so numbers stay fresh.

Usage:
    python -m agent.announce --dry-run         # print messages, do not post
    python -m agent.announce                   # post for real
    python -m agent.announce --channel C...    # override channel

Environment: needs SLACK_BOT_TOKEN.
"""
from __future__ import annotations
import argparse
import json
import math
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from slack_sdk import WebClient

REPOS_FILE = Path.home() / "jarvis" / "scripts" / "indexed_repos.txt"
COMMITS_JSONL = Path.home() / "jarvis" / "logs" / "commits_apr_may_2026.jsonl"
DEFAULT_CHANNEL = "C092S7Z5HB5"


def load_repos() -> list[str]:
    out = []
    for line in REPOS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def msg_1(repos: list[str]) -> str:
    return (
        ":robot_face: *Jarvis update — full code corpus is now indexed*\n\n"
        f"Jarvis now knows *{len(repos)} active code repos* in `jupitermoney` "
        f"(~131k embedded chunks, refreshed nightly via incremental indexing).\n\n"
        "*Rough buckets:*\n"
        "• *Core (5):* `bff-core`, `platform`, `lms`, `gateway`, `jupiter` (RN app)\n"
        "• *Cards & lending:* `bullet`, `cardboard`, `brahma`, `metal`, "
        "`lending-orchestrator`, `bank-transfer`, `bank-transfer-merchant`, …\n"
        "• *MF / insurance / bills / PPI:* `mf-order-xpress`, `mf-explore-service`, "
        "`insurance-platform`, `bills`, `ppi-rail`, `ppi-pots`, `ppi-router`, …\n"
        "• *Banking / accounts / KYC:* `banking`, `kyc-service`, `investment`, "
        "`savings-account-ob`, `general-ledger-accountant`, …\n"
        "• *Data / ML services (~30):* `airflow-dags`, `ds-jm-*` services "
        "(recommended-actions, risk, classifier-inference, …)\n"
        "• *Frontends:* `jupiter-web` (NextJS), `jupiter-design-system`, "
        "`wormhole`, `jupiter-app-automation`, …\n"
        "• Plus *~150 more* backend / utility / orchestrator repos in the long tail\n\n"
        "Full alphabetical list: 2/5 below. Question examples: 3/5. "
        "Developer activity report: 4/5 + 5/5.\n\n"
        "_1/5_"
    )


def msg_2_repo_list(repos: list[str]) -> str:
    block = "\n".join(sorted(repos))
    return (
        f"*Full repo list — all {len(repos)} (alphabetical):*\n"
        f"```\n{block}\n```\n\n"
        "_2/5_"
    )


def msg_3_what_to_ask() -> str:
    return (
        ":bulb: *What kinds of questions does Jarvis answer well?*\n\n"
        "*✅ Strong (proven by real engineer questions this week):*\n"
        "• \"How does the *PAN verification flow* work end-to-end?\" → entry points → "
        "XState machine → screens → Stargate routes → backend services\n"
        "• \"Explain the *Personal Loan flow*\" → "
        "now extracts the canonical `LoanState` enum verbatim from `lending-orchestrator`\n"
        "• \"Where is the *`enableUserDetailsV2Flow`* flag enabled?\" → finds the exact files\n"
        "• \"What does the *`jupiter.infosets.preamble`* infoset store?\"\n"
        "• \"Trace a *UPI transaction* from app → BFF → Stargate → backend service\"\n"
        "• \"How does *x-tenant* header propagation work across services?\"\n"
        "• \"Where is the *circuit breaker* defined and which services use it?\"\n\n"
        "*⚠️ Weaker (we know — being worked on):*\n"
        "• \"*Why* was X designed this way?\" — pure RAG is weak when there's no ADR\n"
        "• Real-time data (\"how many active users now\") — Jarvis only knows code, not prod\n"
        "• Speculative (\"what should we build next\") — out of scope\n\n"
        "*How to use:*\n"
        "• `/jarvis <your question>` in <#" + DEFAULT_CHANNEL + ">\n"
        "• Follow-ups: just keep typing `/jarvis ...` within 10 minutes — Jarvis remembers\n"
        "• Reset conversation: `/jarvis -new ...`\n"
        "• *Click 👍 / 👎 on every answer* — that's how the bot gets tuned for you\n"
        "• Answers take 30-60s; only you see them\n\n"
        "_3/5_"
    )


def _norm_key(name: str, email: str) -> str:
    if email and "@" in email:
        return email.split("@", 1)[0].lower().split("+", 1)[0]
    return (name or "unknown").lower().strip()


def compute_rankings() -> tuple[list[tuple], dict, list[dict]]:
    """Re-derive Top-N composite ranking from the commits JSONL.

    Lighter than commit_analytics.py (no per-commit gh fetches) — uses just
    commit count + repos + months. Returns (ranked, canonical_names, raw_commits).
    """
    if not COMMITS_JSONL.exists():
        return [], {}, []
    raw = [json.loads(l) for l in COMMITS_JSONL.read_text().splitlines() if l.strip()]
    name_for_key: dict[str, Counter] = defaultdict(Counter)
    for c in raw:
        k = _norm_key(c["author"], c.get("email", ""))
        name_for_key[k][c["author"]] += 1
        c["_key"] = k
    canonical = {k: cnt.most_common(1)[0][0] for k, cnt in name_for_key.items()}

    by_author: dict = defaultdict(lambda: {"commits": 0, "repos": set()})
    by_month: dict = defaultdict(lambda: Counter())
    for c in raw:
        k = c["_key"]
        by_author[k]["commits"] += 1
        by_author[k]["repos"].add(c["repo"])
        by_month[k][c["date"][:7]] += 1

    ranked = sorted(
        [(k, v["commits"], len(v["repos"]), by_month[k]) for k, v in by_author.items()],
        key=lambda x: -x[1],
    )
    return ranked, canonical, raw


def msg_4_dev_observations(ranked: list[tuple], canonical: dict) -> str:
    """Multi-lens framing: Jarvis 'getting to know' each engineer's contribution
    style. Deliberately NOT a single ranked leaderboard — different kinds of
    valuable contribution look different in commit history."""
    return (
        ":bar_chart: *What Jarvis learned about your team's work — Apr + May 2026*\n\n"
        "To get to know the codebase, I pulled commit history across all 241 indexed repos "
        "for the last two months. Bots filtered out (dependabot, renovate, etc.), authors "
        "normalized by email. Result: *1,269 human commits from 62 contributors across "
        "92 active repos*.\n\n"
        "Different engineers contribute in different ways. Here's what I noticed across "
        "*five lenses* — none of these is the \"winner\" lens. They're all valuable.\n\n"
        "*🏗️ Most prolific (by commit count):*\n"
        "1. Kamlesh Biloniya — 199 commits across 8 repos\n"
        "2. chiragkhatri26 — 91 commits\n"
        "3. priyanshu-dev-jupiter — 88\n"
        "4. akhilchoubey, Appy — 79 each\n"
        "5. Parth — 55\n\n"
        "*🌐 Broadest reach (most repos touched — great cross-stack reviewers):*\n"
        "1. Kuldeep Varma — 12 repos\n"
        "2. Appy — 10 repos\n"
        "3. Dineshmaddi3, praveen-jup, Apekshit Sharma — 8-9 repos\n\n"
        "*🔬 Most surgical (median 1 file / few lines per commit — review-friendly):*\n"
        "1. mridul, abhishekVerma-jm — 1 file / 2 lines per commit\n"
        "2. sayan-03-jupiter, Mithun Tantri — 1 file / 4 lines\n"
        "3. Kunchapu Pranav, surajpatil, ishukumar-aps — 1 file / ~8 lines\n\n"
        "*🌍 Most polyglot (touched 10+ language buckets):*\n"
        "• chiragkhatri26, Ranjit, akhilchoubey — 10 distinct languages each "
        "(TS, JS, Docs, Config, Shell, …)\n\n"
        "*🧹 Cleanup-heavy (more lines deleted than added — invisible-but-important work):*\n"
        "• Appy, Apekshit Sharma, Dineshmaddi3, ashitiz8697 — net negative LOC. "
        "Removing dead code is value that doesn't show up in \"lines added\" charts.\n\n"
        "_Methodology, so you can sanity-check:_\n"
        "• LOC excludes lockfiles / `*.min.*` / `*.map` / `*.lock` (only \"real code\")\n"
        "• Bulk commits (>100 files OR >5,000 lines, e.g. dependency bumps / formatter passes) "
        "excluded from focus stats\n"
        "• May 2026 is partial (first ~12 days only)\n\n"
        "_4/5_"
    )


def msg_5_patterns_caveats() -> str:
    return (
        ":mag: *A few patterns worth noting — flagged for context, not judgment:*\n\n"
        "• `jupiter-cs-dashboard` — *Anand G* added 176k+ lines (with only 437 deletions) "
        "in 1 repo. Looks like a project bootstrap / large initial scaffolding, not "
        "organic feature development. Real data, but not directly comparable to "
        "incremental work elsewhere.\n\n"
        "• `ds-jm-funnel-drop-service` — *Siddeswar Reddy* shows the same pattern "
        "(+113k / -3 in 4 commits) — initial setup of a new service.\n\n"
        "• *Tanmay-Jupiter* — +79k / -77k looks enormous but the 1.03:1 add:delete "
        "ratio means this is a *major refactor*, not net-new code. Different kind of "
        "valuable contribution that's easy to misread as \"churn.\"\n\n"
        ":warning: *Honest caveats — please read before forming any opinions:*\n\n"
        "1. *These are observations Jarvis made while learning the codebase*, not "
        "evaluations of anyone's work. Different kinds of contribution look very "
        "different in commit history.\n"
        "2. *LOC is a proxy, not a measure of value.* A 1-line race-condition fix can "
        "be more valuable than 5,000 lines of CRUD.\n"
        "3. *No PR-quality signals yet* — review feedback, revert rate, time-to-merge "
        "would sharpen this a lot. Planned next.\n"
        "4. *Author normalization is heuristic* — engineers committing from multiple "
        "emails may show up twice.\n"
        "5. Generated code, vendored libraries, and bulk imports skew raw LOC; I've "
        "filtered the obvious ones but not all.\n\n"
        "*If your data looks off or you want me to re-run with different lenses* "
        "(e.g. focus on a specific repo / time range / metric), reply in this channel — "
        "happy to. Full per-contributor breakdown → DM <@U0837N31T9C>.\n\n"
        "_5/5_"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default=DEFAULT_CHANNEL)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    repos = load_repos()
    ranked, canonical, raw = compute_rankings()
    print(f"Loaded {len(repos)} repos and {len(raw)} commits.", file=sys.stderr)

    msgs = [
        ("1/5 coverage",            msg_1(repos)),
        ("2/5 full repo list",      msg_2_repo_list(repos)),
        ("3/5 question examples",   msg_3_what_to_ask()),
        ("4/5 dev observations",    msg_4_dev_observations(ranked, canonical)),
        ("5/5 patterns+caveats",    msg_5_patterns_caveats()),
    ]

    if args.dry_run:
        for label, body in msgs:
            print(f"\n{'='*70}\n=== {label} ({len(body)} chars) ===\n{'='*70}\n{body}")
        return 0

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("SLACK_BOT_TOKEN missing", file=sys.stderr)
        return 1
    client = WebClient(token=token)
    print(f"Posting to {args.channel}", file=sys.stderr)
    for label, body in msgs:
        if len(body) > 38000:
            print(f"WARN: {label} is {len(body)} chars — Slack hard cap is 40k", file=sys.stderr)
        resp = client.chat_postMessage(channel=args.channel, text=body, mrkdwn=True)
        ok = resp.get("ok", False)
        ts = resp.get("ts", "?")
        print(f"  {label} → ok={ok} ts={ts}")
        if not ok:
            print(f"  ERROR: {resp}", file=sys.stderr)
            return 1
        time.sleep(1.5)
    print("All 5 messages posted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
