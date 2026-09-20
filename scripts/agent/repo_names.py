"""Local repo id → (GitHub org, upstream repo name) mapping.

Most indexed repos are jupitermoney/<name> and use the bare upstream name as
their local id (e.g. 'bff-core', 'jupiter'). External-org repos are namespaced
with '<org>-' at index time to avoid folder / Qdrant repo-id collisions with
same-named jupitermoney repos (e.g. jupitermoney/gatekeeper vs
wearesumhr/gatekeeper). This helper walks the mapping back so that URLs and
gh api paths always point at the correct upstream org.
"""
from __future__ import annotations
from agent.config import GITHUB_ORG

# Prefix → org. Order-independent; each prefix must be unambiguous.
EXTERNAL_ORG_PREFIXES: dict[str, str] = {
    'wearesumhr-': 'wearesumhr',
}


def resolve_gh_org(repo_local_id: str) -> tuple[str, str]:
    """Return (org, upstream_repo_name) for a local INDEXED_REPOS id.

    Bare local ids (no known prefix) default to the configured GITHUB_ORG.
    """
    if repo_local_id:
        for prefix, org in EXTERNAL_ORG_PREFIXES.items():
            if repo_local_id.startswith(prefix):
                return org, repo_local_id[len(prefix):]
    return GITHUB_ORG, repo_local_id

