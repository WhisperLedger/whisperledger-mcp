"""Grounding context prelude — Layer 2.

For questions that fall through to the Sonnet agent (general route, no
prior_match hit), build a small markdown prelude (≤2 KB) and prepend it
to the question. Goal: reduce the agent's tool-call count by giving it
"what we may already know" upfront.

Three components per docs/grounding.md:

  1. Top 3 prior qa_log entries from the SAME ASKER (last 30d, cosine ≥ 0.65 —
     wider threshold than prior_match's 0.85, so we surface RELATED priors
     even if not duplicate-shaped). Format: (date · 1-line preview · qid).

  2. Recent commits touching files mentioned in the question (extracted via
     the same `<repo>/<path>:line` citation regex as prior_match). Git log
     against ~/jarvis/repos/<repo>. Useful for "why does X behave this way"
     where the answer is a recent commit subject.

  3. (Skipped intentionally) — operator memory at ~/.claude/.../memory/ is
     out of scope. Those memories are for Rohit-Claude collaboration; they
     contain rules like "always show Slack drafts first" which would
     confuse the agent answering an engineer's code question. CLAUDE.md is
     already in SYSTEM_PROMPT.

Defensive defaults:
  - No caller_id → only commits-from-files (no prior-Q context)
  - No mentioned files → only prior-Q (no commits)
  - Both empty → return None (no prelude prepended)
  - Any embed/git failure → silently skip that source
  - Env bypass: JARVIS_DISABLE_GROUNDING_CONTEXT=1
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("jarvis.agent.grounding_context")

REPOS_ROOT = Path("/home/ubuntu/jarvis/repos")
AUDIT_LOG = Path("/home/ubuntu/jarvis/logs/grounding_context.jsonl")

DEFAULT_LOOKBACK_DAYS = 30
PRIOR_Q_SIMILARITY_THRESHOLD = 0.65   # wider than prior_match's 0.85
PRIOR_Q_TOP_K = 3
COMMITS_PER_FILE = 3
MAX_PRELUDE_BYTES = 2200
DISABLED_ENV = "JARVIS_DISABLE_GROUNDING_CONTEXT"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _audit(record: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("grounding_context audit log write failed")


def _top_prior_questions(
    question: str, caller_id: str,
    k: int = PRIOR_Q_TOP_K,
    threshold: float = PRIOR_Q_SIMILARITY_THRESHOLD,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[dict]:
    """Return up to k semantically-similar prior questions from caller's
    qa_log history. Reuses prior_match's embedding infrastructure."""
    from . import prior_match as _pm

    history = _pm._load_caller_history(caller_id, lookback_days)
    if not history:
        return []
    # Ensure all history entries have cached embeddings
    cache = _pm._load_cache()
    to_embed_qids = []
    to_embed_questions = []
    for e in history:
        qid = e.get("qid")
        if not qid or qid in cache:
            continue
        to_embed_qids.append(qid)
        to_embed_questions.append((e.get("q") or "").strip())
    if to_embed_questions:
        vecs = _pm._embed_batch(to_embed_questions)
        for qid, vec in zip(to_embed_qids, vecs):
            if vec:
                _pm._save_cache_entry(qid, vec)
    # Embed current Q
    qvec = _pm._embed_one(question)
    if not qvec:
        return []
    # Score
    scored = []
    for e in history:
        qid = e.get("qid")
        if not qid:
            continue
        hvec = cache.get(qid)
        if not hvec:
            continue
        sim = _pm._cosine(qvec, hvec)
        if sim >= threshold:
            scored.append((sim, e))
    scored.sort(key=lambda x: -x[0])
    out = []
    for sim, e in scored[:k]:
        out.append({
            "qid": e.get("qid", ""),
            "ts": e.get("ts", ""),
            "q": (e.get("q") or "").strip(),
            "similarity": round(sim, 3),
        })
    return out


# Match the same patterns prior_match uses + bare-filename mentions.
_FILE_CITATION_RE = re.compile(
    r"`?([\w.-]+)/([\w./\-]+\.(?:kt|kts|java|scala|ts|tsx|js|jsx|py|yml|yaml|json|sql|md))(?::\d+(?:-\d+)?)?`?"
)


def _extract_mentioned_files(question: str) -> list[tuple[str, str]]:
    """Return [(repo, path)] mentioned in the question, deduped."""
    seen = set()
    out: list[tuple[str, str]] = []
    for m in _FILE_CITATION_RE.finditer(question or ""):
        pair = (m.group(1), m.group(2))
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


def _recent_commits(
    repo: str, path: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = COMMITS_PER_FILE,
) -> list[dict]:
    """git log on a specific file — last N commits in window. Returns [{sha, subject, author, date}]."""
    repo_dir = REPOS_ROOT / repo
    if not repo_dir.is_dir():
        return []
    full_path = repo_dir / path
    if not full_path.is_file():
        return []
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "log",
             f"--since={lookback_days}.days.ago",
             "--pretty=format:%h\x01%s\x01%an\x01%ai",
             f"--max-count={limit}",
             "--", path],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return []
    except Exception:
        return []
    commits = []
    for line in out.stdout.splitlines():
        parts = line.split("\x01", 3)
        if len(parts) != 4:
            continue
        sha, subject, author, ts = parts
        commits.append({
            "sha": sha[:10],
            "subject": subject[:120],
            "author": author[:40],
            "date": ts.split(" ")[0],
        })
    return commits


def build_prelude(question: str, caller_id: str | None = None,
                  lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> str | None:
    """Return a small markdown prelude or None if no useful context found.
    The prelude is prepended to the question before Sonnet sees it."""
    if os.environ.get(DISABLED_ENV) == "1":
        return None
    if not question or len(question.strip()) < 12:
        return None

    started = _now_iso()
    prior_qs: list[dict] = []
    commits_by_file: list[tuple[tuple[str, str], list[dict]]] = []

    # 1. Prior questions (if caller_id available)
    if caller_id:
        try:
            prior_qs = _top_prior_questions(question, caller_id, lookback_days=lookback_days)
        except Exception:
            logger.exception("top_prior_questions failed")
            prior_qs = []

    # 2. Commits on mentioned files
    mentioned = _extract_mentioned_files(question)
    for repo, path in mentioned[:5]:  # cap files
        try:
            commits = _recent_commits(repo, path, lookback_days=lookback_days)
            if commits:
                commits_by_file.append(((repo, path), commits))
        except Exception:
            logger.exception("recent_commits failed for %s/%s", repo, path)

    if not prior_qs and not commits_by_file:
        return None

    # Build markdown
    lines: list[str] = ["[GROUNDING — context Jarvis already has on related prior work]", ""]

    if prior_qs:
        lines.append("*Related prior questions you (the same engineer) asked recently:*")
        for pq in prior_qs:
            date = pq["ts"].split("T")[0] if "T" in str(pq["ts"]) else str(pq["ts"])
            preview = pq["q"][:140].replace("\n", " ")
            lines.append(f"- {date} (sim {pq['similarity']}, qid `{pq['qid']}`): {preview!r}")
        lines.append("")

    if commits_by_file:
        lines.append("*Recent commits touching files mentioned in your question:*")
        for (repo, path), commits in commits_by_file:
            lines.append(f"_{repo}/{path}:_")
            for c in commits:
                lines.append(f"  - `{c['sha']}` ({c['date']}) {c['subject']!r} — {c['author']}")
        lines.append("")

    lines.append("_Use the above as a starting hint, but verify against current code if anything "
                 "looks load-bearing. Code may have moved since these references were captured._")
    lines.append("")
    lines.append("---")
    lines.append("")

    prelude = "\n".join(lines)
    if len(prelude.encode("utf-8")) > MAX_PRELUDE_BYTES:
        prelude = prelude[:MAX_PRELUDE_BYTES] + "\n…[truncated]\n---\n"

    _audit({
        "ts": started, "caller_id": caller_id or "",
        "q_preview": question[:200],
        "n_prior_questions": len(prior_qs),
        "n_files_with_commits": len(commits_by_file),
        "prelude_bytes": len(prelude.encode("utf-8")),
    })
    return prelude
