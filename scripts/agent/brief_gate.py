"""Brief-sufficiency gate for /api/v1/fix and /jarvis fix.

Reads a jira_fetch.py-shaped JSON from stdin (must include summary, description,
attachments_count, and ticket). Calls Claude Haiku 4.5 with a tight rubric and
emits a strict-JSON verdict on stdout:

    {"sufficient": bool, "missing": [str, ...], "reason": str}

Always appends one record to ~/jarvis/logs/brief_gate.jsonl regardless of
verdict (for retrospective false-positive analysis). Fails OPEN — if the LLM
call errors, returns sufficient=true so a Haiku outage cannot block production.

The wrapper (jarvis_fix.sh) decides whether to enforce based on the verdict.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

LOG_PATH = Path("/home/ubuntu/jarvis/logs/brief_gate.jsonl")
MODEL = "claude-haiku-4-5-20251001"

RUBRIC = """You gate whether a Jira ticket has enough information for an AI engineer to attempt a code fix without guessing.

A ticket is SUFFICIENT only if it has BOTH:
  (A) A clear, specific symptom OR desired behavior (not vague like "fix bug",
      "improve performance", "update copy", "make it work")
  (B) AT LEAST ONE concrete pointer the engineer can act on:
      - file / class / function / module name
      - API endpoint or route
      - specific UI screen + action sequence
      - reproduction steps
      - screenshot / video / attachment (count > 0 qualifies)
      - stack trace or error log
      - exact copy text or config value to change

A ticket is INSUFFICIENT if EITHER (A) or (B) is missing.

Reply with STRICT JSON only — no prose, no markdown fences:
  {"sufficient": true|false,
   "missing": ["short label for each missing piece"],
   "reason": "one short sentence (<=140 chars) explaining the verdict"}

If sufficient=true, missing should be []."""


def _log(record: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # never fail the gate on logging


def _open_verdict(reason: str) -> dict:
    return {"sufficient": True, "missing": [], "reason": f"gate_open: {reason}"}


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        v = _open_verdict(f"bad input json ({e!s})")
        print(json.dumps(v))
        _log({"ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
              "ticket": None, "verdict": v, "enforced": False, "error": str(e)})
        return 0

    ticket = (data.get("ticket") or "").strip() or None
    summary = (data.get("summary") or "").strip()
    description = (data.get("description") or "").strip()
    att_count = int(data.get("attachments_count") or 0)

    # Cheap pre-check: completely empty brief → don't even bother Haiku.
    if not summary and not description and att_count == 0:
        v = {"sufficient": False,
             "missing": ["summary", "description"],
             "reason": "ticket has no summary, description, or attachments"}
        print(json.dumps(v))
        _log({"ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
              "ticket": ticket, "verdict": v, "enforced": True,
              "summary_len": 0, "desc_len": 0, "att_count": 0,
              "model": None, "shortcut": "empty"})
        return 0

    # Truncate huge descriptions to keep the gate cheap + fast.
    desc_truncated = description[:4000]

    prompt = (
        f"Ticket: {ticket or '(unknown)'}\n"
        f"Title: {summary or '(none)'}\n"
        f"Attachments count: {att_count}\n"
        f"Description:\n{desc_truncated or '(none)'}"
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=MODEL,
            max_tokens=300,
            system=RUBRIC,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        # Strip code fences if Haiku added them.
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
            text = text.strip()
        verdict = json.loads(text)
        if "sufficient" not in verdict:
            raise ValueError("missing 'sufficient' key")
        verdict.setdefault("missing", [])
        verdict.setdefault("reason", "")
        # Normalize types
        verdict["sufficient"] = bool(verdict["sufficient"])
        verdict["missing"] = [str(m) for m in (verdict["missing"] or [])]
        verdict["reason"] = str(verdict["reason"])[:200]
    except Exception as e:
        v = _open_verdict(f"haiku call failed ({type(e).__name__}: {e!s})")
        print(json.dumps(v))
        _log({"ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
              "ticket": ticket, "verdict": v, "enforced": False,
              "summary_len": len(summary), "desc_len": len(description),
              "att_count": att_count, "model": MODEL,
              "error": f"{type(e).__name__}: {e!s}"})
        return 0

    print(json.dumps(verdict))
    _log({"ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
          "ticket": ticket, "verdict": verdict,
          "enforced": not verdict["sufficient"],
          "summary_len": len(summary), "desc_len": len(description),
          "att_count": att_count, "model": MODEL})
    return 0


if __name__ == "__main__":
    sys.exit(main())
