"""Quick retrieval test against the Jarvis index.

Usage:
    python -m indexer.query "how does auth work"
    python -m indexer.query --repo bff-core "graphql schema"
"""
from __future__ import annotations
import argparse
import sys
from qdrant_client.models import Filter, FieldCondition, MatchValue
from . import config, embedder, store


def search(q: str, repo: str | None = None, k: int = 8) -> None:
    vec = embedder.embed_query(q)
    flt = None
    if repo:
        flt = Filter(must=[FieldCondition(key="repo", match=MatchValue(value=repo))])
    res = store.client().query_points(
        collection_name=config.QDRANT_COLLECTION,
        query=vec,
        limit=k,
        query_filter=flt,
        with_payload=True,
    )
    for i, h in enumerate(res.points, 1):
        p = h.payload or {}
        print(f"\n--- #{i} score={h.score:.3f} {p.get('repo')}/{p.get('path')}:{p.get('start_line')}-{p.get('end_line')} ---")
        snippet = (p.get("text") or "")[:600]
        print(snippet)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="+")
    ap.add_argument("--repo", default=None)
    ap.add_argument("-k", type=int, default=8)
    args = ap.parse_args()
    search(" ".join(args.query), repo=args.repo, k=args.k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
