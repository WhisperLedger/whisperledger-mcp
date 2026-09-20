"""Prior-match — Layer 1b of grounding.

For general-route questions (after the router classifier returns `general`),
check if the SAME ASKER has asked a semantically-similar question in the
last 30 days that we can surface instead of re-running the full Sonnet loop.

Same-asker scoping (default per docs/grounding.md):
  - Reduces false-positive risk (people ask similar things in different intents)
  - Avoids cross-user information leakage (someone debugging user X gets back
    another engineer's prior answer about user Y)

Similarity threshold: ≥ 0.85 cosine on voyage-code-3 embeddings.

Freshness guard (Layer 3 lite, since full Layer 3 isn't built yet):
  - Parse the cached answer for file:line citations like `<repo>/<path>:NN`
  - Verify at least one cited file STILL EXISTS in `~/jarvis/repos/<repo>/<path>`
  - If none cited OR none exist → fall through to Sonnet (be conservative)
  - Caller renders a disclosure header so user knows it's a cached answer

Defensive defaults:
  - First-time user (no history) → fall through
  - Question too short → fall through
  - Any exception → fall through silently
  - Embedding service down → fall through silently
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from . import freshness_check as _fc

logger = logging.getLogger("jarvis.agent.prior_match")

QA_LOG = Path("/home/ubuntu/jarvis/logs/qa_log.jsonl")
EMB_CACHE = Path("/home/ubuntu/jarvis/state/qa_embeddings.jsonl")
AUDIT_LOG = Path("/home/ubuntu/jarvis/logs/prior_match.jsonl")
REPOS_ROOT = Path("/home/ubuntu/jarvis/repos")

DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_SIM_THRESHOLD = 0.85
MIN_QUESTION_LEN = 12
MAX_HISTORY_PER_USER = 200   # cap embedding cost on prolific askers
DISABLED_ENV = "JARVIS_DISABLE_PRIOR_MATCH"

# In-process embedding cache (qid → vector). Loaded from disk on first use,
# persisted on each new embed. Survives restart.
_EMB_CACHE: dict[str, list[float]] | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _audit(record: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("prior_match audit log write failed")


def _load_cache() -> dict[str, list[float]]:
    global _EMB_CACHE
    if _EMB_CACHE is not None:
        return _EMB_CACHE
    _EMB_CACHE = {}
    if EMB_CACHE.exists():
        try:
            with EMB_CACHE.open() as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        qid = rec.get("qid")
                        vec = rec.get("vec")
                        if qid and isinstance(vec, list):
                            _EMB_CACHE[qid] = vec
                    except Exception:
                        continue
        except Exception:
            logger.exception("failed to load embedding cache; starting fresh")
            _EMB_CACHE = {}
    return _EMB_CACHE


def _save_cache_entry(qid: str, vec: list[float]) -> None:
    cache = _load_cache()
    cache[qid] = vec
    try:
        EMB_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with EMB_CACHE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"qid": qid, "vec": vec}) + "\n")
    except Exception:
        logger.exception("failed to persist embedding cache entry")


def _embed_one(text: str) -> list[float] | None:
    """Embed via voyage-code-3 (same provider as the code corpus). Returns
    None on any failure (network, auth, rate-limit)."""
    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        return None
    try:
        import voyageai
        client = voyageai.Client(api_key=api_key)
        resp = client.embed([text[:4000]], model="voyage-code-3", input_type="query")
        return list(resp.embeddings[0])
    except Exception:
        logger.exception("voyage embed failed for prior_match")
        return None


def _embed_batch(texts: list[str]) -> list[list[float] | None]:
    """Batch embed. Returns one vector per input (None for failures)."""
    if not texts:
        return []
    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        return [None] * len(texts)
    try:
        import voyageai
        client = voyageai.Client(api_key=api_key)
        # voyage allows up to 128 per call; we cap at MAX_HISTORY_PER_USER above
        clipped = [t[:4000] for t in texts]
        resp = client.embed(clipped, model="voyage-code-3", input_type="document")
        return [list(v) for v in resp.embeddings]
    except Exception:
        logger.exception("voyage batch embed failed for prior_match")
        return [None] * len(texts)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


_TS_FIELDS = ("ts",)


def _parse_ts(rec: dict) -> datetime | None:
    for f in _TS_FIELDS:
        v = rec.get(f)
        if not v:
            continue
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception:
            continue
    return None


def _load_caller_history(
    caller_id: str, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[dict]:
    """Return same-asker qa_log entries within the lookback window.
    Newest-first, capped at MAX_HISTORY_PER_USER."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    hits: list[dict] = []
    if not QA_LOG.exists():
        return hits
    try:
        with QA_LOG.open() as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("user_id") != caller_id:
                    continue
                ts = _parse_ts(e)
                if not ts or ts < cutoff:
                    continue
                q = (e.get("q") or "").strip()
                if len(q) < MIN_QUESTION_LEN:
                    continue
                # Skip the routed-to-autosupport entries — their "answer" is just
                # a forwarding stub, not a real cacheable answer
                if e.get("routed_to") == "autosupport_investigate":
                    continue
                # Skip empty / max-iter / failed entries
                ans = (e.get("answer") or "").strip()
                if not ans or "max iterations" in ans.lower() or "(stopped" in ans.lower():
                    continue
                hits.append(e)
    except Exception:
        logger.exception("failed to scan qa_log for caller history")
        return []
    hits.sort(key=lambda e: _parse_ts(e) or datetime.min.replace(tzinfo=timezone.utc),
              reverse=True)
    return hits[:MAX_HISTORY_PER_USER]


# Citation patterns Jarvis emits — `<repo>/<path>:start-end` or `<repo>/<path>:line`
# Conservatively keep alnum + ._- in repo/path tokens
_CITATION_RE = re.compile(
    r"`?([\w.-]+)/([\w./\-]+\.(?:kt|kts|java|scala|ts|tsx|js|jsx|py|yml|yaml|json|sql|md))(?::\d+(?:-\d+)?)?`?"
)


def _extract_cited_files(answer: str) -> list[tuple[str, str]]:
    """Return [(repo, path)] pairs cited in the answer. De-duplicated."""
    seen = set()
    out: list[tuple[str, str]] = []
    for m in _CITATION_RE.finditer(answer or ""):
        pair = (m.group(1), m.group(2))
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


def _freshness_check(answer: str) -> tuple[bool, str]:
    """Layer 3 — comprehensive freshness check via freshness_check module.

    Delegates to freshness_check.check_answer_freshness which verifies:
      - cited files still exist on disk
      - cited services still resolve in the service registry
      - cited symbols still have declarations in the symbol index

    Cheap-first early-exit on any stale signal.
    Returns (is_fresh, joined-reason-string) — joined for the existing audit log shape.

    Defensive: if the new module raises for any reason, fall back to the
    file-only check that's been in production since Layer 1b.
    """
    try:
        is_fresh, reasons = _fc.check_answer_freshness(answer)
        joined = "; ".join(reasons) if reasons else "(no detail)"
        return (is_fresh, joined)
    except Exception:
        logger.exception("freshness_check module raised — falling back to file-only check")
        pairs = _extract_cited_files(answer)
        if not pairs:
            return (False, "fallback: no file citations to verify")
        existing = sum(1 for repo, path in pairs[:10]
                       if (REPOS_ROOT / repo / path).is_file())
        if existing == 0:
            return (False, f"fallback: all {len(pairs)} cited files no longer exist")
        return (True, f"fallback: {existing}/{len(pairs)} cited files still present")


def find_prior_match(
    question: str,
    caller_id: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    similarity_threshold: float = DEFAULT_SIM_THRESHOLD,
) -> dict[str, Any]:
    """Look for a semantically-similar prior question from the same asker.

    Returns:
      {
        matched: bool,
        qa_log_entry: dict | None,   # the matching prior entry
        similarity: float,           # best cosine similarity found
        freshness_ok: bool,
        reason: str,
        elapsed_sec: float,
      }
    """
    t0 = time.time()
    fallback = {
        "matched": False, "qa_log_entry": None, "similarity": 0.0,
        "freshness_ok": False, "reason": "", "elapsed_sec": 0.0,
    }

    if os.environ.get(DISABLED_ENV) == "1":
        fallback["reason"] = "disabled via env"
        return fallback
    if not caller_id:
        fallback["reason"] = "no caller_id"
        return fallback
    if not question or len(question.strip()) < MIN_QUESTION_LEN:
        fallback["reason"] = "question too short"
        return fallback

    history = _load_caller_history(caller_id, lookback_days)
    if not history:
        fallback["reason"] = "no prior history for this caller"
        return fallback

    # Make sure all history entries have embeddings cached
    cache = _load_cache()
    to_embed_qids: list[str] = []
    to_embed_questions: list[str] = []
    for e in history:
        qid = e.get("qid")
        if not qid:
            continue
        if qid not in cache:
            to_embed_qids.append(qid)
            to_embed_questions.append((e.get("q") or "").strip())
    if to_embed_questions:
        vecs = _embed_batch(to_embed_questions)
        for qid, vec in zip(to_embed_qids, vecs):
            if vec:
                _save_cache_entry(qid, vec)

    # Embed the current question
    qvec = _embed_one(question)
    if not qvec:
        fallback["reason"] = "embed failure on current question"
        fallback["elapsed_sec"] = round(time.time() - t0, 3)
        return fallback

    # Cosine vs each historical entry
    best_sim = 0.0
    best_entry: dict | None = None
    for e in history:
        qid = e.get("qid")
        if not qid:
            continue
        hvec = cache.get(qid)
        if not hvec:
            continue
        sim = _cosine(qvec, hvec)
        if sim > best_sim:
            best_sim = sim
            best_entry = e

    if not best_entry or best_sim < similarity_threshold:
        fallback["similarity"] = best_sim
        fallback["reason"] = f"best similarity {best_sim:.3f} < threshold {similarity_threshold}"
        fallback["elapsed_sec"] = round(time.time() - t0, 3)
        _audit({
            "ts": _now_iso(), "caller_id": caller_id, "q_preview": question[:200],
            "decision": "no_match", "best_similarity": round(best_sim, 4),
            "history_size": len(history), "elapsed_sec": fallback["elapsed_sec"],
        })
        return fallback

    # Freshness check
    fresh, fresh_reason = _freshness_check(best_entry.get("answer") or "")
    elapsed = round(time.time() - t0, 3)

    decision = {
        "matched": fresh,
        "qa_log_entry": best_entry if fresh else None,
        "similarity": best_sim,
        "freshness_ok": fresh,
        "reason": fresh_reason if fresh else f"matched but stale: {fresh_reason}",
        "elapsed_sec": elapsed,
    }
    _audit({
        "ts": _now_iso(), "caller_id": caller_id, "q_preview": question[:200],
        "decision": "match" if fresh else "stale_match",
        "matched_qid": best_entry.get("qid"),
        "matched_ts": best_entry.get("ts"),
        "similarity": round(best_sim, 4),
        "freshness_reason": fresh_reason,
        "history_size": len(history),
        "elapsed_sec": elapsed,
    })
    return decision


def format_match_for_user(decision: dict, question: str) -> str | None:
    """Render the matched prior answer with a transparent disclosure header.
    Returns None if not actually matched."""
    if not decision.get("matched"):
        return None
    entry = decision.get("qa_log_entry") or {}
    matched_ts = entry.get("ts", "?")
    qid = entry.get("qid", "?")
    sim = decision.get("similarity") or 0.0
    answer = entry.get("answer") or ""
    # Pretty matched_ts: 2026-06-22T07:14:01Z → 2026-06-22
    matched_date = matched_ts.split("T")[0] if "T" in str(matched_ts) else str(matched_ts)
    header = (
        f"_:repeat: Surfaced from a similar prior question you asked on *{matched_date}* "
        f"(similarity {sim:.2f}, qid `{qid}`, freshness check passed). "
        f"Reply with `-new` to force a fresh investigation._\n\n---\n\n"
    )
    return header + answer
