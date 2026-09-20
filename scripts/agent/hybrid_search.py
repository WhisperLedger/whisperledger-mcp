"""Hybrid retrieval = vector (Qdrant) + BM25 (sqlite FTS5) fused via RRF.

Reciprocal Rank Fusion is a well-studied simple fusion: score each hit by
sum of 1/(k_const + rank) across both ranklists. Symmetric, no
weight-tuning needed, robust to score scale differences between vector
similarity (cosine) and BM25 (Okapi).

The BM25 ranklist surfaces exact identifier / phrase matches that semantic
vectors miss (e.g. `useVarunaPayment`, `EventApportionmentStrategy`, exact
endpoint paths). The vector ranklist surfaces semantically related chunks
that lexical matching misses (e.g. "auth flow" hitting a chunk about JWT
without that exact phrase).
"""
from __future__ import annotations
import json
import re
import sqlite3
from pathlib import Path

BM25_DB_PATH = Path("/home/ubuntu/jarvis/index/bm25.db")
RRF_K = 60

# Cached connection — opened on first use, reused across queries.
_CONN: sqlite3.Connection | None = None


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        if not BM25_DB_PATH.exists():
            raise RuntimeError(
                f"BM25 index not built — run scripts/build_bm25_index.py "
                f"(expected {BM25_DB_PATH})"
            )
        # check_same_thread=False so FastAPI's worker threads can share.
        _CONN = sqlite3.connect(str(BM25_DB_PATH), check_same_thread=False)
    return _CONN


# FTS5 query syntax has reserved tokens (AND, OR, NEAR, NOT, ", etc).
# Strip non-alphanumeric for safety and quote terms so the user's query
# is treated as a phrase-or-token search, never as boolean syntax.
def _sanitize_query(q: str) -> str:
    # Replace runs of non-word chars with space, then quote each token.
    tokens = re.findall(r"[A-Za-z0-9_]+", q)
    if not tokens:
        return ""
    # Use OR-of-terms — most permissive, lets BM25 rank by term frequency.
    return " OR ".join(f'"{t}"' for t in tokens)


def bm25_search(query: str, k: int = 20, repo: str | None = None) -> list[dict]:
    """Return top-k BM25 hits. Each hit: qdrant_id, repo, path, score."""
    q = _sanitize_query(query)
    if not q:
        return []
    c = _conn()
    sql = """
        SELECT c.qdrant_id, c.repo, c.path, c.start_line, c.end_line,
               c.language, bm25(chunks_fts) AS score
          FROM chunks_fts
          JOIN chunks c ON c.rowid = chunks_fts.rowid
         WHERE chunks_fts MATCH ?
    """
    params: list = [q]
    if repo:
        sql += " AND c.repo = ?"
        params.append(repo)
    sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
    params.append(k)
    rows = c.execute(sql, params).fetchall()
    # FTS5 bm25() returns NEGATIVE numbers (more negative = more relevant).
    # Flip sign so higher = better for downstream consumers.
    out = []
    for row in rows:
        out.append({
            "qdrant_id": row[0],
            "repo": row[1],
            "path": row[2],
            "start_line": row[3],
            "end_line": row[4],
            "language": row[5],
            "score": -float(row[6]) if row[6] is not None else 0.0,
        })
    return out


def rrf_fuse(vector_hits: list[dict], bm25_hits: list[dict],
             k_const: int = RRF_K) -> list[dict]:
    """Reciprocal Rank Fusion: each hit scored by sum(1/(k+rank)) across lists.

    Identity = (repo, path, start_line, end_line) so different chunks of the
    same file rank separately. Returns hits sorted by fused score desc.
    """
    fused: dict[tuple, dict] = {}
    for source_idx, ranklist in enumerate([vector_hits, bm25_hits]):
        for rank, h in enumerate(ranklist, 1):
            key = (h.get("repo"), h.get("path"),
                   h.get("start_line"), h.get("end_line"))
            entry = fused.setdefault(key, {**h, "_fused_score": 0.0,
                                            "_sources": []})
            entry["_fused_score"] += 1.0 / (k_const + rank)
            entry["_sources"].append("vector" if source_idx == 0 else "bm25")
    return sorted(fused.values(), key=lambda r: -r["_fused_score"])
