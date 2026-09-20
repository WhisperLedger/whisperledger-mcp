"""Weekly open-PR aging digest for #technology-team (Tuesdays).

Posts a curated public digest (no individual-author rankings — only PRs and
repos) plus writes a snapshot to disk so future runs can show day-over-day
deltas. Excludes Jarvis-fired PRs (fix/iterate/claudify audit logs) and
bot-authored PRs (dependabot etc.).

Usage:
    open_pr_digest.py                    # dry-run: print what would be sent
    open_pr_digest.py --post             # actually post to #technology-team
    open_pr_digest.py --post --channel D...  # post somewhere else (e.g. DM)

Wired into systemd via jarvis-open-pr-digest.{service,timer}; runs weekly
on Tuesdays at 04:00 UTC (09:30 IST). Manual first run, then timer enabled after sign-off.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOGS_DIR = Path("/home/ubuntu/jarvis/logs")
SNAPSHOT_PATH = Path("/home/ubuntu/jarvis/index/open_pr_snapshot.json")
AUDIT_PATH = LOGS_DIR / "open_pr_digest_audit.jsonl"
PRODUCT_MAP_PATH = Path("/home/ubuntu/jarvis/scripts/repo_products.json")
DEFAULT_CHANNEL = "C092S7Z5HB5"  # #technology-team
JARVIS_UID = "U0B37LWBP98"
ROHIT_UID = "U0837N31T9C"

NOW = datetime.now(timezone.utc)


# ─── helpers ──────────────────────────────────────────────────────────────

def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def days_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 86400


def fmt_dur(d: float) -> str:
    if d < 1:
        return f"{d * 24:.1f}h"
    if d < 60:
        return f"{d:.0f}d"
    return f"{d / 30:.1f}mo"


# ─── repo → product mapping ────────────────────────────────────────────────

_product_rules: list[tuple[str, str]] | None = None  # [(pattern, product_name), ...]


def _load_product_rules() -> list[tuple[str, str]]:
    global _product_rules
    if _product_rules is not None:
        return _product_rules
    rules: list[tuple[str, str]] = []
    if PRODUCT_MAP_PATH.is_file():
        try:
            data = json.loads(PRODUCT_MAP_PATH.read_text())
            for prod in data.get("products", []):
                name = prod.get("name", "?")
                for pat in prod.get("patterns", []):
                    rules.append((pat, name))
        except Exception as e:
            print(f"[warn] failed to parse {PRODUCT_MAP_PATH}: {e}", file=sys.stderr)
    _product_rules = rules
    return rules


def classify_repo(repo: str) -> str:
    import fnmatch
    for pat, prod in _load_product_rules():
        if fnmatch.fnmatchcase(repo, pat) or repo == pat:
            return prod
    return "Other (unmapped)"


# ─── data fetch ───────────────────────────────────────────────────────────

def fetch_open_prs() -> list[dict]:
    """Pull all open PRs across jupitermoney/* via REST search/issues."""
    raw = subprocess.run(
        ["gh", "api", "-X", "GET", "search/issues",
         "-f", "q=org:jupitermoney is:pr is:open",
         "-f", "per_page=100",
         "--paginate", "--slurp"],
        capture_output=True, text=True, check=False,
    )
    if raw.returncode != 0:
        raise RuntimeError(f"gh api search/issues failed: {raw.stderr[:500]}")
    pages = json.loads(raw.stdout) if raw.stdout.strip() else []
    items: list[dict] = []
    for page in pages:
        items.extend(page.get("items", []))
    return items


def load_excluded_pr_urls() -> set[str]:
    """Union of Jarvis-fired PR URLs from audit logs."""
    urls: set[str] = set()
    for fname in ["fix_audit.jsonl", "iterate_audit.jsonl", "claudify_audit.jsonl"]:
        p = LOGS_DIR / fname
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            u = r.get("pr_url") or r.get("pull_request_url")
            if u:
                urls.add(u)
    return urls


def normalize(items: list[dict], exclude_urls: set[str]) -> list[dict]:
    out = []
    for it in items:
        url = it.get("html_url", "")
        if url in exclude_urls:
            continue
        title = it.get("title", "")
        if "Project Claudify" in title or title.lower().startswith("[jarvis]"):
            continue
        author = (it.get("user") or {}).get("login", "?")
        if author.endswith("[bot]"):
            continue  # public digest excludes bot authors
        created = parse_dt(it.get("created_at"))
        updated = parse_dt(it.get("updated_at"))
        if not created or not updated:
            continue
        repo_url = it.get("repository_url", "")
        repo = repo_url.split("/")[-1] if repo_url else "?"
        out.append({
            "number": it.get("number"),
            "repo": repo,
            "title": title,
            "author": author,
            "url": url,
            "is_draft": it.get("draft", False),
            "age_days": days_between(created, NOW),
            "stale_days": days_between(updated, NOW),
        })
    return out


# ─── aggregates ───────────────────────────────────────────────────────────

def compute_aggregates(prs: list[dict]) -> dict:
    from collections import defaultdict
    n = len(prs)
    n_ready = sum(1 for p in prs if not p["is_draft"])
    n_draft = n - n_ready
    stuck = [p for p in prs if p["stale_days"] >= 7 and not p["is_draft"]]
    very_stuck = [p for p in prs if p["stale_days"] >= 30 and not p["is_draft"]]
    ancient = [p for p in prs if p["stale_days"] >= 90 and not p["is_draft"]]

    stale = [p["stale_days"] for p in prs]
    median_stale = statistics.median(stale) if stale else 0

    per_repo: dict[str, list[float]] = defaultdict(list)
    for p in prs:
        per_repo[p["repo"]].append(p["stale_days"])
    top_repos = sorted(
        [(repo, len(s), statistics.median(s)) for repo, s in per_repo.items() if len(s) >= 3],
        key=lambda x: -x[2],
    )[:10]

    # Per-product: total open, stuck count, very-stuck count, median stale.
    # Only count ready-for-review (drafts are work-in-progress, not stuck product work).
    per_product_all: dict[str, list[dict]] = defaultdict(list)
    unmapped_by_repo: dict[str, list[float]] = defaultdict(list)
    for p in prs:
        if p["is_draft"]:
            continue
        prod = classify_repo(p["repo"])
        per_product_all[prod].append(p)
        if prod == "Other (unmapped)":
            unmapped_by_repo[p["repo"]].append(p["stale_days"])
    products = []
    for prod, items in per_product_all.items():
        stales = [it["stale_days"] for it in items]
        n_open = len(items)
        n_stuck = sum(1 for it in items if it["stale_days"] >= 7)
        products.append({
            "name": prod,
            "n_open": n_open,
            "n_stuck": n_stuck,
            "n_very_stuck": sum(1 for it in items if it["stale_days"] >= 30),
            "stuck_rate_pct": int(n_stuck / n_open * 100) if n_open else 0,
            "median_stale_d": statistics.median(stales) if stales else 0,
        })
    # Rank by very-stuck count desc, then by stuck count desc — what's the most
    # *blocked* product, regardless of total volume
    products.sort(key=lambda x: (-x["n_very_stuck"], -x["n_stuck"], -x["n_open"]))

    # Top unmapped repos by very-stuck count (for the appendix that grows mapping)
    unmapped_top = sorted(
        [(repo, len(s), sum(1 for d in s if d >= 30)) for repo, s in unmapped_by_repo.items()],
        key=lambda x: (-x[2], -x[1]),
    )[:8]

    worst_prs = sorted([p for p in prs if not p["is_draft"]],
                       key=lambda p: -p["stale_days"])[:20]
    return {
        "n_total": n,
        "n_ready": n_ready,
        "n_draft": n_draft,
        "n_stuck": len(stuck),
        "n_very_stuck": len(very_stuck),
        "n_ancient": len(ancient),
        "median_stale_d": median_stale,
        "n_repos": len(per_repo),
        "top_repos": top_repos,
        "products": products,
        "unmapped_top": unmapped_top,
        "worst_prs": worst_prs,
    }


def load_prev_snapshot() -> dict | None:
    if not SNAPSHOT_PATH.is_file():
        return None
    try:
        return json.loads(SNAPSHOT_PATH.read_text())
    except Exception:
        return None


def write_snapshot(agg: dict) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps({
        "ts": NOW.isoformat(),
        "n_total": agg["n_total"],
        "n_stuck": agg["n_stuck"],
        "n_very_stuck": agg["n_very_stuck"],
        "n_ancient": agg["n_ancient"],
    }, indent=2))


def delta_str(curr: int, prev: int | None) -> str:
    if prev is None:
        return ""
    d = curr - prev
    if d == 0:
        return " (no change)"
    sign = "+" if d > 0 else ""
    return f" ({sign}{d} since last digest)"


# ─── slack message ────────────────────────────────────────────────────────

def trim(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def render_blocks(agg: dict, prev: dict | None) -> tuple[list[dict], str]:
    """Render as Block Kit (with mrkdwn fallback text). Returns (blocks, fallback_text)."""
    n = agg["n_total"]
    n_stuck = agg["n_stuck"]
    n_very = agg["n_very_stuck"]
    n_ancient = agg["n_ancient"]
    prev_total = (prev or {}).get("n_total")
    prev_stuck = (prev or {}).get("n_stuck")
    prev_very = (prev or {}).get("n_very_stuck")

    pct = lambda x: f" _({x * 100 // n}%)_" if n else ""

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text",
            "text": ":hourglass_flowing_sand:  Open-PR aging digest"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Jupiter engineering pulse · {NOW.strftime('%a %d %b %Y · %H:%M UTC')}"}]},
        {"type": "section", "fields": [
            {"type": "mrkdwn",
                "text": f"*Open PRs*\n{n} across {agg['n_repos']} repos{delta_str(n, prev_total)}"},
            {"type": "mrkdwn",
                "text": f"*Stuck ≥7 days*\n{n_stuck}{pct(n_stuck)}{delta_str(n_stuck, prev_stuck)}"},
            {"type": "mrkdwn",
                "text": f"*Stuck ≥30 days*\n{n_very}{pct(n_very)}{delta_str(n_very, prev_very)}"},
            {"type": "mrkdwn",
                "text": f"*Median staleness*\n{fmt_dur(agg['median_stale_d'])}"},
        ]},
        {"type": "divider"},
    ]

    # Hot spots — products with ≥85% stuck rate AND ≥5 open PRs (signal, not noise)
    hot = [p for p in agg["products"] if p["stuck_rate_pct"] >= 85 and p["n_open"] >= 5]
    if hot:
        hot_lines = [
            f"• *{p['name']}* — {p['n_stuck']} of {p['n_open']} PRs stuck "
            f"({p['stuck_rate_pct']}%), median {fmt_dur(p['median_stale_d'])}"
            for p in hot[:5]
        ]
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": ":fire:  *Hot spots* (products with ≥85% stuck rate)\n" + "\n".join(hot_lines)}})
        blocks.append({"type": "divider"})

    # Product table — code block keeps alignment
    prod_lines = [
        f"{'Product':<38s} {'Open':>5} {'≥7d':>5} {'≥30d':>5} {'Stuck':>6} {'Median':>8}",
        "─" * 70,
    ]
    for prod in agg["products"]:
        if prod["n_open"] == 0:
            continue
        prod_lines.append(
            f"{trim(prod['name'], 38):<38s} {prod['n_open']:>5} {prod['n_stuck']:>5} "
            f"{prod['n_very_stuck']:>5} {prod['stuck_rate_pct']:>5}% "
            f"{fmt_dur(prod['median_stale_d']):>8}"
        )
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": ":dart:  *Which products are most blocked* "
                "_(ready-for-review only, ranked by 30d+ stuck count)_\n"
                "```\n" + "\n".join(prod_lines) + "\n```"}})

    if agg.get("unmapped_top"):
        unmapped_lines = [
            f"{trim(repo, 38):<38s}  {n_total:>3} open · {n_very:>2} stuck 30d+"
            for repo, n_total, n_very in agg["unmapped_top"]
        ]
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": ":mag:  *Repos not yet classified* — DM Jarvis to extend the mapping\n"
                    "```\n" + "\n".join(unmapped_lines) + "\n```"}})

    blocks.append({"type": "divider"})

    # Top stale repos
    repo_lines = [
        f"{trim(repo, 32):<32s}  {npr:>2} PRs · median {fmt_dur(med)}"
        for repo, npr, med in agg["top_repos"]
    ]
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": ":warning:  *Top 10 stuck repos* _(median staleness, ≥3 open PRs)_\n"
                "```\n" + "\n".join(repo_lines) + "\n```"}})

    blocks.append({"type": "divider"})

    # Worst stuck PRs — split across 2 sections so each stays under the 3000-char cap
    worst = agg["worst_prs"]
    half = (len(worst) + 1) // 2
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": ":skull:  *20 oldest stuck PRs* _(ready-for-review, bots excluded)_\n" +
                "\n".join(
                    f"• <{p['url']}|{p['repo']}#{p['number']}> — "
                    f"{trim(p['title'], 55)} _({fmt_dur(p['stale_days'])})_"
                    for p in worst[:half]
                )}})
    if len(worst) > half:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "\n".join(
                    f"• <{p['url']}|{p['repo']}#{p['number']}> — "
                    f"{trim(p['title'], 55)} _({fmt_dur(p['stale_days'])})_"
                    for p in worst[half:])}})

    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f":wave: DM Jarvis (<@{JARVIS_UID}>) for help iterating, unblocking, or "
                f"deciding whether to close. If your stuck PR isn't on the list, DM "
                f"<@{JARVIS_UID}> or <@{ROHIT_UID}> so we can surface it next run. "
                f"Bot-authored PRs excluded. Generated weekly (Tuesdays) from GitHub data."}]})

    # Fallback text for clients that can't render blocks (rare but Slack mobile push prefs etc.)
    fallback = (f"Open-PR aging digest: {n} open, {n_stuck} stuck ≥7d "
                f"({n_stuck * 100 // max(1, n)}%), {n_very} stuck ≥30d. "
                f"Median {fmt_dur(agg['median_stale_d'])} since last activity.")
    return blocks, fallback


def render_slack(agg: dict, prev: dict | None) -> str:
    """Legacy plain-text render (kept for --dry-run readability in terminal)."""
    blocks, fallback = render_blocks(agg, prev)
    # Cheap text extraction for terminal preview
    out = [fallback, ""]
    for b in blocks:
        if b["type"] == "header":
            out.append(f"\n## {b['text']['text']}")
        elif b["type"] == "divider":
            out.append("─" * 60)
        elif b["type"] == "section":
            if "fields" in b:
                for f in b["fields"]:
                    out.append(f["text"])
            else:
                out.append(b["text"]["text"])
        elif b["type"] == "context":
            for e in b["elements"]:
                out.append(f"_{e['text']}_")
    return "\n".join(out)


# ─── slack post + audit ───────────────────────────────────────────────────

def slack_post(channel: str, blocks: list, fallback_text: str, update_ts: str | None = None) -> dict:
    """Post a new message OR update an existing one (chat.update if update_ts given)."""
    token = os.environ["SLACK_BOT_TOKEN"]
    method = "chat.update" if update_ts else "chat.postMessage"
    body = {
        "channel": channel,
        "text": fallback_text,
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if update_ts:
        body["ts"] = update_ts
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def audit(payload: dict) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a") as f:
        f.write(json.dumps(payload) + "\n")


# ─── main ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true",
                    help="Actually post to Slack (default: dry-run, print only)")
    ap.add_argument("--channel", default=DEFAULT_CHANNEL,
                    help="Slack channel ID (default: #technology-team)")
    ap.add_argument("--update-ts",
                    help="Update an existing Slack message ts in place (chat.update) "
                         "instead of posting a new one")
    args = ap.parse_args()

    items = fetch_open_prs()
    excluded = load_excluded_pr_urls()
    prs = normalize(items, excluded)
    agg = compute_aggregates(prs)
    prev = load_prev_snapshot()

    blocks, fallback = render_blocks(agg, prev)
    print(render_slack(agg, prev))

    if not args.post:
        print(f"\n[dry-run] not posting. Re-run with --post to send. "
              f"({len(blocks)} blocks, {len(json.dumps(blocks))} bytes)", file=sys.stderr)
        return 0

    resp = slack_post(args.channel, blocks, fallback, update_ts=args.update_ts)
    audit({
        "ts": NOW.isoformat(),
        "channel": args.channel,
        "action": "update" if args.update_ts else "post",
        "update_ts": args.update_ts,
        "ok": resp.get("ok"),
        "ts_msg": resp.get("ts"),
        "error": resp.get("error"),
        "n_total": agg["n_total"],
        "n_stuck": agg["n_stuck"],
        "n_very_stuck": agg["n_very_stuck"],
    })
    if not resp.get("ok"):
        print(f"\n[error] slack call failed: {resp}", file=sys.stderr)
        return 1

    # only roll snapshot forward on a successful real post (not on an update)
    if not args.update_ts:
        write_snapshot(agg)
    action = "updated" if args.update_ts else "posted"
    print(f"\n[ok] {action} in {args.channel}, ts={resp.get('ts')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
