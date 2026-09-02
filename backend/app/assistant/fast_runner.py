from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.assistant.context import ContextBuilder
from app.assistant.handoff import (
    HandoffCompletedStep,
    HandoffObservation,
    HandoffService,
)
from app.assistant.models import ContextSnapshot, FastTurnStatus
from app.assistant.parser import (
    CompleteDecision,
    EvidenceClaim,
    HandoffDecision,
    InvokeToolsDecision,
    NeedsInputDecision,
    ProposedToolCall,
    RespondDecision,
)
from app.assistant.planner import (
    AvailableTool,
    PlannerOutcome,
    PlanningObservation,
)
from app.assistant.repository import AssistantRepository
from app.assistant.tools import ToolExecutor, ToolInvocation, ToolRegistry
from app.meeting_state.projector import MeetingStateProjector
from app.logging import log_assistant_lifecycle
from app.persistence.database import Database
from app.persistence.models import AssistantExecutionRecord


class FrozenFastModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FastTurnBudget(FrozenFastModel):
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    max_model_rounds: int = Field(default=2, ge=1, le=2)
    max_parallel_read_tools: int = Field(default=2, ge=1, le=2)
    max_external_writes: Literal[0] = 0


class FastTurnRequest(FrozenFastModel):
    session_id: str = Field(min_length=1, max_length=36)
    goal: str = Field(min_length=1, max_length=4_000)
    actor_id: str = Field(default="local-user", min_length=1, max_length=255)
    client_request_id: str | None = Field(default=None, max_length=255)
    mark_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    grant_id: str | None = Field(default=None, max_length=36)
    allow_handoff: bool = False

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Fast Turn goal must not be blank")
        return normalized


class FastTurnResult(FrozenFastModel):
    execution_id: str = Field(min_length=1, max_length=36)
    status: FastTurnStatus
    snapshot_id: str | None = Field(default=None, max_length=36)
    response_text: str | None = None
    claims: tuple[EvidenceClaim, ...] = Field(default_factory=tuple)
    handoff_execution_id: str | None = Field(default=None, max_length=36)
    error_code: str | None = Field(default=None, max_length=128)


class FastPlanner(Protocol):
    async def decide(
        self,
        *,
        profile: Literal["fast_turn"],
        goal: str,
        snapshot: ContextSnapshot,
        available_tools: Sequence[AvailableTool],
        observations: Sequence[PlanningObservation],
        remaining_budget: Mapping[str, object],
    ) -> PlannerOutcome: ...


@dataclass(slots=True)
class _RunProgress:
    snapshot: ContextSnapshot | None = None
    steps: list[HandoffCompletedStep] = field(default_factory=list)
    observations: list[PlanningObservation] = field(default_factory=list)


class FastTurnRunner:
    """Bounded private turn runner with no external-write capability."""

    def __init__(
        self,
        database: Database,
        planner: FastPlanner,
        handoff_service: HandoffService,
        *,
        projector: MeetingStateProjector | None = None,
        registry: ToolRegistry | None = None,
        tool_executor: ToolExecutor | None = None,
        budget: FastTurnBudget | None = None,
        execution_guard=None,
    ) -> None:
        if (registry is None) != (tool_executor is None):
            raise ValueError("Fast Turn registry and executor must be supplied together")
        self._database = database
        self._planner = planner
        self._handoff_service = handoff_service
        self._projector = projector
        self._registry = registry
        self._tool_executor = tool_executor
        self._budget = budget or FastTurnBudget()
        self._execution_guard = execution_guard
        self._active_count = 0

    def _check_authority(self, execution_id):
        if self._execution_guard is not None:
            self._execution_guard.check(execution_id, allow_unowned_read=True)

    @property
    def queue_depth(self) -> int:
        """Fast Turns execute directly under their request budget; no queue exists."""

        return 0

    @property
    def active_count(self) -> int:
        """Number of Fast Turns currently inside their bounded run."""

        return self._active_count

    async def run(
        self,
        request: FastTurnRequest | Mapping[str, object],
    ) -> FastTurnResult:
        started = time.perf_counter()
        self._active_count += 1
        try:
            result = await self._run(request)
            self._log_cycle(
                result,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return result
        finally:
            self._active_count -= 1

    def _log_cycle(self, result: FastTurnResult, *, duration_ms: int) -> None:
        with self._database.session() as db_session:
            execution = AssistantRepository(db_session).get_execution_required(
                result.execution_id
            )
            log_assistant_lifecycle(
                "assistant_fast_turn_cycle",
                session_id=execution.session_id,
                execution_id=execution.id,
                root_execution_id=execution.root_execution_id,
                status=result.status,
                elapsed_ms=duration_ms,
                phase=result.status,
                profile=execution.profile,
                snapshot_id=result.snapshot_id,
                error_code=result.error_code,
            )

    async def _run(
        self,
        request: FastTurnRequest | Mapping[str, object],
    ) -> FastTurnResult:
        turn = FastTurnRequest.model_validate(request)
        execution = self._create_execution(turn)
        if execution.status != "received":
            return self._result_from_execution(execution)

        progress = _RunProgress()
        try:
            async with asyncio.timeout(self._budget.timeout_seconds):
                return await self._run_bounded(turn, execution.id, progress)
        except TimeoutError:
            if turn.allow_handoff:
                return await self._handoff_after_timeout(
                    turn,
                    execution.id,
                    progress,
                )
            return self._fail_execution(
                execution.id,
                error_code="fast_turn_timeout",
            )
        except Exception:
            return self._fail_execution(
                execution.id,
                error_code="fast_turn_failed",
            )

    def _create_execution(self, request: FastTurnRequest) -> AssistantExecutionRecord:
        with self._database.session() as db_session:
            execution = AssistantRepository(db_session).create_execution(
                execution_id=str(uuid.uuid4()),
                session_id=request.session_id,
                profile="fast_turn",
                goal=request.goal,
                client_request_id=request.client_request_id,
                grant_id=request.grant_id,
                budget=self._budget.model_dump(mode="json"),
            )
            db_session.commit()
            return execution

    async def _run_bounded(
        self,
        request: FastTurnRequest,
        execution_id: str,
        progress: _RunProgress,
    ) -> FastTurnResult:
        self._check_authority(execution_id)
        progress.snapshot = await self._build_context(request, execution_id)
        tools = self._available_tools()

        for planning_round in range(1, self._budget.max_model_rounds + 1):
            self._check_authority(execution_id)
            outcome = await self._planner.decide(
                profile="fast_turn",
                goal=request.goal,
                snapshot=progress.snapshot,
                available_tools=tools,
                observations=tuple(progress.observations),
                remaining_budget={
                    "execution_id": execution_id,
                    "model_rounds": self._budget.max_model_rounds - planning_round + 1,
                    "parallel_read_tools": self._budget.max_parallel_read_tools,
                    "external_writes": self._budget.max_external_writes,
                },
            )
            decision = outcome.decision
            self._check_authority(execution_id)
            step = HandoffCompletedStep(
                kind="plan",
                input_payload={"planning_round": planning_round},
                output_payload=decision.model_dump(mode="json"),
                decision_summary=decision.decision_summary,
            )
            progress.steps.append(step)

            if isinstance(decision, (RespondDecision, CompleteDecision)):
                return self._complete_response(
                    execution_id,
                    progress,
                    claims=decision.claims,
                )
            if isinstance(decision, HandoffDecision):
                return await self._commit_handoff(
                    request,
                    execution_id,
                    progress,
                    decision=decision,
                )
            if isinstance(decision, NeedsInputDecision):
                raise RuntimeError("Fast Turn cannot enter NeedsInput")
            if not isinstance(decision, InvokeToolsDecision):
                raise TypeError(f"unsupported Fast Turn decision: {type(decision)!r}")

            self._transition_to_executing_reads(execution_id)
            progress.observations.extend(
                await self._execute_tool_calls(
                    execution_id,
                    decision.tool_calls,
                    planning_round=planning_round,
                )
            )

        if request.allow_handoff:
            reason = EvidenceClaim(
                text="The bounded Fast Turn needs additional durable work.",
                evidence_refs=(progress.snapshot.evidence_refs[0],),
            )
            return await self._commit_handoff(
                request,
                execution_id,
                progress,
                decision=HandoffDecision(
                    decision_summary="Continue beyond the Fast Turn budget",
                    handoff_goal=request.goal,
                    reason=reason,
                ),
            )
        return self._fail_execution(
            execution_id,
            error_code="fast_turn_round_budget_exhausted",
        )

    async def _build_context(
        self,
        request: FastTurnRequest,
        execution_id: str,
    ) -> ContextSnapshot:
        with self._database.session() as db_session:
            repository = AssistantRepository(db_session)
            execution = repository.get_execution_required(execution_id)
            if execution.status == "received":
                execution = repository.transition_execution(
                    execution.id,
                    expected_version=execution.state_version,
                    target_status="contextualizing",
                    event_type="fast_turn.contextualizing",
                    summary="Fast Turn is freezing relevant meeting context",
                )
            db_session.commit()

        with self._database.session() as db_session:
            snapshot = await ContextBuilder(
                db_session,
                projector=self._projector,
            ).build(
                session_id=request.session_id,
                goal=request.goal,
                mark_ids=request.mark_ids,
                actor_id=request.actor_id,
                fast_ask=True,
                persist=True,
            )
            repository = AssistantRepository(db_session)
            execution = repository.get_execution_required(execution_id)
            if execution.status == "contextualizing":
                repository.transition_execution(
                    execution.id,
                    expected_version=execution.state_version,
                    target_status="deciding",
                    event_type="fast_turn.context_frozen",
                    summary="Fast Turn context was frozen",
                    payload={
                        "snapshot_id": snapshot.snapshot_id,
                        "meeting_state_version": snapshot.meeting_state_version,
                    },
                    snapshot_id=snapshot.snapshot_id,
                )
            db_session.commit()
        return snapshot

    def _available_tools(self) -> tuple[AvailableTool, ...]:
        if self._registry is None:
            return ()
        return tuple(
            AvailableTool(
                name=spec.name,
                version=spec.version,
                capability=spec.capability,
                effect=spec.effect,
                description=f"{spec.capability} ({spec.effect})",
                input_schema=spec.input_model.model_json_schema(),
            )
            for spec in self._registry.list_specs()
            if "fast_turn" in spec.allowed_profiles
            and spec.effect != "external_write"
        )

    def _transition_to_executing_reads(self, execution_id: str) -> None:
        with self._database.session() as db_session:
            repository = AssistantRepository(db_session)
            execution = repository.get_execution_required(execution_id)
            if execution.status == "deciding":
                repository.transition_execution(
                    execution.id,
                    expected_version=execution.state_version,
                    target_status="executing_reads",
                    event_type="fast_turn.executing_tools",
                    summary="Fast Turn is executing bounded private tools",
                )
                db_session.commit()

    async def _execute_tool_calls(
        self,
        execution_id: str,
        calls: Sequence[ProposedToolCall],
        *,
        planning_round: int,
    ) -> tuple[PlanningObservation, ...]:
        if self._registry is None or self._tool_executor is None:
            raise RuntimeError("Fast Turn planner requested unavailable tools")
        indexed = list(enumerate(calls))
        read_calls = [
            (index, call)
            for index, call in indexed
            if self._registry.get(call.tool_name).spec.effect == "read"
        ]
        local_calls = [
            (index, call)
            for index, call in indexed
            if self._registry.get(call.tool_name).spec.effect == "local_write"
        ]
        if len(read_calls) + len(local_calls) != len(indexed):
            raise RuntimeError("Fast Turn cannot execute external-write tools")

        semaphore = asyncio.Semaphore(self._budget.max_parallel_read_tools)

        async def execute_read(index: int, call: ProposedToolCall):
            async with semaphore:
                return await self._execute_one_tool(
                    execution_id,
                    call,
                    planning_round=planning_round,
                    index=index,
                )

        values = list(
            await asyncio.gather(
                *(execute_read(index, call) for index, call in read_calls)
            )
        )
        for index, call in local_calls:
            values.append(
                await self._execute_one_tool(
                    execution_id,
                    call,
                    planning_round=planning_round,
                    index=index,
                )
            )
        values.sort(key=lambda value: value[0])
        return tuple(value[1] for value in values)

    async def _execute_one_tool(
        self,
        execution_id: str,
        call: ProposedToolCall,
        *,
        planning_round: int,
        index: int,
    ) -> tuple[int, PlanningObservation]:
        if self._registry is None or self._tool_executor is None:
            raise RuntimeError("Fast Turn tool execution is not configured")
        spec = self._registry.get(call.tool_name).spec
        if "fast_turn" not in spec.allowed_profiles or spec.effect == "external_write":
            raise RuntimeError("Fast Turn tool profile is denied")
        idempotency_key = (
            f"fast:{execution_id}:{planning_round}:{index}:{spec.name}"
            if spec.supports_idempotency
            else None
        )
        result = await self._tool_executor.execute(
            ToolInvocation(
                execution_id=execution_id,
                tool_name=spec.name,
                arguments=call.arguments,
                idempotency_key=idempotency_key,
                evidence_refs=call.evidence_refs,
            )
        )
        observation = PlanningObservation(
            observation_id=f"fastobs_{uuid.uuid4().hex}",
            source="tool",
            summary=f"{spec.name} returned {result.status}",
            payload={
                "tool_name": spec.name,
                "status": result.status,
                "output": result.output,
                "external_reference": result.external_reference,
                "error_code": result.error_code,
            },
            evidence_refs=call.evidence_refs,
        )
        return index, observation

    def _complete_response(
        self,
        execution_id: str,
        progress: _RunProgress,
        *,
        claims: Sequence[EvidenceClaim],
    ) -> FastTurnResult:
        response_text = "\n".join(claim.text for claim in claims)
        result_payload = {
            "response_text": response_text,
            "claims": [claim.model_dump(mode="json") for claim in claims],
        }
        with self._database.session() as db_session:
            if self._execution_guard is not None:
                self._execution_guard.fence(db_session, execution_id, allow_unowned_read=True)
            repository = AssistantRepository(db_session)
            execution = repository.get_execution_required(execution_id)
            for step in progress.steps:
                repository.append_step(
                    execution_id=execution.id,
                    kind=step.kind,
                    input_payload=step.input_payload,
                    output_payload=step.output_payload,
                    decision_summary=step.decision_summary,
                )
            for observation in progress.observations:
                repository.append_observation(
                    execution_id=execution.id,
                    observation_id=observation.observation_id,
                    source=observation.source,
                    source_ref=observation.observation_id,
                    observation={
                        "summary": observation.summary,
                        "payload": observation.payload,
                    },
                    evidence_refs=observation.evidence_refs,
                )
            execution = repository.transition_execution(
                execution.id,
                expected_version=execution.state_version,
                target_status="responding",
                event_type="fast_turn.responding",
                summary="Fast Turn prepared a private response",
            )
            execution = repository.transition_execution(
                execution.id,
                expected_version=execution.state_version,
                target_status="completed",
                event_type="fast_turn.completed",
                summary="Fast Turn completed",
                result=result_payload,
            )
            db_session.commit()
            return self._result_from_execution(execution)

    async def _commit_handoff(
        self,
        request: FastTurnRequest,
        execution_id: str,
        progress: _RunProgress,
        *,
        decision: HandoffDecision,
    ) -> FastTurnResult:
        if not request.allow_handoff:
            return self._fail_execution(
                execution_id,
                error_code="fast_turn_handoff_not_authorized",
            )
        if progress.snapshot is None:
            progress.snapshot = await self._build_context(request, execution_id)
        with self._database.session() as db_session:
            execution = AssistantRepository(db_session).get_execution_required(
                execution_id
            )
            expected_version = execution.state_version
        commit = await self._handoff_service.handoff(
            source_execution_id=execution_id,
            expected_state_version=expected_version,
            snapshot=progress.snapshot,
            handoff_goal=decision.handoff_goal,
            reason=decision.reason,
            completed_steps=tuple(progress.steps),
            observations=tuple(
                HandoffObservation(
                    source=observation.source,
                    source_ref=observation.observation_id,
                    observation={
                        "summary": observation.summary,
                        "payload": observation.payload,
                    },
                    evidence_refs=observation.evidence_refs,
                )
                for observation in progress.observations
            ),
            candidate_plan={
                "required_capabilities": list(decision.required_capabilities),
                "candidate_ids": list(decision.candidate_ids),
            },
            remaining_budget={
                "model_rounds": max(
                    0,
                    self._budget.max_model_rounds - len(progress.steps),
                ),
                "external_writes": 0,
            },
            grant_id=request.grant_id,
            idempotency_scope=(
                f"fast-turn:{request.client_request_id}"
                if request.client_request_id is not None
                else f"fast-turn:{execution_id}"
            ),
        )
        with self._database.session() as db_session:
            source = AssistantRepository(db_session).get_execution_required(execution_id)
            result = self._result_from_execution(source)
        return result.model_copy(
            update={"handoff_execution_id": commit.target_execution_id}
        )

    async def _handoff_after_timeout(
        self,
        request: FastTurnRequest,
        execution_id: str,
        progress: _RunProgress,
    ) -> FastTurnResult:
        if progress.snapshot is None:
            progress.snapshot = await self._build_context(request, execution_id)
        progress.steps.append(
            HandoffCompletedStep(
                kind="plan",
                input_payload={"reason": "fast_turn_timeout"},
                output_payload={"kind": "handoff"},
                decision_summary="Continue after the Fast Turn timeout",
            )
        )
        decision = HandoffDecision(
            decision_summary="Continue as a durable Action Run",
            handoff_goal=request.goal,
            reason=EvidenceClaim(
                text="The authorized Fast Turn exceeded its latency budget.",
                evidence_refs=(progress.snapshot.evidence_refs[0],),
            ),
        )
        return await self._commit_handoff(
            request,
            execution_id,
            progress,
            decision=decision,
        )

    def _fail_execution(self, execution_id: str, *, error_code: str) -> FastTurnResult:
        with self._database.session() as db_session:
            repository = AssistantRepository(db_session)
            execution = repository.get_execution_required(execution_id)
            if execution.status not in {"completed", "handed_off", "failed", "cancelled"}:
                execution = repository.transition_execution(
                    execution.id,
                    expected_version=execution.state_version,
                    target_status="failed",
                    event_type="fast_turn.failed",
                    summary="Fast Turn ended without a response",
                    error_code=error_code,
                    error_message="The private Fast Turn could not complete.",
                )
                db_session.commit()
            return self._result_from_execution(execution)

    @staticmethod
    def _result_from_execution(
        execution: AssistantExecutionRecord,
    ) -> FastTurnResult:
        payload = dict(execution.result_json or {})
        claims = tuple(
            EvidenceClaim.model_validate(value)
            for value in payload.get("claims", ())
        )
        return FastTurnResult(
            execution_id=execution.id,
            status=execution.status,
            snapshot_id=execution.snapshot_id,
            response_text=payload.get("response_text"),
            claims=claims,
            handoff_execution_id=payload.get("handoff_execution_id"),
            error_code=execution.error_code,
        )


__all__ = [
    "FastTurnBudget",
    "FastTurnRequest",
    "FastTurnResult",
    "FastTurnRunner",
]
