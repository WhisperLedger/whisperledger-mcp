"""One-time: compute and persist file hashes for already-indexed repos.

After switching from full to incremental mode, existing repos have chunks in
Qdrant but no hash file on disk. Running the incremental indexer on them
without this backfill would trigger the "first incremental run" wipe and
re-embed everything (wasted cost). Run this once first so the next incremental
run sees all files as unchanged.

Usage:
    python -m indexer.backfill_hashes <repo1> <repo2> ...
    python -m indexer.backfill_hashes --all   # every dir in REPOS_DIR
"""
from __future__ import annotations
import argparse
import sys
from . import config, hashes, walker


def backfill(repo: str) -> int:
    repo_root = config.REPOS_DIR / repo
    if not repo_root.is_dir():
        print(f"[{repo}] NOT FOUND, skipping", flush=True)
        return 0
    new_hashes: dict[str, str] = {}
    for rel_path, _text, h in walker.walk_repo(repo_root):
        new_hashes[rel_path] = h
    hashes.save(repo, new_hashes)
    print(f"[{repo}] hashed {len(new_hashes)} files", flush=True)
    return len(new_hashes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repos", nargs="*")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    if args.all:
        repos = sorted(p.name for p in config.REPOS_DIR.iterdir() if p.is_dir())
    else:
        repos = args.repos
    if not repos:
        ap.error("pass repo name(s) or --all")
    total = 0
    for r in repos:
        total += backfill(r)
    print(f"\nTotal: {total} files hashed across {len(repos)} repos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
