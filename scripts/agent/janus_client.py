"""Janus MCP client — Jarvis's bridge to the Janus growth-bot Amplitude lookup.

Single source of truth for talking to Janus. Used by the `janus_user_journey`
agent tool (Slack `/jarvis ...` + HTTP `/api/v1/ask` + autosupport investigate
+ MCP). Per-engineer attribution via `caller_id` (no bearer token — Janus
runs locally and sources its own Amplitude creds via the wrapper).

Pattern modelled on `jove_client.py` (HTTP transport). Differences:
- stdio transport (Janus wrapper spawned per call on same EC2 box)
- no auth header (creds live inside the wrapper's env)
- caller_id is the per-call audit key in place of a bearer
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

log = logging.getLogger("jarvis.janus_client")

JANUS_WRAPPER = os.environ.get(
    "JANUS_AMPLITUDE_MCP_WRAPPER",
    "/home/ubuntu/test_databricks_connection/run_janus_amplitude_mcp.sh",
)
AUDIT_LOG = Path.home() / "jarvis" / "logs" / "janus_audit.jsonl"
_DEFAULT_TIMEOUT_SEC = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _audit(record: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("janus audit log write failed")


async def _call_tool_async(
    tool_name: str,
    arguments: dict[str, Any],
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
) -> Any:
    """Open one stdio session, call one tool, return parsed JSON content."""
    params = StdioServerParameters(command=JANUS_WRAPPER, args=[])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=10)
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


def user_journey(
    user_id: str,
    lookback_hours: int = 24,
    event_filter: list[str] | None = None,
    max_events: int = 200,
    include_properties: bool = True,
    include_session_summary: bool = True,
    caller_id: str = "",
) -> dict:
    """Fetch a Jupiter user's recent Amplitude event stream via Janus.

    Returns the raw JSON dict from Janus (success or error shape):
      Success: {user_id, amplitude_user_id, lookback_window, events[], summary, audit_ref}
      Error:   {error_code, error, ...}  where error_code ∈
               {invalid_request, user_not_found, upstream_error}

    Caller is responsible for branching on `error_code in result`.
    """
    args: dict[str, Any] = {
        "user_id": user_id,
        "lookback_hours": lookback_hours,
        "max_events": max_events,
        "include_properties": include_properties,
        "include_session_summary": include_session_summary,
        "caller_id": caller_id or "",
    }
    if event_filter:
        args["event_filter"] = event_filter

    started = _now_iso()
    try:
        result = asyncio.run(_call_tool_async("user_journey", args))
    except Exception as e:
        log.exception("janus user_journey call failed")
        err = {
            "error_code": "upstream_error",
            "error": f"{type(e).__name__}: {e!s}",
        }
        _audit({
            "ts": started, "tool": "user_journey", "args": args,
            "outcome": "exception", "error": err["error"], "caller_id": caller_id,
        })
        return err

    finished = _now_iso()
    # Audit metadata — payload size, not events themselves (PII-shaped)
    ev_count = 0
    err_code = None
    if isinstance(result, dict):
        ev_count = len(result.get("events") or [])
        err_code = result.get("error_code")
    _audit({
        "ts": finished, "tool": "user_journey",
        "user_id_prefix": (user_id or "")[:8],
        "lookback_hours": lookback_hours,
        "event_filter": event_filter,
        "max_events": max_events,
        "events_returned": ev_count,
        "error_code": err_code,
        "caller_id": caller_id,
        "started_at": started,
    })
    return result or {"error_code": "upstream_error", "error": "empty response"}

def event_count(
    event_type: str,
    lookback_days: int = 7,
    filter_property: str = "",
    filter_value: str = "",
    group_by: str = "",
    caller_id: str = "",
) -> dict:
    """Fleet-wide count for ONE Amplitude event over a trailing window.

    Returns {event_type, lookback_days, unique_users, total_events, daily[],
    top_values[], audit_ref} on success, or {error_code, error} on failure.

    Use for "how many users did X in the last N days" questions. For
    multi-step funnels / retention / multi-dimension breakdowns, deflect to
    the specific Amplitude dashboard (this tool is the simple-count surface,
    not a full segmentation engine).

    filter_property is an EVENT property. For built-in dimensions like
    platform / country / version, use group_by instead.
    """
    args: dict = {
        "event_type": event_type,
        "lookback_days": lookback_days,
        "caller_id": caller_id or "",
    }
    if filter_property:
        args["filter_property"] = filter_property
    if filter_value:
        args["filter_value"] = filter_value
    if group_by:
        args["group_by"] = group_by

    started = _now_iso()
    try:
        result = asyncio.run(_call_tool_async("event_count", args))
    except Exception as e:
        log.exception("janus event_count call failed")
        err = {"error_code": "upstream_error", "error": f"{type(e).__name__}: {e!s}"}
        _audit({
            "ts": started, "tool": "event_count", "args": args,
            "outcome": "exception", "error": err["error"], "caller_id": caller_id,
        })
        return err

    finished = _now_iso()
    unique_users = 0
    total_events = 0
    err_code = None
    if isinstance(result, dict):
        unique_users = int(result.get("unique_users") or 0)
        total_events = int(result.get("total_events") or 0)
        err_code = result.get("error_code")
    _audit({
        "ts": finished, "tool": "event_count",
        "event_type": event_type,
        "lookback_days": lookback_days,
        "filter_property": filter_property or None,
        "group_by": group_by or None,
        "unique_users": unique_users,
        "total_events": total_events,
        "error_code": err_code,
        "caller_id": caller_id,
        "started_at": started,
    })
    return result or {"error_code": "upstream_error", "error": "empty response"}
