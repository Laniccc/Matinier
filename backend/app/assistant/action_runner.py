from __future__ import annotations

from app.assistant.plugin_repository import MeetingPluginDenied

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.assistant.context import ContextBuilder
from app.assistant.critic import Critic, CriticReview
from app.assistant.models import ActionRunStatus, ContextSnapshot
from app.assistant.parser import (
    CompleteDecision,
    EvidenceClaim,
    HandoffDecision,
    InvokeToolsDecision,
    NeedsInputDecision,
    ProposedToolCall,
    RespondDecision,
)
from app.assistant.planner import AvailableTool, PlannerOutcome, PlanningObservation
from app.assistant.repository import AssistantRepository
from app.assistant.subagents import SubagentCoordinator
from app.assistant.tools import ToolExecutor, ToolInvocation, ToolRegistry
from app.assistant.tools.contracts import ToolResult, ToolSpec
from app.meeting_state.projector import MeetingStateProjector
from app.logging import log_assistant_lifecycle
from app.persistence.database import Database
from app.persistence.models import (
    AssistantContextSnapshotRecord,
    AssistantExecutionRecord,
    AssistantToolCallRecord,
)


TERMINAL_ACTION_STATUSES = frozenset(
    {"completed", "partial", "failed", "cancelled"}
)


class FrozenActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ActionRunBudget(FrozenActionModel):
    max_steps: int = Field(default=20, ge=1)
    max_model_calls: int = Field(default=16, ge=1)
    max_planning_rounds: int = Field(default=4, ge=1)
    reconciliation_delays_seconds: tuple[float, ...] = Field(
        default=(1.0, 3.0, 10.0),
        min_length=1,
        max_length=10,
    )


class ActionRunResult(FrozenActionModel):
    execution_id: str = Field(min_length=1, max_length=36)
    status: ActionRunStatus
    snapshot_id: str | None = Field(default=None, max_length=36)
    response_text: str | None = None
    claims: tuple[EvidenceClaim, ...] = Field(default_factory=tuple)
    question: str | None = None
    choices: tuple[str, ...] = Field(default_factory=tuple)
    error_code: str | None = Field(default=None, max_length=128)


class ActionPlanner(Protocol):
    async def decide(
        self,
        *,
        profile: str,
        goal: str,
        snapshot: ContextSnapshot,
        available_tools: Sequence[AvailableTool],
        observations: Sequence[PlanningObservation],
        remaining_budget: Mapping[str, object],
    ) -> PlannerOutcome: ...


class ActionInvocationPolicy(Protocol):
    def build(
        self,
        *,
        execution: AssistantExecutionRecord,
        snapshot: ContextSnapshot,
        call: ProposedToolCall,
        spec: ToolSpec,
        planning_round: int,
        call_index: int,
        step_id: str,
    ) -> ToolInvocation: ...


class DefaultActionInvocationPolicy:
    """Derives safe generic keys; domain adapters may inject stricter policies."""

    def build(
        self,
        *,
        execution: AssistantExecutionRecord,
        snapshot: ContextSnapshot,
        call: ProposedToolCall,
        spec: ToolSpec,
        planning_round: int,
        call_index: int,
        step_id: str,
    ) -> ToolInvocation:
        del snapshot
        candidate_value = call.arguments.get("candidate_id")
        candidate_id = candidate_value if isinstance(candidate_value, str) else None
        logical_action_key = None
        idempotency_key = None
        if spec.effect == "external_write":
            canonical = json.dumps(
                call.arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            lineage = candidate_id or hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            logical_action_key = hashlib.sha256(
                f"{spec.capability}:v1:{lineage}".encode("utf-8")
            ).hexdigest()
            idempotency_key = hashlib.sha256(
                (
                    f"{execution.grant_id or 'no-grant'}:"
                    f"{spec.capability}:{logical_action_key}"
                ).encode("utf-8")
            ).hexdigest()
        elif spec.supports_idempotency:
            idempotency_key = hashlib.sha256(
                (
                    f"{execution.id}:{planning_round}:{call_index}:"
                    f"{spec.name}"
                ).encode("utf-8")
            ).hexdigest()
        return ToolInvocation(
            execution_id=execution.id,
            tool_name=spec.name,
            arguments=call.arguments,
            grant_id=execution.grant_id,
            candidate_id=candidate_id,
            requested_resource_scope={},
            logical_action_key=logical_action_key,
            idempotency_key=idempotency_key,
            evidence_refs=call.evidence_refs,
            step_id=step_id,
        )


class FreshnessCheck(FrozenActionModel):
    snapshot: ContextSnapshot
    changed: bool
    writable: bool
    reason: str


class RelevantContextGuard:
    """Refreshes only relevant context before an external mutation."""

    def __init__(
        self,
        database: Database,
        *,
        projector: MeetingStateProjector | None = None,
        catch_up_timeout_seconds: float = 15.0,
    ) -> None:
        if catch_up_timeout_seconds <= 0:
            raise ValueError("catch_up_timeout_seconds must be positive")
        self._database = database
        self._projector = projector
        self._catch_up_timeout_seconds = catch_up_timeout_seconds

    async def check(
        self,
        *,
        execution: AssistantExecutionRecord,
        snapshot: ContextSnapshot,
    ) -> FreshnessCheck:
        if self._projector is not None and self._projector.started and self._projector.can_request_catch_up(execution.session_id):
            target = await self._projector.request_catch_up(execution.session_id)
            await target.wait(timeout=self._catch_up_timeout_seconds)
        actor_id = "local-user"
        for message in snapshot.evidence_messages:
            if message.message_kind == "user_input":
                actor_id = message.actor_id
                break
        with self._database.session() as db_session:
            fresh = await ContextBuilder(
                db_session,
                projector=self._projector,
            ).build(
                session_id=execution.session_id,
                goal=execution.goal,
                actor_id=actor_id,
                fast_ask=False,
                persist=False,
            )
            changed = fresh.relevant_context_hash != snapshot.relevant_context_hash
            if changed:
                AssistantRepository(db_session).create_context_snapshot(
                    snapshot_id=fresh.snapshot_id,
                    session_id=fresh.session_id,
                    meeting_state_version=fresh.meeting_state_version,
                    state_slice=fresh.state_slice,
                    source_frontier=fresh.source_frontier,
                    evidence_refs=fresh.evidence_refs,
                    evidence_messages=fresh.evidence_messages,
                    relevant_context_hash=fresh.relevant_context_hash,
                    created_at=fresh.created_at,
                )
                db_session.commit()
            else:
                fresh = snapshot
        freshness = fresh.state_slice.get("freshness", {})
        state_status = (
            freshness.get("status")
            if isinstance(freshness, Mapping)
            else None
        )
        writable = state_status not in {"stale", "rebuilding"}
        reason = (
            "relevant_context_changed"
            if changed
            else "relevant_context_current"
        )
        if not writable:
            reason = "meeting_state_not_fresh"
        return FreshnessCheck(
            snapshot=fresh,
            changed=changed,
            writable=writable,
            reason=reason,
        )


@dataclass(slots=True)
class _ActionProgress:
    snapshot: ContextSnapshot
    observations: list[PlanningObservation] = field(default_factory=list)
    planning_rounds: int = 0
    model_calls: int = 0
    steps: int = 0


@dataclass(frozen=True, slots=True)
class _PreparedCall:
    index: int
    call: ProposedToolCall
    step_id: str


@dataclass(frozen=True, slots=True)
class _ToolBatchOutcome:
    observations: tuple[PlanningObservation, ...]
    has_pending: bool
    has_unknown: bool


class ActionRunRunner:
    """Durable bounded orchestrator for one independent slow execution."""

    def __init__(
        self,
        database: Database,
        planner: ActionPlanner,
        registry: ToolRegistry,
        tool_executor: ToolExecutor,
        subagents: SubagentCoordinator,
        critic: Critic,
        *,
        projector: MeetingStateProjector | None = None,
        context_guard: RelevantContextGuard | None = None,
        invocation_policy: ActionInvocationPolicy | None = None,
        budget: ActionRunBudget | None = None,
        execution_guard=None,
    ) -> None:
        self._database = database
        self._planner = planner
        self._registry = registry
        self._tool_executor = tool_executor
        self._subagents = subagents
        self._critic = critic
        self._budget = budget or ActionRunBudget()
        self._execution_guard = execution_guard
        self._context_guard = context_guard or RelevantContextGuard(
            database,
            projector=projector,
        )
        self._invocation_policy = (
            invocation_policy or DefaultActionInvocationPolicy()
        )

    async def run(self, execution_id: str) -> ActionRunResult:
        started = time.perf_counter()
        try:
            self._check_authority(execution_id)
            result = await self._run(execution_id)
        except MeetingPluginDenied:
            await self._execution_guard.reconcile_read_only(execution_id, self._tool_executor)
            result = self._result(self._get_execution(execution_id))
        except Exception:
            result = self._fail(execution_id, error_code="action_run_failed")
        execution = self._get_execution(execution_id)
        log_assistant_lifecycle(
            "assistant_action_run_cycle",
            session_id=execution.session_id,
            execution_id=execution.id,
            root_execution_id=execution.root_execution_id,
            status=result.status,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            phase=result.status,
            profile=execution.profile,
            snapshot_id=execution.snapshot_id,
            error_code=result.error_code,
        )
        return result

    def _check_authority(self, execution_id):
        if self._execution_guard is not None:
            self._execution_guard.check(execution_id)

    async def _run(self, execution_id: str) -> ActionRunResult:
        execution = self._get_execution(execution_id)
        if execution.profile != "action_run":
            raise ValueError("ActionRunRunner requires an action_run execution")
        if execution.status in TERMINAL_ACTION_STATUSES or execution.status == "needs_input":
            return self._result(execution)
        if execution.status in {"waiting_external", "reconciling"}:
            resumed = await self._resume_external(execution.id)
            if resumed.status != "planning":
                return resumed

        progress = self._load_progress(execution.id)
        available_tools = self._available_tools()
        while (
            progress.planning_rounds < self._budget.max_planning_rounds
            and progress.model_calls < self._budget.max_model_calls
            and progress.steps < self._budget.max_steps
        ):
            self._check_authority(execution.id)
            execution = self._ensure_planning(execution.id)
            planning_round = progress.planning_rounds + 1
            remaining_calls = self._budget.max_model_calls - progress.model_calls
            subagent_result = await self._subagents.run_for_execution(
                execution_id=execution.id,
                planning_round=planning_round,
                goal=execution.goal,
                snapshot=progress.snapshot,
                available_tools=available_tools,
                observations=tuple(progress.observations),
                remaining_model_calls=remaining_calls,
            )
            progress.model_calls += subagent_result.model_call_count
            self._check_authority(execution.id)
            self._persist_observations(
                execution.id,
                subagent_result.observations,
            )
            progress.observations.extend(subagent_result.observations)

            remaining_calls = self._budget.max_model_calls - progress.model_calls
            if remaining_calls <= 0:
                return self._finish_partial(
                    execution.id,
                    error_code="action_model_budget_exhausted",
                )
            review = await self._critic.review(
                execution_id=execution.id,
                planning_round=planning_round,
                goal=execution.goal,
                snapshot=progress.snapshot,
                branches=subagent_result.branches,
                observations=tuple(progress.observations),
                remaining_model_calls=remaining_calls,
            )
            progress.model_calls += review.model_call_count
            self._check_authority(execution.id)
            critic_observation = review.observation.model_copy(
                update={
                    "payload": {
                        **review.observation.payload,
                        "model_call_count": review.model_call_count,
                    }
                }
            )
            self._persist_observations(execution.id, (critic_observation,))
            progress.observations.append(critic_observation)

            remaining_calls = self._budget.max_model_calls - progress.model_calls
            if remaining_calls <= 0:
                return self._finish_partial(
                    execution.id,
                    error_code="action_model_budget_exhausted",
                )
            outcome = await self._planner.decide(
                profile="action_run",
                goal=execution.goal,
                snapshot=progress.snapshot,
                available_tools=available_tools,
                observations=tuple(progress.observations),
                remaining_budget={
                    "execution_id": execution.id,
                    "model_calls": remaining_calls,
                    "planning_rounds": (
                        self._budget.max_planning_rounds - planning_round + 1
                    ),
                    "steps": self._budget.max_steps - progress.steps,
                },
            )
            progress.model_calls += outcome.model_call_count
            self._check_authority(execution.id)
            progress.planning_rounds = planning_round
            progress.steps += 1
            self._persist_plan_step(execution.id, planning_round, outcome)
            decision = outcome.decision

            if isinstance(decision, (RespondDecision, CompleteDecision)):
                terminal_status: ActionRunStatus = (
                    decision.completion_status
                    if isinstance(decision, CompleteDecision)
                    else "completed"
                )
                return self._complete(
                    execution.id,
                    claims=decision.claims,
                    terminal_status=terminal_status,
                )
            if isinstance(decision, NeedsInputDecision):
                return self._needs_input(execution.id, decision)
            if isinstance(decision, HandoffDecision):
                raise RuntimeError("Action Run cannot create another Handoff")
            if not isinstance(decision, InvokeToolsDecision):
                raise TypeError(f"unsupported Action Run decision: {type(decision)!r}")

            external_requested = any(
                self._registry.get(call.tool_name).spec.effect == "external_write"
                for call in decision.tool_calls
            )
            if external_requested and not review.external_writes_allowed:
                return self._needs_input_for_evidence(execution.id, progress.snapshot)
            if external_requested:
                freshness = await self._context_guard.check(
                    execution=execution,
                    snapshot=progress.snapshot,
                )
                if not freshness.writable:
                    return self._needs_input_for_freshness(
                        execution.id,
                        freshness.snapshot,
                    )
                if freshness.changed:
                    progress.snapshot = freshness.snapshot
                    observation = PlanningObservation(
                        observation_id=str(uuid.uuid4()),
                        source="system",
                        summary=(
                            "Relevant meeting context changed before the external write; "
                            "the Action Run will replan."
                        ),
                        payload={"reason": freshness.reason},
                        evidence_refs=freshness.snapshot.evidence_refs,
                    )
                    self._persist_observations(execution.id, (observation,))
                    progress.observations.append(observation)
                    self._transition(
                        execution.id,
                        "executing",
                        event_type="action.context_refreshed",
                        summary="Action Run refreshed relevant context",
                        snapshot_id=freshness.snapshot.snapshot_id,
                    )
                    self._transition(
                        execution.id,
                        "observing",
                        event_type="action.context_change_observed",
                        summary="Action Run observed a relevant context change",
                    )
                    self._transition(
                        execution.id,
                        "planning",
                        event_type="action.replanning",
                        summary="Action Run is replanning with refreshed context",
                    )
                    continue

            if progress.steps + len(decision.tool_calls) > self._budget.max_steps:
                return self._finish_partial(
                    execution.id,
                    error_code="action_step_budget_exhausted",
                )
            prepared = self._prepare_tool_steps(execution.id, decision.tool_calls)
            progress.steps += len(prepared)
            self._transition(
                execution.id,
                "executing",
                event_type="action.executing_tools",
                summary="Action Run is executing an approved tool batch",
            )
            batch = await self._execute_tool_batch(
                execution=self._get_execution(execution.id),
                snapshot=progress.snapshot,
                planning_round=planning_round,
                calls=prepared,
            )
            self._persist_observations(execution.id, batch.observations)
            progress.observations.extend(batch.observations)
            if batch.has_unknown:
                self._transition(
                    execution.id,
                    "reconciling",
                    event_type="action.reconciling",
                    summary="Action Run is reconciling an unknown external outcome",
                )
                resumed = await self._resume_external(execution.id)
                if resumed.status != "planning":
                    return resumed
                # Reconciliation can finish inside this run. Reload its durable
                # observations/budget and continue: returning `planning` here
                # strands the execution because no external wait remains.
                progress = self._load_progress(execution.id)
                continue
            if batch.has_pending:
                record = self._transition(
                    execution.id,
                    "waiting_external",
                    event_type="action.waiting_external",
                    summary="Action Run is waiting for an external result",
                )
                return self._result(record)
            self._transition(
                execution.id,
                "observing",
                event_type="action.observing",
                summary="Action Run persisted tool observations",
            )

        return self._finish_partial(
            execution_id,
            error_code="action_budget_exhausted",
        )

    def _load_progress(self, execution_id: str) -> _ActionProgress:
        with self._database.session() as db_session:
            repository = AssistantRepository(db_session)
            execution = repository.get_execution_required(execution_id)
            if execution.snapshot_id is None:
                raise ValueError("Action Run has no Context Snapshot")
            snapshot_record = repository.get_context_snapshot(execution.snapshot_id)
            if snapshot_record is None:
                raise LookupError("Action Run Context Snapshot was not found")
            steps = repository.list_steps(execution_id)
            observation_records = repository.list_observations(execution_id)
            subagent_records = repository.list_subagent_runs(execution_id)
            planning_rounds = sum(step.kind == "plan" for step in steps)
            model_calls = sum(
                int((step.output_json or {}).get("model_call_count", 0))
                for step in steps
            )
            model_calls += sum(
                int((record.result_json or {}).get("model_call_count", 0))
                for record in subagent_records
            )
            model_calls += sum(
                int(record.observation_json.get("model_call_count", 0))
                for record in observation_records
            )
            observations = [
                self._observation_from_record(record)
                for record in observation_records
            ]
            return _ActionProgress(
                snapshot=self._snapshot_from_record(snapshot_record),
                observations=observations,
                planning_rounds=planning_rounds,
                model_calls=model_calls,
                steps=execution.step_count,
            )

    def _available_tools(self) -> tuple[AvailableTool, ...]:
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
            if "action_run" in spec.allowed_profiles
        )

    def _ensure_planning(self, execution_id: str) -> AssistantExecutionRecord:
        execution = self._get_execution(execution_id)
        if execution.status == "queued":
            return self._transition(
                execution_id,
                "planning",
                event_type="action.planning",
                summary="Action Run started a planning round",
            )
        if execution.status == "observing":
            return self._transition(
                execution_id,
                "planning",
                event_type="action.replanning",
                summary="Action Run is replanning from observations",
            )
        if execution.status != "planning":
            raise RuntimeError(f"Action Run cannot plan from {execution.status}")
        return execution

    def _persist_plan_step(
        self,
        execution_id: str,
        planning_round: int,
        outcome: PlannerOutcome,
    ) -> None:
        with self._database.session() as db_session:
            if self._execution_guard is not None:
                self._execution_guard.fence(db_session, execution_id)
            AssistantRepository(db_session).append_step(
                execution_id=execution_id,
                kind="plan",
                input_payload={"planning_round": planning_round},
                output_payload={
                    "decision": outcome.decision.model_dump(mode="json"),
                    "model_call_count": outcome.model_call_count,
                    "repair_attempted": outcome.repair_attempted,
                },
                decision_summary=outcome.decision.decision_summary,
            )
            db_session.commit()

    def _persist_observations(
        self,
        execution_id: str,
        observations: Sequence[PlanningObservation],
    ) -> None:
        if not observations:
            return
        with self._database.session() as db_session:
            if self._execution_guard is not None:
                self._execution_guard.fence(db_session, execution_id)
            repository = AssistantRepository(db_session)
            existing_ids = {
                record.id for record in repository.list_observations(execution_id)
            }
            for observation in observations:
                if observation.observation_id in existing_ids:
                    continue
                repository.append_observation(
                    execution_id=execution_id,
                    observation_id=observation.observation_id,
                    source=observation.source,
                    source_ref=observation.observation_id,
                    observation={
                        "summary": observation.summary,
                        "payload": observation.payload,
                        "model_call_count": observation.payload.get(
                            "model_call_count",
                            0,
                        ),
                    },
                    evidence_refs=observation.evidence_refs,
                )
            db_session.commit()

    def _prepare_tool_steps(
        self,
        execution_id: str,
        calls: Sequence[ProposedToolCall],
    ) -> tuple[_PreparedCall, ...]:
        with self._database.session() as db_session:
            if self._execution_guard is not None:
                self._execution_guard.fence(db_session, execution_id)
            repository = AssistantRepository(db_session)
            prepared = tuple(
                _PreparedCall(
                    index=index,
                    call=call,
                    step_id=repository.append_step(
                        execution_id=execution_id,
                        kind="tool",
                        input_payload={
                            "tool_name": call.tool_name,
                            "arguments": call.arguments,
                            "purpose": call.purpose,
                            "evidence_refs": list(call.evidence_refs),
                        },
                        decision_summary=call.purpose,
                    ).id,
                )
                for index, call in enumerate(calls)
            )
            db_session.commit()
            return prepared

    async def _execute_tool_batch(
        self,
        *,
        execution: AssistantExecutionRecord,
        snapshot: ContextSnapshot,
        planning_round: int,
        calls: Sequence[_PreparedCall],
    ) -> _ToolBatchOutcome:
        read_calls = [
            call
            for call in calls
            if self._registry.get(call.call.tool_name).spec.effect == "read"
        ]
        write_calls = [
            call
            for call in calls
            if self._registry.get(call.call.tool_name).spec.effect != "read"
        ]

        async def execute(prepared: _PreparedCall):
            spec = self._registry.get(prepared.call.tool_name).spec
            invocation = self._invocation_policy.build(
                execution=execution,
                snapshot=snapshot,
                call=prepared.call,
                spec=spec,
                planning_round=planning_round,
                call_index=prepared.index,
                step_id=prepared.step_id,
            )
            invocation = self._bind_prepared_external_call(invocation, spec)
            result = await self._tool_executor.execute(invocation)
            return prepared.index, self._tool_observation(prepared, result)

        values = list(await asyncio.gather(*(execute(call) for call in read_calls)))
        for call in write_calls:
            values.append(await execute(call))
        values.sort(key=lambda value: value[0])
        observations = tuple(value[1] for value in values)
        statuses = {
            observation.payload.get("status") for observation in observations
        }
        return _ToolBatchOutcome(
            observations=observations,
            has_pending="pending" in statuses,
            has_unknown="unknown" in statuses,
        )

    def _bind_prepared_external_call(
        self,
        invocation: ToolInvocation,
        spec: ToolSpec,
    ) -> ToolInvocation:
        if spec.effect != "external_write" or invocation.logical_action_key is None:
            return invocation
        registered = self._registry.get(spec.name)
        with self._database.session() as db_session:
            repository = AssistantRepository(db_session)
            claim = repository.observe_external_action_claim(
                provider=registered.adapter.provider_name,
                capability=spec.capability,
                logical_action_key=invocation.logical_action_key,
            )
            if (
                claim is None
                or claim.status != "reserved"
                or claim.holder_execution_id != invocation.execution_id
                or claim.tool_call_id is None
            ):
                return invocation
            tool_call = repository.get_tool_call(claim.tool_call_id)
            if tool_call is None or tool_call.status != "prepared":
                return invocation
            return invocation.model_copy(
                update={"prepared_tool_call_id": tool_call.id}
            )

    @staticmethod
    def _tool_observation(
        prepared: _PreparedCall,
        result: ToolResult,
    ) -> PlanningObservation:
        return PlanningObservation(
            observation_id=str(uuid.uuid4()),
            source="tool",
            summary=f"{prepared.call.tool_name} returned {result.status}",
            payload={
                "tool_name": prepared.call.tool_name,
                "status": result.status,
                "output": result.output,
                "external_reference": result.external_reference,
                "error_code": result.error_code,
            },
            evidence_refs=prepared.call.evidence_refs,
        )

    async def _resume_external(self, execution_id: str) -> ActionRunResult:
        execution = self._get_execution(execution_id)
        with self._database.session() as db_session:
            repository = AssistantRepository(db_session)
            calls = repository.list_tool_calls(
                execution_id,
                statuses=("requesting", "pending", "unknown", "reconciling"),
            )
            for call in calls:
                if call.effect == "external_write" and call.status == "requesting":
                    repository.update_tool_call(
                        call.id,
                        expected_status="requesting",
                        status="unknown",
                        error_code="tool_outcome_unknown_after_restart",
                        error_message=(
                            "The external outcome requires reconciliation after restart."
                        ),
                    )
            db_session.commit()
        if not calls:
            if execution.status in {"waiting_external", "reconciling"}:
                self._transition(
                    execution_id,
                    "observing",
                    event_type="action.external_observed",
                    summary="Action Run has no unfinished external operation",
                )
                record = self._transition(
                    execution_id,
                    "planning",
                    event_type="action.replanning",
                    summary="Action Run resumed planning",
                )
                return self._result(record)
            return self._result(execution)

        external_unknown = any(
            call.effect == "external_write"
            and call.status in {"requesting", "unknown", "reconciling"}
            for call in calls
        )
        if external_unknown and execution.status == "waiting_external":
            execution = self._transition(
                execution_id,
                "reconciling",
                event_type="action.reconciling",
                summary="Action Run is reconciling an external operation",
            )

        all_resolved = True
        for call in calls:
            registered = self._registry.get(call.tool_name)
            if not registered.spec.supports_reconciliation:
                if call.effect == "external_write":
                    return self._needs_input_from_reconciliation(
                        execution_id,
                        "unverifiable_external_side_effect",
                    )
                self._fail_unreconcilable_local_tool(call)
                continue
            if call.effect == "external_write":
                result: ToolResult | None = None
                for delay in self._budget.reconciliation_delays_seconds:
                    await asyncio.sleep(delay)
                    result = await self._tool_executor.reconcile(call.id)
                    if result.status in {"succeeded", "failed"}:
                        break
                    if result.error_code == "duplicate_external_side_effect":
                        return self._needs_input_from_reconciliation(
                            execution_id,
                            "duplicate_external_side_effect",
                        )
                if result is None or result.status not in {"succeeded", "failed"}:
                    return self._needs_input_from_reconciliation(
                        execution_id,
                        "external_outcome_unknown",
                    )
            else:
                result = await self._tool_executor.reconcile(call.id)
                if result.status == "pending":
                    all_resolved = False

        if not all_resolved:
            return self._result(self._get_execution(execution_id))
        execution = self._get_execution(execution_id)
        if execution.status in {"waiting_external", "reconciling"}:
            self._transition(
                execution_id,
                "observing",
                event_type="action.external_observed",
                summary="Action Run reconciled external observations",
            )
            execution = self._transition(
                execution_id,
                "planning",
                event_type="action.replanning",
                summary="Action Run resumed planning after reconciliation",
            )
        return self._result(execution)

    def _complete(
        self,
        execution_id: str,
        *,
        claims: Sequence[EvidenceClaim],
        terminal_status: ActionRunStatus,
    ) -> ActionRunResult:
        result = {
            "response_text": "\n".join(claim.text for claim in claims),
            "claims": [claim.model_dump(mode="json") for claim in claims],
        }
        self._transition(
            execution_id,
            "executing",
            event_type="action.finalizing",
            summary="Action Run is finalizing its result",
        )
        self._transition(
            execution_id,
            "observing",
            event_type="action.final_observation",
            summary="Action Run persisted its final observation",
        )
        record = self._transition(
            execution_id,
            terminal_status,
            event_type=f"action.{terminal_status}",
            summary=f"Action Run {terminal_status}",
            result=result,
        )
        return self._result(record)

    def _finish_partial(
        self,
        execution_id: str,
        *,
        error_code: str,
    ) -> ActionRunResult:
        execution = self._get_execution(execution_id)
        if execution.status == "queued":
            execution = self._transition(
                execution_id,
                "planning",
                event_type="action.budget_planning",
                summary="Action Run restored its budget state",
            )
        if execution.status == "planning":
            self._transition(
                execution_id,
                "executing",
                event_type="action.budget_finalizing",
                summary="Action Run is finalizing at its configured budget",
            )
            execution = self._transition(
                execution_id,
                "observing",
                event_type="action.budget_observed",
                summary="Action Run reached its configured budget",
            )
        elif execution.status == "executing":
            execution = self._transition(
                execution_id,
                "observing",
                event_type="action.budget_observed",
                summary="Action Run reached its configured budget",
            )
        if execution.status == "observing":
            execution = self._transition(
                execution_id,
                "partial",
                event_type="action.partial",
                summary="Action Run completed partially within its budget",
                result={"error_code": error_code},
            )
        return self._result(execution)

    def _fail_unreconcilable_local_tool(
        self,
        call: AssistantToolCallRecord,
    ) -> None:
        with self._database.session() as db_session:
            repository = AssistantRepository(db_session)
            repository.update_tool_call(
                call.id,
                expected_status=call.status,
                status="failed",
                result={
                    "status": "failed",
                    "output": None,
                    "external_reference": call.external_reference_json,
                    "error_code": "local_tool_cannot_resume",
                    "error_message": (
                        "The interrupted local tool cannot resume automatically."
                    ),
                    "retryable": True,
                },
                external_reference=call.external_reference_json,
                error_code="local_tool_cannot_resume",
                error_message=(
                    "The interrupted local tool cannot resume automatically."
                ),
            )
            repository.append_observation(
                execution_id=call.execution_id,
                source="system",
                source_ref=call.id,
                observation={
                    "tool_name": call.tool_name,
                    "status": "failed",
                    "error_code": "local_tool_cannot_resume",
                },
            )
            db_session.commit()

    def _needs_input(
        self,
        execution_id: str,
        decision: NeedsInputDecision,
    ) -> ActionRunResult:
        record = self._transition(
            execution_id,
            "needs_input",
            event_type="action.needs_input",
            summary="Action Run needs user input",
            result={
                "question": decision.question,
                "choices": list(decision.choices),
                "evidence_refs": list(decision.evidence_refs),
            },
        )
        return self._result(record)

    def _needs_input_for_evidence(
        self,
        execution_id: str,
        snapshot: ContextSnapshot,
    ) -> ActionRunResult:
        return self._needs_input(
            execution_id,
            NeedsInputDecision(
                decision_summary="External writes remain evidence-gated",
                question="Please confirm or clarify the evidence required for this action.",
                evidence_refs=(snapshot.evidence_refs[0],),
            ),
        )

    def _needs_input_for_freshness(
        self,
        execution_id: str,
        snapshot: ContextSnapshot,
    ) -> ActionRunResult:
        return self._needs_input(
            execution_id,
            NeedsInputDecision(
                decision_summary="Meeting context is not fresh enough to write",
                question="Please wait for meeting context to catch up before executing.",
                evidence_refs=(snapshot.evidence_refs[0],),
            ),
        )

    def _needs_input_from_reconciliation(
        self,
        execution_id: str,
        error_code: str,
    ) -> ActionRunResult:
        execution = self._get_execution(execution_id)
        if execution.status == "waiting_external":
            execution = self._transition(
                execution_id,
                "reconciling",
                event_type="action.reconciling",
                summary="Action Run is reconciling an external operation",
            )
        record = self._transition(
            execution_id,
            "needs_input",
            event_type="action.needs_input",
            summary="Action Run needs input for an uncertain external outcome",
            result={
                "question": (
                    "The external action could not be verified automatically. "
                    "Please inspect the external system before continuing."
                ),
                "choices": [],
                "error_code": error_code,
            },
        )
        return self._result(record)

    def _fail(self, execution_id: str, *, error_code: str) -> ActionRunResult:
        execution = self._get_execution(execution_id)
        if execution.status not in TERMINAL_ACTION_STATUSES:
            execution = self._transition(
                execution_id,
                "failed",
                event_type="action.failed",
                summary="Action Run ended without a completed result",
                error_code=error_code,
                error_message="The Action Run could not complete.",
            )
        return self._result(execution)

    def _transition(
        self,
        execution_id: str,
        target_status: str,
        *,
        event_type: str,
        summary: str,
        snapshot_id: str | None = None,
        result: Mapping[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> AssistantExecutionRecord:
        with self._database.session() as db_session:
            if self._execution_guard is not None and target_status not in {"failed", "cancelled"}:
                self._execution_guard.fence(db_session, execution_id)
            repository = AssistantRepository(db_session)
            execution = repository.get_execution_required(execution_id)
            execution = repository.transition_execution(
                execution.id,
                expected_version=execution.state_version,
                target_status=target_status,
                event_type=event_type,
                summary=summary,
                snapshot_id=snapshot_id,
                result=result,
                error_code=error_code,
                error_message=error_message,
            )
            db_session.commit()
            return execution

    def _get_execution(self, execution_id: str) -> AssistantExecutionRecord:
        with self._database.session() as db_session:
            return AssistantRepository(db_session).get_execution_required(execution_id)

    @staticmethod
    def _snapshot_from_record(
        record: AssistantContextSnapshotRecord,
    ) -> ContextSnapshot:
        return ContextSnapshot(
            snapshot_id=record.id,
            session_id=record.session_id,
            meeting_state_version=record.meeting_state_version,
            state_slice=record.state_slice_json,
            source_frontier=record.source_frontier_json,
            evidence_refs=tuple(record.evidence_refs_json),
            evidence_messages=tuple(record.evidence_messages_json),
            relevant_context_hash=record.relevant_context_hash,
            created_at=record.created_at,
        )

    @staticmethod
    def _observation_from_record(record) -> PlanningObservation:
        payload = dict(record.observation_json)
        summary = payload.pop("summary", None)
        nested_payload = payload.pop("payload", None)
        if summary is None:
            tool_name = payload.get("tool_name", "tool")
            status = payload.get("status", "observed")
            summary = f"{tool_name} returned {status}"
        return PlanningObservation(
            observation_id=record.id,
            source=record.source,
            summary=summary,
            payload=(nested_payload if isinstance(nested_payload, dict) else payload),
            evidence_refs=tuple(record.evidence_refs_json),
        )

    @staticmethod
    def _result(execution: AssistantExecutionRecord) -> ActionRunResult:
        payload = dict(execution.result_json or {})
        return ActionRunResult(
            execution_id=execution.id,
            status=execution.status,
            snapshot_id=execution.snapshot_id,
            response_text=payload.get("response_text"),
            claims=tuple(
                EvidenceClaim.model_validate(value)
                for value in payload.get("claims", ())
            ),
            question=payload.get("question"),
            choices=tuple(payload.get("choices", ())),
            error_code=execution.error_code or payload.get("error_code"),
        )


__all__ = [
    "ActionInvocationPolicy",
    "ActionPlanner",
    "ActionRunBudget",
    "ActionRunResult",
    "ActionRunRunner",
    "DefaultActionInvocationPolicy",
    "FreshnessCheck",
    "RelevantContextGuard",
]
