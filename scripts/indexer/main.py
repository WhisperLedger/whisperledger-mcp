"""Index a single repo into Qdrant — incremental by default.

Reads ~/jarvis/index/hashes/<repo>.json to identify unchanged files and skip
re-embedding them. Use --full to force a full delete+reindex.

Usage:
    python -m indexer.main <repo_name>        # incremental
    python -m indexer.main <repo_name> --full # force full reindex
    python -m indexer.main --all              # incremental over all dirs
"""
from __future__ import annotations
import argparse
import sys
import time
import uuid
from pathlib import Path
from qdrant_client.models import PointStruct
from . import config, embedder, hashes, store, walker
from .chunker import chunk_text

# ACL-aware collection routing. Restricted repos live in a separate Qdrant
# collection so retrieval-time ACLs can hide them from unauthorized callers.
# See ~/.config/jarvis/restricted_acl.json + scripts/agent/acl.py.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
try:
    from agent import acl as _acl
except Exception:
    _acl = None

def _collection_for(repo_name: str) -> str:
    if _acl is None:
        return config.QDRANT_COLLECTION
    try:
        return _acl.collection_for_repo(repo_name)
    except Exception:
        return config.QDRANT_COLLECTION

_LANG_BY_EXT = {
    "kt": "kotlin", "kts": "kotlin",
    "java": "java", "scala": "scala", "sbt": "scala",
    "py": "python",
    "ts": "typescript", "tsx": "typescript",
    "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
    "go": "go", "rs": "rust",
    "sql": "sql",
    "tf": "terraform", "hcl": "hcl", "tfvars": "terraform",
    "yaml": "yaml", "yml": "yaml", "json": "json",
    "gradle": "gradle", "gql": "graphql", "graphql": "graphql", "proto": "proto",
    "sh": "shell", "bash": "shell", "zsh": "shell",
    "html": "html", "md": "markdown", "mdx": "markdown",
}


def _lang(ext: str) -> str:
    return _LANG_BY_EXT.get(ext, ext or "unknown")


def index_repo(repo_name: str, full: bool = False) -> dict:
    repo_root = config.REPOS_DIR / repo_name
    if not repo_root.is_dir():
        # Missing repo clone — warn and skip, do NOT raise. The wrapper
        # (reindex_all.sh) attempts a fresh shallow-clone before indexing; if
        # that failed (e.g. repo deleted from GitHub, network blip), we still
        # want the rest of the nightly reindex to proceed. Returning a clear
        # status dict + non-fatal exit lets us continue past one bad entry.
        msg = f"repo not found: {repo_root} — skipping (likely clone failure or stale indexed_repos.txt entry)"
        print(msg, flush=True)
        return {"repo": repo_name, "skipped": True, "reason": "missing_clone"}

    # Capture HEAD SHA so every chunk emitted in this run gets a permanent
    # GitHub commit-pinned permalink. Failures fall through to commit_sha=None;
    # the search_code formatter then falls back to "HEAD" as the ref.
    import subprocess as _sp
    try:
        commit_sha = _sp.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True
        ).strip() or None
    except Exception as _e:
        print(f"[{repo_name}] WARN: git rev-parse failed ({_e!s}) — chunks will use HEAD fallback", flush=True)
        commit_sha = None


    target_collection = _collection_for(repo_name)
    store.ensure_collection(target_collection)
    if target_collection != config.QDRANT_COLLECTION:
        print(f"[{repo_name}] routing to restricted collection: {target_collection}", flush=True)
    started = time.time()

    # Load prior hash state.
    old_hashes = {} if full else hashes.load(repo_name)
    if full:
        print(f"[{repo_name}] --full: wiping all existing chunks for repo", flush=True)
        store.delete_repo(repo_name, collection=target_collection)
        hashes.drop(repo_name)
    elif not old_hashes:
        # First incremental run for this repo. There may already be chunks from
        # earlier non-incremental runs; without this wipe, we'd double-index.
        print(f"[{repo_name}] first incremental run — wiping any pre-existing chunks", flush=True)
        store.delete_repo(repo_name, collection=target_collection)

    # Walk and compute new hash table.
    new_hashes: dict[str, str] = {}
    files_to_index: list[tuple[str, str]] = []  # (rel_path, text)
    files_unchanged = 0
    for rel_path, text, h in walker.walk_repo(repo_root):
        new_hashes[rel_path] = h
        if not full and old_hashes.get(rel_path) == h:
            files_unchanged += 1
            continue
        files_to_index.append((rel_path, text))

    # Paths to delete BEFORE re-upserting:
    # - paths in old_hashes but not in new (file deleted from repo)
    # - paths we're about to re-embed (defensive: ensures idempotency even if
    #   prior partial runs left chunks behind)
    paths_to_delete: list[str] = []
    if not full:
        paths_to_delete = [p for p in old_hashes if p not in new_hashes]
        paths_to_delete.extend(rel_path for rel_path, _ in files_to_index)
        if paths_to_delete:
            print(f"[{repo_name}] deleting chunks for {len(paths_to_delete)} stale/changed/new paths",
                  flush=True)
            store.delete_chunks_for_paths(repo_name, paths_to_delete, collection=target_collection)

    files_new_or_changed = len(files_to_index)
    files_removed = len([p for p in old_hashes if p not in new_hashes])
    print(f"[{repo_name}] files: {len(new_hashes)} total · "
          f"{files_unchanged} unchanged · {files_new_or_changed} new/changed · "
          f"{files_removed} removed",
          flush=True)

    # Embed + upsert only the new/changed files.
    pending_texts: list[str] = []
    pending_meta: list[dict] = []
    chunks_made = 0
    embed_calls = 0

    def flush() -> None:
        nonlocal embed_calls
        if not pending_texts:
            return
        embed_calls += 1
        vectors = embedder.embed_documents(pending_texts)
        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vec,
                payload={**meta, "text": txt},
            )
            for vec, meta, txt in zip(vectors, pending_meta, pending_texts)
        ]
        store.upsert_points(points, collection=target_collection)
        pending_texts.clear()
        pending_meta.clear()

    for i, (rel_path, text) in enumerate(files_to_index, 1):
        ext = Path(rel_path).suffix.lstrip(".").lower()
        for ch in chunk_text(text, ext):
            pending_texts.append(ch.text)
            pending_meta.append({
                "repo": repo_name,
                "path": rel_path,
                "ext": ext,
                "language": _lang(ext),
                "start_line": ch.start_line,
                "end_line": ch.end_line,
                "content_hash": new_hashes[rel_path],
                "commit_sha": commit_sha,
                "symbols": list(ch.symbols) if ch.symbols else [],
            })
            chunks_made += 1
            if len(pending_texts) >= config.EMBED_BATCH_MAX_ITEMS:
                flush()
        if i % 100 == 0:
            print(f"[{repo_name}] indexed {i}/{files_new_or_changed} files, "
                  f"chunks={chunks_made}, embed_calls={embed_calls}", flush=True)
    flush()

    # Persist the new hash table only AFTER successful indexing.
    hashes.save(repo_name, new_hashes)

    elapsed = round(time.time() - started, 1)
    summary = {
        "repo": repo_name,
        "files_total": len(new_hashes),
        "files_unchanged": files_unchanged,
        "files_new_or_changed": files_new_or_changed,
        "files_removed": files_removed,
        "chunks_made": chunks_made,
        "embed_calls": embed_calls,
        "elapsed_sec": elapsed,
        "mode": "full" if full else "incremental",
    }
    print(f"[{repo_name}] DONE {summary}", flush=True)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", nargs="?", help="repo name under ~/jarvis/repos")
    ap.add_argument("--all", action="store_true", help="index every directory in REPOS_DIR")
    ap.add_argument("--full", action="store_true", help="force full delete+reindex (no incremental)")
    args = ap.parse_args()

    if args.all:
        repos = sorted(p.name for p in config.REPOS_DIR.iterdir() if p.is_dir())
    elif args.repo:
        repos = [args.repo]
    else:
        ap.error("pass a repo name or --all")
    for r in repos:
        index_repo(r, full=args.full)
    return 0


if __name__ == "__main__":
    sys.exit(main())
