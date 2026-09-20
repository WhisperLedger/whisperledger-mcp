"""Build sqlite FTS5 BM25 index from Qdrant jarvis_code.

Output: ~/jarvis/index/bm25.db with two tables:
  chunks      — meta (qdrant_id, repo, path, start_line, end_line)
  chunks_fts  — FTS5 virtual table on text column, joined via rowid

Idempotent: drops + rebuilds tables. Bulk insert via executemany. Ships with
the BM25 score function builtin to FTS5.
"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

from qdrant_client import QdrantClient

DB_PATH = Path("/home/ubuntu/jarvis/index/bm25.db")
COLLECTION = "jarvis_code"
BATCH = 1000


def main():
    client = QdrantClient(host="localhost", port=6333)
    total = client.count(collection_name=COLLECTION).count
    print(f"[build_bm25] {total} chunks to ingest")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        # Drop + recreate so re-runs are clean.
        conn.execute("DROP TABLE IF EXISTS chunks_fts")
        conn.execute("DROP TABLE IF EXISTS chunks")
        conn.execute("""
            CREATE TABLE chunks (
                rowid INTEGER PRIMARY KEY,
                qdrant_id TEXT NOT NULL UNIQUE,
                repo TEXT NOT NULL,
                path TEXT NOT NULL,
                start_line INTEGER,
                end_line INTEGER,
                language TEXT
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                text,
                content='',  -- contentless so we manage rowid<->text mapping ourselves
                tokenize='porter unicode61'
            )
        """)
        conn.execute("CREATE INDEX idx_chunks_repo ON chunks(repo)")
        conn.execute("CREATE INDEX idx_chunks_path ON chunks(path)")
        conn.commit()

        next_offset = None
        ingested = 0
        chunks_batch: list[tuple] = []
        fts_batch: list[tuple] = []
        while True:
            pts, next_offset = client.scroll(
                collection_name=COLLECTION,
                limit=BATCH,
                offset=next_offset,
                with_payload=True,
                with_vectors=False,
            )
            if not pts:
                break
            for p in pts:
                pl = p.payload or {}
                text = pl.get("text") or ""
                if not text.strip():
                    continue
                rowid = ingested + len(chunks_batch) + 1
                chunks_batch.append((
                    rowid,
                    str(p.id),
                    pl.get("repo", ""),
                    pl.get("path", ""),
                    pl.get("start_line"),
                    pl.get("end_line"),
                    pl.get("language", ""),
                ))
                fts_batch.append((rowid, text))
                if len(chunks_batch) >= BATCH:
                    conn.executemany(
                        "INSERT INTO chunks(rowid, qdrant_id, repo, path, start_line, end_line, language) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        chunks_batch,
                    )
                    conn.executemany(
                        "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
                        fts_batch,
                    )
                    conn.commit()
                    ingested += len(chunks_batch)
                    chunks_batch.clear()
                    fts_batch.clear()
                    if ingested % 10000 == 0:
                        print(f"  ingested={ingested}")
            if next_offset is None:
                break

        if chunks_batch:
            conn.executemany(
                "INSERT INTO chunks(rowid, qdrant_id, repo, path, start_line, end_line, language) VALUES (?, ?, ?, ?, ?, ?, ?)",
                chunks_batch,
            )
            conn.executemany(
                "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
                fts_batch,
            )
            conn.commit()
            ingested += len(chunks_batch)

        # FTS5 optimize compacts the inverted index for query speed.
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        conn.commit()
        print(f"[build_bm25] DONE ingested={ingested}")
        size = DB_PATH.stat().st_size / 1024 / 1024
        print(f"[build_bm25] db size: {size:.1f} MB")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
