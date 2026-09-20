"""Usage report for the Jarvis Slack bot.

Reads ~/jarvis/logs/slackbot.log, extracts each question / answer / rejection,
resolves Slack user IDs to real names (cached), and prints a tidy report.

Usage:
    python -m agent.usage_report                # all-time
    python -m agent.usage_report --since today  # today's activity only
    python -m agent.usage_report --jsonl        # JSONL per-question for piping
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

LOG_PATH = Path.home() / "jarvis" / "logs" / "slackbot.log"
NAME_CACHE = Path.home() / "jarvis" / "index" / "slack_users.json"

# Regexes match the log lines our bot emits.
Q_RE = re.compile(r"^(\S+ \S+).*q from (U\w+):\s+'(.*)'$")
ANS_RE = re.compile(r"^(\S+ \S+).*answered (U\w+) in ([\d.]+)s \(iters=(\d+), tools=(\d+)")
REJ_RE = re.compile(r"^(\S+ \S+).*rejected q from (U\w+) in (\S+) \((.*?)\)")


# --- name cache + resolution ---------------------------------------------------

def load_cache() -> dict[str, str]:
    if NAME_CACHE.exists():
        try:
            return json.loads(NAME_CACHE.read_text())
        except Exception:
            return {}
    return {}


def save_cache(cache: dict[str, str]) -> None:
    NAME_CACHE.parent.mkdir(parents=True, exist_ok=True)
    NAME_CACHE.write_text(json.dumps(cache, indent=2))


def resolve_names(user_ids: set[str], client: WebClient | None) -> dict[str, str]:
    cache = load_cache()
    if not client:
        return cache
    for uid in user_ids:
        if uid in cache:
            continue
        try:
            info = client.users_info(user=uid)["user"]
            cache[uid] = info.get("real_name") or info.get("name") or uid
        except SlackApiError as e:
            cache[uid] = f"(? {uid})"
            print(f"  ! could not resolve {uid}: {e.response['error']}", file=sys.stderr)
    save_cache(cache)
    return cache


# --- log parsing ---------------------------------------------------------------

def parse_log(path: Path) -> tuple[list[dict], list[dict]]:
    qs: list[dict] = []
    rejs: list[dict] = []
    if not path.exists():
        return qs, rejs
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = Q_RE.search(line)
        if m:
            qs.append({"ts": m.group(1), "user_id": m.group(2), "q": m.group(3)})
            continue
        m = ANS_RE.search(line)
        if m:
            uid = m.group(2)
            # attach to most recent unanswered q from same user
            for q in reversed(qs):
                if q["user_id"] == uid and "elapsed" not in q:
                    q["elapsed"] = float(m.group(3))
                    q["iters"] = int(m.group(4))
                    q["tools"] = int(m.group(5))
                    break
            continue
        m = REJ_RE.search(line)
        if m:
            rejs.append({
                "ts": m.group(1), "user_id": m.group(2),
                "channel_id": m.group(3), "channel_name": m.group(4),
            })
    return qs, rejs


# --- filtering -----------------------------------------------------------------

def _parse_since(since: str) -> datetime:
    s = since.lower()
    today = datetime.combine(date.today(), datetime.min.time())
    if s == "today":
        return today
    if s == "yesterday":
        return today - timedelta(days=1)
    if s.endswith("h"):
        return datetime.now() - timedelta(hours=int(s[:-1]))
    if s.endswith("d"):
        return datetime.now() - timedelta(days=int(s[:-1]))
    # iso date
    return datetime.fromisoformat(s)


def _ts(s: str) -> datetime:
    # log format: "2026-05-12 09:14:31,730"
    return datetime.strptime(s.split(",")[0], "%Y-%m-%d %H:%M:%S")


def filter_since(items: list[dict], since: datetime) -> list[dict]:
    return [x for x in items if _ts(x["ts"]) >= since]


# --- output --------------------------------------------------------------------

def print_table(qs: list[dict], rejs: list[dict], names: dict[str, str]) -> None:
    print(f"\nJarvis usage report — {len(qs)} answered, {len(rejs)} rejected\n")
    if qs:
        n_users = len({q["user_id"] for q in qs})
        avg = round(sum(q.get("elapsed", 0) for q in qs if "elapsed" in q)
                    / max(1, sum(1 for q in qs if "elapsed" in q)), 1)
        print(f"  {n_users} unique users · avg {avg}s per answer\n")
        for q in qs:
            name = names.get(q["user_id"], q["user_id"])
            stats = (f"{q['elapsed']:>5.1f}s · {q['iters']} iters · {q['tools']:>2} tools"
                     if "elapsed" in q else "(no completion record)")
            print(f"  {q['ts']}  {name[:24]:24}  {stats}")
            print(f"    > {q['q']}\n")
    if rejs:
        print(f"\nRejected ({len(rejs)}):\n")
        for r in rejs:
            name = names.get(r["user_id"], r["user_id"])
            print(f"  {r['ts']}  {name[:24]:24}  in {r['channel_name']} ({r['channel_id']})")


def print_jsonl(qs: list[dict], rejs: list[dict], names: dict[str, str]) -> None:
    for q in qs:
        rec = dict(q)
        rec["user"] = names.get(q["user_id"])
        print(json.dumps(rec, ensure_ascii=False))
    for r in rejs:
        rec = dict(r); rec["user"] = names.get(r["user_id"]); rec["status"] = "rejected"
        print(json.dumps(rec, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="today, yesterday, 24h, 7d, or 2026-05-12")
    ap.add_argument("--jsonl", action="store_true", help="JSONL output (one record per line)")
    ap.add_argument("--no-resolve", action="store_true", help="Skip Slack API lookup; use cached names only.")
    args = ap.parse_args()

    qs, rejs = parse_log(LOG_PATH)
    if args.since:
        cutoff = _parse_since(args.since)
        qs = filter_since(qs, cutoff)
        rejs = filter_since(rejs, cutoff)

    user_ids = {q["user_id"] for q in qs} | {r["user_id"] for r in rejs}
    client = None if args.no_resolve else WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    names = resolve_names(user_ids, client)

    if args.jsonl:
        print_jsonl(qs, rejs, names)
    else:
        print_table(qs, rejs, names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
