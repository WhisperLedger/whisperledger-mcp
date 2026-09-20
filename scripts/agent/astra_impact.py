"""Impact analysis tool — given a file or symbol, find downstream consumers,
contract changes, and missing test coverage.

Composes existing tools:
- lookup_symbol(name) → where is X declared?
- grep_all_repos(name) → who references X cross-repo?
- lookup_service(name) → if X looks like a service, get consumers
- search_code(query about tests) → coverage check

Returns a structured analysis the agent can render as markdown.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from agent.config import ROOT_DIR

REPOS_DIR = ROOT_DIR / "repos"


def impact_analysis(target: str, repo: str | None = None) -> str:
    """Trace impact of changing `target` (file path OR symbol name).

    Returns JSON with: target_type, declarations, downstream_consumers,
    cross_repo_refs, tests_found, service_links.
    """
    target = target.strip()
    is_file = "/" in target or "." in target.rsplit("/", 1)[-1] and target.endswith(
        (".kt", ".kts", ".ts", ".tsx", ".java", ".py", ".scala", ".js", ".jsx")
    )
    target_type = "file" if is_file else "symbol"

    declarations: list[dict] = []
    consumers: list[dict] = []
    cross_repo: list[dict] = []
    tests_found: list[dict] = []
    service_links: list[dict] = []

    # 1. If it looks like a symbol, find where it's declared via lookup_symbol.
    if target_type == "symbol":
        try:
            from . import tools
            r = json.loads(tools.lookup_symbol(target, repo=repo, k=5))
            for h in r.get("hits", []):
                declarations.append({
                    "repo": h.get("repo"), "path": h.get("path"),
                    "lines": f"{h.get('start_line')}-{h.get('end_line')}",
                    "permalink": h.get("permalink"),
                })
        except Exception:
            pass

    # 2. Find cross-repo references via grep_all_repos.
    try:
        from . import tools as agent_tools
        # Use grep_all_repos for symbol; for file path, search for the file name.
        search_term = target if target_type == "symbol" else Path(target).name
        gr = json.loads(agent_tools.grep_all_repos(pattern=re.escape(search_term),
                                                    file_glob=None, limit=40))
        for hit in gr.get("hits", [])[:40]:
            cross_repo.append({
                "repo": hit.get("repo"),
                "path": hit.get("path"),
                "line": hit.get("line"),
                "context": (hit.get("context") or "")[:200],
            })
        # Bucket cross-repo refs by repo so the agent can summarize "used in N repos".
        from collections import Counter
        repo_counts = Counter(c["repo"] for c in cross_repo if c.get("repo"))
        consumers = [{"repo": r, "ref_count": n}
                     for r, n in repo_counts.most_common(15)]
    except Exception:
        pass

    # 3. Test coverage check — heuristic search for files that test the target.
    test_terms: list[str] = []
    if target_type == "symbol":
        test_terms.append(target)
    elif target_type == "file":
        base = Path(target).stem
        test_terms.append(base)

    for term in test_terms[:2]:
        try:
            tests_search = json.loads(agent_tools.grep_all_repos(
                pattern=re.escape(term),
                file_glob="*Test*",
                limit=10,
            ))
            for hit in tests_search.get("hits", [])[:8]:
                tests_found.append({
                    "repo": hit.get("repo"),
                    "path": hit.get("path"),
                    "line": hit.get("line"),
                })
        except Exception:
            pass

    # 4. Service link — is the target name a known service?
    try:
        svc = json.loads(agent_tools.lookup_service(target))
        if not svc.get("error"):
            service_links.append(svc)
    except Exception:
        pass

    return json.dumps({
        "target": target,
        "target_type": target_type,
        "declarations": declarations,
        "consumer_repos": consumers,
        "cross_repo_refs_sample": cross_repo[:20],
        "tests_found_sample": tests_found[:10],
        "service_links": service_links,
        "summary_hint": (
            "Use this when the user asks 'what does X break?', 'where is Y used?', "
            "'who depends on Z?'. For confident downstream-impact reporting, "
            "cite the declarations + consumer_repos counts; recommend running "
            "the tests_found before merging."
        ),
    }, ensure_ascii=False)
