"""Thin async-friendly wrapper around the OpsGenie v2 + v1 REST API.

Read-only by design (v0). Used by agent/investigate.py to pre-fetch alert /
incident context when `/jarvis investigate <opsgenie-url-or-id>` is invoked.

No new dependencies — pure stdlib (urllib + json + re).

Auth: reads `OPSGENIE_API_KEY` from environment. Errors clearly if not set so
the caller can fall back to free-form mode.

API region: US (`api.opsgenie.com`) by default; override via
`OPSGENIE_API_BASE` env var if Jupiter ever migrates.
"""
from __future__ import annotations
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger("jarvis.opsgenie")

_BASE = os.environ.get("OPSGENIE_API_BASE", "https://api.opsgenie.com")
_TIMEOUT_SEC = float(os.environ.get("OPSGENIE_HTTP_TIMEOUT_SEC", "8"))

_UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)
# Matches both:
#   https://<tenant>.app.opsgenie.com/alert/detail/<uuid>/...
#   https://<tenant>.app.opsgenie.com/incident/detail/<uuid>/...
_URL_RE = re.compile(
    r"https?://[^/]*opsgenie\.com/(alert|incident)/detail/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


class OpsGenieError(Exception):
    """Raised on API / config errors. The caller should fall back gracefully."""


def _get_api_key() -> str:
    key = os.environ.get("OPSGENIE_API_KEY")
    if not key:
        raise OpsGenieError("OPSGENIE_API_KEY not set in environment")
    return key


def _request(path: str) -> dict:
    """GET against OpsGenie. Returns parsed JSON `data` field on 200, else raises."""
    url = f"{_BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"GenieKey {_get_api_key()}",
            "Accept": "application/json",
            "User-Agent": "jarvis-opsgenie-client/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("data", payload)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400]
        raise OpsGenieError(f"OpsGenie HTTP {e.code} on GET {path}: {body}") from e
    except urllib.error.URLError as e:
        raise OpsGenieError(f"OpsGenie network error on GET {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise OpsGenieError(f"OpsGenie returned non-JSON on GET {path}: {e}") from e


# --- ID / URL detection -------------------------------------------------------

def detect_ref(text: str) -> Optional[tuple[str, str]]:
    """Parse `text` for an OpsGenie reference.

    Returns (kind, id) where kind ∈ {"alert","incident"}, or None if not detected.
    Recognises:
      - Full OpsGenie URL → uses path segment to determine alert vs incident
      - Bare UUID → treats as ambiguous; caller should try both
                   (we return ("alert", uuid) and let the caller fall back)
    """
    m = _URL_RE.search(text)
    if m:
        kind = m.group(1).lower()
        return (kind, m.group(2).lower())
    m = _UUID_RE.search(text.strip())
    if m and text.strip() == m.group(1):
        # Bare UUID only — ambiguous; caller resolves
        return ("alert", m.group(1).lower())
    return None


# --- Fetchers -----------------------------------------------------------------

def get_alert(alert_id: str) -> dict:
    """GET /v2/alerts/{id}. Returns the alert detail dict."""
    return _request(f"/v2/alerts/{alert_id}")


def get_alert_notes(alert_id: str, limit: int = 20) -> list[dict]:
    """GET /v2/alerts/{id}/notes. Returns the recent notes list (most recent first)."""
    data = _request(f"/v2/alerts/{alert_id}/notes?limit={limit}")
    return data if isinstance(data, list) else []


def get_incident(incident_id: str) -> dict:
    """GET /v1/incidents/{id}. Returns the incident detail dict."""
    return _request(f"/v1/incidents/{incident_id}")


def get_incident_notes(incident_id: str, limit: int = 20) -> list[dict]:
    """GET /v1/incidents/{id}/notes. Returns the recent notes list."""
    data = _request(f"/v1/incidents/{incident_id}/notes?limit={limit}")
    return data if isinstance(data, list) else []


# --- High-level convenience ---------------------------------------------------

def fetch_context(text: str) -> Optional[dict]:
    """Detect + fetch full OpsGenie context for an investigation request.

    Returns a structured dict suitable for inlining into an LLM prompt, or
    None if `text` doesn't reference an OpsGenie alert/incident, or `{...,
    "error": "..."}` if detection succeeded but fetch failed.

    Tries the URL-inferred type first; if the bare-UUID path returns 404 as
    an alert, falls back to incident.
    """
    ref = detect_ref(text)
    if not ref:
        return None
    kind, ref_id = ref

    def _try_alert(aid: str) -> Optional[dict]:
        try:
            alert = get_alert(aid)
        except OpsGenieError as e:
            if "HTTP 404" in str(e):
                return None
            raise
        try:
            notes = get_alert_notes(aid)
        except OpsGenieError:
            notes = []
        return _shape_alert(aid, alert, notes)

    def _try_incident(iid: str) -> Optional[dict]:
        try:
            inc = get_incident(iid)
        except OpsGenieError as e:
            if "HTTP 404" in str(e):
                return None
            raise
        try:
            notes = get_incident_notes(iid)
        except OpsGenieError:
            notes = []
        return _shape_incident(iid, inc, notes)

    try:
        if kind == "incident":
            shaped = _try_incident(ref_id) or _try_alert(ref_id)
        else:
            shaped = _try_alert(ref_id) or _try_incident(ref_id)
    except OpsGenieError as e:
        log.warning("opsgenie fetch failed: %s", e)
        return {"kind": kind, "id": ref_id, "error": str(e)}

    if not shaped:
        return {"kind": kind, "id": ref_id,
                "error": f"OpsGenie returned 404 for both /alerts/{ref_id} and /incidents/{ref_id}"}
    return shaped


def _shape_alert(aid: str, alert: dict, notes: list[dict]) -> dict:
    """Normalise an alert dict into the prompt-ready shape."""
    return {
        "kind": "alert",
        "id": aid,
        "tinyId": alert.get("tinyId"),
        "message": alert.get("message"),
        "status": alert.get("status"),
        "acknowledged": alert.get("acknowledged"),
        "priority": alert.get("priority"),
        "source": alert.get("source"),
        "integration": (alert.get("integration") or {}).get("name"),
        "owner": alert.get("owner"),
        "responders": [r.get("name") or r.get("id") for r in (alert.get("responders") or [])],
        "teams": [t.get("name") or t.get("id") for t in (alert.get("teams") or [])],
        "tags": alert.get("tags") or [],
        "created_at": alert.get("createdAt"),
        "updated_at": alert.get("updatedAt"),
        "description": alert.get("description"),
        "details": alert.get("details") or {},  # custom k/v from the source
        "notes": _shape_notes(notes),
    }


def _shape_incident(iid: str, inc: dict, notes: list[dict]) -> dict:
    """Normalise an incident dict into the prompt-ready shape."""
    return {
        "kind": "incident",
        "id": iid,
        "tinyId": inc.get("tinyId"),
        "message": inc.get("message"),
        "status": inc.get("status"),
        "priority": inc.get("priority"),
        "impactedServices": [s.get("name") or s for s in (inc.get("impactedServices") or [])],
        "responders": [r.get("name") or r.get("id") for r in (inc.get("responders") or [])],
        "tags": inc.get("tags") or [],
        "created_at": inc.get("createdAt"),
        "updated_at": inc.get("updatedAt"),
        "description": inc.get("description"),
        "details": inc.get("details") or {},
        "notes": _shape_notes(notes),
    }


def _shape_notes(notes: list[dict]) -> list[dict]:
    """Keep only the fields useful for an investigation prompt; cap each note's text."""
    out = []
    for n in notes[:20]:
        out.append({
            "ts": n.get("createdAt"),
            "by": n.get("owner") or (n.get("user") or {}).get("name"),
            "text": (n.get("note") or "")[:600],
        })
    return out
