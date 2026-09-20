"""Feedback report — joins qa_log.jsonl × feedback.jsonl and summarises.

Usage:
    python -m agent.feedback_report                  # all-time
    python -m agent.feedback_report --since 7d       # last 7 days
    python -m agent.feedback_report --jsonl          # JSONL per-rated-question

Shows: total Q&As, rating coverage, 👍/👎 split, top-5 thumbs-down questions
(the failure modes worth investigating), and avg cost/iter/tools per rating.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

QA_LOG = Path.home() / "jarvis" / "logs" / "qa_log.jsonl"
FB_LOG = Path.home() / "jarvis" / "logs" / "feedback.jsonl"
NAME_CACHE = Path.home() / "jarvis" / "index" / "slack_users.json"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _parse_since(s: str) -> datetime:
    s = s.lower()
    now = datetime.utcnow()
    today = datetime.combine(date.today(), datetime.min.time())
    if s == "today": return today
    if s == "yesterday": return today - timedelta(days=1)
    if s.endswith("h"): return now - timedelta(hours=int(s[:-1]))
    if s.endswith("d"): return now - timedelta(days=int(s[:-1]))
    return datetime.fromisoformat(s)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.rstrip("Z"))


def load_names(user_ids: set[str], resolve: bool) -> dict[str, str]:
    cache: dict[str, str] = {}
    if NAME_CACHE.exists():
        try:
            cache = json.loads(NAME_CACHE.read_text())
        except Exception:
            pass
    if not resolve:
        return cache
    missing = user_ids - set(cache)
    if missing:
        client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))
        for uid in missing:
            try:
                u = client.users_info(user=uid)["user"]
                cache[uid] = u.get("real_name") or u.get("name") or uid
            except SlackApiError:
                cache[uid] = uid
        NAME_CACHE.parent.mkdir(parents=True, exist_ok=True)
        NAME_CACHE.write_text(json.dumps(cache, indent=2))
    return cache


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="today, yesterday, 24h, 7d, or 2026-05-12")
    ap.add_argument("--jsonl", action="store_true")
    ap.add_argument("--no-resolve", action="store_true", help="Skip Slack API name lookup")
    args = ap.parse_args()

    qa = _load_jsonl(QA_LOG)
    fb = _load_jsonl(FB_LOG)

    if args.since:
        cutoff = _parse_since(args.since)
        qa = [r for r in qa if _ts(r["ts"]) >= cutoff]
        fb = [r for r in fb if _ts(r["ts"]) >= cutoff]

    # latest feedback per qid (a user can change their mind)
    latest_fb: dict[str, dict] = {}
    for r in fb:
        prev = latest_fb.get(r["qid"])
        if prev is None or _ts(r["ts"]) > _ts(prev["ts"]):
            latest_fb[r["qid"]] = r

    user_ids = {r["user_id"] for r in qa} | {r["user_id"] for r in fb}
    names = load_names(user_ids, resolve=not args.no_resolve)

    rated = [q for q in qa if q["qid"] in latest_fb]
    ups = [q for q in rated if latest_fb[q["qid"]]["rating"] == "up"]
    downs = [q for q in rated if latest_fb[q["qid"]]["rating"] == "down"]

    if args.jsonl:
        for q in rated:
            rec = dict(q)
            rec["rating"] = latest_fb[q["qid"]]["rating"]
            rec["user"] = names.get(q["user_id"])
            print(json.dumps(rec, ensure_ascii=False))
        return 0

    print(f"\nJarvis feedback report")
    print(f"  Total Q&As:   {len(qa)}")
    if qa:
        rate = len(rated) / len(qa) * 100
        print(f"  Rated:        {len(rated)} ({rate:.0f}%)")
        if rated:
            up_pct = len(ups) / len(rated) * 100
            print(f"  👍 Helpful:    {len(ups)} ({up_pct:.0f}%)")
            print(f"  👎 Needs work: {len(downs)} ({100 - up_pct:.0f}%)")

    if downs:
        print(f"\n👎 Thumbs-down questions (failure modes to investigate):")
        for q in downs[:10]:
            user = names.get(q["user_id"], q["user_id"])
            print(f"  • [{q['ts']}] {user[:20]:20} ({q['iterations']}i/{len(q['tool_calls'])}t) — {q['q'][:100]}")

    if ups:
        avg = lambda key: round(sum(q.get(key, 0) for q in ups) / len(ups), 1)
        print(f"\n👍 Helpful stats: avg {avg('elapsed_sec')}s · {avg('iterations')} iters · "
              f"{round(sum(len(q.get('tool_calls', [])) for q in ups)/len(ups), 1)} tools")
    if downs:
        avg = lambda key: round(sum(q.get(key, 0) for q in downs) / len(downs), 1)
        print(f"👎 Needs-work stats: avg {avg('elapsed_sec')}s · {avg('iterations')} iters · "
              f"{round(sum(len(q.get('tool_calls', [])) for q in downs)/len(downs), 1)} tools")

    if qa and not rated:
        print(f"\n(no feedback yet; users haven't clicked any buttons)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
