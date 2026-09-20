"""Index PR descriptions across indexed repos into the jarvis_prs collection.

For each repo: fetch PRs (open + recently-closed within PR_LOOKBACK_DAYS) via
gh api, embed (title + body) via Voyage, upsert into Qdrant.

Usage:
    python -m indexer.pr_indexer <repo>          # one repo
    python -m indexer.pr_indexer --all           # every repo in indexed_repos.txt
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from qdrant_client.models import PointStruct
from . import config, embedder, store

REPOS_FILE = Path.home() / "jarvis" / "scripts" / "indexed_repos.txt"

# Restricted repos are excluded from PR indexing. Their PRs would land in the
# shared jarvis_prs collection with no ACL enforcement, so simpler to skip.
# Revisit if per-collection PR retrieval becomes worth the complexity.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
try:
    from agent import acl as _acl
    _RESTRICTED_REPOS = _acl.all_restricted_repos()
except Exception:
    _RESTRICTED_REPOS = set()

BOT_HINTS = ("dependabot", "[bot]", "renovate", "snyk")


def _is_bot(login: str) -> bool:
    s = (login or "").lower()
    return any(h in s for h in BOT_HINTS)


def _clean_body(body: str) -> str:
    if not body:
        return ""
    # Strip image tags entirely; markdown images are noise for retrieval.
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    # Strip raw HTML comments (PR template instructions).
    body = re.sub(r"<!--[\s\S]*?-->", "", body)
    # Collapse runs of whitespace.
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[: config.PR_BODY_MAX_CHARS]


def fetch_prs(repo: str, since_iso: str) -> list[dict]:
    """Fetch open + recently-updated closed PRs via gh api."""
    out: list[dict] = []
    for state in ("open", "closed"):
        cmd = [
            "gh", "api", "--paginate",
            f"repos/jupitermoney/{repo}/pulls?state={state}&sort=updated&direction=desc&per_page=100",
            "--jq",
            '.[] | {number: .number, title: .title, body: .body, '
            'state: .state, merged_at: .merged_at, '
            'created_at: .created_at, updated_at: .updated_at, '
            'author: .user.login, html_url: .html_url}',
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            continue
        if res.returncode != 0:
            continue
        for line in res.stdout.splitlines():
            if not line.strip():
                continue
            try:
                pr = json.loads(line)
            except json.JSONDecodeError:
                continue
            # cutoff filter — gh sorts by updated desc; bail when we cross it
            if pr.get("updated_at", "") < since_iso:
                break
            if _is_bot(pr.get("author", "")):
                continue
            out.append(pr)
    return out


def index_repo_prs(repo: str) -> dict:
    store.ensure_prs_collection()
    since_dt = datetime.now(timezone.utc) - timedelta(days=config.PR_LOOKBACK_DAYS)
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    started = time.time()
    print(f"[{repo}] fetching PRs since {since_iso[:10]}...", flush=True)
    prs = fetch_prs(repo, since_iso)

    # Idempotent: replace all PRs for this repo each run.
    store.delete_repo_prs(repo)

    if not prs:
        print(f"[{repo}] no PRs in window", flush=True)
        return {"repo": repo, "prs": 0, "embed_calls": 0, "elapsed_sec": round(time.time() - started, 1)}

    texts: list[str] = []
    metas: list[dict] = []
    for pr in prs:
        title = (pr.get("title") or "").strip()
        body = _clean_body(pr.get("body") or "")
        # Combined embed text — title + body + metadata footer for context.
        composed = (
            f"PR #{pr['number']}: {title}\n\n"
            f"{body}\n\n"
            f"---\nState: {pr['state']}, "
            f"Author: {pr.get('author', '?')}, "
            f"Merged: {pr.get('merged_at') or 'no'}"
        )
        texts.append(composed)
        metas.append({
            "repo": repo,
            "pr_number": pr["number"],
            "title": title,
            "state": pr["state"],
            "author": pr.get("author"),
            "merged_at": pr.get("merged_at"),
            "created_at": pr.get("created_at"),
            "updated_at": pr.get("updated_at"),
            "html_url": pr.get("html_url"),
        })

    # Batch-embed (embedder handles token-budget batching).
    vectors = embedder.embed_documents(texts)
    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=vec,
            payload={**meta, "text": txt},
        )
        for vec, meta, txt in zip(vectors, metas, texts)
    ]
    store.upsert_pr_points(points)

    elapsed = round(time.time() - started, 1)
    summary = {
        "repo": repo,
        "prs": len(prs),
        "open": sum(1 for p in prs if p["state"] == "open"),
        "closed": sum(1 for p in prs if p["state"] == "closed"),
        "elapsed_sec": elapsed,
    }
    print(f"[{repo}] DONE {summary}", flush=True)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", nargs="?")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if args.all:
        repos = [
            line.split("#", 1)[0].strip()
            for line in REPOS_FILE.read_text().splitlines()
            if line.split("#", 1)[0].strip()
        ]
        skipped = [r for r in repos if r in _RESTRICTED_REPOS]
        repos = [r for r in repos if r not in _RESTRICTED_REPOS]
        if skipped:
            print(f"[pr_indexer] skipping {len(skipped)} restricted repos from PR indexing (per ACL): {sorted(skipped)}", flush=True)
    elif args.repo:
        if args.repo in _RESTRICTED_REPOS:
            print(f"[pr_indexer] refusing to PR-index restricted repo {args.repo!r} (ACL rules out PR collection)", flush=True)
            return 2
        repos = [args.repo]
    else:
        ap.error("pass a repo name or --all")

    totals = {"prs": 0, "repos_processed": 0, "repos_with_prs": 0}
    for r in repos:
        try:
            s = index_repo_prs(r)
            totals["prs"] += s["prs"]
            totals["repos_processed"] += 1
            if s["prs"]:
                totals["repos_with_prs"] += 1
        except Exception as e:
            print(f"[{r}] ERROR: {type(e).__name__}: {e}", flush=True)
    print(f"\n=== TOTALS: {totals} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
