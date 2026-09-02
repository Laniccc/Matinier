from __future__ import annotations

from app.assistant.plugin_repository import MeetingPluginDenied

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.assistant.models import ContextSnapshot, SubagentRole
from app.assistant.planner import AvailableTool, PlanningObservation
from app.assistant.repository import AssistantRepository
from app.persistence.database import Database
from app.persistence.models import AssistantSubagentRunRecord


SUBAGENT_ROLES: tuple[SubagentRole, ...] = (
    "evidence",
    "conflict",
    "linear_research",
)


class FrozenSubagentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SubagentAnalysis(FrozenSubagentModel):
    summary: str = Field(min_length=1, max_length=2_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    model_call_count: int = Field(default=1, ge=1, le=2)
    blocks_external_write: bool = False

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Subagent summary must not be blank")
        return normalized


class SubagentBranchResult(FrozenSubagentModel):
    subagent_run_id: str = Field(min_length=1, max_length=36)
    role: SubagentRole
    status: Literal["completed", "failed"]
    analysis: SubagentAnalysis | None = None
    error_code: str | None = Field(default=None, max_length=128)
    model_call_count: int = Field(default=0, ge=0, le=2)


class SubagentRoundResult(FrozenSubagentModel):
    branches: tuple[SubagentBranchResult, ...]
    observations: tuple[PlanningObservation, ...]
    model_call_count: int = Field(ge=0)


class SubagentAnalyzer(Protocol):
    async def analyze(
        self,
        *,
        role: SubagentRole,
        task: Mapping[str, object],
        goal: str,
        snapshot: ContextSnapshot,
        available_tools: Sequence[AvailableTool],
        observations: Sequence[PlanningObservation],
        remaining_model_calls: int,
    ) -> SubagentAnalysis: ...


class SubagentCoordinator:
    """Runs the three durable read-only branches without holding DB Sessions."""

    def __init__(
        self,
        database: Database,
        analyzers: Mapping[SubagentRole, SubagentAnalyzer],
        *,
        max_subagents_per_run: int = 3,
        global_concurrency: int = 6,
        max_model_calls_per_branch: int = 2,
        execution_guard=None,
    ) -> None:
        if not 0 <= max_subagents_per_run <= len(SUBAGENT_ROLES):
            raise ValueError("max_subagents_per_run must be between zero and three")
        if global_concurrency < 1:
            raise ValueError("global_concurrency must be positive")
        if not 1 <= max_model_calls_per_branch <= 2:
            raise ValueError("Subagent model-call budget must be one or two")
        missing = set(SUBAGENT_ROLES[:max_subagents_per_run]) - set(analyzers)
        if missing:
            raise ValueError(
                "missing Subagent analyzers: " + ", ".join(sorted(missing))
            )
        self._database = database
        self._analyzers = dict(analyzers)
        self._max_subagents_per_run = max_subagents_per_run
        self._max_model_calls_per_branch = max_model_calls_per_branch
        self._semaphore = asyncio.Semaphore(global_concurrency)
        self._execution_guard = execution_guard

    async def run_for_execution(
        self,
        *,
        execution_id: str,
        planning_round: int,
        goal: str,
        snapshot: ContextSnapshot,
        available_tools: Sequence[AvailableTool],
        observations: Sequence[PlanningObservation],
        remaining_model_calls: int,
    ) -> SubagentRoundResult:
        records = self._ensure_branches(
            execution_id=execution_id,
            planning_round=planning_round,
            snapshot_id=snapshot.snapshot_id,
            observations=observations,
        )
        completed: list[SubagentBranchResult] = []
        runnable: list[AssistantSubagentRunRecord] = []
        for record in records:
            if record.status == "completed" and record.result_json is not None:
                analysis = SubagentAnalysis.model_validate(record.result_json)
                completed.append(
                    SubagentBranchResult(
                        subagent_run_id=record.id,
                        role=record.role,
                        status="completed",
                        analysis=analysis,
                        model_call_count=analysis.model_call_count,
                    )
                )
            elif record.status == "failed":
                completed.append(
                    SubagentBranchResult(
                        subagent_run_id=record.id,
                        role=record.role,
                        status="failed",
                        error_code=record.error_code or "subagent_failed",
                        model_call_count=int(
                            (record.result_json or {}).get("model_call_count", 0)
                        ),
                    )
                )
            else:
                runnable.append(record)

        available_calls = max(0, remaining_model_calls)
        new_call_count = 0
        if runnable and available_calls:
            allocations: list[tuple[AssistantSubagentRunRecord, int]] = []
            remaining_calls = available_calls
            for position, record in enumerate(runnable):
                if remaining_calls <= 0:
                    break
                branches_left = len(runnable) - position
                branch_calls = min(
                    self._max_model_calls_per_branch,
                    max(1, remaining_calls - (branches_left - 1)),
                )
                allocations.append((record, branch_calls))
                remaining_calls -= branch_calls
            results = await asyncio.gather(
                *(
                    self._run_branch(
                        record_id=record.id,
                        goal=goal,
                        snapshot=snapshot,
                        available_tools=self._scoped_tools(
                            record.role,
                            available_tools,
                        ),
                        observations=observations,
                        max_calls=branch_calls,
                    )
                    for record, branch_calls in allocations
                )
            )
            new_call_count = sum(result.model_call_count for result in results)
            completed.extend(results)

        completed.sort(key=lambda branch: SUBAGENT_ROLES.index(branch.role))
        branch_observations = tuple(
            self._as_observation(branch, snapshot)
            for branch in completed
        )
        return SubagentRoundResult(
            branches=tuple(completed),
            observations=branch_observations,
            model_call_count=new_call_count,
        )

    def _ensure_branches(
        self,
        *,
        execution_id: str,
        planning_round: int,
        snapshot_id: str,
        observations: Sequence[PlanningObservation],
    ) -> list[AssistantSubagentRunRecord]:
        with self._database.session() as db_session:
            repository = AssistantRepository(db_session)
            records = repository.list_subagent_runs(execution_id)
            configured_roles = SUBAGENT_ROLES[: self._max_subagents_per_run]
            latest_by_role = {
                role: next(
                    (
                        record
                        for record in reversed(records)
                        if record.role == role
                    ),
                    None,
                )
                for role in configured_roles
            }
            roles_to_create = [
                role for role in configured_roles if latest_by_role[role] is None
            ]
            refresh_reasons: dict[SubagentRole, str] = {}
            refreshed_roles = {
                record.role
                for record in records
                if record.planning_round > 1
            }
            if planning_round > 1 and self._has_user_observation(observations):
                for role in configured_roles:
                    latest = latest_by_role[role]
                    if (
                        latest is not None
                        and role not in refreshed_roles
                        and self._requires_user_input_refresh(latest)
                    ):
                        roles_to_create.append(role)
                        refresh_reasons[role] = "user_input_observed"
            if (
                planning_round > 1
                and "linear_research" in configured_roles
                and "linear_research" not in refreshed_roles
                and "linear_research" not in refresh_reasons
                and self._has_task_search_observation(observations)
            ):
                roles_to_create.append("linear_research")
                refresh_reasons["linear_research"] = "task_search_observed"

            for role in dict.fromkeys(roles_to_create):
                latest_by_role[role] = repository.create_subagent_run(
                    execution_id=execution_id,
                    planning_round=planning_round,
                    role=role,
                    snapshot_id=snapshot_id,
                    task=self._task_for(role),
                    budget={
                        "max_model_calls": self._max_model_calls_per_branch,
                        "allowed_effects": ["read"],
                        "refresh_reason": refresh_reasons.get(
                            role,
                            "initial_analysis",
                        ),
                    },
                )
            db_session.commit()
            return [
                record
                for role in configured_roles
                if (record := latest_by_role[role]) is not None
            ]

    @staticmethod
    def _has_task_search_observation(
        observations: Sequence[PlanningObservation],
    ) -> bool:
        return any(
            observation.source == "tool"
            and observation.payload.get("tool_name") == "task.search"
            and observation.payload.get("status") == "succeeded"
            for observation in observations
        )

    @staticmethod
    def _has_user_observation(
        observations: Sequence[PlanningObservation],
    ) -> bool:
        return any(observation.source == "user" for observation in observations)

    @staticmethod
    def _requires_user_input_refresh(
        record: AssistantSubagentRunRecord,
    ) -> bool:
        return record.status != "completed" or bool(
            (record.result_json or {}).get("blocks_external_write")
        )

    async def _run_branch(
        self,
        *,
        record_id: str,
        goal: str,
        snapshot: ContextSnapshot,
        available_tools: Sequence[AvailableTool],
        observations: Sequence[PlanningObservation],
        max_calls: int,
    ) -> SubagentBranchResult:
        with self._database.session() as db_session:
            repository = AssistantRepository(db_session)
            record = repository.get_subagent_run(record_id)
            if record is None:
                raise LookupError(f"Assistant Subagent run not found: {record_id}")
            record = repository.transition_subagent_run(
                record.id, expected_status="queued", status="running"
            )
            role: SubagentRole = record.role
            task = dict(record.task_json)
            db_session.commit()

        calls = 0
        while calls < max_calls:
            calls += 1
            try:
                async with self._semaphore:
                    if self._execution_guard is not None:
                        self._execution_guard.check(record.execution_id)
                    analysis = await self._analyzers[role].analyze(
                        role=role,
                        task=task,
                        goal=goal,
                        snapshot=snapshot,
                        available_tools=available_tools,
                        observations=observations,
                        remaining_model_calls=max_calls - calls + 1,
                    )
                if not set(analysis.evidence_refs).issubset(snapshot.evidence_refs):
                    raise ValueError("Subagent cited evidence outside its Snapshot")
                consumed_calls = max(calls, analysis.model_call_count)
                if consumed_calls > max_calls:
                    raise ValueError("Subagent exceeded its model-call budget")
                analysis = analysis.model_copy(
                    update={"model_call_count": consumed_calls}
                )
                with self._database.session() as db_session:
                    if self._execution_guard is not None:
                        self._execution_guard.fence(db_session, record.execution_id)
                    AssistantRepository(db_session).transition_subagent_run(
                        record_id,
                        expected_status="running",
                        status="completed",
                        result=analysis.model_dump(mode="json"),
                    )
                    db_session.commit()
                return SubagentBranchResult(
                    subagent_run_id=record_id,
                    role=role,
                    status="completed",
                    analysis=analysis,
                    model_call_count=consumed_calls,
                )
            except MeetingPluginDenied:
                raise
            except Exception:
                if calls < max_calls:
                    continue

        with self._database.session() as db_session:
            AssistantRepository(db_session).transition_subagent_run(
                record_id,
                expected_status="running",
                status="failed",
                result={"model_call_count": calls},
                error_code="subagent_failed",
                error_message="The read-only Subagent could not complete.",
            )
            db_session.commit()
        return SubagentBranchResult(
            subagent_run_id=record_id,
            role=role,
            status="failed",
            error_code="subagent_failed",
            model_call_count=calls,
        )

    @staticmethod
    def _task_for(role: SubagentRole) -> dict[str, object]:
        missions = {
            "evidence": "Verify candidate fields and their meeting evidence.",
            "conflict": "Check duplicates, dependencies, conflicts, and corrections.",
            "linear_research": "Research related tasks and identities using read tools only.",
        }
        return {"role": role, "mission": missions[role]}

    @staticmethod
    def _scoped_tools(
        role: SubagentRole,
        available_tools: Sequence[AvailableTool],
    ) -> tuple[AvailableTool, ...]:
        read_tools = tuple(tool for tool in available_tools if tool.effect == "read")
        if role == "linear_research":
            return tuple(
                tool
                for tool in read_tools
                if tool.name.startswith("task.")
                or tool.capability.startswith("task.")
            )
        return tuple(
            tool
            for tool in read_tools
            if not tool.name.startswith("task.")
            and not tool.capability.startswith("task.")
        )

    @staticmethod
    def _as_observation(
        branch: SubagentBranchResult,
        snapshot: ContextSnapshot,
    ) -> PlanningObservation:
        if branch.analysis is not None:
            return PlanningObservation(
                observation_id=branch.subagent_run_id,
                source="model",
                summary=branch.analysis.summary,
                payload={
                    "role": branch.role,
                    "status": branch.status,
                    **branch.analysis.payload,
                },
                evidence_refs=branch.analysis.evidence_refs,
            )
        evidence_refs = (
            (snapshot.evidence_refs[0],) if snapshot.evidence_refs else ()
        )
        return PlanningObservation(
            observation_id=branch.subagent_run_id,
            source="system",
            summary=f"The {branch.role} branch did not complete.",
            payload={"role": branch.role, "status": branch.status},
            evidence_refs=evidence_refs,
        )


__all__ = [
    "SUBAGENT_ROLES",
    "SubagentAnalysis",
    "SubagentAnalyzer",
    "SubagentBranchResult",
    "SubagentCoordinator",
    "SubagentRoundResult",
]
