from __future__ import annotations

import os
import uuid
import datetime as dt
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select

from app.persistence.models import (
    AssistantToolCallRecord,
    MeetingStateHeadRecord,
)


router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    process: Literal["api"] = "api"


class ReadinessCheck(BaseModel):
    ok: bool
    detail: str


class ProjectorFreshness(BaseModel):
    status: str
    session_count: int
    pending_segment_count: int
    worst_lag_ms: int
    last_success_at: dt.datetime | None


class AssistantHealth(BaseModel):
    enabled: bool
    runtime_started: bool
    projector: ProjectorFreshness
    fast_turn_queue_depth: int
    fast_turn_active_count: int
    action_run_queue_depth: int
    action_run_active_count: int
    action_run_oldest_queued_age_seconds: float | None
    tool_call_unknown_count: int
    tool_call_reconciling_count: int
    recovery_backlog_count: int
    task_adapter_provider: str
    task_adapter_available: bool


class PluginHealth(BaseModel):
    framework_enabled: bool
    container_runtime_available: bool
    installed_count: int
    enabled_count: int
    ready_count: int
    quarantined_count: int
    media_projector_status: str
    media_projector_lag: int
    rpc_pending_count: int


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    process: Literal["api"] = "api"
    checks: dict[str, ReadinessCheck]
    assistant: AssistantHealth
    plugins: PluginHealth


@router.get("/health", response_model=HealthResponse)
@router.get("/health/live", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@router.get("/health/ready", response_model=ReadinessResponse)
def readiness(request: Request, response: Response) -> ReadinessResponse:
    settings = request.app.state.settings
    database = request.app.state.database
    checks: dict[str, ReadinessCheck] = {
        "settings": ReadinessCheck(ok=True, detail="validated"),
        "livekit": ReadinessCheck(
            ok=all(
                (
                    settings.livekit_url.strip(),
                    settings.livekit_api_key.strip(),
                    settings.livekit_api_secret.strip(),
                )
            ),
            detail="configured",
        ),
    }

    try:
        database.check_read_write()
    except Exception as error:
        checks["database"] = ReadinessCheck(
            ok=False,
            detail=f"unavailable:{type(error).__name__}",
        )
    else:
        checks["database"] = ReadinessCheck(ok=True, detail="read_write")

    try:
        _check_directories(settings.persistent_directories)
    except OSError as error:
        checks["directories"] = ReadinessCheck(
            ok=False,
            detail=f"unavailable:{type(error).__name__}",
        )
    else:
        checks["directories"] = ReadinessCheck(
            ok=True,
            detail="read_write",
        )

    ready = all(check.ok for check in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        checks=checks,
        assistant=_assistant_health(request),
        plugins=_plugin_health(request),
    )


def _plugin_health(request: Request) -> PluginHealth:
    runtime = getattr(request.app.state, "plugin_host_runtime", None)
    if runtime is None:
        return PluginHealth(
            framework_enabled=False,
            container_runtime_available=False,
            installed_count=0,
            enabled_count=0,
            ready_count=0,
            quarantined_count=0,
            media_projector_status="unavailable",
            media_projector_lag=0,
            rpc_pending_count=0,
        )
    try:
        return PluginHealth.model_validate(runtime.health_snapshot())
    except Exception:
        return PluginHealth(
            framework_enabled=True,
            container_runtime_available=False,
            installed_count=0,
            enabled_count=0,
            ready_count=0,
            quarantined_count=0,
            media_projector_status="error",
            media_projector_lag=0,
            rpc_pending_count=0,
        )


def _assistant_health(request: Request) -> AssistantHealth:
    settings = request.app.state.settings
    runtime = getattr(request.app.state, "assistant_runtime", None)
    with request.app.state.database.session() as db_session:
        heads = list(db_session.scalars(select(MeetingStateHeadRecord)))
        tool_call_unknown_count = _tool_call_count(db_session, "unknown")
        tool_call_reconciling_count = _tool_call_count(
            db_session,
            "reconciling",
        )
        recovery_backlog_count = int(
            db_session.scalar(
                select(
                    func.count(
                        func.distinct(AssistantToolCallRecord.execution_id)
                    )
                ).where(
                    AssistantToolCallRecord.status.in_(
                        ("unknown", "reconciling")
                    )
                )
            )
            or 0
        )
    if runtime is None:
        return AssistantHealth(
            enabled=False,
            runtime_started=False,
            projector=ProjectorFreshness(
                status="disabled",
                session_count=0,
                pending_segment_count=0,
                worst_lag_ms=0,
                last_success_at=None,
            ),
            fast_turn_queue_depth=0,
            fast_turn_active_count=0,
            action_run_queue_depth=0,
            action_run_active_count=0,
            action_run_oldest_queued_age_seconds=None,
            tool_call_unknown_count=tool_call_unknown_count,
            tool_call_reconciling_count=tool_call_reconciling_count,
            recovery_backlog_count=recovery_backlog_count,
            task_adapter_provider=settings.task_system_provider,
            task_adapter_available=False,
        )

    scheduler = runtime.action_runtime.scheduler
    projector = runtime.projector
    freshness_status = "idle"
    if not projector.started:
        freshness_status = "stopped"
    elif heads:
        priority = {"ready": 0, "lagging": 1, "stale": 2, "rebuilding": 3}
        freshness_status = max(
            (head.status for head in heads),
            key=lambda value: priority.get(value, 4),
        )
    last_success_values = [
        head.last_success_at for head in heads if head.last_success_at is not None
    ]
    freshness = ProjectorFreshness(
        status=freshness_status,
        session_count=len(heads),
        pending_segment_count=sum(head.pending_segment_count for head in heads),
        worst_lag_ms=max((head.lag_ms for head in heads), default=0),
        last_success_at=max(last_success_values) if last_success_values else None,
    )
    return AssistantHealth(
        enabled=True,
        runtime_started=projector.started and scheduler.started,
        projector=freshness,
        fast_turn_queue_depth=runtime.fast_runner.queue_depth,
        fast_turn_active_count=runtime.fast_runner.active_count,
        action_run_queue_depth=scheduler.queue_depth,
        action_run_active_count=scheduler.active_count,
        action_run_oldest_queued_age_seconds=(
            scheduler.oldest_queued_age_seconds
        ),
        tool_call_unknown_count=tool_call_unknown_count,
        tool_call_reconciling_count=tool_call_reconciling_count,
        recovery_backlog_count=recovery_backlog_count,
        task_adapter_provider=settings.task_system_provider,
        task_adapter_available=(
            settings.task_system_configured
            and runtime.task_adapter.provider_name != "disabled"
        ),
    )


def _tool_call_count(db_session, status_value: str) -> int:
    return int(
        db_session.scalar(
            select(func.count(AssistantToolCallRecord.id)).where(
                AssistantToolCallRecord.status == status_value
            )
        )
        or 0
    )


def _check_directories(directories) -> None:
    for directory in directories:
        if not directory.is_dir():
            raise FileNotFoundError(str(directory))
        probe = directory / f".readiness-{uuid.uuid4().hex}.tmp"
        try:
            with probe.open("xb") as handle:
                handle.write(b"ready")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            probe.unlink(missing_ok=True)
