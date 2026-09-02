from __future__ import annotations
from app.api.legacy_meeting import reject_legacy_meeting_write

import datetime as dt
import hashlib
import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)
from sqlalchemy.orm import Session

from app.api.dependencies import (
    get_app_settings,
    get_assistant_runtime,
    get_db_session,
)
from app.assistant.bootstrap import AssistantRuntime
from app.assistant.context import ContextBuilder
from app.assistant.fast_runner import FastTurnRequest
from app.assistant.repository import (
    AssistantIdempotencyConflictError,
    AssistantRepository,
    AssistantStateConflictError,
)
from app.assistant.state_machine import is_terminal_execution_status
from app.persistence.models import (
    AssistantEventRecord,
    AssistantExecutionRecord,
    AssistantStepRecord,
    AssistantToolCallRecord,
    SessionRecord,
)
from app.settings import Settings


router = APIRouter(prefix="/api", tags=["assistant"])
logger = logging.getLogger(__name__)


class FrozenApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TurnGrantRequest(FrozenApiModel):
    actor_id: str = Field(min_length=1, max_length=255)
    capabilities: tuple[str, ...] = Field(min_length=1, max_length=20)
    candidate_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    max_side_effects: int = Field(ge=1, le=20)
    expires_at: dt.datetime
    unresolved_identity_policy: Literal["placeholder"] = "placeholder"

    @model_validator(mode="after")
    def validate_scope(self) -> TurnGrantRequest:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("Grant capabilities must be unique")
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("Grant Candidate IDs must be unique")
        if "task.create" not in self.capabilities:
            raise ValueError("Execute mode requires the task.create capability")
        return self


class AssistantTurnCreate(FrozenApiModel):
    intent_mode: Literal["ask", "execute"]
    message: str = Field(min_length=1, max_length=4_000)
    client_request_id: str = Field(min_length=1, max_length=255)
    actor_id: str = Field(default="local-user", min_length=1, max_length=255)
    mark_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    allow_handoff: bool = True
    grant: TurnGrantRequest | None = None

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Assistant message must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_mode(self) -> AssistantTurnCreate:
        if self.intent_mode == "ask" and self.grant is not None:
            raise ValueError("Ask mode cannot carry an external-write Grant")
        if self.intent_mode == "execute" and self.grant is None:
            raise ValueError("Execute mode requires an external-write Grant")
        if self.grant is not None and self.grant.actor_id != self.actor_id:
            raise ValueError("Grant actor must match the requesting actor")
        return self


class NeedsInputSummary(FrozenApiModel):
    question: str
    choices: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    error_code: str | None = None


class ExternalEffectsSummary(FrozenApiModel):
    confirmed: int = Field(ge=0)
    unknown: int = Field(ge=0)
    existing_actions_remain: bool


class AssistantExecutionSummary(FrozenApiModel):
    execution_id: str
    session_id: str
    profile: Literal["fast_turn", "action_run"]
    root_execution_id: str
    parent_execution_id: str | None
    snapshot_id: str | None
    grant_id: str | None
    client_request_id: str | None
    goal: str
    status: str
    state_version: int
    step_count: int
    result: dict[str, Any] | None
    needs_input: NeedsInputSummary | None
    external_effects: ExternalEffectsSummary
    error_code: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    completed_at: dt.datetime | None


class AssistantTurnAccepted(FrozenApiModel):
    execution: AssistantExecutionSummary
    event_cursor: int = Field(ge=0)


class AssistantSessionState(FrozenApiModel):
    active_executions: tuple[AssistantExecutionSummary, ...]
    recent_terminal_executions: tuple[AssistantExecutionSummary, ...]
    snapshot_cursor: int = Field(ge=0)


class AssistantEventResponse(FrozenApiModel):
    event_id: int
    session_id: str
    execution_id: str
    root_execution_id: str
    state_version: int
    schema_version: int
    event_type: str
    phase: str | None
    status: str
    summary: str
    payload: dict[str, JsonValue]
    created_at: dt.datetime


class AssistantEventsPage(FrozenApiModel):
    events: tuple[AssistantEventResponse, ...]
    next_cursor: int = Field(ge=0)
    has_more: bool


class AssistantStepSummary(FrozenApiModel):
    sequence: int
    kind: str
    decision_summary: str | None
    created_at: dt.datetime


class AssistantToolCallSummary(FrozenApiModel):
    tool_call_id: str
    tool_name: str
    capability: str
    effect: str
    status: str
    external_reference: dict[str, Any] | None
    error_code: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class AssistantExecutionDetail(FrozenApiModel):
    execution: AssistantExecutionSummary
    steps: tuple[AssistantStepSummary, ...]
    tool_calls: tuple[AssistantToolCallSummary, ...]


class AssistantInputRequest(FrozenApiModel):
    client_operation_id: str = Field(min_length=1, max_length=255)
    expected_state_version: int = Field(ge=1)
    input: str = Field(min_length=1, max_length=4_000)

    @field_validator("input")
    @classmethod
    def normalize_input(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Assistant input must not be blank")
        return normalized


class AssistantCancelRequest(FrozenApiModel):
    client_operation_id: str = Field(min_length=1, max_length=255)
    expected_state_version: int = Field(ge=1)


class AssistantOperationResponse(FrozenApiModel):
    execution: AssistantExecutionSummary


def _require_session(db_session: Session, session_id: str) -> None:
    if db_session.get(SessionRecord, session_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )


def _public_json(value: Any) -> Any:
    hidden_keys = {
        "chain_of_thought",
        "hidden_reasoning",
        "provider_error",
        "raw_error",
        "traceback",
        "intent_token", "admin_token", "ui_nonce", "api_key", "authorization",
        "cookie", "password", "secret",
    }
    if isinstance(value, dict):
        return {
            str(key): _public_json(item)
            for key, item in value.items()
            if str(key).casefold() not in hidden_keys
        }
    if isinstance(value, (list, tuple)):
        return [_public_json(item) for item in value]
    return value


def _external_effects(
    repository: AssistantRepository,
    execution_id: str,
) -> ExternalEffectsSummary:
    calls = repository.list_tool_calls(execution_id, effect="external_write")
    confirmed = 0
    unknown = 0
    for call in calls:
        result = dict(call.result_json or {})
        if call.status == "succeeded":
            confirmed += max(0, int(result.get("confirmed_side_effects") or 0))
        elif call.status in {"requesting", "pending", "unknown", "reconciling"}:
            unknown += 1
    return ExternalEffectsSummary(
        confirmed=confirmed,
        unknown=unknown,
        existing_actions_remain=confirmed > 0,
    )


def _needs_input(record: AssistantExecutionRecord) -> NeedsInputSummary | None:
    if record.status != "needs_input":
        return None
    result = dict(record.result_json or {})
    question = result.get("question")
    if not isinstance(question, str) or not question.strip():
        question = "This action needs more information."
    choices = tuple(
        str(value) for value in result.get("choices", ()) if str(value).strip()
    )
    evidence_refs = tuple(
        str(value)
        for value in result.get("evidence_refs", ())
        if str(value).strip()
    )
    error_code = result.get("error_code")
    return NeedsInputSummary(
        question=question,
        choices=choices,
        evidence_refs=evidence_refs,
        error_code=error_code if isinstance(error_code, str) else None,
    )


def _execution_summary(
    repository: AssistantRepository,
    record: AssistantExecutionRecord,
) -> AssistantExecutionSummary:
    return AssistantExecutionSummary(
        execution_id=record.id,
        session_id=record.session_id,
        profile=record.profile,
        root_execution_id=record.root_execution_id,
        parent_execution_id=record.parent_execution_id,
        snapshot_id=record.snapshot_id,
        grant_id=record.grant_id,
        client_request_id=record.client_request_id,
        goal=record.goal,
        status=record.status,
        state_version=record.state_version,
        step_count=record.step_count,
        result=(
            _public_json(dict(record.result_json))
            if record.result_json is not None
            else None
        ),
        needs_input=_needs_input(record),
        external_effects=_external_effects(repository, record.id),
        error_code=record.error_code,
        created_at=record.created_at,
        updated_at=record.updated_at,
        completed_at=record.completed_at,
    )


def _event_response(record: AssistantEventRecord) -> AssistantEventResponse:
    return AssistantEventResponse(
        event_id=record.id,
        session_id=record.session_id,
        execution_id=record.execution_id,
        root_execution_id=record.root_execution_id,
        state_version=record.state_version,
        schema_version=record.schema_version,
        event_type=record.event_type,
        phase=record.phase,
        status=record.status,
        summary=record.summary,
        payload=_public_json(dict(record.payload_json)),
        created_at=record.created_at,
    )


def _step_summary(record: AssistantStepRecord) -> AssistantStepSummary:
    return AssistantStepSummary(
        sequence=record.sequence,
        kind=record.kind,
        decision_summary=record.decision_summary,
        created_at=record.created_at,
    )


def _tool_call_summary(record: AssistantToolCallRecord) -> AssistantToolCallSummary:
    return AssistantToolCallSummary(
        tool_call_id=record.id,
        tool_name=record.tool_name,
        capability=record.capability,
        effect=record.effect,
        status=record.status,
        external_reference=(
            _public_json(dict(record.external_reference_json))
            if record.external_reference_json is not None
            else None
        ),
        error_code=record.error_code,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _operation_hash(kind: Literal["input", "cancel"], payload: BaseModel) -> str:
    canonical = json.dumps(
        {"kind": kind, "request": payload.model_dump(mode="json")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _operation_replay(
    repository: AssistantRepository,
    *,
    execution_id: str,
    client_operation_id: str,
    kind: Literal["input", "cancel"],
    request_hash: str,
) -> AssistantOperationResponse | None:
    existing = repository.get_client_operation(
        execution_id=execution_id,
        client_operation_id=client_operation_id,
    )
    if existing is None:
        return None
    if existing.kind != kind or existing.request_hash != request_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "client_operation_conflict",
                "message": "Client operation ID is bound to another request.",
            },
        )
    return AssistantOperationResponse.model_validate(existing.response_json)


def _state_conflict(
    repository: AssistantRepository,
    record: AssistantExecutionRecord,
    *,
    code: str = "stale_state_version",
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": code,
            "latest_execution": _execution_summary(
                repository,
                record,
            ).model_dump(mode="json"),
        },
    )


def _validate_grant_expiry(
    grant: TurnGrantRequest,
    settings: Settings,
) -> None:
    if grant.expires_at.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Grant expiration must include a timezone",
        )
    now = dt.datetime.now(dt.UTC)
    expires_at = grant.expires_at.astimezone(dt.UTC)
    if expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Grant expiration must be in the future",
        )
    if expires_at > now + dt.timedelta(
        seconds=settings.assistant_grant_max_ttl_seconds
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Grant expiration exceeds the configured maximum",
        )


async def _enqueue(
    runtime: AssistantRuntime,
    execution_id: str,
    *,
    resume: bool = False,
) -> bool:
    try:
        if resume:
            await runtime.action_runtime.scheduler.resume(execution_id)
        else:
            await runtime.action_runtime.scheduler.enqueue(execution_id)
        return True
    except RuntimeError:
        logger.warning(
            "Action Run %s is durable but could not be scheduled; startup "
            "recovery will retry it",
            execution_id,
        )
        return False


async def _reschedule_if_active(
    runtime: AssistantRuntime,
    record: AssistantExecutionRecord,
) -> None:
    if record.profile != "action_run" or record.status not in {
        "queued",
        "planning",
        "observing",
        "waiting_external",
        "reconciling",
    }:
        return
    await _enqueue(
        runtime,
        record.id,
        resume=record.status == "planning",
    )


@router.post(
    "/sessions/{session_id}/assistant/turns",
    dependencies=[Depends(reject_legacy_meeting_write)],
    response_model=AssistantTurnAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_assistant_turn(
    session_id: str,
    payload: AssistantTurnCreate,
    runtime: AssistantRuntime = Depends(get_assistant_runtime),
    settings: Settings = Depends(get_app_settings),
    db_session: Session = Depends(get_db_session),
) -> AssistantTurnAccepted:
    _require_session(db_session, session_id)
    repository = AssistantRepository(db_session)
    existing = repository.get_execution_by_client_request(
        session_id=session_id,
        client_request_id=payload.client_request_id,
    )
    expected_profile = (
        "fast_turn" if payload.intent_mode == "ask" else "action_run"
    )
    if existing is not None and (
        existing.profile != expected_profile or existing.goal != payload.message
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "client_request_conflict",
                "message": "Client request ID is bound to another turn.",
            },
        )
    if existing is not None and not (
        payload.intent_mode == "ask"
        and existing.profile == "fast_turn"
        and existing.status == "received"
    ):
        await _reschedule_if_active(runtime, existing)
        return AssistantTurnAccepted(
            execution=_execution_summary(repository, existing),
            event_cursor=repository.current_event_cursor(),
        )

    if payload.intent_mode == "ask":
        db_session.rollback()
        await runtime.fast_runner.run(
            FastTurnRequest(
                session_id=session_id,
                goal=payload.message,
                actor_id=payload.actor_id,
                client_request_id=payload.client_request_id,
                mark_ids=payload.mark_ids,
                allow_handoff=payload.allow_handoff,
            )
        )
        repository = AssistantRepository(db_session)
        execution = repository.get_execution_by_client_request(
            session_id=session_id,
            client_request_id=payload.client_request_id,
        )
        if execution is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Assistant Fast Turn did not persist an execution",
            )
        return AssistantTurnAccepted(
            execution=_execution_summary(repository, execution),
            event_cursor=repository.current_event_cursor(),
        )

    grant_request = payload.grant
    assert grant_request is not None
    if runtime.task_adapter.provider_name == "disabled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "task_system_disabled"},
        )
    _validate_grant_expiry(grant_request, settings)
    try:
        context = await ContextBuilder(
            db_session,
            projector=runtime.projector,
            stale_after_seconds=settings.meeting_state_stale_after_seconds,
        ).build(
            session_id=session_id,
            goal=payload.message,
            mark_ids=payload.mark_ids,
            candidate_ids=grant_request.candidate_ids,
            actor_id=payload.actor_id,
            persist=True,
        )
    except (LookupError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error
    team_id = runtime.task_adapter.connection.team_id
    grant = repository.create_grant(
        session_id=session_id,
        actor_id=grant_request.actor_id,
        goal=payload.message,
        capabilities=grant_request.capabilities,
        resource_scope={"linear_team_id": team_id},
        candidate_ids=grant_request.candidate_ids,
        max_side_effects=grant_request.max_side_effects,
        expires_at=grant_request.expires_at,
        linear_team_id=team_id,
        unresolved_identity_policy=grant_request.unresolved_identity_policy,
    )
    execution = repository.create_execution(
        session_id=session_id,
        profile="action_run",
        goal=payload.message,
        snapshot_id=context.snapshot_id,
        grant_id=grant.id,
        client_request_id=payload.client_request_id,
        budget={
            "max_steps": settings.assistant_action_max_steps,
            "max_model_calls": settings.assistant_action_max_model_calls,
            "max_planning_rounds": settings.assistant_action_max_planning_rounds,
        },
    )
    if execution.grant_id != grant.id:
        db_session.rollback()
        repository = AssistantRepository(db_session)
        execution = repository.get_execution_by_client_request(
            session_id=session_id,
            client_request_id=payload.client_request_id,
        )
        if execution is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "client_request_race"},
            )
        await _reschedule_if_active(runtime, execution)
        return AssistantTurnAccepted(
            execution=_execution_summary(repository, execution),
            event_cursor=repository.current_event_cursor(),
        )
    repository.append_event(
        execution_id=execution.id,
        event_type="action.context_frozen",
        phase=execution.status,
        summary="Action Run context was frozen",
        payload={
            "snapshot_id": context.snapshot_id,
            "meeting_state_version": context.meeting_state_version,
            "evidence_count": len(context.evidence_refs),
        },
        state_version=execution.state_version,
    )
    db_session.commit()
    await _enqueue(runtime, execution.id)
    return AssistantTurnAccepted(
        execution=_execution_summary(repository, execution),
        event_cursor=repository.current_event_cursor(),
    )


@router.get(
    "/sessions/{session_id}/assistant/state",
    response_model=AssistantSessionState,
)
def get_assistant_state(
    session_id: str,
    db_session: Session = Depends(get_db_session),
) -> AssistantSessionState:
    _require_session(db_session, session_id)
    repository = AssistantRepository(db_session)
    records = repository.list_executions(session_id)
    active: list[AssistantExecutionSummary] = []
    terminal: list[AssistantExecutionSummary] = []
    for record in records:
        summary = _execution_summary(repository, record)
        if is_terminal_execution_status(record.profile, record.status):
            if len(terminal) < 20:
                terminal.append(summary)
        else:
            active.append(summary)
    return AssistantSessionState(
        active_executions=tuple(active),
        recent_terminal_executions=tuple(terminal),
        snapshot_cursor=repository.current_event_cursor(),
    )


@router.get(
    "/sessions/{session_id}/assistant/events",
    response_model=AssistantEventsPage,
)
def list_assistant_events(
    session_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1),
    db_session: Session = Depends(get_db_session),
) -> AssistantEventsPage:
    _require_session(db_session, session_id)
    repository = AssistantRepository(db_session)
    bounded_limit = min(limit, 100)
    records = repository.list_session_events(
        session_id,
        after=after,
        limit=bounded_limit,
    )
    page_cursor = records[-1].id if records else after
    has_more = repository.has_session_events_after(session_id, page_cursor)
    next_cursor = (
        page_cursor
        if has_more
        else max(page_cursor, repository.current_event_cursor())
    )
    return AssistantEventsPage(
        events=tuple(_event_response(record) for record in records),
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/assistant/executions/{execution_id}",
    response_model=AssistantExecutionDetail,
)
def get_assistant_execution(
    execution_id: str,
    db_session: Session = Depends(get_db_session),
) -> AssistantExecutionDetail:
    repository = AssistantRepository(db_session)
    record = repository.get_execution(execution_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant execution not found",
        )
    return AssistantExecutionDetail(
        execution=_execution_summary(repository, record),
        steps=tuple(_step_summary(item) for item in repository.list_steps(record.id)),
        tool_calls=tuple(
            _tool_call_summary(item)
            for item in repository.list_tool_calls(record.id)
        ),
    )


@router.post(
    "/assistant/executions/{execution_id}/input",
    dependencies=[Depends(reject_legacy_meeting_write)],
    response_model=AssistantOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_assistant_input(
    execution_id: str,
    payload: AssistantInputRequest,
    runtime: AssistantRuntime = Depends(get_assistant_runtime),
    db_session: Session = Depends(get_db_session),
) -> AssistantOperationResponse:
    repository = AssistantRepository(db_session)
    record = repository.get_execution(execution_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant execution not found",
        )
    request_hash = _operation_hash("input", payload)
    replay = _operation_replay(
        repository,
        execution_id=execution_id,
        client_operation_id=payload.client_operation_id,
        kind="input",
        request_hash=request_hash,
    )
    if replay is not None:
        latest = repository.get_execution_required(execution_id)
        await _reschedule_if_active(runtime, latest)
        return replay
    if record.state_version != payload.expected_state_version:
        raise _state_conflict(repository, record)
    if record.profile != "action_run" or record.status != "needs_input":
        raise _state_conflict(
            repository,
            record,
            code="execution_not_waiting_for_input",
        )
    grant = repository.get_grant(record.grant_id) if record.grant_id else None
    repository.append_observation(
        execution_id=record.id,
        source="user",
        source_ref=payload.client_operation_id,
        observation={
            "input": payload.input,
            "actor_id": grant.actor_id if grant is not None else "local-user",
        },
    )
    try:
        updated = repository.transition_execution(
            record.id,
            expected_version=payload.expected_state_version,
            target_status="planning",
            event_type="action.input_received",
            summary="Action Run received user input",
            payload={"client_operation_id": payload.client_operation_id},
            result={"input_received": True},
        )
    except AssistantStateConflictError as error:
        latest = repository.get_execution_required(record.id)
        raise _state_conflict(repository, latest) from error
    response = AssistantOperationResponse(
        execution=_execution_summary(repository, updated)
    )
    try:
        repository.record_client_operation(
            execution_id=record.id,
            client_operation_id=payload.client_operation_id,
            kind="input",
            request_hash=request_hash,
            response=response.model_dump(mode="json"),
        )
    except AssistantIdempotencyConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "client_operation_conflict"},
        ) from error
    db_session.commit()
    await _enqueue(runtime, record.id, resume=True)
    return response


@router.post(
    "/assistant/executions/{execution_id}/cancel",
    dependencies=[Depends(reject_legacy_meeting_write)],
    response_model=AssistantOperationResponse,
)
def cancel_assistant_execution(
    execution_id: str,
    payload: AssistantCancelRequest,
    _: AssistantRuntime = Depends(get_assistant_runtime),
    db_session: Session = Depends(get_db_session),
) -> AssistantOperationResponse:
    repository = AssistantRepository(db_session)
    record = repository.get_execution(execution_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant execution not found",
        )
    request_hash = _operation_hash("cancel", payload)
    replay = _operation_replay(
        repository,
        execution_id=execution_id,
        client_operation_id=payload.client_operation_id,
        kind="cancel",
        request_hash=request_hash,
    )
    if replay is not None:
        return replay
    if record.state_version != payload.expected_state_version:
        raise _state_conflict(repository, record)
    if is_terminal_execution_status(record.profile, record.status):
        raise _state_conflict(
            repository,
            record,
            code="execution_already_terminal",
        )
    effects = _external_effects(repository, record.id)
    result = dict(record.result_json or {})
    result.update(
        {
            "cancelled": True,
            "confirmed_external_side_effects": effects.confirmed,
            "unknown_external_side_effects": effects.unknown,
            "existing_external_actions_remain": effects.existing_actions_remain,
        }
    )
    try:
        updated = repository.transition_execution(
            record.id,
            expected_version=payload.expected_state_version,
            target_status="cancelled",
            event_type="execution.cancelled",
            summary="Execution cancelled by the user",
            payload={
                "confirmed_external_side_effects": effects.confirmed,
                "unknown_external_side_effects": effects.unknown,
            },
            result=result,
        )
    except AssistantStateConflictError as error:
        latest = repository.get_execution_required(record.id)
        raise _state_conflict(repository, latest) from error
    response = AssistantOperationResponse(
        execution=_execution_summary(repository, updated)
    )
    try:
        repository.record_client_operation(
            execution_id=record.id,
            client_operation_id=payload.client_operation_id,
            kind="cancel",
            request_hash=request_hash,
            response=response.model_dump(mode="json"),
        )
    except AssistantIdempotencyConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "client_operation_conflict"},
        ) from error
    db_session.commit()
    return response


__all__ = ["router"]
