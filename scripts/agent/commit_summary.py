"""Summarize commits across indexed jupitermoney repos for a date range.

Uses gh api (parallel) to fetch per-repo commit history, aggregates by author +
month + repo, prints summary tables, and writes the raw commit list to a JSON
file for further analysis.

Usage:
    python -m agent.commit_summary                          # default Apr+May 2026
    python -m agent.commit_summary --since 2026-04-01 --until 2026-06-01
    python -m agent.commit_summary --repos-file <path>      # custom repo list
    python -m agent.commit_summary --raw-out commits.jsonl  # also dump raw data
"""
from __future__ import annotations
import argparse
import json
import subprocess
from .repo_names import resolve_gh_org
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

DEFAULT_REPOS_FILE = Path.home() / "jarvis" / "scripts" / "indexed_repos.txt"


# Author normalization — different commits sometimes use different names/emails
# for the same person (laptop vs desktop, work vs personal). Light heuristic:
# group by lowercased email's local-part, use most-common display name as label.
def _norm_key(name: str, email: str) -> str:
    if email and "@" in email:
        local = email.split("@", 1)[0].lower()
        # collapse common variants like ".jr", "+work" suffixes
        return local.split("+", 1)[0]
    return (name or "unknown").lower().strip()


def load_repos(path: Path) -> list[str]:
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def fetch_commits(repo: str, since: str, until: str) -> list[dict]:
    """Return list of {sha, author, email, date, message, repo} dicts."""
    cmd = [
        "gh", "api", "--paginate",
        (lambda o=resolve_gh_org(repo): f"repos/{o[0]}/{o[1]}/commits?since={since}&until={until}&per_page=100")(),
        "--jq",
        '.[] | {sha: .sha[:7], author: .commit.author.name, '
        'email: .commit.author.email, date: .commit.author.date, '
        'message: ((.commit.message | split("\\n")[0])[:140])}',
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return []
    if res.returncode != 0:
        return []
    commits = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            obj["repo"] = repo
            commits.append(obj)
        except json.JSONDecodeError:
            continue
    return commits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-04-01T00:00:00Z")
    ap.add_argument("--until", default="2026-06-01T00:00:00Z")
    ap.add_argument("--repos-file", default=str(DEFAULT_REPOS_FILE))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--raw-out", default=None, help="Write raw commits to JSONL here")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    repos = load_repos(Path(args.repos_file))
    print(f"Pulling commits from {len(repos)} repos in jupitermoney "
          f"(since {args.since}, until {args.until}, workers={args.workers})...",
          file=sys.stderr)

    all_commits: list[dict] = []
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(fetch_commits, r, args.since, args.until): r for r in repos}
        for i, fut in enumerate(as_completed(futures), 1):
            repo = futures[fut]
            try:
                commits = fut.result()
            except Exception:
                failed.append(repo)
                continue
            all_commits.extend(commits)
            if i % 25 == 0 or i == len(repos):
                print(f"  ... {i}/{len(repos)} done, {len(all_commits)} commits so far",
                      file=sys.stderr)

    # Filter out bot commits (common patterns).
    BOT_HINTS = ("dependabot", "[bot]", "bot@", "noreply", "renovate", "snyk")
    def is_bot(c: dict) -> bool:
        s = (c.get("author", "") + " " + c.get("email", "")).lower()
        return any(h in s for h in BOT_HINTS)
    bot_commits = [c for c in all_commits if is_bot(c)]
    human_commits = [c for c in all_commits if not is_bot(c)]

    # Normalize author identities.
    name_for_key: dict[str, Counter] = defaultdict(Counter)
    for c in human_commits:
        key = _norm_key(c["author"], c.get("email", ""))
        name_for_key[key][c["author"]] += 1
        c["_key"] = key

    canonical_name = {k: cnt.most_common(1)[0][0] for k, cnt in name_for_key.items()}

    # Aggregations
    by_author = Counter()
    by_repo = Counter()
    by_month = defaultdict(Counter)              # author_key -> Counter[month -> count]
    repos_per_author: dict[str, set] = defaultdict(set)
    for c in human_commits:
        k = c["_key"]
        by_author[k] += 1
        by_repo[c["repo"]] += 1
        month = c["date"][:7]                    # "YYYY-MM"
        by_month[k][month] += 1
        repos_per_author[k].add(c["repo"])

    print(f"\n{'='*70}")
    print(f"COMMIT ACTIVITY — jupitermoney — {args.since[:10]} to {args.until[:10]}")
    print(f"{'='*70}")
    print(f"  Repos scanned:    {len(repos)}")
    print(f"  Repos with commits: {len(by_repo)}")
    print(f"  Total commits:    {len(all_commits)}")
    print(f"    ├─ Human:       {len(human_commits)}")
    print(f"    └─ Bot:         {len(bot_commits)}")
    print(f"  Unique committers (humans): {len(by_author)}")
    if failed:
        print(f"  Failed to fetch from {len(failed)} repos (skipped)")

    print(f"\n--- TOP {args.top} CONTRIBUTORS (human commits, all repos) ---")
    print(f"{'#':>3}  {'Author':28} {'Commits':>8} {'Repos':>6}  Apr → May (Δ)")
    for rank, (k, n) in enumerate(by_author.most_common(args.top), 1):
        name = canonical_name[k][:28]
        apr = by_month[k].get("2026-04", 0)
        may = by_month[k].get("2026-05", 0)
        delta = may - apr
        sign = "+" if delta >= 0 else ""
        print(f"{rank:>3}  {name:28} {n:>8} {len(repos_per_author[k]):>6}  "
              f"{apr:>3} → {may:>3} ({sign}{delta})")

    print(f"\n--- TOP 15 MOST-ACTIVE REPOS ---")
    print(f"{'#':>3}  {'Repo':40} {'Commits':>8}  Top contributor")
    for rank, (repo, n) in enumerate(by_repo.most_common(15), 1):
        # who committed most to this repo
        repo_authors = Counter()
        for c in human_commits:
            if c["repo"] == repo:
                repo_authors[c["_key"]] += 1
        top_k, top_n = repo_authors.most_common(1)[0] if repo_authors else ("?", 0)
        top_name = canonical_name.get(top_k, top_k)
        print(f"{rank:>3}  {repo:40} {n:>8}  {top_name} ({top_n})")

    print(f"\n--- BREADTH: contributors active across MOST repos ---")
    breadth = sorted(repos_per_author.items(), key=lambda kv: -len(kv[1]))[:10]
    for k, repos_set in breadth:
        name = canonical_name[k]
        print(f"  {name:28} {by_author[k]:>4} commits across {len(repos_set):>3} repos")

    print(f"\n--- MONTHLY TOTALS ---")
    apr = sum(c["date"][:7] == "2026-04" for c in human_commits)
    may = sum(c["date"][:7] == "2026-05" for c in human_commits)
    print(f"  April 2026:  {apr:>5} commits")
    print(f"  May 2026:    {may:>5} commits")

    if args.raw_out:
        raw_path = Path(args.raw_out).expanduser()
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        with raw_path.open("w", encoding="utf-8") as f:
            for c in human_commits:
                c.pop("_key", None)
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"\nRaw commits written to {raw_path} ({len(human_commits)} records)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
