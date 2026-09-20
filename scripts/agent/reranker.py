"""Haiku-based cross-encoder reranker for code search.

Takes a query + a list of vector-search hits, runs a single Haiku call with
the query and numbered chunk previews, returns the hits reordered by Haiku's
relevance judgment. Fails open: any error returns the original order so
production is never blocked by a reranker outage.

Cost: ~$0.003 per rerank (Haiku 4.5 at $1/Mtok input, ~2.5k token prompts).
Latency: ~1-2s per rerank.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("jarvis.reranker")

MODEL = "claude-haiku-4-5-20251001"
MAX_INPUT_HITS = 20  # cap the input size; more is wasted Haiku tokens
SNIPPET_CHARS = 600  # per-hit preview length

RUBRIC = """You are a code-search reranker. Given a USER QUERY and a list of CANDIDATE chunks (each tagged with an index, repo, file path, line range, and snippet), pick the chunks MOST RELEVANT to the query in order of decreasing relevance.

Judgment rules:
- Prefer chunks that DEFINE the thing the user is asking about (class, function, endpoint, state machine, etc.)
- Prefer chunks that CONTAIN the actual code the user wants vs. chunks that merely MENTION it
- For "where is X" questions, prefer the DECLARING file/chunk
- For "how does X work" questions, prefer the IMPLEMENTATION chunk
- Deprioritize CLAUDE.md / README / docs unless the query explicitly asks about documentation
- Deprioritize generated files (schemas, .d.ts only declarations) unless explicitly relevant
- A chunk with a clear signature + body usually beats a chunk with just imports or comments

Output STRICT JSON only — no prose, no markdown fences:
{"top": [idx1, idx2, idx3, ...]}

The list must contain ONLY indices from 0 to N-1 (where N is the number of candidates). Return AT MOST k indices (k is provided). The first index is the most relevant. Do NOT include duplicates."""


def rerank(query: str, hits: list[dict], k: int = 5) -> list[dict]:
    """Rerank `hits` by Haiku relevance. Returns top-k hits in new order.

    Each hit is expected to have at least: repo, path, start_line, end_line, snippet.
    Returns hits[:k] in original order if reranking fails.
    """
    if not hits:
        return []
    k = max(1, min(int(k), len(hits)))
    candidates = hits[:MAX_INPUT_HITS]

    lines = [
        f"USER QUERY: {query}",
        "",
        f"CANDIDATES (n={len(candidates)}):",
        "",
    ]
    for i, h in enumerate(candidates):
        snippet = (h.get("snippet") or "")[:SNIPPET_CHARS]
        lines.append(
            f"[{i}] {h.get('repo')}/{h.get('path')}:"
            f"{h.get('start_line')}-{h.get('end_line')}"
        )
        if h.get("symbols"):
            lines.append(f"    symbols: {h.get('symbols')[:8]}")
        lines.append(f"    snippet:")
        for ln in snippet.splitlines():
            lines.append(f"      {ln}")
        lines.append("")
    lines.append(f"Return top {k} indices, most relevant first.")

    prompt = "\n".join(lines)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        # Cache the rubric — same 500-token system block on every rerank call.
        # Saves ~$0.0001/call at 5-min TTL.
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=[{
                "type": "text",
                "text": RUBRIC,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
            text = text.strip()
        # Haiku sometimes returns the JSON object followed by prose or trailing
        # text. Locate the first {...} block and parse just that.
        import re as _re
        m = _re.search(r"\{[^{}]*\}", text, _re.DOTALL)
        if m:
            text = m.group(0)
        verdict = json.loads(text)
        order: list[int] = verdict.get("top") or []
        # Sanitize: dedupe, enforce valid range, cap to k.
        seen: set[int] = set()
        clean: list[int] = []
        for x in order:
            try:
                i = int(x)
            except Exception:
                continue
            if 0 <= i < len(candidates) and i not in seen:
                seen.add(i)
                clean.append(i)
                if len(clean) >= k:
                    break
        if not clean:
            return hits[:k]
        reranked = [candidates[i] for i in clean]
        # If Haiku returned fewer than k, top up from original order to ensure
        # the caller always gets k hits (or all hits if fewer exist).
        if len(reranked) < k:
            for h in candidates:
                if h in reranked:
                    continue
                reranked.append(h)
                if len(reranked) >= k:
                    break
        return reranked[:k]
    except Exception as e:
        logger.warning(f"reranker fell back to vector order: {type(e).__name__}: {e!s}")
        return hits[:k]
