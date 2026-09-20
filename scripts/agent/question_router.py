"""Pre-flight question router — Layer 1 of the grounding plan (docs/grounding.md).

Before the Sonnet agent loop runs on a /jarvis question, classify the question
into one of these routes via a cheap Haiku 4.5 call:

  - meta_capabilities → answer from get_capabilities() (no Sonnet)
  - lookup_service    → call lookup_service tool directly (no Sonnet)
  - lookup_symbol     → call lookup_symbol tool directly (no Sonnet)
  - general           → fall through to the full Sonnet agent loop

prior_match (semantic match against qa_log) is Phase 1b — separate file.

Defensive defaults:
  - Low-confidence classifications → fall through to Sonnet
  - Any classifier exception → fall through silently
  - Fast-path execution failure → fall through to Sonnet
  - Modeled on scripts/agent/investigate_intent.py (existing Haiku-classifier pattern)
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger("jarvis.agent.question_router")

LOG_PATH = Path("/home/ubuntu/jarvis/logs/question_router.jsonl")
MODEL = "claude-haiku-4-5-20251001"
DISABLED_ENV = "JARVIS_DISABLE_QUESTION_ROUTER"  # set=1 to bypass routing entirely

Route = Literal["meta_capabilities", "lookup_service", "lookup_symbol", "general"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _log(record: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("question_router log write failed")


_RUBRIC = """You are a pre-flight router for Jarvis (Jupiter's internal AI engineer agent).
Decide which fast path (if any) should handle this question, BEFORE Jarvis spins up
its full retrieval+reasoning loop. The goal is to skip the expensive Sonnet agent
loop for questions that map cleanly onto a single deterministic tool.

ROUTES:

* meta_capabilities — question is ABOUT JARVIS ITSELF. Examples:
  - "what can you do"
  - "do you have access to X"
  - "how do I review a PR with you"
  - "can you read confluence"
  - "what's the cost of running you"
  - "list your features"
  - any "do you support / can you / are you able to" framed at Jarvis-the-tool
  Fast-path: answer from the capabilities manifest. No code search needed.

* lookup_service — question asks WHERE A JUPITER SERVICE LIVES / what URL / port /
  namespace / consumers / OpenAPI spec. Examples:
  - "where is bullet-ms deployed"
  - "what's the URL for the lending service"
  - "what namespace does deposit-platform run in"
  - "which port does pay-service use"
  - "who consumes the auth service"
  Service names are typically lowercase-hyphenated (e.g. "bullet-ms", "deposit-platform").
  Fast-path: invoke lookup_service(service_name) directly.
  Provide: fast_path_args = {"name_or_alias": "<service-name>"}.

* lookup_symbol — question asks WHERE A CODE SYMBOL IS DEFINED. The symbol is a
  CamelCase class / interface / enum / object name, OR an explicit function name
  (with parens or "function" keyword). Examples:
  - "where is PaymentsController defined"
  - "find the FraudDetector class"
  - "where is getUserById defined"
  - "where is the JarvisFraudResponse enum"
  NOT for: general "how does X work" or "explain X" — those are general.
  Fast-path: invoke lookup_symbol(name) directly.
  Provide: fast_path_args = {"name": "<symbol-name>"}.

* general — EVERYTHING ELSE. Multi-step investigations, "how does X work",
  cross-repo analysis, fix-mode questions, code-walkthroughs, why-questions, etc.
  When in doubt, return general. False positives on fast paths are MUCH worse than
  false negatives (false positive returns a wrong-shaped answer; false negative just
  uses the regular agent — no quality loss).

Output a SINGLE JSON object (no markdown, no fence):
{
  "route": "meta_capabilities" | "lookup_service" | "lookup_symbol" | "general",
  "confidence": "low" | "medium" | "high",
  "fast_path_args": {...} or null,
  "reason": "<short why>"
}

Confidence guidance:
- "high": the question is unambiguously one shape (e.g. "where is bullet-ms deployed",
  "what can you do")
- "medium": shape fits but could plausibly need more investigation
- "low": ambiguous — only use for borderline cases. Router caller will treat low as
  "fall through to general" anyway, so prefer 'low' over guessing wrong.

Set is_fast_path = (route != general AND confidence >= medium). Otherwise treat as general.

Output ONLY the JSON."""


def detect_route(question: str) -> dict:
    """Run Haiku classifier. Returns:
      {
        "route": Route,
        "confidence": "low|medium|high",
        "fast_path_args": dict | None,
        "reason": str,
        "is_fast_path": bool,   # convenience — caller checks this
      }
    Fails OPEN — any error returns route=general with is_fast_path=False.
    """
    fallback = {
        "route": "general",
        "confidence": "low",
        "fast_path_args": None,
        "reason": "fallback (classifier did not run)",
        "is_fast_path": False,
    }

    if os.environ.get(DISABLED_ENV) == "1":
        fallback["reason"] = "router disabled via env"
        return fallback

    if not question or len(question.strip()) < 4:
        fallback["reason"] = "question too short"
        _log({"ts": _now_iso(), "q": question[:200], "decision": fallback})
        return fallback

    try:
        import anthropic
    except Exception:
        logger.warning("anthropic SDK missing — question router skipped")
        return fallback
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return fallback

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=_RUBRIC,
            messages=[{"role": "user", "content": f"QUESTION:\n{question[:2500]}"}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)

        route = parsed.get("route") or "general"
        if route not in ("meta_capabilities", "lookup_service", "lookup_symbol", "general"):
            route = "general"
        confidence = parsed.get("confidence") or "low"
        if confidence not in ("low", "medium", "high"):
            confidence = "low"

        fast_path_args = parsed.get("fast_path_args")
        if not isinstance(fast_path_args, dict):
            fast_path_args = None

        is_fast_path = (route != "general") and (confidence in ("medium", "high"))

        # Defensive: if Haiku says fast_path but didn't give args, downgrade
        if is_fast_path and route in ("lookup_service", "lookup_symbol"):
            key = "name_or_alias" if route == "lookup_service" else "name"
            if not (fast_path_args or {}).get(key):
                is_fast_path = False
                confidence = "low"

        decision = {
            "route": route,
            "confidence": confidence,
            "fast_path_args": fast_path_args,
            "reason": str(parsed.get("reason") or "")[:300],
            "is_fast_path": is_fast_path,
        }
        _log({
            "ts": _now_iso(),
            "q": question[:500],
            "decision": decision,
            "input_tokens": getattr(resp.usage, "input_tokens", 0),
            "output_tokens": getattr(resp.usage, "output_tokens", 0),
        })
        return decision
    except Exception as e:
        logger.exception("question router classifier failed")
        fallback["reason"] = f"classifier error: {type(e).__name__}: {e!s}"[:300]
        _log({"ts": _now_iso(), "q": question[:200], "decision": fallback,
              "error": f"{type(e).__name__}: {e!s}"})
        return fallback


def run_fast_path(route: Route, fast_path_args: dict | None) -> str | None:
    """Execute the fast path tool directly. Returns the answer markdown, or None
    if the fast-path errored / returned empty. Caller treats None as "fall through
    to general". Never raises."""
    if not fast_path_args:
        fast_path_args = {}
    try:
        from agent import tools as _tools

        if route == "meta_capabilities":
            # Return the full capability manifest formatted nicely.
            raw = _tools.get_capabilities()
            return _format_capabilities_for_user(raw)

        if route == "lookup_service":
            name = (fast_path_args.get("name_or_alias") or "").strip()
            if not name:
                return None
            raw = _tools.lookup_service(name)
            return _format_lookup_service(name, raw)

        if route == "lookup_symbol":
            name = (fast_path_args.get("name") or "").strip()
            if not name:
                return None
            raw = _tools.lookup_symbol(name)
            return _format_lookup_symbol(name, raw)

        return None
    except Exception:
        logger.exception("fast path execution failed for route=%s", route)
        return None


def _format_capabilities_for_user(raw: str) -> str:
    """Render the capabilities manifest as a clean Slack-friendly summary.
    The raw JSON has every detail; for a user asking 'what can you do?' we
    want a compact list with names + commands + one-line summaries."""
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    caps = data.get("capabilities") or []
    if not caps:
        return "_(no capabilities loaded — manifest may be empty)_"

    # Group by category for readability
    by_cat: dict[str, list[dict]] = {}
    for c in caps:
        by_cat.setdefault(c.get("category", "other"), []).append(c)

    lines = ["*What I can do today:*", ""]
    cat_order = ["qa", "code-review", "code-edit", "investigation", "docs", "infra",
                 "http_api", "other"]
    for cat in cat_order + [c for c in by_cat if c not in cat_order]:
        if cat not in by_cat:
            continue
        lines.append(f"_*{cat}*_")
        for c in by_cat[cat]:
            cmd = c.get("command", "").strip()
            name = c.get("name", "").strip()
            summary = (c.get("summary") or "").strip()
            # Trim summary to 1 line
            summary = summary.split(". ")[0][:200]
            head = f"`{cmd}`" if cmd else f"*{name}*"
            lines.append(f"• {head} — {summary}")
        lines.append("")
    lines.append(f"_({len(caps)} capabilities loaded from `scripts/agent/capabilities.py`. "
                 "Ask a specific question for full detail on any of these.)_")
    return "\n".join(lines)


def _format_lookup_service(name: str, raw: str) -> str:
    """Render lookup_service result as a Slack-friendly answer."""
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if "error" in data:
        return (f"No service registry entry matches `{name}`. "
                f"_({data.get('error', '')[:200]})_\n\n"
                f"Try the full agent: `/jarvis where is the {name} service`")
    nm = data.get("name", name)
    parts = [f"*Service: `{nm}`*"]
    if data.get("source_repo"):
        parts.append(f"• Source repo: `{data['source_repo']}`")
    if data.get("k8s_url"):
        parts.append(f"• K8s in-cluster URL: `{data['k8s_url']}`")
    if data.get("route53_url"):
        parts.append(f"• Route53 cross-cluster URL: `{data['route53_url']}`")
    if data.get("namespace"):
        parts.append(f"• Namespace: `{data['namespace']}`")
    if data.get("port"):
        parts.append(f"• Port: `{data['port']}`")
    if data.get("openapi_spec"):
        parts.append(f"• OpenAPI spec: `{data['openapi_spec']}`")
    if data.get("exposed_paths"):
        eps = data["exposed_paths"][:5]
        parts.append(f"• Exposed paths ({len(data['exposed_paths'])}): " +
                     ", ".join(f"`{p}`" for p in eps))
    if data.get("consumers"):
        cs = data["consumers"][:8]
        parts.append(f"• Consumers ({len(data['consumers'])}): " +
                     ", ".join(f"`{c}`" for c in cs))
    if data.get("aliases"):
        parts.append(f"• Aliases: " + ", ".join(f"`{a}`" for a in data["aliases"][:5]))
    parts.append("")
    parts.append("_Sub-second answer from the pre-built service registry. "
                 "Ask a follow-up for deeper investigation._")
    return "\n".join(parts)


def _format_lookup_symbol(name: str, raw: str) -> str:
    """Render lookup_symbol result as a Slack-friendly answer."""
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    hits = data.get("hits") or data.get("declaring_chunks") or []
    if not hits or data.get("error"):
        err = data.get("error", "no declaring chunk found")
        return (f"No symbol matching `{name}` is declared anywhere in the indexed corpus. "
                f"_({err})_\n\n"
                f"It may be a third-party symbol, a recently-renamed identifier, or "
                f"the codebase uses a different name. Try the full agent: "
                f"`/jarvis where is {name} defined`")
    parts = [f"*Symbol `{name}` is declared in:*"]
    for h in hits[:5]:
        repo = h.get("repo", "?")
        path = h.get("path", "?")
        start = h.get("start_line", h.get("line", ""))
        end = h.get("end_line", "")
        loc = f"`{repo}/{path}`"
        if start:
            loc += f":{start}"
            if end and end != start:
                loc += f"-{end}"
        permalink = h.get("permalink")
        if permalink:
            parts.append(f"• <{permalink}|{loc}>")
        else:
            parts.append(f"• {loc}")
        if h.get("symbols"):
            parts.append(f"  _({', '.join(h['symbols'][:5])})_")
    if len(hits) > 5:
        parts.append(f"_… and {len(hits)-5} more hits_")
    parts.append("")
    parts.append("_Sub-second deterministic match from the symbol index. "
                 "Ask a follow-up for context on what each does._")
    return "\n".join(parts)
