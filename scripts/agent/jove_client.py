"""Jove MCP client — Jarvis's bridge to the Jove product-expert agent.

Single source of truth for talking to Jove. Used by the `/jarvis ask <space>:`
Slack handler and by the streaming API's direct Confluence-page handoff.
Shared bearer auth via JOVE_MCP_TOKEN.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from typing import Any

from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession

log = logging.getLogger("jarvis.jove_client")

JOVE_MCP_URL = os.environ.get("JOVE_MCP_URL", "http://localhost:8010/mcp/")
JOVE_MCP_TOKEN = os.environ.get("JOVE_MCP_TOKEN", "")

# Module-level cache of the spaces list. Refreshed at bot startup + on demand.
_SPACES_CACHE: dict[str, dict[str, Any]] | None = None
_SPACES_CACHE_TS: float = 0.0
_SPACES_TTL_SEC = 3600  # 1h


async def _call_tool(tool_name: str, arguments: dict[str, Any], timeout_sec: float = 90):
    """Open an MCP session, call one tool, return parsed JSON content."""
    if not JOVE_MCP_TOKEN:
        raise RuntimeError("JOVE_MCP_TOKEN not set in env")
    headers = {"Authorization": f"Bearer {JOVE_MCP_TOKEN}"}
    async with streamablehttp_client(JOVE_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=15)
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments),
                timeout=timeout_sec,
            )
            if not result.content:
                return None
            block = result.content[0]
            text = getattr(block, "text", str(block))
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw_text": text}


# ── Public sync wrappers (Slack handler runs in threads) ───────────────────

def read_confluence_page(url: str, timeout_sec: float = 30) -> dict[str, Any]:
    """Read one caller-provided Confluence page through Jove's live-read MCP tool."""
    url = url.strip()
    if not url:
        raise ValueError("Confluence page URL is required")
    result = asyncio.run(_call_tool(
        "jove_read_confluence_page",
        {"url": url},
        timeout_sec=timeout_sec,
    ))
    if not isinstance(result, dict):
        raise RuntimeError("Jove returned an invalid Confluence page response")
    return result

def list_spaces(force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Return {space_key → {space_name, page_count, latest_indexed_at, ...}}.

    Cached 1h. Pass force_refresh=True to bust.
    """
    global _SPACES_CACHE, _SPACES_CACHE_TS
    now = time.time()
    if (
        not force_refresh
        and _SPACES_CACHE is not None
        and (now - _SPACES_CACHE_TS) < _SPACES_TTL_SEC
    ):
        return _SPACES_CACHE
    try:
        resp = asyncio.run(_call_tool("jove_list_confluence_spaces", {}))
    except Exception as e:
        log.exception("jove list_spaces failed")
        if _SPACES_CACHE is not None:
            log.warning("returning stale spaces cache (%d entries)", len(_SPACES_CACHE))
            return _SPACES_CACHE
        raise
    spaces = (resp or {}).get("spaces", []) if isinstance(resp, dict) else []
    cache = {s["space_key"]: s for s in spaces if "space_key" in s}
    _SPACES_CACHE = cache
    _SPACES_CACHE_TS = now
    return cache


def is_known_space(space_key: str) -> bool:
    """Case-insensitive check that space_key is in Jove's index."""
    spaces = list_spaces()
    return any(k.lower() == space_key.lower() for k in spaces)


def resolve_space_key(input_key: str) -> str | None:
    """Return the canonical space_key matching input_key (case-insensitive), or None.

    Tries (in priority order):
        1. Exact case-insensitive match on space_key   (`tech` → TECH)
        2. Exact case-insensitive match on space_name  (`Technology` → TECH)
        3. Unique prefix match on space_name           (`data` → DS for `Data Science`)

    Returns None if no match OR multiple candidates (caller can use resolve_space_candidates).
    """
    inp = input_key.strip().lower()
    spaces = list_spaces()
    for k in spaces:
        if k.lower() == inp:
            return k
    for k, s in spaces.items():
        if (s.get("space_name") or "").lower() == inp:
            return k
    prefix_matches = [
        k for k, s in spaces.items()
        if (s.get("space_name") or "").lower().startswith(inp)
    ]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    return None


def resolve_space_candidates(input_key: str) -> list[str]:
    """Return all space_keys whose key OR name matches input_key (substring, case-insensitive).
    Used to show disambiguation when resolve_space_key returns None."""
    inp = input_key.strip().lower()
    spaces = list_spaces()
    matches = [
        k for k, s in spaces.items()
        if inp in k.lower() or inp in (s.get("space_name") or "").lower()
    ]
    # De-prioritise long-tail spaces; sort by page count descending
    return sorted(matches, key=lambda k: -(spaces[k].get("page_count") or 0))


def space_label(space_key: str) -> str:
    """Return 'Technology (TECH)' or just the key if name unknown."""
    spaces = list_spaces()
    s = spaces.get(space_key)
    if s and s.get("space_name") and s["space_name"].lower() != space_key.lower():
        return f"{s['space_name']} ({space_key})"
    return space_key


def estimated_refresh_seconds(space_key: str, sec_per_page: float = 1.0) -> int:
    """Estimate refresh wall-time from cached page_count. Per Jove team: ~1s/page."""
    spaces = list_spaces()
    s = spaces.get(space_key)
    pages = (s or {}).get("page_count") or 0
    return int(pages * sec_per_page)


def refresh_space(space_key: str) -> dict[str, Any]:
    """Trigger Jove to re-pull a single space. Returns {run_id, status, mode, space_key}.

    Status values: 'started' | 'already_running'
    """
    return asyncio.run(_call_tool("jove_refresh_confluence_space",
                                    {"space_key": space_key}, timeout_sec=15))


def get_run_status(run_id: str) -> dict[str, Any]:
    """Poll the status of a refresh run.

    Returns: {status, pages_discovered, pages_crawled, pages_indexed,
              pages_skipped, pages_failed, started_at, finished_at}
    Status values: 'running' | 'completed' | 'failed'
    """
    return asyncio.run(_call_tool("jove_get_index_run_status",
                                    {"run_id": run_id}, timeout_sec=15))


def suggest_spaces(input_key: str, n: int = 5) -> list[str]:
    """Suggest the n closest space_keys by case-insensitive substring match,
    falling back to top-N by page_count."""
    spaces = list_spaces()
    inp = input_key.lower()
    # First: any space whose key OR name contains the input substring
    matches = [
        k for k, s in spaces.items()
        if inp in k.lower() or inp in (s.get("space_name") or "").lower()
    ]
    if matches:
        return sorted(matches, key=lambda k: -(spaces[k].get("page_count") or 0))[:n]
    # Otherwise: top N by page_count
    return sorted(spaces.keys(), key=lambda k: -(spaces[k].get("page_count") or 0))[:n]


import re as _re

# Common LLM agent-step transition phrases that Jove concatenates without separators.
# Each pattern matches: <sentence-ending punctuation><no whitespace><transition phrase>
# and inserts \n\n between them. Conservative — only fires when there's clearly a
# missing break (punctuation immediately followed by a capitalised transition word).
_TRANSITION_PHRASES = [
    "I'll ", "Let me ", "Now ", "Perfect", "Great", "Excellent", "Got it",
    "OK ", "Okay", "Alright", "Here's ", "Here is ", "Looking at ",
    "Based on ", "After ", "Done", "Done!", "Found",
]
_NORMALIZE_RE = _re.compile(
    r"([\.\!\?\)])(" + "|".join(_re.escape(p) for p in _TRANSITION_PHRASES) + r")"
)


def normalize_jove_response(text: str) -> str:
    """Post-process Jove's chat response: insert \\n\\n at agent-step boundaries.

    Jove's chat endpoint concatenates the agent's intermediate "I'll search…" /
    "Perfect, I found…" texts with the final answer without preserving line
    breaks between steps. This function re-inserts paragraph breaks at common
    LLM transition phrases when they're stuck immediately after sentence-ending
    punctuation. Idempotent — running twice has no extra effect.

    Pattern-based, so it's an approximation. The proper fix is server-side in
    Jove (insert separators when concatenating chunks). Until that ships, this
    keeps Slack output readable.
    """
    if not text:
        return text
    # Apply the regex once
    return _NORMALIZE_RE.sub(r"\1\n\n\2", text)


REFRESH_HINT = (
    "\n\nIMPORTANT: the user has requested a *refreshed* answer because they "
    "suspect Jove's index is stale relative to the latest Confluence updates. "
    "If your latest_indexed_at is older than the source page's latest_modified, "
    "please re-fetch the page via the live Confluence API before answering. "
    "If you cannot re-fetch, say so honestly and report your latest_indexed_at."
)


def ask(query: str, user_id_email: str, confluence_space: str,
        session_id: str | None = None,
        refresh: bool = False,
        timeout_sec: float = 120) -> dict[str, Any]:
    """Call jove_chat with skill='research' scoped to `confluence_space`.

    Args:
        refresh: if True, busts Jarvis's space-cache AND appends a refresh hint
            to the query so Jove knows to re-fetch live Confluence content if
            its index is older than the source's latest_modified.

    Returns: {response, session_id, skill, ... (whatever Jove returns)}
    """
    if refresh:
        # Bust our local space cache (in case the schema/inventory changed)
        list_spaces(force_refresh=True)
        # And hint Jove that the user wants fresh data
        query = query + REFRESH_HINT
    args: dict[str, Any] = {
        "skill": "research",
        "query": query,
        "user_id": user_id_email,
        "confluence_space": confluence_space,
    }
    if session_id:
        args["session_id"] = session_id
    result = asyncio.run(_call_tool("jove_chat", args, timeout_sec=timeout_sec))
    # Post-process the response field for readability (insert paragraph breaks
    # at agent-step boundaries that Jove concatenates without separators)
    if isinstance(result, dict) and isinstance(result.get("response"), str):
        result["response"] = normalize_jove_response(result["response"])
    return result
