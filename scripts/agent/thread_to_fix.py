"""Extract a fix-mode brief from a Slack thread.

Used by the @jarvis-mention handler when the user signals fix-intent
(`fix this`, `draft a PR`, etc). Calls Haiku 4.5 with the thread context +
the user's trigger message and returns a strict-JSON brief that can be
posted back to the user for confirmation before firing /api/v1/fix.

Returns dict shape:
  {
    "is_fix_request": bool,        # false → don't proceed
    "repo": str | None,            # must be in JARVIS_WRITE_ALLOWED_REPOS
    "task_description": str,       # 1-2 sentence brief for jarvis_fix.sh
    "file_pointers": list[str],    # files/symbols mentioned in the thread
    "confidence": "low|medium|high",
    "missing_info": list[str],     # what would make the brief sharper
    "reason": str,                 # short explanation when is_fix_request=false
  }

Fails open: any error returns {"is_fix_request": False, "reason": "<err>"}.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path("/home/ubuntu/jarvis/logs/thread_to_fix.jsonl")
MODEL = "claude-haiku-4-5-20251001"


def _allowed_repos() -> list[str]:
    raw = os.environ.get("JARVIS_WRITE_ALLOWED_REPOS", "")
    repos = [r.strip() for r in raw.split() if r.strip()]
    return repos or ["jupiter", "bff-core", "jarvis", "jupiter-design-system"]


def _log(record: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z")
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def extract(thread_messages: list[dict], trigger_text: str,
            requester_user: str | None = None) -> dict:
    """Pull a fix brief out of a Slack thread.

    `thread_messages` is the conversation so far (oldest first), each
    `{role: "user"|"assistant", content: str}`. `trigger_text` is the
    requesting @mention's text (with the `<@bot_uid>` token already stripped).
    """
    allowed = _allowed_repos()
    rubric = (
        "You convert a Slack debugging discussion into a fix-mode brief for "
        "Jarvis (an automated PR-drafting bot).\n\n"
        "Decide first if the user is actually asking for a CODE FIX (vs just "
        "discussion / asking questions / venting). Only return is_fix_request=true "
        "when the user has converged on a concrete change they want made.\n\n"
        f"Allowed repos: {allowed}. If you cannot identify ONE of these from the "
        "thread, set repo=null and is_fix_request=false (don't guess).\n\n"
        "task_description must be 1-3 sentences in imperative form ('Add idempotency "
        "key support to ...', 'Fix null-pointer in ...'). Include the specific file "
        "or symbol if mentioned.\n\n"
        "file_pointers: file paths or class/function names mentioned in the thread. "
        "Empty list if none.\n\n"
        "confidence: 'high' when the change is unambiguous + has a file pointer + "
        "the thread converged. 'medium' when the change is clear but there's some "
        "ambiguity. 'low' when intent is fuzzy.\n\n"
        "missing_info: list specific things that would sharpen the brief (e.g. "
        "'which file?', 'desired behavior on null?', 'should we add a test?'). "
        "Empty list if none.\n\n"
        "Output STRICT JSON only — no prose, no markdown fences."
    )

    convo = []
    for msg in thread_messages[-30:]:
        role = msg.get("role") or "user"
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        convo.append({"role": role, "content": content[:3000]})
    # The trigger text is the most recent user signal; emphasize it.
    convo.append({
        "role": "user",
        "content": (
            f"REQUESTING USER: {requester_user or 'unknown'}\n"
            f"TRIGGER MESSAGE (the @-mention asking for a fix):\n"
            f"{trigger_text}\n\n"
            "Reply with the strict-JSON brief."
        ),
    })

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=MODEL,
            max_tokens=600,
            system=rubric,
            messages=convo,
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
            text = text.strip()
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
        result = json.loads(text)
    except Exception as e:
        result = {"is_fix_request": False, "reason": f"extractor_error: {type(e).__name__}: {e!s}",
                  "repo": None, "task_description": "", "file_pointers": [],
                  "confidence": "low", "missing_info": []}

    # Normalize + enforce repo allowlist.
    result.setdefault("is_fix_request", False)
    result.setdefault("repo", None)
    result.setdefault("task_description", "")
    result.setdefault("file_pointers", [])
    result.setdefault("confidence", "low")
    result.setdefault("missing_info", [])
    result.setdefault("reason", "")
    if result.get("repo") and result["repo"] not in allowed:
        result["is_fix_request"] = False
        result["reason"] = (f"repo '{result['repo']}' is not in "
                            f"JARVIS_WRITE_ALLOWED_REPOS={allowed}")
        result["repo"] = None

    _log({
        "trigger_text": trigger_text[:300],
        "requester": requester_user,
        "thread_msg_count": len(thread_messages),
        "verdict": result,
    })
    return result
