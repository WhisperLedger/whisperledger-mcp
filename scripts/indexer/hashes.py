"""Per-repo file-hash store for incremental indexing.

Each repo gets a JSON file at ~/jarvis/index/hashes/{repo}.json that maps
relative paths to their content hash (first 16 chars of sha256). On reindex
we compare to detect changed / removed / new files and only re-embed deltas.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from . import config

HASHES_DIR = Path(config.QDRANT_URL).parent  # placeholder; reassigned below

# Hash files live alongside the qdrant_storage dir, not inside it.
HASHES_DIR = Path.home() / "jarvis" / "index" / "hashes"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _path_for(repo: str) -> Path:
    return HASHES_DIR / f"{repo}.json"


def load(repo: str) -> dict[str, str]:
    p = _path_for(repo)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save(repo: str, hashes: dict[str, str]) -> None:
    HASHES_DIR.mkdir(parents=True, exist_ok=True)
    p = _path_for(repo)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(hashes, sort_keys=True, separators=(",", ":")))
    os.replace(tmp, p)


def drop(repo: str) -> None:
    """Forget all hashes for a repo (e.g. for forced full reindex)."""
    p = _path_for(repo)
    if p.exists():
        p.unlink()
