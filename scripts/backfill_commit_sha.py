"""One-time backfill: stamp commit_sha onto every existing chunk in jarvis_code.

For each repo with a local clone, run `git rev-parse HEAD` and Qdrant
`set_payload` with filter `repo=<name>`. Chunks that survived hash-diff
without re-embedding have unchanged content_hash → file content identical
at HEAD → assigning today's SHA is correct for the surviving chunks.

Skips repos without a local clone (rare; the next reindex will stamp them).
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

REPOS_DIR = Path("/home/ubuntu/jarvis/repos")
INDEXED_REPOS_FILE = Path("/home/ubuntu/jarvis/scripts/indexed_repos.txt")
COLLECTION = "jarvis_code"

def repos() -> list[str]:
    out = []
    for line in INDEXED_REPOS_FILE.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out

def head_sha(repo: str) -> str | None:
    path = REPOS_DIR / repo
    if not (path / ".git").is_dir():
        return None
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(path), text=True
        ).strip() or None
    except Exception as e:
        print(f"  WARN: rev-parse failed for {repo}: {e!s}")
        return None

def main() -> int:
    client = QdrantClient(host="localhost", port=6333)
    all_repos = repos()
    print(f"[backfill] {len(all_repos)} indexed repos")
    n_ok = n_skip = n_fail = 0
    for repo in all_repos:
        sha = head_sha(repo)
        if not sha:
            print(f"  - {repo}: SKIP (no local clone)")
            n_skip += 1
            continue
        try:
            client.set_payload(
                collection_name=COLLECTION,
                payload={"commit_sha": sha},
                points=Filter(
                    must=[FieldCondition(key="repo", match=MatchValue(value=repo))]
                ),
                wait=False,
            )
            print(f"  - {repo}: {sha[:12]} ✓")
            n_ok += 1
        except Exception as e:
            print(f"  - {repo}: FAIL {type(e).__name__}: {e!s}")
            n_fail += 1
    print(f"\n[backfill] ok={n_ok} skip={n_skip} fail={n_fail}")
    return 0 if n_fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
