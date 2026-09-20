"""AutoSupport endpoints — Intelligent Resolution Orchestration integration.

Spec authored by Ritheesh Urankar (SRE team) for the AutoSupport platform that
sits in front of Slack / Freshdesk / Jira and routes operational requests to
Jarvis for diagnostic investigation. This module implements the three contracts
defined in the AutoSupport design doc:

    POST   /api/v1/autosupport/investigate              → 202 + job ack
    GET    /api/v1/autosupport/investigate/{id}         → poll fallback (mirrors callback payload)
    POST   /api/v1/autosupport/sync                     → drift check on recommended actions

Hard contracts (do NOT relax without re-talking to Ritheesh):
  - Confidence ordinals (low/medium/high) are derived DETERMINISTICALLY in
    Python from the agent's tool_calls trace. The LLM does NOT pick the
    ordinal. This was the load-bearing decision after the v10 review.
  - `auto_executable` is HARD-CODED to `false` on every recommended_action.
    Jarvis emits structure; AutoSupport + SRE desk decide automation.
  - database_contexts always set `note: "sre_execution_required"`. No SQL
    safety variables. SRE manual desk handles all SQL.
  - `idempotency_key` is generated server-side (caller can omit). Accepts
    any opaque string ≤128 chars if provided.

Async pattern mirrors /api/v1/fix: 202 ack + in-memory job store + optional
callback_url with single-attempt POST + GET poll as fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("jarvis.api.autosupport")

# --- module state -------------------------------------------------------------

_AUDIT_LOG = Path.home() / "jarvis" / "logs" / "autosupport_audit.jsonl"
_CALLBACK_LOG = Path.home() / "jarvis" / "logs" / "autosupport_callbacks.jsonl"
_CALLBACK_TIMEOUT_SEC = int(os.environ.get("JARVIS_AUTOSUPPORT_CALLBACK_TIMEOUT_SEC", "10"))
_CONCURRENCY = int(os.environ.get("JARVIS_AUTOSUPPORT_CONCURRENCY", "3"))
_HARD_TIMEOUT_SEC = int(os.environ.get("JARVIS_AUTOSUPPORT_TIMEOUT_SEC", "300"))
_ESTIMATED_DURATION_SEC = 90  # advertised in 202 ack; real avg ~85s per Ritheesh's doc

_semaphore: asyncio.Semaphore | None = None
_jobs: dict[str, dict] = {}
_jobs_lock = asyncio.Lock()
_idempotency_keys: dict[str, tuple[str, float]] = {}
_IDEMPOTENCY_WINDOW_SEC = 300


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _est_cost_usd(res) -> float:
    return round(
        (res.input_tokens * 3
         + res.cache_read_tokens * 0.30
         + res.cache_creation_tokens * 3.75
         + res.output_tokens * 15) / 1_000_000,
        4,
    )


def _new_id() -> str:
    return f"as-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def _audit(record: dict) -> None:
    try:
        _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("autosupport audit log write failed")


async def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_CONCURRENCY)
    return _semaphore


# --- request / response models ------------------------------------------------

Channel = Literal["slack", "freshdesk", "jira"]
Ordinal = Literal["low", "medium", "high"]
IssueCategory = Literal[
    "product_defect", "infrastructure_fault", "configuration_drift",
    "third_party_outage", "security_anomaly", "technical_debt",
]
ConfidenceReason = Literal[
    "registry_confirmed", "spec_hit", "multiple_code_hits", "weak_hit",
]
ActionType = Literal["read_only", "requires_sre"]
ActionRouting = Literal["resolution_executor", "sre_escalation_router"]
EvidenceType = Literal["code", "commit", "service_registry", "openapi_spec", "pr_description"]
HttpMethod = Literal["GET", "POST", "PUT", "DELETE", "PATCH"]


class JarvisInvestigationRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=200)
    channel: Channel
    user_id: str = Field(..., min_length=1, max_length=200)
    issue_description: str = Field(..., min_length=1, max_length=16000)
    callback_url: Optional[str] = Field(None, max_length=2000)


class JarvisInvestigationAck(BaseModel):
    investigation_id: str
    request_id: str
    status: Literal["QUEUED", "PROCESSING"]
    estimated_duration_sec: int
    created_at: str


class EvidenceItem(BaseModel):
    type: EvidenceType
    repos: list[str] = Field(default_factory=list)
    files: Optional[list[str]] = None


class ApiContext(BaseModel):
    context_id: str
    method: HttpMethod
    service: str
    endpoint: str
    api_confidence: Ordinal
    headers: Optional[dict[str, str]] = None
    body_json: Optional[dict] = None


class DatabaseContext(BaseModel):
    context_id: str
    target_db: str
    raw_sql: str
    note: Literal["sre_execution_required"] = "sre_execution_required"


class ActionPayload(BaseModel):
    api_contexts: Optional[list[ApiContext]] = None
    database_contexts: Optional[list[DatabaseContext]] = None


class RecommendedAction(BaseModel):
    action_id: str
    type: ActionType
    description: str
    actions_confidence: Ordinal
    auto_executable: Literal[False] = False
    routing: ActionRouting
    private_note: Optional[str] = None
    payload: ActionPayload
    escalation_reason: Optional[str] = None


class Findings(BaseModel):
    root_cause_hypothesis: str = Field(..., min_length=10)
    issue_category: IssueCategory
    findings_confidence: Ordinal
    confidence_reason: ConfidenceReason
    affected_services: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)


class JarvisInvestigationCallbackPayload(BaseModel):
    investigation_id: str
    request_id: str
    completed_at: str
    status: Literal["COMPLETED", "FAILED"]
    findings: Findings
    recommended_actions: list[RecommendedAction]
    audit_ref: str
    cost_usd: float | None = None


# Sync endpoint -------------

class SyncActionRequest(BaseModel):
    action_id: str
    services: list[str] = Field(..., min_length=1)
    update_required: Literal[False] = False
    payload: ActionPayload


class JarvisSyncRequest(BaseModel):
    action_ids: list[SyncActionRequest] = Field(..., min_length=1)


class SyncResult(BaseModel):
    action_id: str
    update_required: bool
    updated_services: Optional[list[str]] = None
    updated_payload: Optional[ActionPayload] = None


class JarvisSyncResponse(BaseModel):
    sync_results: list[SyncResult]
    synced_at: str
    audit_ref: str


# --- agent invocation ---------------------------------------------------------

_AGENT_INVESTIGATION_PROMPT = """You are Jarvis investigating a support issue from the AutoSupport platform (Slack / Freshdesk / Jira). Investigate using your retrieval tools (search_code, search_prs, lookup_service, lookup_symbol, grep_all_repos, read_file, fetch_pr_diff, parse_stacktrace).

Produce a thorough technical answer that covers:

1. **Root cause** — what's broken, with exact file:line citations.
2. **Affected services** — which Jupiter services are in the failure path.
3. **Evidence** — list the files you read or the registry entries you confirmed, by repo + path.
4. **Recommended actions** — one or more concrete next steps the AutoSupport platform can take. For each action, specify:
   - whether it's a read-only check (e.g., curl an API to verify) or requires SRE intervention (DB query, manual config change)
   - the exact API endpoint to call, with method + service + path + required headers, if applicable
   - the SQL to run if a database lookup is needed (just describe — SRE desk will execute)
   - private operator notes that should not be shared with the end user

Lean on `lookup_service` to confirm exact service names. Lean on the OpenAPI spec hits in `gateway/bifrost/src/main/resources/specs/stargate/*.yaml` to confirm endpoint paths. Don't invent endpoints — if you can't find one, say so and recommend SRE escalation.

For any SQL query you recommend, FIRST call `grep_repo(<repo>, "CREATE TABLE", file_glob="*.sql")` or `list_repo_files(<repo>, "src/main/resources/db/migration/**")` to locate the exact table name from a Flyway migration file. Never infer table names from Kotlin entity class names or JPA/JOOQ annotations — migration files are the ground truth.

IMPORTANT — log systems are NOT SQL databases: Loki, Kibana, Elasticsearch, and Datadog Logs use LogQL/KQL/Lucene, not SQL. Do NOT write a raw_sql query for log searches. Put Loki/Kibana query patterns in your prose under the private_note section only. Only use database_contexts for real SQL databases (PostgreSQL, MySQL, TiDB).

Be comprehensive — your output will be structurally extracted into a JSON contract."""


_HAIKU_EXTRACTOR_RUBRIC = """You are a JSON structurer. Given Jarvis's technical investigation of a support issue, extract a strict JSON object matching this exact shape. NO markdown, NO prose, NO code fence — output the JSON only.

{
  "root_cause_hypothesis": "Detailed diagnostics from the investigation, ≥10 chars. Reference exact file:line where given.",
  "issue_category": "product_defect|infrastructure_fault|configuration_drift|third_party_outage|security_anomaly|technical_debt",
  "affected_services": ["service-name-1", "service-name-2"],
  "evidence": [
    {"type": "code", "repos": ["repo-name"], "files": ["path/to/file.kt:42"]},
    {"type": "openapi_spec", "repos": ["gateway"], "files": ["bifrost/src/main/resources/specs/stargate/pay.yaml"]},
    {"type": "service_registry", "repos": []},
    {"type": "pr_description", "repos": ["jupiter"], "files": ["#1234"]}
  ],
  "recommended_actions": [
    {
      "action_id": "act_1",
      "type": "read_only|requires_sre",
      "description": "Plain-language imperative step.",
      "routing": "resolution_executor|sre_escalation_router",
      "private_note": "Optional SRE-only runbook text, or null.",
      "escalation_reason": "Required when type=requires_sre, else null.",
      "payload": {
        "api_contexts": [
          {
            "context_id": "ctx_1",
            "method": "GET|POST|PUT|DELETE|PATCH",
            "service": "exact-service-name",
            "endpoint": "/exact/path",
            "headers": {"X-Tenant": "urn:tenant:jupiter"},
            "body_json": null
          }
        ],
        "database_contexts": [
          {"context_id": "ctx_2", "target_db": "lending_v2_prod", "raw_sql": "SELECT ..."}
        ]
      }
    }
  ]
}

HARD RULES:
- Every action MUST have ≥1 api_contexts entry OR ≥1 database_contexts entry. Skip actions you cannot specify.
- DO NOT include any confidence fields (findings_confidence / actions_confidence / api_confidence) — Python will derive them.
- DO NOT include auto_executable — Python hardcodes it to false.
- DO NOT include "note" inside database_contexts — Python will add note=sre_execution_required.
- If the investigation provides no grounded action path, emit a single action with type=requires_sre, routing=sre_escalation_router, escalation_reason set, and a database_context describing what data SRE should pull manually.
- issue_category: pick the BEST single match from the enum.
- database_contexts is ONLY for real SQL databases (PostgreSQL / MySQL / TiDB). DO NOT emit database_contexts for Loki, Kibana, Elasticsearch, Datadog Logs, or any log aggregation system — those belong in private_note only (as LogQL / KQL / query strings).
- For raw_sql, use the EXACT table name as found in a Flyway migration file (*.sql). If the investigation did not cite a migration file for this table, append a SQL comment /* VERIFY TABLE NAME */ so the SRE knows to confirm before running.

Output ONLY the JSON. No leading text. No trailing text. No code fence."""


def _build_investigation_prompt(req: JarvisInvestigationRequest) -> str:
    return (
        f"{_AGENT_INVESTIGATION_PROMPT}\n\n"
        f"=== INCOMING REQUEST ===\n"
        f"channel: {req.channel}\n"
        f"user_id: {req.user_id}\n"
        f"request_id: {req.request_id}\n\n"
        f"=== ISSUE DESCRIPTION ===\n"
        f"{req.issue_description}\n"
    )


def _haiku_extract_structured(investigation_markdown: str) -> Optional[dict]:
    """Second pass: Haiku 4.5 structures the agent's prose into the contract JSON.

    Returns None on parse failure; caller falls back to escalation payload.
    """
    try:
        import anthropic
    except Exception:
        logger.exception("anthropic SDK not available for haiku extraction")
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY missing — haiku extraction skipped")
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4000,
            system=_HAIKU_EXTRACTOR_RUBRIC,
            messages=[{
                "role": "user",
                "content": (
                    "INVESTIGATION OUTPUT:\n\n" + investigation_markdown[:60000]
                ),
            }],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return _extract_agent_json(raw)
    except Exception as e:
        logger.exception("haiku extraction failed: %s", e)
        return None


# --- deterministic ordinal derivation ----------------------------------------

def _derive_findings_confidence(agent_json: dict,
                                tool_calls: list[dict]) -> tuple[Ordinal, ConfidenceReason]:
    """Derive findings_confidence + confidence_reason from the agent's declared
    evidence (enumerated under the rubric) cross-checked with the tool-calls
    trace.

    The agent enumerates evidence types; Python decides which ordinal that
    enumeration earns. This keeps the label deterministic (LLM cannot choose
    a label, only enumerate items that map to one).

    Priority order (registry_confirmed > spec_hit > multiple_code_hits > weak_hit):
      - registry_confirmed: agent declared evidence with type=service_registry,
        OR called lookup_service / lookup_symbol successfully
      - spec_hit: agent declared evidence with type=openapi_spec referencing
        an actual .yaml/.yml file
      - multiple_code_hits: ≥3 distinct file paths declared in code evidence
        AND ≥2 retrieval tool_calls were made
      - weak_hit: anything less
    """
    evidence = agent_json.get("evidence") or []
    declared_registry = any(e.get("type") == "service_registry"
                            and (e.get("repos") or e.get("files"))
                            for e in evidence if isinstance(e, dict))
    declared_spec = any(
        e.get("type") == "openapi_spec"
        and any(re.search(r"\.ya?ml(:|$)", f or "") for f in (e.get("files") or []))
        for e in evidence if isinstance(e, dict)
    )
    code_files: set[str] = set()
    for e in evidence:
        if isinstance(e, dict) and e.get("type") == "code":
            for f in (e.get("files") or []):
                if f:
                    code_files.add(str(f).split(":")[0])

    # Cross-check with tool_calls — independent signal of retrieval depth.
    tool_names = [str((tc or {}).get("name") or "") for tc in tool_calls]
    called_registry = any(n in ("lookup_service", "lookup_symbol") for n in tool_names)
    n_retrieval = sum(1 for n in tool_names
                      if n in ("lookup_service", "lookup_symbol", "search_code",
                               "search_prs", "grep_repo", "grep_all_repos",
                               "read_file", "list_repo_files"))

    if declared_registry or called_registry:
        return "high", "registry_confirmed"
    if declared_spec:
        return "high", "spec_hit"
    if len(code_files) >= 3 and n_retrieval >= 2:
        return "medium", "multiple_code_hits"
    return "low", "weak_hit"


def _derive_api_confidence(ctx: dict, evidence: list[dict],
                           registry_services: set[str]) -> Ordinal:
    """Per-api_context ordinal:
      - high: service appears in registry_services (validated against actual
        Jupiter service registry) AND endpoint path is referenced by an
        openapi_spec evidence entry
      - medium: service is registry-validated OR endpoint is referenced by
        any openapi_spec / code evidence file path string
      - low: neither (purely inferred — caller should NOT auto-execute)
    """
    service = (ctx.get("service") or "").strip().lower()
    endpoint = (ctx.get("endpoint") or "").strip()
    in_registry = bool(service) and (service in registry_services)

    # endpoint matched against evidence file path collection
    spec_hit = False
    code_hit = False
    for e in evidence:
        if not isinstance(e, dict):
            continue
        files = e.get("files") or []
        etype = e.get("type") or ""
        # A weak signal: if the agent paired this endpoint with a spec/code
        # evidence entry, the endpoint string typically appears either in the
        # spec file path OR is verifiably present in the linked code. We
        # don't introspect the file content here (live read would cost too
        # much for each ordinal), so the proxy is: declared in evidence at
        # all.
        if etype == "openapi_spec" and files:
            spec_hit = True
        if etype == "code" and files:
            code_hit = True

    if in_registry and spec_hit:
        return "high"
    if in_registry or spec_hit or code_hit:
        return "medium"
    return "low"


def _derive_action_confidence(action: dict, findings_conf: Ordinal) -> Ordinal:
    """Per-action: high if action's API contexts include ≥1 high + 0 low.
    medium if action has any medium and no low. low otherwise.
    Also clamped to findings_conf — an action can't be more confident than the finding.
    """
    api_confs = [
        c.get("api_confidence") for c in (action.get("payload", {}).get("api_contexts") or [])
        if c.get("api_confidence")
    ]
    if not api_confs:
        # database-only action — confidence reflects the parent finding
        derived = findings_conf
    else:
        if "low" in api_confs:
            derived = "low"
        elif all(c == "high" for c in api_confs):
            derived = "high"
        else:
            derived = "medium"

    order = {"low": 0, "medium": 1, "high": 2}
    return derived if order[derived] <= order[findings_conf] else findings_conf


def _registry_services_for_action(action: dict) -> set[str]:
    """Validate each api_context.service against the actual service registry.

    This is the LOAD-BEARING check: api_confidence='high' requires that the
    declared service genuinely exists in Jupiter's pre-built service registry,
    not just that the LLM wrote a plausible-looking name.
    """
    out: set[str] = set()
    services_to_check: set[str] = set()
    for ctx in (action.get("payload", {}).get("api_contexts") or []):
        s = (ctx.get("service") or "").strip().lower()
        if s:
            services_to_check.add(s)
    if not services_to_check:
        return out
    try:
        from agent import tools as agent_tools
    except Exception:
        return out
    for s in services_to_check:
        try:
            parsed = json.loads(agent_tools.lookup_service(s))
        except Exception:
            continue
        name = (parsed.get("name") or "").strip().lower()
        if name:
            out.add(name)
        for alias in (parsed.get("aliases") or []):
            out.add(str(alias).strip().lower())
    return out


# --- response assembly --------------------------------------------------------

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _extract_agent_json(raw: str) -> Optional[dict]:
    """Pull the JSON object from the agent's answer. Tolerates a stray code fence."""
    if not raw:
        return None
    txt = raw.strip()
    # strip markdown fence if model added one despite instructions
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt)
    try:
        return json.loads(txt)
    except Exception:
        pass
    m = _JSON_BLOCK.search(txt)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _fallback_callback(req: JarvisInvestigationRequest, investigation_id: str,
                       reason: str, cost_usd: float | None = None) -> JarvisInvestigationCallbackPayload:
    """When the agent fails or returns unparseable output, ship a structurally
    valid payload that escalates to SRE — never silent failure."""
    audit_ref = secrets.token_hex(12)
    return JarvisInvestigationCallbackPayload(
        investigation_id=investigation_id,
        request_id=req.request_id,
        completed_at=_now_iso(),
        status="FAILED",
        findings=Findings(
            root_cause_hypothesis=(
                f"Jarvis investigation could not produce a grounded hypothesis "
                f"({reason}). SRE escalation recommended."
            ),
            issue_category="infrastructure_fault",
            findings_confidence="low",
            confidence_reason="weak_hit",
            affected_services=[],
            evidence=[],
        ),
        recommended_actions=[
            RecommendedAction(
                action_id="esc_1",
                type="requires_sre",
                description="Escalate to SRE desk for manual diagnosis.",
                actions_confidence="low",
                routing="sre_escalation_router",
                escalation_reason=f"Jarvis investigation failed: {reason}",
                payload=ActionPayload(
                    database_contexts=[
                        DatabaseContext(
                            context_id="esc_db_1",
                            target_db="unknown",
                            raw_sql=f"-- Manual investigation required. Original issue: {req.issue_description[:300]}",
                        )
                    ]
                ),
            )
        ],
        audit_ref=audit_ref,
        cost_usd=cost_usd,
    )


def _build_callback(req: JarvisInvestigationRequest, investigation_id: str,
                    agent_json: dict, tool_calls: list[dict],
                    cost_usd: float | None = None) -> JarvisInvestigationCallbackPayload:
    """Take the agent's JSON output + tool_calls trace; assemble the final
    callback payload with deterministic confidence ordinals layered on top."""

    findings_conf, confidence_reason = _derive_findings_confidence(agent_json, tool_calls)
    audit_ref = secrets.token_hex(12)

    # Build findings — root_cause_hypothesis enforced ≥10 chars by pydantic
    raw_hypothesis = str(agent_json.get("root_cause_hypothesis") or "").strip()
    if len(raw_hypothesis) < 10:
        raw_hypothesis = (raw_hypothesis + " [Insufficient grounding for high-confidence diagnosis.]").strip()

    issue_category = agent_json.get("issue_category") or "infrastructure_fault"
    if issue_category not in IssueCategory.__args__:  # type: ignore[attr-defined]
        issue_category = "infrastructure_fault"

    evidence_raw = agent_json.get("evidence") or []
    evidence: list[EvidenceItem] = []
    for e in evidence_raw:
        if not isinstance(e, dict):
            continue
        etype = e.get("type")
        if etype not in EvidenceType.__args__:  # type: ignore[attr-defined]
            continue
        evidence.append(EvidenceItem(
            type=etype,
            repos=[str(r) for r in (e.get("repos") or [])][:10],
            files=[str(f) for f in (e.get("files") or [])][:20] or None,
        ))

    findings = Findings(
        root_cause_hypothesis=raw_hypothesis,
        issue_category=issue_category,  # type: ignore[arg-type]
        findings_confidence=findings_conf,
        confidence_reason=confidence_reason,
        affected_services=[str(s) for s in (agent_json.get("affected_services") or [])][:20],
        evidence=evidence,
    )

    # Build actions with per-context + per-action confidence overlays
    actions_raw = agent_json.get("recommended_actions") or []
    actions: list[RecommendedAction] = []
    for i, a in enumerate(actions_raw):
        if not isinstance(a, dict):
            continue
        payload_raw = a.get("payload") or {}
        api_ctxs_raw = payload_raw.get("api_contexts") or []
        db_ctxs_raw = payload_raw.get("database_contexts") or []

        # Per-action registry check — validates each declared service exists.
        registry_services = _registry_services_for_action(a)
        action_evidence = agent_json.get("evidence") or []

        api_ctxs: list[ApiContext] = []
        for j, c in enumerate(api_ctxs_raw):
            if not isinstance(c, dict):
                continue
            method = c.get("method") or "GET"
            if method not in HttpMethod.__args__:  # type: ignore[attr-defined]
                method = "GET"
            ordinal = _derive_api_confidence(c, action_evidence, registry_services)
            api_ctxs.append(ApiContext(
                context_id=str(c.get("context_id") or f"ctx_a_{i}_{j}"),
                method=method,  # type: ignore[arg-type]
                service=str(c.get("service") or ""),
                endpoint=str(c.get("endpoint") or ""),
                api_confidence=ordinal,
                headers=c.get("headers") if isinstance(c.get("headers"), dict) else None,
                body_json=c.get("body_json") if isinstance(c.get("body_json"), dict) else None,
            ))

        db_ctxs: list[DatabaseContext] = []
        for j, c in enumerate(db_ctxs_raw):
            if not isinstance(c, dict):
                continue
            db_ctxs.append(DatabaseContext(
                context_id=str(c.get("context_id") or f"ctx_d_{i}_{j}"),
                target_db=str(c.get("target_db") or "unknown"),
                raw_sql=str(c.get("raw_sql") or "-- no SQL provided"),
            ))

        if not api_ctxs and not db_ctxs:
            # Hard rule: every action needs ≥1 context — skip malformed ones
            continue

        atype = a.get("type") or "requires_sre"
        if atype not in ActionType.__args__:  # type: ignore[attr-defined]
            atype = "requires_sre"

        routing = a.get("routing")
        if routing not in ActionRouting.__args__:  # type: ignore[attr-defined]
            routing = "sre_escalation_router" if atype == "requires_sre" else "resolution_executor"

        action_conf = _derive_action_confidence(
            {"payload": {"api_contexts": [ac.model_dump() for ac in api_ctxs]}}, findings_conf,
        )

        escalation_reason = a.get("escalation_reason")
        if atype == "requires_sre" and not escalation_reason:
            escalation_reason = "Action requires manual SRE intervention."

        actions.append(RecommendedAction(
            action_id=str(a.get("action_id") or f"act_{i+1}"),
            type=atype,  # type: ignore[arg-type]
            description=str(a.get("description") or "Investigate further.")[:1000],
            actions_confidence=action_conf,
            routing=routing,  # type: ignore[arg-type]
            private_note=(str(a.get("private_note"))[:2000] if a.get("private_note") else None),
            payload=ActionPayload(
                api_contexts=api_ctxs or None,
                database_contexts=db_ctxs or None,
            ),
            escalation_reason=str(escalation_reason)[:500] if escalation_reason else None,
        ))

    if not actions:
        # Anti-silent-failure constraint: must emit ≥1 action
        actions.append(RecommendedAction(
            action_id="esc_1",
            type="requires_sre",
            description="Escalate to SRE desk — no automatable steps identified.",
            actions_confidence="low",
            routing="sre_escalation_router",
            escalation_reason="No grounded recommended actions emitted.",
            payload=ActionPayload(
                database_contexts=[DatabaseContext(
                    context_id="esc_db_1", target_db="unknown",
                    raw_sql=f"-- Manual investigation required. Original issue: {(agent_json.get('root_cause_hypothesis') or '')[:300]}",
                )]
            ),
        ))

    return JarvisInvestigationCallbackPayload(
        investigation_id=investigation_id,
        request_id=req.request_id,
        completed_at=_now_iso(),
        status="COMPLETED",
        findings=findings,
        recommended_actions=actions,
        audit_ref=audit_ref,
        cost_usd=cost_usd,
    )


# --- async investigation runner ----------------------------------------------

async def create_investigation(req: JarvisInvestigationRequest, caller: str) -> dict:
    """Create + schedule an investigation job. Returns the job snapshot dict."""
    sem = await _get_semaphore()
    investigation_id = _new_id()
    now = _now_iso()

    async with _jobs_lock:
        _jobs[investigation_id] = {
            "investigation_id": investigation_id,
            "request_id": req.request_id,
            "channel": req.channel,
            "user_id": req.user_id,
            "caller": caller,
            "status": "QUEUED",
            "callback_url": req.callback_url,
            "issue_description_preview": req.issue_description[:300],
            "created_at": now,
        }

    _audit({
        "event": "received",
        "investigation_id": investigation_id,
        "request_id": req.request_id,
        "channel": req.channel,
        "caller": caller,
        "ts": now,
    })

    asyncio.create_task(_run_investigation(req, investigation_id, sem))
    snapshot = await get_investigation(investigation_id)
    return snapshot or {
        "investigation_id": investigation_id,
        "request_id": req.request_id,
        "status": "QUEUED",
        "created_at": now,
    }


async def _run_investigation(req: JarvisInvestigationRequest, investigation_id: str,
                             sem: asyncio.Semaphore) -> None:
    """Execute the investigation, derive ordinals, persist + fire callback."""
    async with sem:
        async with _jobs_lock:
            _jobs[investigation_id]["status"] = "PROCESSING"
            _jobs[investigation_id]["processing_started_at"] = _now_iso()

        _audit({
            "event": "start",
            "investigation_id": investigation_id,
            "request_id": req.request_id,
            "ts": _now_iso(),
        })

        # Run the agent in a thread (ask() is blocking — sync Anthropic client).
        from agent.agent import ask  # late-import to keep cold-load light
        loop = asyncio.get_running_loop()

        callback_payload: Optional[JarvisInvestigationCallbackPayload] = None
        elapsed = 0.0
        cost_usd: float | None = None
        try:
            started = time.time()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, ask, _build_investigation_prompt(req)),
                timeout=_HARD_TIMEOUT_SEC,
            )
            elapsed = round(time.time() - started, 2)
            cost_usd = _est_cost_usd(result)
            # Two-pass: agent investigates in markdown (its native shape);
            # Haiku then structures the prose into the strict contract JSON.
            parsed = await loop.run_in_executor(
                None, _haiku_extract_structured, result.answer,
            )
            if parsed is None:
                callback_payload = _fallback_callback(req, investigation_id,
                                                     "haiku could not structure agent output",
                                                     cost_usd=cost_usd)
            else:
                callback_payload = _build_callback(req, investigation_id, parsed,
                                                   result.tool_calls or [],
                                                   cost_usd=cost_usd)
        except asyncio.TimeoutError:
            callback_payload = _fallback_callback(req, investigation_id,
                                                  f"agent timed out after {_HARD_TIMEOUT_SEC}s")
        except Exception as e:
            logger.exception("autosupport investigation failed: %s", investigation_id)
            callback_payload = _fallback_callback(req, investigation_id,
                                                  f"agent error: {type(e).__name__}")

        async with _jobs_lock:
            _jobs[investigation_id]["status"] = callback_payload.status
            _jobs[investigation_id]["completed_at"] = callback_payload.completed_at
            _jobs[investigation_id]["callback_payload"] = callback_payload.model_dump()
            _jobs[investigation_id]["elapsed_sec"] = elapsed
            _jobs[investigation_id]["cost_usd"] = callback_payload.cost_usd

        _audit({
            "event": "completed",
            "investigation_id": investigation_id,
            "request_id": req.request_id,
            "status": callback_payload.status,
            "findings_confidence": callback_payload.findings.findings_confidence,
            "n_actions": len(callback_payload.recommended_actions),
            "elapsed_sec": elapsed,
            "cost_usd": callback_payload.cost_usd,
            "ts": _now_iso(),
        })

        # Fire the callback (best-effort, single attempt)
        await _maybe_fire_callback(investigation_id, callback_payload)


async def _maybe_fire_callback(investigation_id: str,
                               payload: JarvisInvestigationCallbackPayload) -> None:
    snapshot = await get_investigation(investigation_id)
    url = (snapshot or {}).get("callback_url")
    if not url:
        return
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _post_callback, url, payload.model_dump(),
                                            investigation_id)
        _log_callback({**result, "investigation_id": investigation_id, "url": url, "ts": _now_iso()})
        if result.get("ok"):
            logger.info("autosupport callback fired for %s → %s (HTTP %s)",
                        investigation_id, url, result.get("status"))
        else:
            logger.warning("autosupport callback failed for %s → %s: %s",
                           investigation_id, url, result.get("error"))
    except Exception as e:
        logger.exception("autosupport callback runner crashed for %s", investigation_id)
        _log_callback({"investigation_id": investigation_id, "url": url, "ts": _now_iso(),
                       "ok": False, "error": f"{type(e).__name__}: {e!s}"})


def _post_callback(url: str, payload: dict, investigation_id: str) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Jarvis-Investigation-Id": investigation_id,
            "X-Jarvis-Event": f"investigation.{payload.get('status', '').lower()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_CALLBACK_TIMEOUT_SEC) as r:
            return {"ok": True, "status": r.status}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code,
                "error": f"HTTPError {e.code}: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e!s}"}


def _log_callback(record: dict) -> None:
    try:
        _CALLBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _CALLBACK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("autosupport callback log write failed")


async def get_investigation(investigation_id: str) -> Optional[dict]:
    async with _jobs_lock:
        snap = _jobs.get(investigation_id)
        return dict(snap) if snap else None


# Idempotency -------------

async def check_idempotency_key(key: str) -> Optional[str]:
    cutoff = time.time() - _IDEMPOTENCY_WINDOW_SEC
    async with _jobs_lock:
        stale = [k for k, (_, ts) in _idempotency_keys.items() if ts < cutoff]
        for k in stale:
            _idempotency_keys.pop(k, None)
        hit = _idempotency_keys.get(key)
        return hit[0] if hit else None


async def record_idempotency_key(key: str, investigation_id: str) -> None:
    async with _jobs_lock:
        _idempotency_keys[key] = (investigation_id, time.time())


# --- sync endpoint: drift check on recommended actions -----------------------

def run_sync(req: JarvisSyncRequest) -> JarvisSyncResponse:
    """For each requested action, re-look-up the services in the registry.

    If a service moved to a different in-cluster / cross-cluster URL, populate
    updated_services + updated_payload (with rewritten endpoints). Else mirror
    the request fields back with update_required=False.

    This is best-effort drift detection. A truly stale endpoint inside a feign
    client's path can't be caught without re-running a full investigation; we
    detect service-level moves only.
    """
    from agent import tools as agent_tools  # late import

    audit_ref = secrets.token_hex(12)
    results: list[SyncResult] = []

    for a in req.action_ids:
        updated_services: list[str] = []
        services_changed = False
        for svc in a.services:
            try:
                hit = json.loads(agent_tools.lookup_service(svc))
            except Exception:
                hit = {}
            canonical = (hit.get("name") or svc).strip()
            updated_services.append(canonical)
            if canonical.lower() != svc.lower():
                services_changed = True

        # If any service name canonicalized, also update api_contexts.service
        updated_payload: Optional[ActionPayload] = None
        if services_changed and a.payload.api_contexts:
            new_ctxs: list[ApiContext] = []
            for ctx in a.payload.api_contexts:
                svc_lower = (ctx.service or "").lower()
                canonical_match = next(
                    (u for u in updated_services if u.lower() == svc_lower), ctx.service,
                )
                new_ctxs.append(ApiContext(
                    context_id=ctx.context_id,
                    method=ctx.method,
                    service=canonical_match,
                    endpoint=ctx.endpoint,
                    api_confidence=ctx.api_confidence,
                    headers=ctx.headers,
                    body_json=ctx.body_json,
                ))
            updated_payload = ActionPayload(
                api_contexts=new_ctxs,
                database_contexts=a.payload.database_contexts,
            )

        if services_changed:
            results.append(SyncResult(
                action_id=a.action_id,
                update_required=True,
                updated_services=updated_services,
                updated_payload=updated_payload or a.payload,
            ))
        else:
            results.append(SyncResult(
                action_id=a.action_id,
                update_required=False,
            ))

    _audit({
        "event": "sync",
        "n_actions": len(req.action_ids),
        "n_with_drift": sum(1 for r in results if r.update_required),
        "audit_ref": audit_ref,
        "ts": _now_iso(),
    })

    return JarvisSyncResponse(
        sync_results=results,
        synced_at=_now_iso(),
        audit_ref=audit_ref,
    )


def estimated_duration_sec() -> int:
    return _ESTIMATED_DURATION_SEC
