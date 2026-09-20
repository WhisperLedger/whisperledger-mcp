"""Shared production-incident investigation logic.

Used by:
- HTTP API: scripts/api/server.py (/api/v1/alert-analysis endpoint, called by Sumith's SRE bot)
- Slack: /jarvis investigate <description-or-opsgenie-url-or-id> (engineer-driven)

Single source of truth for the investigation prompt: 4 structured sections (source,
flow, logs, failure modes) so both surfaces produce consistent output.

OpsGenie support (v0, 2026-05-17): if the input is an OpsGenie alert/incident URL
or UUID, the alert/incident detail (message, status, priority, responders,
recent notes) is pre-fetched and injected into the prompt so Jarvis has the
real context — not just the bare ID — when running its code investigation.
"""
from __future__ import annotations
import json
import logging
from .agent import ask
from . import opsgenie_client

log = logging.getLogger("jarvis.investigate")


def build_investigation_prompt(
    alert_or_description: str,
    service: str | None = None,
    extra_context: str | None = None,
) -> str:
    """Build the structured investigation prompt.

    If `alert_or_description` looks like an OpsGenie alert/incident reference
    (URL or bare UUID), the OpsGenie context is fetched and inlined. If the
    fetch fails, the request still proceeds with the raw text (degraded but
    not broken).

    Args:
        alert_or_description: alert name, free-text description, OpsGenie URL,
            or bare OpsGenie UUID
        service: optional owning service or team name
        extra_context: optional free-form context (stack trace, Grafana URL, etc.)
    """
    og_block = _fetch_opsgenie_block(alert_or_description)

    parts: list[str] = []
    if og_block and not og_block.get("error"):
        # Use the OpsGenie alert/incident message as the headline
        headline = og_block.get("message") or alert_or_description
        parts.append(f"Production alert / incident to investigate: `{headline}`")
        parts.append(_render_opsgenie_context(og_block))
    elif og_block and og_block.get("error"):
        parts.append(f"Production alert / issue to investigate: `{alert_or_description}`")
        parts.append(
            f"\n(Note: detected an OpsGenie reference but the lookup failed — "
            f"{og_block['error']}. Proceeding with the raw reference text only.)\n"
        )
    else:
        parts.append(f"Production alert / issue to investigate: `{alert_or_description}`")

    if service:
        parts.append(f"(Service / team: {service})")
    if extra_context:
        parts.append(f"\nAdditional context:\n{extra_context}\n")

    parts.append(
        "\nProvide a structured analysis:\n"
        "1. **Source**: which repo + which file emits this metric / handles this code path? "
        "Cite repo/path:lines.\n"
        "2. **Complete code flow** (with file:line for each step):\n"
        "   - Entry point (HTTP route / queue consumer / scheduled job)\n"
        "   - Business logic\n"
        "   - Downstream calls\n"
        "   - Where the metric is emitted, on which code path\n"
        "3. **Logs in this flow**: specific logger.info/warn/error statements with file:line. "
        "Include the format strings so an oncall can grep Kibana for them.\n"
        "4. **Likely failure modes**: realistic conditions that would cause this alert to fire / "
        "this issue to manifest, *ranked by probability*. For each:\n"
        "   - 1-line root-cause hypothesis\n"
        "   - Concrete investigation commands (SQL queries, Kibana searches, metric names, "
        "Grafana panel references) where possible\n"
        "   - If a fix path is obvious, note it (1 line); otherwise say 'needs investigation'\n"
        "\n"
        "Be sharp. An oncall reads this at 3am — give them a runbook, not a dissertation."
    )
    return "\n".join(parts)


def _fetch_opsgenie_block(text: str) -> dict | None:
    """Try to fetch OpsGenie context. Never raises — returns None or {..., error}."""
    try:
        return opsgenie_client.fetch_context(text)
    except Exception:
        log.exception("opsgenie fetch_context unexpected error")
        return None


def _render_opsgenie_context(b: dict) -> str:
    """Format the OpsGenie alert/incident block as a markdown section for the prompt."""
    lines = [f"\n**OpsGenie {b['kind']} context** (fetched live):"]
    lines.append(f"- ID: `{b['id']}`  ·  tinyId: `{b.get('tinyId')}`")
    if b.get("priority"):
        lines.append(f"- Priority: {b['priority']}  ·  Status: {b.get('status')}")
    if b.get("kind") == "alert" and b.get("integration"):
        lines.append(f"- Source / integration: {b.get('source')} via {b['integration']}")
    if b.get("impactedServices"):
        lines.append(f"- Impacted services: {', '.join(b['impactedServices'])}")
    if b.get("responders"):
        lines.append(f"- Responders / teams: {', '.join(b['responders'])}")
    if b.get("tags"):
        lines.append(f"- Tags: {', '.join(b['tags'])}")
    if b.get("created_at"):
        lines.append(f"- Created: {b['created_at']}  ·  Updated: {b.get('updated_at')}")
    if b.get("description"):
        desc = b["description"][:1500]
        lines.append(f"\nDescription:\n```\n{desc}\n```")
    if b.get("details"):
        # Custom k/v from the alert source — often the most useful payload
        details_text = json.dumps(b["details"], indent=2)[:1500]
        lines.append(f"\nAlert-source details:\n```json\n{details_text}\n```")
    if b.get("notes"):
        lines.append("\nRecent notes (most recent first):")
        for n in b["notes"][:8]:
            by = n.get("by") or "?"
            ts = n.get("ts") or ""
            txt = (n.get("text") or "")[:400]
            lines.append(f"  - [{ts} by {by}] {txt}")
    return "\n".join(lines)


def investigate(
    alert_or_description: str,
    service: str | None = None,
    extra_context: str | None = None,
):
    """Run an investigation and return the agent's structured analysis (AgentResult)."""
    prompt = build_investigation_prompt(alert_or_description, service, extra_context)
    return ask(prompt)
