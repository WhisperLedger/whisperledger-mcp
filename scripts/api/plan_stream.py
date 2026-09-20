"""HTTP transport for Jarvis's standalone implementation-planning agent.

The router is assembled by ``api.server`` so it can reuse the existing auth,
audit-log, and Portal-spend hooks without importing server globals back into
this module.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Literal

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.plan_agent import (
    PlanDoneEvent,
    PlanEvent,
    PlanPhaseEvent,
    PlanReadyEvent,
    PlanRequest,
    PlanScopeResolvedEvent,
    PlanToolCalledEvent,
    PlanToolResultEvent,
    PlanTurn,
    run_plan_streaming,
)

_HEARTBEAT = ": heartbeat\n\n"
_HEARTBEAT_INTERVAL_SEC = 5.0


class PlanTurnRequest(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4_000)


class PlanStreamRequest(BaseModel):
    """Context supplied by Merlin for a new, refined, or replanned plan."""

    session_id: str | None = Field(None, alias="sessionId", max_length=128)
    intent: Literal["create", "refine", "replan"] = "create"
    session_summary: str = Field("", alias="sessionSummary", max_length=12_000)
    recent_turns: list[PlanTurnRequest] = Field(default_factory=list, alias="recentTurns", max_length=6)
    current_prompt: str = Field(..., alias="currentPrompt", min_length=4, max_length=16_000)
    previous_plan: dict[str, Any] | None = Field(None, alias="previousPlan")
    feedback: str | None = Field(None, max_length=8_000)
    max_cost_usd: float = Field(5.0, alias="maxCostUsd", gt=0, le=5.0)

    class Config:
        allow_population_by_field_name = True

    def to_agent_request(self) -> PlanRequest:
        return PlanRequest(
            session_id=self.session_id,
            intent=self.intent,
            session_summary=self.session_summary,
            recent_turns=tuple(PlanTurn(role=turn.role, content=turn.content) for turn in self.recent_turns),
            current_prompt=self.current_prompt,
            previous_plan=self.previous_plan,
            feedback=self.feedback,
            max_cost_usd=self.max_cost_usd,
        )


def _sse(event_name: str, payload: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_from_event(event: PlanEvent) -> str:
    if isinstance(event, PlanPhaseEvent):
        return _sse("phase", {"phase": event.phase, "label": event.label})
    if isinstance(event, PlanToolCalledEvent):
        return _sse("tool_called", {"name": event.name, "label": event.label, "args": event.args})
    if isinstance(event, PlanToolResultEvent):
        return _sse("tool_result", {
            "name": event.name,
            "label": event.label,
            "preview": event.preview,
            "chars": event.chars,
            "isError": event.is_error,
        })
    if isinstance(event, PlanScopeResolvedEvent):
        return _sse("scope_resolved", event.scope)
    if isinstance(event, PlanReadyEvent):
        repo = (event.plan.get("scope") or {}).get("primaryRepo") or None
        payload: dict[str, Any] = {"plan": event.plan}
        if repo:
            payload["repo"] = repo
        return _sse("plan_ready", payload)
    if isinstance(event, PlanDoneEvent):
        return _sse("done", {
            "iterations": event.iterations,
            "toolCallsCount": event.tool_calls_count,
            "inputTokens": event.input_tokens,
            "outputTokens": event.output_tokens,
            "cacheReadTokens": event.cache_read_tokens,
            "cacheCreationTokens": event.cache_creation_tokens,
            "elapsedSec": event.elapsed_sec,
            "estimatedCostUsd": event.estimated_cost_usd,
            "budgetUsd": event.budget_usd,
            "budgetRemainingUsd": round(event.budget_usd - event.estimated_cost_usd, 4),
            "scope": event.scope,
        })
    return ""


def create_plan_router(
    *,
    check_auth: Callable[[str | None], Any],
    log_request: Callable[[dict[str, Any]], None],
    record_spend: Callable[[str, float], Any] | None,
    now_iso: Callable[[], str],
) -> APIRouter:
    """Build the plan router with server-owned auth and accounting hooks."""
    router = APIRouter()

    @router.post("/api/v1/plan/stream")
    async def plan_stream_endpoint(
        req: PlanStreamRequest,
        authorization: str | None = Header(default=None),
        x_jarvis_caller: str | None = Header(default=None, alias="X-Jarvis-Caller"),
    ):
        """Generate a context-aware, repository-discovering implementation plan."""
        user = check_auth(authorization)
        caller = (
            (user.email if user else None)
            or (x_jarvis_caller or "anonymous").strip()[:64]
            or "anonymous"
        )
        if user is not None and user.budget_remaining_usd <= 0:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "daily_budget_exceeded",
                    "email": user.email,
                    "daily_budget_usd": user.daily_budget_usd,
                    "spent_today_usd": user.spent_today_usd,
                },
            )
        if user is not None and req.max_cost_usd > user.budget_remaining_usd:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "daily_budget_would_be_exceeded",
                    "endpoint": "plan/stream",
                    "requested_usd": req.max_cost_usd,
                    "budget_remaining_usd": user.budget_remaining_usd,
                    "daily_budget_usd": user.daily_budget_usd,
                    "email": user.email,
                },
            )

        caller_id = (user.email if user else None) or "api:anonymous"
        loop = asyncio.get_event_loop()
        event_queue: asyncio.Queue[PlanEvent | Exception | None] = asyncio.Queue()

        def on_event(event: PlanEvent) -> None:
            loop.call_soon_threadsafe(event_queue.put_nowait, event)

        def run_agent() -> None:
            try:
                run_plan_streaming(req.to_agent_request(), on_event, caller_id=caller_id)
            except Exception as exc:
                loop.call_soon_threadsafe(event_queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(event_queue.put_nowait, None)

        loop.run_in_executor(None, run_agent)

        async def event_stream():
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(event_queue.get(), timeout=_HEARTBEAT_INTERVAL_SEC)
                    except asyncio.TimeoutError:
                        yield _HEARTBEAT
                        continue

                    if item is None:
                        return
                    if isinstance(item, Exception):
                        yield _sse("error", {"error": str(item)})
                        return

                    line = _sse_from_event(item)
                    if line:
                        yield line

                    if isinstance(item, PlanDoneEvent):
                        log_request({
                            "endpoint": "plan/stream",
                            "ts": now_iso(),
                            "session_id": req.session_id,
                            "intent": req.intent,
                            "current_prompt_preview": req.current_prompt[:200],
                            "scope": item.scope,
                            "iterations": item.iterations,
                            "tool_calls": item.tool_calls_count,
                            "elapsed_sec": item.elapsed_sec,
                            "cost_usd": item.estimated_cost_usd,
                            "input_tokens": item.input_tokens,
                            "output_tokens": item.output_tokens,
                            "cache_creation_tokens": item.cache_creation_tokens,
                            "cache_read_tokens": item.cache_read_tokens,
                            "caller": caller,
                        })
                        if user is not None and record_spend is not None:
                            try:
                                record_spend(user.user_id, item.estimated_cost_usd)
                            except Exception:
                                # Accounting failure must not turn a complete plan into an error.
                                import logging
                                logging.getLogger("jarvis.api.plan_stream").exception("portal record_spend failed")
                        return
            except GeneratorExit:
                # Keep parity with /ask/stream: a client disconnect does not
                # interrupt an in-flight model turn on the worker thread.
                pass

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return router
