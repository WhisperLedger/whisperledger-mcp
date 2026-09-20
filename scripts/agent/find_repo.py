"""Quick repo-name lookup. Usage: python -m agent.find_repo <substring> [<substring> ...]"""
from __future__ import annotations
import json
import sys
from pathlib import Path

REPOS_JSON = Path.home() / "jarvis" / "index" / "repos_v2.json"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: find_repo <substring> [<substring> ...]", file=sys.stderr)
        return 2
    needles = [s.lower() for s in sys.argv[1:]]
    repos = json.loads(REPOS_JSON.read_text())
    hits = [r for r in repos
            if any(n in r["name"].lower() or n in (r.get("description") or "").lower() for n in needles)]
    hits.sort(key=lambda r: r["pushedAt"], reverse=True)
    print(f"Found {len(hits)} candidates for {needles!r}:")
    for r in hits[:30]:
        arch = "ARCH" if r["isArchived"] else "ok"
        fork = "FORK" if r["isFork"] else ""
        lang = (r.get("primaryLanguage") or {}).get("name", "-") if r.get("primaryLanguage") else "-"
        kb = r.get("diskUsage", 0)
        desc = (r.get("description") or "")[:55]
        print(f"  {arch:5} {fork:5} {r['pushedAt'][:10]}  {lang:12}  {kb:>7} KB  {r['name']:42}  {desc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
