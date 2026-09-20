"""Freshness check — Layer 3 of grounding.

Verify that a cached/grounded answer's citations still resolve in current
state before surfacing to a user.

Today (Layer-3-lite, in prior_match._freshness_check): file-exists only.
This module adds:
  - Service-still-in-registry (cheap, in-process via lookup_service)
  - Symbol-still-defined (medium, via lookup_symbol → Qdrant)
  - File-exists (cheap, fs stat) — same as before, now structured

Skipped intentionally:
  - PR state (e.g. `gh pr view 1234`). High latency (~1-2s per check),
    AND a closed/merged PR usually doesn't invalidate the prior answer
    (the description of why X was changed is still accurate). Low ROI.

Cheap-first early-exit: if ANY required check fails, return is_fresh=False
without running the more expensive checks. Cost stays sub-100ms on the
happy path.

Design philosophy: be conservative — false positives (stale → fresh) are
worse than false negatives (fresh → stale, fall through to Sonnet).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("jarvis.agent.freshness_check")

REPOS_ROOT = Path("/home/ubuntu/jarvis/repos")

# Citation patterns mined from the answer
_FILE_RE = re.compile(
    r"`?([\w.-]+)/([\w./\-]+\.(?:kt|kts|java|scala|ts|tsx|js|jsx|py|yml|yaml|json|sql|md))(?::\d+(?:-\d+)?)?`?"
)
# Lowercase-hyphenated service names (e.g. bullet-ms, deposit-platform).
# Bounded to common shapes; intentionally narrow to avoid false-positive matches.
_SERVICE_RE = re.compile(r"`([a-z][a-z0-9-]{2,40}-(?:ms|service|platform))`")
# CamelCase class/interface/enum names (≥2 capital letters, ≥6 chars)
_SYMBOL_RE = re.compile(r"`?\b([A-Z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*)\b`?")

# Max checks per category — bound the cost
MAX_FILES_CHECKED = 10
MAX_SERVICES_CHECKED = 5
MAX_SYMBOLS_CHECKED = 5
# Minimum-needed: require ≥1 of each category that's PRESENT in the answer
# to pass. If a category has no citations in the answer, it's "vacuously fresh".


def _extract_files(text: str) -> list[tuple[str, str]]:
    seen = set()
    out: list[tuple[str, str]] = []
    for m in _FILE_RE.finditer(text or ""):
        pair = (m.group(1), m.group(2))
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def _extract_services(text: str) -> list[str]:
    seen = set()
    out: list[str] = []
    for m in _SERVICE_RE.finditer(text or ""):
        s = m.group(1).lower()
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _extract_symbols(text: str) -> list[str]:
    # Filter against obvious noise (common English words that happen to be CamelCase
    # in markdown headings etc.) — only accept symbols that look like code identifiers
    # (i.e. mentioned inside backticks OR have at least 2 capital letters AND ≥6 chars)
    seen = set()
    out: list[str] = []
    for m in _SYMBOL_RE.finditer(text or ""):
        s = m.group(1)
        if len(s) < 6:
            continue
        # Reject all-caps abbreviations (e.g. SQL, JSON) — those aren't code symbols
        if s.isupper():
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _check_files(pairs: list[tuple[str, str]]) -> tuple[int, int]:
    """Return (existing_count, missing_count). Caps at MAX_FILES_CHECKED."""
    existing = 0
    missing = 0
    for repo, path in pairs[:MAX_FILES_CHECKED]:
        full = REPOS_ROOT / repo / path
        if full.is_file():
            existing += 1
        else:
            missing += 1
    return existing, missing


def _check_services(names: list[str]) -> tuple[int, int]:
    """Return (resolving_count, missing_count). Caps at MAX_SERVICES_CHECKED."""
    if not names:
        return (0, 0)
    try:
        from agent import tools as _tools
    except Exception:
        # If tools can't load (shouldn't happen), be conservative — skip rather than fail
        return (0, 0)
    resolving = 0
    missing = 0
    for name in names[:MAX_SERVICES_CHECKED]:
        try:
            import json as _json
            raw = _tools.lookup_service(name)
            data = _json.loads(raw)
            if "error" in data:
                missing += 1
            else:
                resolving += 1
        except Exception:
            missing += 1
    return resolving, missing


def _check_symbols(names: list[str]) -> tuple[int, int]:
    """Return (declaring_count, missing_count). Caps at MAX_SYMBOLS_CHECKED."""
    if not names:
        return (0, 0)
    try:
        from agent import tools as _tools
    except Exception:
        return (0, 0)
    declaring = 0
    missing = 0
    for name in names[:MAX_SYMBOLS_CHECKED]:
        try:
            import json as _json
            raw = _tools.lookup_symbol(name)
            data = _json.loads(raw)
            hits = data.get("hits") or data.get("declaring_chunks") or []
            if hits and not data.get("error"):
                declaring += 1
            else:
                missing += 1
        except Exception:
            missing += 1
    return declaring, missing


def check_answer_freshness(answer: str) -> tuple[bool, list[str]]:
    """Comprehensive freshness check on a cached answer.

    Returns (is_fresh, reasons) where reasons is a list of human-readable
    findings (always populated for audit, regardless of is_fresh).

    Algorithm (cheap-first early-exit):
      1. Extract all file / service / symbol citations
      2. If NO citations of any type → return (False, ["no citations to verify"])
         (conservative: better to re-investigate than serve a context-free answer)
      3. Check files first (cheapest). If files cited but ALL missing → False
      4. Check services next. If services cited but ALL missing → False
      5. Check symbols last. If symbols cited but ALL missing → False
      6. Otherwise → True
    """
    if not answer:
        return (False, ["empty answer"])

    files = _extract_files(answer)
    services = _extract_services(answer)
    symbols = _extract_symbols(answer)

    reasons: list[str] = []
    n_cited = len(files) + len(services) + len(symbols)
    if n_cited == 0:
        return (False, ["no citations to verify"])

    # 1. Files
    if files:
        f_existing, f_missing = _check_files(files)
        reasons.append(f"files: {f_existing}/{f_existing + f_missing} still exist")
        if f_existing == 0:
            return (False, reasons + [f"all {f_missing} cited files missing — stale"])

    # 2. Services
    if services:
        s_resolving, s_missing = _check_services(services)
        reasons.append(f"services: {s_resolving}/{s_resolving + s_missing} still in registry")
        if s_resolving == 0:
            return (False, reasons + [f"all {s_missing} cited services missing — stale"])

    # 3. Symbols
    if symbols:
        sy_declaring, sy_missing = _check_symbols(symbols)
        reasons.append(f"symbols: {sy_declaring}/{sy_declaring + sy_missing} still declared")
        if sy_declaring == 0:
            return (False, reasons + [f"all {sy_missing} cited symbols undeclared — stale"])

    return (True, reasons)
