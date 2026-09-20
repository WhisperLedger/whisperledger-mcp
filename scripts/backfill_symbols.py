"""Backfill the `symbols` payload field on every chunk by regex-extracting
class/function/const names from chunk text.

Faster than re-embedding the whole corpus — symbols are extracted from the
existing chunk text via per-language patterns and set_payload-ed onto each
point. Future reindexes (realtime webhook or nightly) will overwrite these
with AST-derived symbols when files change.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

COLLECTION = "jarvis_code"
BATCH = 256

# Kotlin / Java
PAT_KOTLIN = re.compile(
    r"(?m)^[\t ]*(?:public |private |internal |protected |open |abstract |sealed |data |inline |suspend |override )*"
    r"(?:class|interface|object|enum class|fun|val|var)[\t ]+([A-Z_][\w]+|[a-z_][\w]+)\b",
)
# TS/JS — class/interface/enum/type/function/const/let with capitalized OR camelCase exports
PAT_TS = re.compile(
    r"(?m)^[\t ]*(?:export\s+)?(?:default\s+)?"
    r"(?:async\s+)?"
    r"(?:class|interface|enum|type|function|const|let)[\t ]+([A-Za-z_][\w]+)\b",
)
# Python (rare in our corpus but worth catching)
PAT_PY = re.compile(r"(?m)^[\t ]*(?:async\s+)?(?:class|def)[\t ]+([A-Za-z_][\w]+)\s*[(:]")
# Scala
PAT_SCALA = re.compile(r"(?m)^[\t ]*(?:case\s+|sealed\s+|abstract\s+)?(?:class|trait|object|def|val)[\t ]+([A-Za-z_][\w]+)\b")


def extract_symbols(text: str, lang: str) -> list[str]:
    syms: list[str] = []
    if lang in ("kotlin", "java"):
        syms = PAT_KOTLIN.findall(text)
    elif lang in ("typescript", "javascript"):
        syms = PAT_TS.findall(text)
    elif lang == "python":
        syms = PAT_PY.findall(text)
    elif lang == "scala":
        syms = PAT_SCALA.findall(text)
    # Common: dedupe, keep order, drop very short or all-lowercase short ones
    seen = set()
    out = []
    for s in syms:
        if s in seen or len(s) < 3:
            continue
        seen.add(s)
        out.append(s)
    return out


def main():
    client = QdrantClient(host="localhost", port=6333)
    total = client.count(collection_name=COLLECTION).count
    print(f"[backfill_symbols] {total} chunks to scan")
    next_offset = None
    scanned = 0
    updated = 0
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
        # Group set_payload calls by symbols list to batch.
        # Easier: one set_payload per point with the derived symbols.
        for p in pts:
            pl = p.payload or {}
            if pl.get("symbols"):
                # Already populated (by recent reindex) — skip.
                scanned += 1
                continue
            text = pl.get("text") or ""
            lang = pl.get("language") or ""
            syms = extract_symbols(text, lang)
            if not syms:
                scanned += 1
                continue
            client.set_payload(
                collection_name=COLLECTION,
                payload={"symbols": syms},
                points=[p.id],
                wait=False,
            )
            updated += 1
            scanned += 1
        if scanned % 5000 < BATCH:
            print(f"  scanned={scanned} updated={updated}")
        if next_offset is None:
            break
    print(f"[backfill_symbols] DONE scanned={scanned} updated={updated}")


if __name__ == "__main__":
    main()
