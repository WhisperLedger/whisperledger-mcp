"""Detect 'investigate user X' shape in a /jarvis ask question.

When matched, slackbot forwards the question to /api/v1/autosupport/investigate
instead of running the lightweight /api/v1/ask agent. Tushar's feedback (2026-06-19)
drove this: production debugging questions ("why did user U123 fail at step Y",
"trace user X's onboarding journey") need the structured investigation pipeline
(deterministic confidence ordinals, structured log/DB/Amplitude queries) — not
a freeform markdown answer.

Returns dict:
  {
    "is_investigate_request": bool,
    "summary": str,           # the question repackaged as a one-paragraph
                              # issue_description for /api/v1/autosupport/investigate
    "subject_user_id": str | None,  # user / customer id mentioned in the question,
                                    # if any (e.g. "U06BN5VADTN", "abc-123")
    "confidence": "low|medium|high",
    "reason": str,            # why this did/didn't match
  }

Fails OPEN — any error returns is_investigate_request=False, so q&a still works.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("jarvis.agent.investigate_intent")

LOG_PATH = Path("/home/ubuntu/jarvis/logs/investigate_intent.jsonl")
MODEL = "claude-haiku-4-5-20251001"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _log(record: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("investigate_intent log write failed")


_RUBRIC = """You are an intent classifier for Jarvis. Decide whether a Slack question is a
USER-LEVEL PRODUCTION DEBUGGING request that should be routed to the structured
investigation pipeline (which produces literal log strings, DB queries, Amplitude
queries, and a confidence-graded root-cause hypothesis), versus a general code/
docs question that should go to the lightweight Q&A path.

INVESTIGATE_REQUEST = TRUE when the question is shaped like:
  - "why did user <id> fail at <step>"
  - "trace <user_id>'s onboarding / loan / payment / KYC journey"
  - "debug user X's prefunding / VKYC / EKYC / disbursal failure"
  - "what happened to <customer_id> during <flow>"
  - "user <id> got error <code/message> — investigate"
  - "compare success vs failure user journey for <user_id>"
  - "user X landed on unexpected screen Y" (the cause is a runtime/product event, not code)
  - Anything where the right answer requires correlating logs + DB + product events for a SPECIFIC USER

INVESTIGATE_REQUEST = FALSE for:
  - "how does X flow work" (general — Q&A path)
  - "where is symbol Y defined" (code lookup — Q&A path)
  - "what does service Z do" (Q&A path)
  - "fix this bug in repo R" (fix mode, not investigate)
  - "review PR #N" (review mode)
  - "what was changed in commit S" (git archaeology — Q&A path)
  - General architecture questions ("which service owns X")
  - Anything that doesn't reference a specific user/customer/account in production

Output a SINGLE JSON object (no markdown, no fence):
{
  "is_investigate_request": true | false,
  "summary": "<one-paragraph repackaging of the question as an issue_description suitable for /api/v1/autosupport/investigate; preserve the user_id, the failing step, and any error message verbatim>",
  "subject_user_id": "<user/customer id mentioned, or null>",
  "confidence": "low" | "medium" | "high",
  "reason": "<short why>"
}

Confidence rubric: high if the question explicitly names a user_id and a failure;
medium if it names one of the two; low if both are implied. Set is_investigate_request
to true only at medium+ confidence — false positives are worse than false negatives
(false positive burns ~1$/130s; false negative gracefully falls back to Q&A).

Output ONLY the JSON."""


_USER_ID_PATTERNS = [
    re.compile(r"\b(U[A-Z0-9]{8,})\b"),          # Slack user IDs
    re.compile(r"\b([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})\b"),  # UUID
    re.compile(r"\b(GUS-[a-zA-Z0-9-]+)\b"),       # Jupiter-style GUS- ids
    re.compile(r"\b(cust(?:omer)?[_-]?\d{4,})\b", re.IGNORECASE),
]


def _extract_user_id(text: str) -> str | None:
    for p in _USER_ID_PATTERNS:
        m = p.search(text or "")
        if m:
            return m.group(1)
    return None


def detect_investigate_intent(question: str) -> dict:
    """Run Haiku classifier. Returns the decision dict described in module docstring."""
    fallback = {
        "is_investigate_request": False,
        "summary": question[:1000],
        "subject_user_id": _extract_user_id(question),
        "confidence": "low",
        "reason": "fallback (classifier did not run)",
    }
    if not question or len(question.strip()) < 10:
        fallback["reason"] = "question too short"
        _log({"ts": _now_iso(), "q": question[:200], "decision": fallback})
        return fallback

    try:
        import anthropic
    except Exception:
        logger.warning("anthropic SDK missing — investigate-intent skipped")
        _log({"ts": _now_iso(), "q": question[:200], "decision": fallback,
              "skip_reason": "anthropic-sdk-missing"})
        return fallback
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        _log({"ts": _now_iso(), "q": question[:200], "decision": fallback,
              "skip_reason": "no-api-key"})
        return fallback

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=600,
            system=_RUBRIC,
            messages=[{"role": "user", "content": f"QUESTION:\n{question[:3000]}"}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        # Coerce + validate
        decision = {
            "is_investigate_request": bool(parsed.get("is_investigate_request")),
            "summary": str(parsed.get("summary") or question)[:2000],
            "subject_user_id": parsed.get("subject_user_id") or _extract_user_id(question),
            "confidence": parsed.get("confidence") or "low",
            "reason": str(parsed.get("reason") or "")[:300],
        }
        # Defense: if classifier returned True but no real user id mentioned anywhere, downgrade.
        if decision["is_investigate_request"] and not decision["subject_user_id"]:
            heur_id = _extract_user_id(question)
            if not heur_id:
                decision["is_investigate_request"] = False
                decision["reason"] = (
                    "downgraded: classifier returned true but no user_id detected — "
                    + decision["reason"]
                )[:300]
        _log({
            "ts": _now_iso(),
            "q": question[:500],
            "decision": decision,
            "input_tokens": getattr(resp.usage, "input_tokens", 0),
            "output_tokens": getattr(resp.usage, "output_tokens", 0),
        })
        return decision
    except Exception as e:
        logger.exception("investigate-intent classifier failed")
        fallback["reason"] = f"classifier error: {type(e).__name__}: {e!s}"[:300]
        _log({"ts": _now_iso(), "q": question[:200], "decision": fallback,
              "error": f"{type(e).__name__}: {e!s}"})
        return fallback
