"""Tools the Jarvis agent can call. Pure Python — no LLM here."""
from __future__ import annotations
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote
from .repo_names import resolve_gh_org
from qdrant_client.models import Filter, FieldCondition, MatchValue
from indexer import config, embedder, store
from . import acl

REPOS_DIR = config.REPOS_DIR
MAX_FILE_BYTES_RETURNED = 200_000   # 200KB hard cap on file content
MAX_LINES_RETURNED = 2000
MAX_LIST_ENTRIES = 200
MAX_SNIPPET_CHARS = 1200

def _load_indexed_repos() -> list[str]:
    """Single source of truth: scripts/indexed_repos.txt (one repo per line, # comments)."""
    p = Path(__file__).parent.parent / "indexed_repos.txt"
    if not p.exists():
        # Defensive fallback to the original Phase 0 set.
        return ["bff-core", "platform", "lms", "gateway", "jupiter"]
    out: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


INDEXED_REPOS = _load_indexed_repos()


def _repo_gate(repo: str, tool: str) -> str | None:
    """Return an error JSON if the current caller may not access this repo,
    else None. Always audit the deny. Never leak the restricted collection
    name or allowlist — only that access is denied."""
    if acl.can_access_repo(repo):
        return None
    caller = acl.get_caller()
    acl.audit_restricted_access(caller, tool, repo=repo, denied=True,
                                reason='caller_not_in_allowlist')
    return json.dumps({"error": f"unknown repo: {repo}",
                       "indexed_repos_count": len(INDEXED_REPOS)})


def _visible_repos() -> list[str]:
    """INDEXED_REPOS filtered to what the current caller may see. Restricted
    repos that the caller cannot access are hidden from listings entirely
    (their names would themselves leak allowlist membership signals)."""
    caller = acl.get_caller()
    visible_restricted = acl.restricted_repos_visible_to(caller)
    hidden = acl.all_restricted_repos() - visible_restricted
    return [r for r in INDEXED_REPOS if r not in hidden]


def _search_collections_for_current_caller() -> list[str]:
    return acl.collections_for_caller(acl.get_caller())


def _gh_permalink(repo: str | None, path: str | None, commit_sha: str | None,
                  start_line: int | None, end_line: int | None) -> str | None:
    """Build a GitHub blob URL. Commit-pinned when commit_sha is present —
    these survive force-pushes, branch renames, and future commits.
    Falls back to the literal ref ``HEAD`` (GitHub resolves to default branch
    tip at view time) when commit_sha is missing — only happens for pre-
    backfill chunks. Returns None if we don't have enough to build a URL."""
    if not repo or not path:
        return None
    ref = commit_sha or "HEAD"
    _org, _upstream = resolve_gh_org(repo)
    url = f"https://github.com/{_org}/{_upstream}/blob/{ref}/{path}"
    if start_line:
        url += f"#L{start_line}"
        if end_line and end_line != start_line:
            url += f"-L{end_line}"
    return url




# --- search_code ----------------------------------------------------------------

def search_code_vector(query: str, repo: str | None = None, k: int = 8) -> str:
    """Vector-only search across indexed code (no reranker). Returns JSON-string list of hits.
    Searches every Qdrant collection the current caller is allowed to see
    (public + any restricted collections the caller is on the allowlist for);
    merges results by score and returns top-k."""
    k = max(1, min(int(k), 20))
    # If caller pinned a repo, refuse early when they can't see it.
    if repo:
        gate = _repo_gate(repo, 'search_code_vector')
        if gate:
            return gate
    vec = embedder.embed_query(query)
    flt = None
    if repo:
        flt = Filter(must=[FieldCondition(key="repo", match=MatchValue(value=repo))])
    all_points = []
    caller = acl.get_caller()
    for col in _search_collections_for_current_caller():
        res = store.client().query_points(
            collection_name=col,
            query=vec,
            limit=k,
            query_filter=flt,
            with_payload=True,
        )
        for h in res.points:
            all_points.append((col, h))
    # Merge by descending score, take top-k.
    all_points.sort(key=lambda ph: ph[1].score, reverse=True)
    all_points = all_points[:k]
    hits = []
    restricted_hits: list[dict] = []
    for col, h in all_points:
        p = h.payload or {}
        snippet = (p.get("text") or "")[:MAX_SNIPPET_CHARS]
        commit_sha = p.get("commit_sha")
        hit = {
            "repo": p.get("repo"),
            "path": p.get("path"),
            "start_line": p.get("start_line"),
            "end_line": p.get("end_line"),
            "language": p.get("language"),
            "score": round(float(h.score), 3),
            "snippet": snippet,
            "commit_sha": commit_sha,
            "permalink": _gh_permalink(
                p.get("repo"), p.get("path"), commit_sha,
                p.get("start_line"), p.get("end_line"),
            ),
        }
        hits.append(hit)
        if col != acl.PUBLIC_COLLECTION:
            restricted_hits.append({"repo": hit["repo"], "path": hit["path"],
                                    "score": hit["score"], "_col": col})
    if restricted_hits:
        # One audit line per query is enough — coalesce by collection.
        for col in {rh["_col"] for rh in restricted_hits}:
            acl.audit_restricted_access(
                caller, 'search_code_vector', collection=col,
                hits=[rh for rh in restricted_hits if rh["_col"] == col],
            )
    return json.dumps({"hits": hits}, ensure_ascii=False)




# --- search_code_hybrid ---------------------------------------------------------

def search_code_hybrid(query: str, repo: str | None = None, k: int = 8) -> str:
    """Vector + BM25 retrieval fused via Reciprocal Rank Fusion.

    Runs both ranklists in parallel (Qdrant vector top-20, sqlite FTS5 BM25
    top-20), fuses by sum(1/(60+rank)), returns top-k. BM25 surfaces exact
    identifier/phrase matches that pure semantic vectors miss; vector surfaces
    semantically related chunks BM25 misses. Same hit schema as search_code
    so consumers can swap one for the other transparently.
    """
    from . import hybrid_search as hs

    k = max(1, min(int(k), 20))
    K_INNER = 20  # how many to pull from each ranklist before fusion

    # Vector ranklist — fan-out across allowed collections, merge by score.
    if repo:
        gate = _repo_gate(repo, "search_code_hybrid")
        if gate:
            return gate
    vec = embedder.embed_query(query)
    flt = None
    if repo:
        flt = Filter(must=[FieldCondition(key="repo", match=MatchValue(value=repo))])
    all_vec_points = []
    for _col in _search_collections_for_current_caller():
        _r = store.client().query_points(
            collection_name=_col,
            query=vec,
            limit=K_INNER,
            query_filter=flt,
            with_payload=True,
        )
        for _h in _r.points:
            all_vec_points.append((_col, _h))
    all_vec_points.sort(key=lambda cp: cp[1].score, reverse=True)
    all_vec_points = all_vec_points[:K_INNER]
    vector_hits = []
    payload_by_qid: dict[str, dict] = {}
    qid_to_col: dict[str, str] = {}
    for _col, h in all_vec_points:
        pl = h.payload or {}
        vector_hits.append({
            "repo": pl.get("repo"),
            "path": pl.get("path"),
            "start_line": pl.get("start_line"),
            "end_line": pl.get("end_line"),
            "language": pl.get("language"),
            "score": round(float(h.score), 3),
        })
        payload_by_qid[str(h.id)] = pl
        qid_to_col[str(h.id)] = _col

    # BM25 ranklist.
    try:
        bm25_hits = hs.bm25_search(query, k=K_INNER, repo=repo)
    except RuntimeError as e:
        # Index not built — degrade gracefully to vector-only.
        return json.dumps({"hits": vector_hits[:k], "fused": False,
                           "warning": str(e)}, ensure_ascii=False)

    # Pull the full Qdrant payload for any BM25 hit we don't already have
    # (it has the snippet text + commit_sha + symbols).
    missing_qids = [h["qdrant_id"] for h in bm25_hits
                    if h["qdrant_id"] not in payload_by_qid]
    if missing_qids:
        retrieved = store.client().retrieve(
            collection_name=config.QDRANT_COLLECTION,
            ids=missing_qids,
            with_payload=True,
        )
        for r in retrieved:
            payload_by_qid[str(r.id)] = r.payload or {}

    # Fuse via RRF.
    fused = hs.rrf_fuse(vector_hits, bm25_hits)

    # Hydrate hits with full payload (text snippet, permalink, symbols, etc).
    out = []
    for h in fused[:k]:
        # Find the matching payload — try matching by (repo, path, lines).
        match_pl = None
        for qid, pl in payload_by_qid.items():
            if (pl.get("repo") == h.get("repo")
                and pl.get("path") == h.get("path")
                and pl.get("start_line") == h.get("start_line")
                and pl.get("end_line") == h.get("end_line")):
                match_pl = pl
                break
        snippet = (match_pl or {}).get("text", "")[:MAX_SNIPPET_CHARS] if match_pl else ""
        commit_sha = (match_pl or {}).get("commit_sha") if match_pl else None
        out.append({
            "repo": h.get("repo"),
            "path": h.get("path"),
            "start_line": h.get("start_line"),
            "end_line": h.get("end_line"),
            "language": h.get("language"),
            "score": round(h.get("_fused_score", 0.0), 4),
            "sources": h.get("_sources", []),
            "snippet": snippet,
            "commit_sha": commit_sha,
            "permalink": _gh_permalink(
                h.get("repo"), h.get("path"), commit_sha,
                h.get("start_line"), h.get("end_line"),
            ),
            "symbols": (match_pl or {}).get("symbols", []) if match_pl else [],
        })
    return json.dumps({"hits": out, "fused": True}, ensure_ascii=False)

# --- read_file ------------------------------------------------------------------

def read_file(repo: str, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    """Read a file from a cloned repo. Optional 1-indexed line range. Bounded."""
    if repo not in INDEXED_REPOS:
        return json.dumps({"error": f"unknown repo: {repo}", "indexed_repos": INDEXED_REPOS})
    gate = _repo_gate(repo, "read_file")
    if gate:
        return gate
    if acl.collection_for_repo(repo) != acl.PUBLIC_COLLECTION:
        acl.audit_restricted_access(acl.get_caller(), "read_file", repo=repo)
    full = (REPOS_DIR / repo / path).resolve()
    repo_root = (REPOS_DIR / repo).resolve()
    # path traversal guard
    if not str(full).startswith(str(repo_root) + "/"):
        return json.dumps({"error": "path escapes repo root"})
    if not full.is_file():
        return json.dumps({"error": f"not found: {repo}/{path}"})
    try:
        text = full.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return json.dumps({"error": "binary or non-utf8 file"})
    lines = text.splitlines()
    total_lines = len(lines)
    s = max(1, int(start_line)) if start_line else 1
    e = min(total_lines, int(end_line)) if end_line else total_lines
    # cap returned lines
    if e - s + 1 > MAX_LINES_RETURNED:
        e = s + MAX_LINES_RETURNED - 1
    sliced = "\n".join(lines[s - 1:e])
    if len(sliced.encode("utf-8")) > MAX_FILE_BYTES_RETURNED:
        sliced = sliced.encode("utf-8")[:MAX_FILE_BYTES_RETURNED].decode("utf-8", errors="ignore")
    return json.dumps({
        "repo": repo,
        "path": path,
        "total_lines": total_lines,
        "returned_lines": [s, e],
        "content": sliced,
    }, ensure_ascii=False)


# --- search_prs -----------------------------------------------------------------

def search_prs(query: str, repo: str | None = None, k: int = 8) -> str:
    """Semantic search over PR descriptions (jarvis_prs collection).

    PR descriptions usually capture rationale, alternatives considered, linked
    tickets — way more 'why' content than commit messages or code itself.
    Use when the question is about motivation / design intent / trade-offs.

    Restricted repos are not PR-indexed (v1); search_prs never returns hits
    from them regardless of caller.
    """
    if repo:
        gate = _repo_gate(repo, "search_prs")
        if gate:
            return gate
    k = max(1, min(int(k), 20))
    vec = embedder.embed_query(query)
    flt = None
    if repo:
        flt = Filter(must=[FieldCondition(key="repo", match=MatchValue(value=repo))])
    res = store.client().query_points(
        collection_name=config.QDRANT_COLLECTION_PRS,
        query=vec,
        limit=k,
        query_filter=flt,
        with_payload=True,
    )
    hits = []
    for h in res.points:
        p = h.payload or {}
        snippet = (p.get("text") or "")[:MAX_SNIPPET_CHARS]
        hits.append({
            "repo": p.get("repo"),
            "pr_number": p.get("pr_number"),
            "title": p.get("title"),
            "state": p.get("state"),
            "author": p.get("author"),
            "merged_at": p.get("merged_at"),
            "html_url": p.get("html_url"),
            "score": round(float(h.score), 3),
            "snippet": snippet,
        })
    return json.dumps({"hits": hits}, ensure_ascii=False)


# --- git_history ----------------------------------------------------------------

def git_history(repo: str, path: str, limit: int = 10) -> str:
    """Last N commits touching a file. Crucial for 'why' questions — design
    rationale usually lives in commit messages, not the code itself."""
    if repo not in INDEXED_REPOS:
        return json.dumps({"error": f"unknown repo: {repo}", "indexed_repos_count": len(INDEXED_REPOS)})
    gate = _repo_gate(repo, "git_history")
    if gate:
        return gate
    if acl.collection_for_repo(repo) != acl.PUBLIC_COLLECTION:
        acl.audit_restricted_access(acl.get_caller(), "git_history", repo=repo)
    limit = max(1, min(int(limit), 30))
    encoded_path = quote(path, safe="/")
    _org, _upstream = resolve_gh_org(repo)
    url = f"repos/{_org}/{_upstream}/commits?path={encoded_path}&per_page={limit}"
    try:
        res = subprocess.run(
            ["gh", "api", url, "--jq",
             '.[] | {sha: .sha[:7], author: .commit.author.name, '
             'date: (.commit.author.date | sub("T.*$"; "")), '
             'subject: ((.commit.message | split("\\n")[0])[:200]), '
             'body: ((.commit.message | split("\\n")[2:] | join("\\n"))[:1500])}'],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "gh api timeout"})
    if res.returncode != 0:
        return json.dumps({"error": f"gh api failed: {res.stderr.strip()[:300]}"})
    commits = []
    for line in res.stdout.splitlines():
        if line.strip():
            try:
                c = json.loads(line)
                # Drop empty body to keep payload tight.
                if not c.get("body", "").strip():
                    c.pop("body", None)
                commits.append(c)
            except json.JSONDecodeError:
                pass
    return json.dumps({
        "repo": repo, "path": path, "commits": commits, "returned_count": len(commits),
    }, ensure_ascii=False)


# --- grep_repo ------------------------------------------------------------------

def grep_repo(
    repo: str,
    pattern: str,
    file_glob: str | None = None,
    limit: int = 50,
) -> str:
    """Exact-string regex search via `grep -E` over an indexed repo's clone.

    Complements search_code (semantic, via Qdrant) — use grep_repo when you
    need to find a LITERAL string: HTTP path like '/rew/v1/...', a class /
    function / enum name, an error message, env var, OpenAPI operationId,
    config key. Semantic search is unreliable for exact-string finds;
    grep is deterministic. Returns up to `limit` matches.
    """
    if repo not in INDEXED_REPOS:
        return json.dumps({"error": f"unknown repo: {repo}", "indexed_repos_count": len(INDEXED_REPOS)})
    gate = _repo_gate(repo, "grep_repo")
    if gate:
        return gate
    if acl.collection_for_repo(repo) != acl.PUBLIC_COLLECTION:
        acl.audit_restricted_access(acl.get_caller(), "grep_repo", repo=repo)
    limit = max(1, min(int(limit), 200))
    repo_root = REPOS_DIR / repo
    if not repo_root.is_dir():
        return json.dumps({"error": f"repo clone missing on box: {repo_root}"})
    cmd = [
        "grep", "-rInE",
        "--exclude-dir=node_modules", "--exclude-dir=.git",
        "--exclude-dir=build", "--exclude-dir=dist",
        "--exclude-dir=target", "--exclude-dir=ios/Pods",
        "--exclude-dir=android/build",
    ]
    if file_glob:
        cmd.append(f"--include={file_glob}")
    cmd.extend([pattern, str(repo_root)])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "grep timeout (30s) — narrow your pattern or add file_glob"})
    matches: list[dict] = []
    truncated = False
    for line in r.stdout.splitlines():
        m = re.match(r"^(.+?):(\d+):(.*)$", line)
        if not m:
            continue
        try:
            rel = str(Path(m.group(1)).relative_to(repo_root))
        except ValueError:
            rel = m.group(1)
        matches.append({
            "path": rel,
            "line": int(m.group(2)),
            "text": m.group(3)[:300],
        })
        if len(matches) >= limit:
            truncated = True
            break
    return json.dumps({
        "repo": repo,
        "pattern": pattern,
        "file_glob": file_glob,
        "match_count": len(matches),
        "truncated": truncated,
        "matches": matches,
    }, ensure_ascii=False)


# --- grep_all_repos --------------------------------------------------------------

def grep_all_repos(
    pattern: str,
    file_glob: str | None = None,
    limit: int = 50,
) -> str:
    """CROSS-REPO regex grep across every indexed repo clone in one call.

    Use INSTEAD OF grep_repo when you don't know which repo holds the string.
    Critical for service-discovery: a K8s DNS like `deposit-manager-ms.cbs.svc.
    cluster.local` is hardcoded in consumer config files but you don't know
    which consumer — this tool finds it in one shot. Also for "where is class
    X referenced", "which repos call endpoint Y", etc.

    Returns matches with `repo` AND `path` fields. Wider time-budget than
    grep_repo (60s) since the search is wider. Always pass a tight pattern
    and ideally a file_glob to avoid noisy hits — a broad pattern like
    `user` would match millions of lines.
    """
    limit = max(1, min(int(limit), 200))
    if not REPOS_DIR.is_dir():
        return json.dumps({"error": f"REPOS_DIR missing: {REPOS_DIR}"})
    cmd = [
        "grep", "-rInE",
        "--exclude-dir=node_modules", "--exclude-dir=.git",
        "--exclude-dir=build", "--exclude-dir=dist",
        "--exclude-dir=target", "--exclude-dir=ios/Pods",
        "--exclude-dir=android/build",
    ]
    if file_glob:
        cmd.append(f"--include={file_glob}")
    cmd.extend([pattern, str(REPOS_DIR)])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return json.dumps({
            "error": "cross-repo grep timeout (60s) — narrow pattern or add file_glob",
        })
    matches: list[dict] = []
    truncated = False
    for line in r.stdout.splitlines():
        m = re.match(r"^(.+?):(\d+):(.*)$", line)
        if not m:
            continue
        try:
            rel = str(Path(m.group(1)).relative_to(REPOS_DIR))
        except ValueError:
            continue
        parts = rel.split("/", 1)
        repo = parts[0]
        path = parts[1] if len(parts) > 1 else ""
        matches.append({
            "repo": repo,
            "path": path,
            "line": int(m.group(2)),
            "text": m.group(3)[:300],
        })
        if len(matches) >= limit:
            truncated = True
            break
    # Aggregate: which repos had hits?
    _visible = set(_visible_repos())
    _dropped = [m for m in matches if m["repo"] not in _visible]
    matches = [m for m in matches if m["repo"] in _visible]
    if _dropped:
        acl.audit_restricted_access(acl.get_caller(), "grep_all_repos",
                                    hits=None, denied=True,
                                    reason=f"filtered {len(_dropped)} hits from restricted repos")
    from collections import Counter
    repo_counts = Counter(m_["repo"] for m_ in matches)
    return json.dumps({
        "pattern": pattern,
        "file_glob": file_glob,
        "match_count": len(matches),
        "truncated": truncated,
        "repos_with_hits": dict(repo_counts.most_common(20)),
        "matches": matches,
    }, ensure_ascii=False)


# --- lookup_service -------------------------------------------------------------

_SERVICE_REGISTRY_PATH = Path("/home/ubuntu/jarvis/index/service_registry.json")
_service_registry_cache: dict | None = None
_service_alias_index: dict[str, str] | None = None  # alias (lowercased) → canonical name


def _load_service_registry(force: bool = False) -> dict:
    """Lazy-load the service registry. Refreshes on file mtime change."""
    global _service_registry_cache, _service_alias_index
    if _service_registry_cache is not None and not force:
        return _service_registry_cache
    if not _SERVICE_REGISTRY_PATH.is_file():
        _service_registry_cache = {"_meta": {}, "services": {}}
        _service_alias_index = {}
        return _service_registry_cache
    try:
        _service_registry_cache = json.loads(_SERVICE_REGISTRY_PATH.read_text())
    except Exception:
        _service_registry_cache = {"_meta": {}, "services": {}}
    # Build alias index (canonical name + aliases → canonical name).
    # Two passes so explicit overrides always beat auto-derived `-ms`-stripped aliases.
    idx: dict[str, str] = {}
    services = (_service_registry_cache.get("services") or {})
    # Pass 1 (weak): canonical name itself + auto-derived `-ms`-stripped form
    for canonical in services.keys():
        idx[canonical.lower()] = canonical
        if canonical.endswith("-ms"):
            idx.setdefault(canonical[:-3].lower(), canonical)
    # Pass 2 (strong): explicit aliases from overrides — always wins
    for canonical, entry in services.items():
        for alias in entry.get("aliases", []) or []:
            idx[str(alias).lower()] = canonical
    _service_alias_index = idx
    return _service_registry_cache


def lookup_service(name_or_alias: str) -> str:
    """Look up a Jupiter internal service by canonical name or alias.

    Returns the registry entry as JSON: K8s in-cluster URL, Route53 cross-cluster
    URL, namespace/port, consumer repos, source repo, OpenAPI spec file, exposed
    paths, sample Feign client, auth pattern. Use this INSTEAD OF grep_all_repos
    when the user asks "where is service X" / "what's the URL for Y" / "what
    endpoints does Z expose" — sub-second answer from the pre-built registry
    versus 30s+ of grep_all_repos + manual stitching.

    Aliases supported: canonical name (e.g. `bullet-ms`), name without `-ms`
    suffix (`bullet`), Jupiter acronyms (`llm` → lending-lifecycle-manager-ms),
    and human-readable names (`Loan Lifecycle Manager`).
    """
    if not name_or_alias or not isinstance(name_or_alias, str):
        return json.dumps({"error": "name_or_alias must be a non-empty string"})
    reg = _load_service_registry()
    services = reg.get("services") or {}
    idx = _service_alias_index or {}
    key = name_or_alias.strip().lower()
    canonical = idx.get(key)
    # Fallback: substring search if no exact alias match
    if not canonical:
        substrings = [n for n in services.keys() if key in n.lower()]
        if len(substrings) == 1:
            canonical = substrings[0]
        elif len(substrings) > 1:
            return json.dumps({
                "error": f"ambiguous '{name_or_alias}' — matched {len(substrings)} services",
                "candidates": substrings[:10],
                "hint": "be more specific, or pass the canonical name (e.g. 'bullet-ms')",
            })
    if not canonical:
        # Total miss — return the closest alphabetical suggestions
        all_names = sorted(services.keys())
        return json.dumps({
            "error": f"no service matching '{name_or_alias}'",
            "registry_size": len(services),
            "registry_age_utc": (reg.get("_meta") or {}).get("generated_at_utc"),
            "hint": "try grep_all_repos('<service-name>', file_glob='*.yml') to find the service URL in consumer configs",
            "sample_canonical_names": all_names[:5] + ["..."] + all_names[-5:] if len(all_names) > 10 else all_names,
        })
    entry = services[canonical]
    # Strip the internal `_consumer_files` bookkeeping field for cleaner output
    out = {k: v for k, v in entry.items() if not k.startswith("_")}
    out["canonical_name"] = canonical
    return json.dumps(out, ensure_ascii=False)


# --- list_repo_files ------------------------------------------------------------

def list_repo_files(repo: str, glob: str = "**/*", limit: int = 100) -> str:
    """List file paths in a repo matching a glob (e.g. '**/auth/**/*.kt')."""
    if repo not in INDEXED_REPOS:
        return json.dumps({"error": f"unknown repo: {repo}", "indexed_repos": INDEXED_REPOS})
    gate = _repo_gate(repo, "list_repo_files")
    if gate:
        return gate
    if acl.collection_for_repo(repo) != acl.PUBLIC_COLLECTION:
        acl.audit_restricted_access(acl.get_caller(), "list_repo_files", repo=repo)
    limit = max(1, min(int(limit), MAX_LIST_ENTRIES))
    repo_root = REPOS_DIR / repo
    matches: list[str] = []
    truncated = False
    for p in repo_root.glob(glob):
        if not p.is_file():
            continue
        if any(part in {".git", "node_modules", "build", "target", "dist"} for part in p.parts):
            continue
        matches.append(str(p.relative_to(repo_root)))
        if len(matches) >= limit:
            truncated = True
            break
    return json.dumps({"repo": repo, "matches": sorted(matches), "truncated": truncated})


# --- tool schemas (for Anthropic API) -------------------------------------------

def lookup_symbol(name: str, repo: str | None = None, k: int = 10) -> str:
    """Find chunks that DECLARE the given symbol (class/function/const name).

    Deterministic lookup: filters Qdrant on the `symbols` payload array. Use
    BEFORE search_code when you have an exact name (e.g. "PaymentsController",
    "useVarunaPayment", "ApportionmentStrategy") — it returns the chunks where
    that name is actually defined, not chunks that mention it.
    """
    k = max(1, min(int(k), 50))
    if repo:
        gate = _repo_gate(repo, "lookup_symbol")
        if gate:
            return gate
    flt_must = [FieldCondition(key="symbols", match=MatchValue(value=name))]
    if repo:
        flt_must.append(FieldCondition(key="repo", match=MatchValue(value=repo)))
    pts: list = []
    caller = acl.get_caller()
    restricted_meta: list[dict] = []
    for _col in _search_collections_for_current_caller():
        _p, _ = store.client().scroll(
            collection_name=_col,
            scroll_filter=Filter(must=flt_must),
            limit=k,
            with_payload=True,
        )
        for _pt in _p:
            pts.append(_pt)
            if _col != acl.PUBLIC_COLLECTION:
                _pl = _pt.payload or {}
                restricted_meta.append({"repo": _pl.get("repo"),
                                        "path": _pl.get("path"), "_col": _col})
    pts = pts[:k]
    if restricted_meta:
        for _col in {rm["_col"] for rm in restricted_meta}:
            acl.audit_restricted_access(caller, "lookup_symbol", collection=_col,
                                        hits=[rm for rm in restricted_meta if rm["_col"] == _col])
    hits = []
    for p in pts:
        pl = p.payload or {}
        snippet = (pl.get("text") or "")[:MAX_SNIPPET_CHARS]
        commit_sha = pl.get("commit_sha")
        hits.append({
            "repo": pl.get("repo"),
            "path": pl.get("path"),
            "start_line": pl.get("start_line"),
            "end_line": pl.get("end_line"),
            "language": pl.get("language"),
            "symbols": pl.get("symbols", []),
            "snippet": snippet,
            "commit_sha": commit_sha,
            "permalink": _gh_permalink(
                pl.get("repo"), pl.get("path"), commit_sha,
                pl.get("start_line"), pl.get("end_line"),
            ),
        })
    return json.dumps({"name": name, "hits": hits, "count": len(hits)},
                      ensure_ascii=False)


def search_code(query: str, repo: str | None = None, k: int = 8) -> str:
    """Default code search: vector retrieval + Haiku rerank.

    Pulls top-20 from vector search and reranks via Claude Haiku 4.5 to lift
    the actual answer to rank 1. Adds ~1-2s latency and ~$0.003 per query;
    eval shows hits@1 +10pp, hits@3 +10pp, MRR +0.08 vs vector-only.

    Opt out with JARVIS_DISABLE_RERANK=1 to fall back to pure vector.
    The pre-rerank function remains available as search_code_vector if
    a caller needs sub-second latency.
    """
    import os
    k = max(1, min(int(k), 20))
    if os.environ.get("JARVIS_DISABLE_RERANK"):
        return search_code_vector(query, repo=repo, k=k)
    # Otherwise pull a wider pool and rerank to top-k.
    from . import reranker
    import json as _json
    pool = _json.loads(search_code_vector(query, repo=repo, k=20))
    hits = pool.get("hits", [])
    reranked = reranker.rerank(query, hits, k=k)
    return _json.dumps({"hits": reranked, "reranked": True,
                        "pool_size": len(hits)}, ensure_ascii=False)


def search_code_reranked(query: str, repo: str | None = None, k: int = 5) -> str:
    """Vector search + Haiku reranker. Returns top-k after cross-encoder rerank.

    1. Pulls top-20 candidates from vector search.
    2. Haiku 4.5 reranks them against the query rubric ("define the thing"
       beats "mention the thing"; prefer code over docs; etc).
    3. Returns top-k in Haiku's order.

    Cost: ~$0.003 per query on top of vector search. Adds ~1-2s latency.
    Falls back to vector order if the Haiku call fails — never blocks
    production on reranker outage.
    """
    from . import reranker
    k = max(1, min(int(k), 10))
    # Pull a wider candidate pool so the reranker has signal to discriminate.
    pool = json.loads(search_code(query, repo=repo, k=20))
    hits = pool.get("hits", [])
    reranked = reranker.rerank(query, hits, k=k)
    return json.dumps({"hits": reranked, "reranked": True,
                       "pool_size": len(hits)}, ensure_ascii=False)


def search_multi(queries: list[str], repo: str | None = None, k_per_query: int = 5) -> str:
    """Run multiple vector searches in parallel, deduplicate, then apply a single
    global Haiku rerank across all candidates.

    Per-query reranking (the old approach) ranked each query's results in isolation
    and then merged by raw vector score — discarding the reranker's quality signal
    at merge time. This approach instead:
      1. Fans out search_code_vector in parallel (no per-query rerank, wider k=20 pool).
      2. Deduplicates hits by repo/path/start_line, keeping highest vector score.
      3. Runs ONE Haiku rerank on the merged pool using all queries as combined intent.

    Net latency is roughly the same (~1.5s for parallel vector searches + ~1.5s for
    the single global rerank vs ~1.5s for parallel per-query reranks). Quality is
    better because all candidates are ranked jointly against the full search intent.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import json as _json
    import os

    queries = [q for q in queries if q and q.strip()][:5]  # cap at 5, drop blanks
    if not queries:
        return _json.dumps({"error": "no queries provided"})
    k_per_query = max(1, min(int(k_per_query), 10))

    # Step 1: parallel vector-only searches with a wide pool (k=20) so the
    # global reranker has enough candidates to discriminate across all angles.
    def _run(q: str) -> tuple[str, list]:
        try:
            raw = _json.loads(search_code_vector(q, repo=repo, k=20))
            return q, raw.get("hits", [])
        except Exception:
            return q, []

    results_by_query: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=len(queries)) as executor:
        futures = {executor.submit(_run, q): q for q in queries}
        for future in as_completed(futures):
            q, hits = future.result()
            results_by_query[q] = hits

    # Step 2: deduplicate by repo/path/start_line — keep highest vector score copy
    seen: dict[str, dict] = {}
    for q, hits in results_by_query.items():
        for hit in hits:
            key = f"{hit.get('repo', '')}/{hit.get('path', '')}:{hit.get('start_line', 0)}"
            if key not in seen or hit.get("score", 0) > seen[key].get("score", 0):
                seen[key] = {**hit, "matched_query": q}

    merged = sorted(seen.values(), key=lambda h: h.get("score", 0), reverse=True)

    # Step 3: single global rerank — all candidates ranked jointly against the
    # combined intent of every query. Skipped when JARVIS_DISABLE_RERANK is set.
    reranked = False
    if not os.environ.get("JARVIS_DISABLE_RERANK") and len(merged) > 1:
        try:
            from . import reranker as _reranker
            combined_intent = " | ".join(queries)
            merged = _reranker.rerank(combined_intent, merged, k=len(merged))
            reranked = True
        except Exception:
            pass  # fail open: merged is already in vector-score order

    return _json.dumps({
        "hits": merged,
        "total_unique": len(merged),
        "queries_run": list(results_by_query.keys()),
        "reranked": reranked,
    }, ensure_ascii=False)


def why_was_this_changed(repo: str, path: str, line: int | None = None) -> str:
    """Git-archaeology wrapper. Delegates to scripts/agent/git_archaeology.py.
    Returns a strict JSON response with commit + PR + jira + review comments.
    """
    from . import git_archaeology
    return git_archaeology.why_was_this_changed(repo, path, line)


def impact_analysis(target: str, repo: str | None = None) -> str:
    """Impact-analysis wrapper. Delegates to scripts/agent/jarvis_impact.py."""
    from . import jarvis_impact
    return jarvis_impact.impact_analysis(target, repo)


def parse_stacktrace(stacktrace: str) -> str:
    """Parse Kotlin/Java/Node/Python stacktraces into structured frames."""
    from . import parse_stacktrace as ps
    return ps.parse_stacktrace(stacktrace)


def write_test_for(repo: str, file_path: str, function: str | None = None) -> str:
    """Draft test cases for a function or file — matches existing repo test style."""
    from . import write_test_for as wt
    return wt.write_test_for(repo, file_path, function)


def service_tour(repo: str) -> str:
    """15-min walkthrough of a service / repo for new engineers."""
    from . import service_tour as st
    return st.service_tour(repo)



def janus_user_journey(
    user_id: str,
    lookback_hours: int = 24,
    event_filter: list[str] | None = None,
    max_events: int = 200,
    caller_id: str = "",
) -> str:
    """Fetch a Jupiter user's recent Amplitude event stream via Janus (MCP/stdio).

    Use for "why did user X end up on screen Y", "what triggered this state
    transition", "how did user Z bypass step W" — questions where logs + code
    cannot answer because the cause is a product-level event (deeplink, push,
    A/B variant, campaign, screen nav).

    Returns a compact JSON string with timeline + summary + audit_ref. On error,
    returns a JSON error envelope with error_code in {invalid_request,
    user_not_found, upstream_error} — surface that to the user honestly, do
    not retry on invalid_request.

    Defaults to a tight window (24h) so timelines stay readable. Pass
    event_filter for a clean trace (e.g. ["Deeplink Opened", "Screen Viewed",
    "Push Notification Clicked"]). Max 168h (7d) hard ceiling on Janus side.
    """
    from agent import janus_client

    try:
        result = janus_client.user_journey(
            user_id=user_id,
            lookback_hours=lookback_hours,
            event_filter=event_filter,
            max_events=max_events,
            caller_id=caller_id or "",
        )
    except Exception as e:
        return json.dumps({"error_code": "upstream_error",
                           "error": f"{type(e).__name__}: {e!s}"})

    # Janus already returns the shape we want. Just stringify for the agent.
    # Add a note hinting at IST conversion for the agent to render.
    if isinstance(result, dict) and "events" in result:
        result["_jarvis_note"] = (
            "Timestamps are UTC. Convert to IST (+5:30) when surfacing to engineers. "
            "If summary.window_incomplete=true, advise the engineer to narrow window/filter."
        )
    return json.dumps(result, ensure_ascii=False)


def janus_event_count(
    event_type: str,
    lookback_days: int = 7,
    filter_property: str = "",
    filter_value: str = "",
    group_by: str = "",
    caller_id: str = "",
) -> str:
    """Fleet-wide count for ONE Amplitude event over a trailing window via Janus.

    Use for "how many users did X in the last N days" — simple count questions
    like "how many users saw bottom-sheet-viewed with vpn-detected last 7 days".
    Returns {event_type, lookback_days, unique_users, total_events, daily[],
    top_values[], audit_ref} on success.

    filter_property is an EVENT property paired with filter_value. For built-in
    dimensions (platform / country / version), use group_by instead.

    For multi-step funnels / retention / rich multi-dimension breakdowns,
    deflect to the specific named Amplitude dashboard (not this tool).

    Default lookback_days=7, max 90.
    """
    from agent import janus_client

    try:
        result = janus_client.event_count(
            event_type=event_type,
            lookback_days=lookback_days,
            filter_property=filter_property,
            filter_value=filter_value,
            group_by=group_by,
            caller_id=caller_id or "",
        )
    except Exception as e:
        return json.dumps({"error_code": "upstream_error",
                           "error": f"{type(e).__name__}: {e!s}"})
    return json.dumps(result, ensure_ascii=False)


TOOL_SCHEMAS = [
    {
        "name": "search_prs",
        "description": (
            "Semantic search over PR descriptions across all indexed repos. PR descriptions "
            "are usually richer than commit messages — they include problem statement, "
            "alternatives considered, links to tickets/Confluence, screenshots refs. The "
            "BEST source for 'why was X built this way' / 'what trade-offs were considered' / "
            "'what was the original motivation' questions. Returns title + body snippet + URL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query."},
                "repo": {"type": "string", "description": "Optional. Restrict to one repo."},
                "k": {"type": "integer", "description": "Number of hits (1-20). Default 8."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_multi",
        "description": (
            "Run up to 5 search queries IN PARALLEL and return deduplicated, globally reranked results. "
            "**Use this instead of sequential search_code calls whenever you need to search "
            "multiple angles, phrasings, or sub-topics at once.** Collapses N round-trips "
            "into 1 — major latency win for multi-faceted questions. Parallel vector searches "
            "are deduplicated then a single Haiku rerank ranks all candidates jointly against "
            "your combined search intent — better quality than per-query reranking. "
            "Returns the same hit shape as search_code plus a "
            "`matched_query` field showing which query found each chunk."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of 2-5 distinct search queries to run in parallel.",
                    "minItems": 2,
                    "maxItems": 5,
                },
                "repo": {"type": "string", "description": "Optional. Restrict all queries to one repo."},
                "k_per_query": {"type": "integer", "description": "Hits to return per query before dedup (1-10). Default 5."},
            },
            "required": ["queries"],
        },
    },
    {
        "name": "search_code",
        "description": (
            "Semantic search across the indexed Jupiter code corpus. Returns the most "
            "relevant code chunks across all repos (or scoped to one repo). Use this "
            "for a single focused search. Use search_multi when you need multiple angles."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query."},
                "repo": {
                    "type": "string",
                    "description": f"Optional. Restrict to one repo. One of: {', '.join(INDEXED_REPOS)}.",
                },
                "k": {"type": "integer", "description": "Number of hits to return (1-20). Default 8."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the full text (or a line range) of a file from a cloned repo. Use after "
            "search_code when you need more context around a hit, or to verify a claim."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": f"One of: {', '.join(INDEXED_REPOS)}."},
                "path": {"type": "string", "description": "Path relative to repo root."},
                "start_line": {"type": "integer", "description": "1-indexed start line (optional)."},
                "end_line": {"type": "integer", "description": "1-indexed end line, inclusive (optional)."},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "git_history",
        "description": (
            "Show the last N commits that touched a specific file. CRUCIAL for "
            "'why' questions — the rationale behind code decisions usually lives "
            "in commit messages, not in the code itself. Use this WHENEVER the "
            "user asks 'why was X done this way', 'what changed in X recently', "
            "or 'how did X evolve'. Returns commit subject + body (where engineers "
            "explain the WHY)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": f"One of the {len(INDEXED_REPOS)} indexed repos."},
                "path": {"type": "string", "description": "File path relative to repo root (from a search_code hit)."},
                "limit": {"type": "integer", "description": "Max commits to return (1-30). Default 10."},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "grep_repo",
        "description": (
            "Exact-string regex search (grep -E) over a SINGLE indexed repo's clone. "
            "Use when you already know WHICH repo holds the string. If you don't, "
            "use grep_all_repos instead (cross-repo). "
            "Deterministic exact-string find: HTTP path, class/function/enum name, "
            "error message, env var, OpenAPI operationId, config key. Returns up "
            "to `limit` matches with file:line."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": f"One of: {', '.join(INDEXED_REPOS)}."},
                "pattern": {"type": "string", "description": "Regex (grep -E flavor). Escape special chars."},
                "file_glob": {"type": "string", "description": "Optional. Restrict to filenames matching this glob (e.g. '*.yaml', '*.kt')."},
                "limit": {"type": "integer", "description": "Max matches to return (1-200). Default 50."},
            },
            "required": ["repo", "pattern"],
        },
    },
    {
        "name": "lookup_symbol",
        "description": (
            "Find chunks that DECLARE a given symbol name (class, function, const, etc). "
            "Deterministic filter on the chunk's `symbols` payload array — returns "
            "the chunks where the name is actually defined, not chunks that mention it. "
            "USE BEFORE search_code when you have an exact identifier "
            "(e.g. `lookup_symbol(\"PaymentsController\")`, `lookup_symbol(\"useVarunaPayment\")`). "
            "Optional `repo` filter narrows to one repo. Returns per-hit "
            "repo/path/lines + commit-pinned permalink."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact symbol identifier (case-sensitive)."},
                "repo": {"type": "string", "description": "Optional. Restrict to one indexed repo."},
                "k": {"type": "integer", "description": "Max hits to return (1-50). Default 10."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "why_was_this_changed",
        "description": (
            "Narrate why a specific file (or file:line) is the way it is. "
            "Combines `git blame` (commit that last touched that line), the PR that introduced it, "
            "linked Jira tickets, and reviewer comments into one structured response. "
            "Use when the user asks \"why is this code like this?\", \"who decided X?\", \"what PR added this?\", "
            "or \"is there a ticket for this?\". Returns commit + PR + jira keys + review_comments + commit-pinned permalink."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Indexed repo name."},
                "path": {"type": "string", "description": "File path relative to repo root."},
                "line": {"type": "integer", "description": "Optional. 1-indexed line for git blame."},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "impact_analysis",
        "description": (
            "Trace the downstream impact of changing a file or symbol. Returns where "
            "it's declared, which repos reference it, how many references per repo, related test files, "
            "and any matching service registry entry. Use when the user asks "
            "\"what breaks if I change X?\", \"who depends on Y?\", \"where is Z used?\", or "
            "\"do we have tests for this?\". Target can be a symbol (`PaymentsController`, `useVarunaPayment`) or a file path "
            "(`bff-core/.../PaymentsController.kt`)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Symbol name or file path."},
                "repo": {"type": "string", "description": "Optional. Restrict declaration lookup to one repo."},
            },
            "required": ["target"],
        },
    },
    {
        "name": "parse_stacktrace",
        "description": (
            "Parse a stacktrace (Kotlin/Java JVM, JS/Node V8, Python) into structured "
            "frames: {file, line, function, language, raw_line}. Returns deepest-first "
            "(top frame = actual failure site). USE FIRST when the user pastes any stacktrace, "
            "exception trace, or error output. Then call lookup_symbol or read_file on the top "
            "frame to investigate. Optionally call why_was_this_changed on the same file:line."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stacktrace": {"type": "string", "description": "Raw stacktrace text from logs / paste."},
            },
            "required": ["stacktrace"],
        },
    },
    {
        "name": "write_test_for",
        "description": (
            "Draft test cases for a function or file. Pulls the source under test + 1-2 existing "
            "tests from the same repo as style examples, calls Haiku to generate covering happy "
            "path + edge cases + errors. Returns markdown the user can copy. USE when user asks "
            "\"write a test for X\" or \"can you cover Y with tests?\""
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Indexed repo name."},
                "file_path": {"type": "string", "description": "Path to file under test (relative to repo root)."},
                "function": {"type": "string", "description": "Optional. Specific function name to focus on."},
            },
            "required": ["repo", "file_path"],
        },
    },
    {
        "name": "service_tour",
        "description": (
            "15-min walkthrough of a service or repo for a new engineer. Returns CLAUDE.md "
            "excerpt, "
            "entry points (controllers / main classes / App.tsx), top directories with file counts, "
            "and the last 5 merged non-bot PRs. USE when user asks \"tour me through X\", \"I'm new to "
            "service Y, where do I start?\", or \"give me a walkthrough of Z\". Render the response as "
            "a walking tour, not a data dump."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Indexed repo name."},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "lookup_service",
        "description": (
            "Look up a Jupiter internal microservice in the pre-built service registry. "
            "Returns K8s in-cluster URL (svc.cluster.local), Route53 cross-cluster URL "
            "(.<account>.internal), namespace, port, consumer repos, source repo, "
            "OpenAPI spec file, exposed HTTP paths, and a sample Feign-client file. "
            "USE THIS FIRST for 'where is service X deployed', 'what's the URL for Y', "
            "'what endpoints does Z expose', 'who calls W' — sub-second deterministic "
            "answer vs 30s+ of grep_all_repos. The registry indexes 200+ services "
            "auto-discovered from consumer application.yml + manual cross-cluster "
            "overrides confirmed by platform-eng. Accepts canonical name (e.g. "
            "'bullet-ms'), name-without-suffix ('bullet'), acronyms ('llm'), or "
            "human names ('Loan Lifecycle Manager'). If miss, returns helpful hint "
            "and you can fall back to grep_all_repos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name_or_alias": {"type": "string", "description": "Service name or alias (e.g. 'bullet-ms', 'llm', 'Loan Lifecycle Manager')."},
            },
            "required": ["name_or_alias"],
        },
    },
    {
        "name": "grep_all_repos",
        "description": (
            "CROSS-REPO regex grep across ALL ~240 indexed repos in one call. "
            "Use when you DON'T know which repo holds the string. CRITICAL for "
            "service-discovery: a K8s DNS like 'deposit-manager-ms.cbs.svc.cluster.local' "
            "is hardcoded in consumer config files but you don't know WHICH consumer — "
            "this tool finds it in one shot. Also for: 'where is class X referenced', "
            "'which repos call endpoint Y', 'who consumes service Z'. Returns matches "
            "with repo+path+line plus an aggregate `repos_with_hits` count. ALWAYS "
            "pair with a tight pattern + file_glob — broad patterns like 'user' will "
            "match millions of lines."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex (grep -E flavor). Make it SPECIFIC — e.g. 'deposit-manager-ms' not 'deposit'."},
                "file_glob": {"type": "string", "description": "STRONGLY recommended. Restrict to filenames (e.g. '*.yml' for service configs, '*.kt' for Kotlin clients)."},
                "limit": {"type": "integer", "description": "Max matches (1-200). Default 50."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_repo_files",
        "description": (
            "List file paths in a repo matching a glob pattern. Use to discover related files "
            "(e.g. '**/auth/**/*.kt')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": f"One of: {', '.join(INDEXED_REPOS)}."},
                "glob": {"type": "string", "description": "Glob pattern. Default '**/*'."},
                "limit": {"type": "integer", "description": "Max entries (1-200). Default 100."},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "get_capabilities",
        "description": (
            "Return Jarvis's own capability manifest — the canonical, current list of "
            "what Jarvis can and can't do. Use this WHENEVER the user asks a self-referential "
            "question about Jarvis: 'what can you do', 'are you good at X', 'do you have a Y "
            "feature', 'how does code review work', 'can I review a PR with you', etc. "
            "ALWAYS prefer this over freelancing from prompt knowledge — the manifest is "
            "the single source of truth and is updated on every feature ship. Optionally "
            "filter by category (qa | code-review | code-edit | docs | meta | infra) or by "
            "search-substring."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Optional. Filter to one category: qa, code-review, code-edit, docs, meta, infra.",
                },
                "search": {
                    "type": "string",
                    "description": "Optional. Substring match on name/command/summary (case-insensitive).",
                },
            },
        },
    },
    {
        "name": "janus_user_journey",
        "description": (
            "Fetch a Jupiter user's recent Amplitude event stream via Janus. "
            "Use for 'why did user X end up on screen Y', 'how did user Z bypass step W', "
            "'what triggered this state transition' — questions where logs+code cannot answer "
            "because the cause is a product-level event (deeplink, push notification, A/B variant, "
            "campaign, screen navigation). Returns chronological events + session summary. "
            "Defaults to a tight 24h window; pass event_filter for a clean trace. Max 168h (7d). "
            "Timestamps are UTC — convert to IST (+5:30) when surfacing to engineers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Jupiter customer uuid (user-vault userId form)."
                },
                "lookback_hours": {
                    "type": "integer",
                    "description": "Trailing window in hours. Default 24, max 168 (7d). Anything over 168 returns invalid_request."
                },
                "event_filter": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of Amplitude event_type names to narrow the result (e.g. ['Deeplink Opened', 'Screen Viewed', 'Push Notification Clicked']). When absent, returns all events."
                },
                "max_events": {
                    "type": "integer",
                    "description": "Default 200, max 1000."
                },
                "caller_id": {
                    "type": "string",
                    "description": "Engineer's id for per-caller audit (e.g. slack:U06BN5VADTN). Pass through whatever is available."
                }
            },
            "required": ["user_id"]
        }
    },
    {
        "name": "janus_event_count",
        "description": (
            "Fleet-wide count for ONE Amplitude event over a trailing window via Janus. "
            "Use for 'how many users did X in the last N days' questions — simple counts, "
            "not multi-step funnels. Optional single-property filter + optional group_by "
            "breakdown. Returns {unique_users, total_events, daily[], top_values[], audit_ref}. "
            "filter_property is an EVENT property; for built-in dimensions like platform/country/version, "
            "use group_by instead. For multi-step funnels / retention / rich multi-dimension breakdowns, "
            "deflect to the specific named Amplitude dashboard."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "description": "Amplitude event_type name (exact, e.g. 'bottom-sheet-viewed')."
                },
                "lookback_days": {
                    "type": "integer",
                    "description": "Trailing window in days (daily granularity). Default 7, max 90."
                },
                "filter_property": {
                    "type": "string",
                    "description": "Optional event property to filter on (paired with filter_value)."
                },
                "filter_value": {
                    "type": "string",
                    "description": "Value for filter_property."
                },
                "group_by": {
                    "type": "string",
                    "description": "Optional property to break the count down by (returned as top_values[])."
                },
                "caller_id": {
                    "type": "string",
                    "description": "Engineer's id for per-caller audit (e.g. slack:U06BN5VADTN)."
                }
            },
            "required": ["event_type"]
        }
    },
]


def get_capabilities(category: str | None = None, search: str | None = None) -> str:
    """Return Jarvis's capability manifest as JSON. The single source of truth for self-referential answers."""
    from . import capabilities as _cap
    items = _cap.CAPABILITIES
    if category:
        items = [c for c in items if c.get("category") == category]
    if search:
        s = search.lower()
        items = [c for c in items
                 if s in c.get("name", "").lower()
                 or s in c.get("command", "").lower()
                 or s in c.get("summary", "").lower()]
    return json.dumps({"capabilities": items, "count": len(items),
                        "all_categories": _cap.categories()}, indent=2)


TOOL_DISPATCH = {
    "search_multi": search_multi,
    "search_code": search_code,
    "search_code_vector": search_code_vector,
    "search_code_reranked": search_code_reranked,
    "search_prs": search_prs,
    "grep_repo": grep_repo,
    "grep_all_repos": grep_all_repos,
    "lookup_symbol": lookup_symbol,
    "why_was_this_changed": why_was_this_changed,
    "impact_analysis": impact_analysis,
    "parse_stacktrace": parse_stacktrace,
    "write_test_for": write_test_for,
    "service_tour": service_tour,
    "lookup_service": lookup_service,
    "read_file": read_file,
    "list_repo_files": list_repo_files,
    "git_history": git_history,
    "get_capabilities": get_capabilities,
    "janus_user_journey": janus_user_journey,
    "janus_event_count": janus_event_count,
}


def run_tool(name: str, args: dict) -> str:
    fn = TOOL_DISPATCH.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        return fn(**args)
    except TypeError as e:
        return json.dumps({"error": f"bad args for {name}: {e}"})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
