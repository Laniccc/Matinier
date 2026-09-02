from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field

from app.assistant.action_runner import (
    ActionInvocationPolicy,
    DefaultActionInvocationPolicy,
)
from app.assistant.parser import ProposedToolCall
from app.assistant.tools import (
    ToolAdapter,
    ToolExecutionContext,
    ToolInvocation,
    ToolRegistration,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from app.persistence.models import AssistantExecutionRecord
from app.assistant.models import ContextSnapshot
from app.assistant.repository import AssistantRepository
from app.assistant.subagents import SUBAGENT_ROLES, SubagentAnalysis
from app.task_system.models import (
    ExternalTask,
    TaskDecisionContext,
    TaskSearchResult,
    TaskServiceOutcome,
)
from app.task_system.service import TaskNeedsInput, TaskSystemService


class FrozenTaskToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskSearchToolInput(FrozenTaskToolModel):
    candidate_id: str = Field(min_length=1, max_length=36)
    candidate_revision: int = Field(ge=1)


class TaskSearchToolOutput(FrozenTaskToolModel):
    result: TaskSearchResult


class TaskGetToolInput(FrozenTaskToolModel):
    task_ref: str = Field(min_length=1, max_length=255)


class TaskGetToolOutput(FrozenTaskToolModel):
    task: ExternalTask


class TaskCreateToolInput(FrozenTaskToolModel):
    candidate_id: str = Field(min_length=1, max_length=36)
    candidate_revision: int = Field(ge=1)
    responsible_subject_required: bool = False


class TaskCreateToolOutput(FrozenTaskToolModel):
    outcome: TaskServiceOutcome


class _TaskToolBase:
    def __init__(self, service: TaskSystemService) -> None:
        self._service = service

    @property
    def provider_name(self) -> str:
        return self._service.adapter.provider_name


class TaskSearchToolAdapter(_TaskToolBase):
    async def execute(self, context: ToolExecutionContext) -> ToolResult:
        request = TaskSearchToolInput.model_validate(context.arguments)
        result = await self._service.search_candidate(
            request.candidate_id,
            expected_revision=request.candidate_revision,
        )
        return ToolResult(
            status="succeeded",
            output=TaskSearchToolOutput(result=result).model_dump(mode="json"),
        )

    async def reconcile(self, context: ToolExecutionContext) -> ToolResult:
        return await self.execute(context)


class TaskGetToolAdapter(_TaskToolBase):
    async def execute(self, context: ToolExecutionContext) -> ToolResult:
        request = TaskGetToolInput.model_validate(context.arguments)
        task = await self._service.get(request.task_ref)
        return ToolResult(
            status="succeeded",
            output=TaskGetToolOutput(task=task).model_dump(mode="json"),
        )

    async def reconcile(self, context: ToolExecutionContext) -> ToolResult:
        return await self.execute(context)


class TaskCreateToolAdapter(_TaskToolBase):
    async def execute(self, context: ToolExecutionContext) -> ToolResult:
        request = TaskCreateToolInput.model_validate(context.arguments)
        if context.candidate_id != request.candidate_id:
            return ToolResult(
                status="failed",
                error_code="candidate_context_mismatch",
                error_message="The task Candidate does not match the authorized action.",
                confirmed_side_effects=0,
            )
        expected_key = self._service.action_key_for_candidate(
            request.candidate_id,
            expected_revision=request.candidate_revision,
        )
        if context.logical_action_key != expected_key:
            return ToolResult(
                status="failed",
                error_code="task_action_key_mismatch",
                error_message="The task action key does not match the Candidate lineage.",
                confirmed_side_effects=0,
            )
        if context.grant is None:
            return ToolResult(
                status="failed",
                error_code="task_grant_missing",
                error_message="The task creation authorization is unavailable.",
                confirmed_side_effects=0,
            )
        try:
            outcome = await self._service.execute_candidate(
                request.candidate_id,
                expected_revision=request.candidate_revision,
                actor_id=context.grant.actor_id,
                decision_context=self._trusted_decision_context(
                    context.execution_id
                ),
                responsible_subject_required=(
                    request.responsible_subject_required
                ),
            )
        except TaskNeedsInput as error:
            return ToolResult(
                status="failed",
                error_code=error.code,
                error_message=error.question,
                confirmed_side_effects=0,
            )
        if outcome.task is None:
            return ToolResult(
                status="failed",
                error_code="task_result_missing",
                error_message="The task workflow returned no task to verify.",
                confirmed_side_effects=0,
            )
        return ToolResult(
            status="succeeded",
            output=TaskCreateToolOutput(outcome=outcome).model_dump(mode="json"),
            external_reference={
                "task_ref": outcome.task.external_id,
                "identifier": outcome.task.identifier,
                "url": outcome.task.url,
                "action_key": outcome.action_key,
            },
            confirmed_side_effects=1 if outcome.created else 0,
        )

    def _trusted_decision_context(
        self,
        execution_id: str,
    ) -> TaskDecisionContext:
        with self._service.database.session() as db_session:
            records = AssistantRepository(db_session).list_subagent_runs(
                execution_id
            )
            latest = {
                role: next(
                    (
                        record
                        for record in reversed(records)
                        if record.role == role
                    ),
                    None,
                )
                for role in SUBAGENT_ROLES
            }

        def agrees(role: str) -> bool:
            record = latest.get(role)
            if (
                record is None
                or record.status != "completed"
                or record.result_json is None
            ):
                return False
            analysis = SubagentAnalysis.model_validate(record.result_json)
            return (
                not analysis.blocks_external_write
                and (role != "evidence" or bool(analysis.evidence_refs))
            )

        return TaskDecisionContext(
            evidence_confirms_single_deliverable=agrees("evidence"),
            conflict_agent_agrees=agrees("conflict"),
            research_agent_agrees=agrees("linear_research"),
        )

    async def reconcile(self, context: ToolExecutionContext) -> ToolResult:
        request = TaskCreateToolInput.model_validate(context.arguments)
        result = await self._service.reconcile_candidate(
            request.candidate_id,
            expected_revision=request.candidate_revision,
        )
        if result.status == "none":
            return ToolResult(
                status="unknown",
                external_reference=context.external_reference,
                error_code="task_create_not_found",
                error_message="No task with the exact action key was found yet.",
            )
        if result.status == "multiple":
            return ToolResult(
                status="unknown",
                external_reference=context.external_reference,
                error_code="duplicate_external_side_effect",
                error_message="Multiple tasks share the exact action key.",
            )
        task = await self._service.get(result.matches[0].external_id)
        outcome = TaskServiceOutcome(
            disposition="same_action",
            action_key=task.action_key
            or self._service.action_key_for_candidate(
                request.candidate_id,
                expected_revision=request.candidate_revision,
            ),
            task=task,
            created=True,
        )
        return ToolResult(
            status="succeeded",
            output=TaskCreateToolOutput(outcome=outcome).model_dump(mode="json"),
            external_reference={
                "task_ref": task.external_id,
                "identifier": task.identifier,
                "url": task.url,
                "action_key": outcome.action_key,
            },
            confirmed_side_effects=1,
        )


class TaskSystemInvocationPolicy(ActionInvocationPolicy):
    """Binds task.create to server Team config and Candidate lineage."""

    def __init__(self, service: TaskSystemService) -> None:
        self._service = service
        self._fallback = DefaultActionInvocationPolicy()

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
        if spec.name != "task.create":
            return self._fallback.build(
                execution=execution,
                snapshot=snapshot,
                call=call,
                spec=spec,
                planning_round=planning_round,
                call_index=call_index,
                step_id=step_id,
            )
        request = TaskCreateToolInput.model_validate(call.arguments)
        logical_action_key = self._service.action_key_for_candidate(
            request.candidate_id,
            expected_revision=request.candidate_revision,
        )
        idempotency_key = hashlib.sha256(
            (
                f"{execution.grant_id or 'no-grant'}:"
                f"{spec.capability}:{logical_action_key}"
            ).encode("utf-8")
        ).hexdigest()
        return ToolInvocation(
            execution_id=execution.id,
            tool_name=spec.name,
            arguments=call.arguments,
            grant_id=execution.grant_id,
            candidate_id=request.candidate_id,
            requested_resource_scope={
                "linear_team_id": self._service.adapter.connection.team_id
            },
            logical_action_key=logical_action_key,
            idempotency_key=idempotency_key,
            evidence_refs=call.evidence_refs,
            step_id=step_id,
        )


class TaskSystemToolProvider:
    """Expose task tools through the same protocol-neutral provider seam."""

    def __init__(self, service: TaskSystemService, *, timeout_seconds: float = 15.0):
        self._service = service
        self._timeout_seconds = timeout_seconds

    @property
    def provider_name(self) -> str:
        return self._service.adapter.provider_name

    def registrations(self) -> tuple[ToolRegistration, ...]:
        return (
            ToolRegistration(ToolSpec(
            name="task.search",
            version="1",
            capability="task.search",
            effect="read",
            input_model=TaskSearchToolInput,
            output_model=TaskSearchToolOutput,
            timeout_seconds=self._timeout_seconds,
            supports_idempotency=False,
            supports_reconciliation=False,
        ), TaskSearchToolAdapter(self._service)),
            ToolRegistration(ToolSpec(
            name="task.get",
            version="1",
            capability="task.get",
            effect="read",
            input_model=TaskGetToolInput,
            output_model=TaskGetToolOutput,
            timeout_seconds=self._timeout_seconds,
            supports_idempotency=False,
            supports_reconciliation=False,
        ), TaskGetToolAdapter(self._service)),
            ToolRegistration(ToolSpec(
            name="task.create",
            version="1",
            capability="task.create",
            effect="external_write",
            input_model=TaskCreateToolInput,
            output_model=TaskCreateToolOutput,
            timeout_seconds=self._timeout_seconds,
            supports_idempotency=True,
            supports_reconciliation=True,
            allowed_profiles=frozenset({"action_run"}),
        ), TaskCreateToolAdapter(self._service)),
    )


def register_task_tools(
    registry: ToolRegistry,
    service: TaskSystemService,
    *,
    timeout_seconds: float = 15.0,
) -> None:
    registry.register_provider(TaskSystemToolProvider(
        service,
        timeout_seconds=timeout_seconds,
    ))


__all__ = [
    "TaskCreateToolInput",
    "TaskCreateToolOutput",
    "TaskGetToolInput",
    "TaskGetToolOutput",
    "TaskSearchToolInput",
    "TaskSearchToolOutput",
    "TaskSystemInvocationPolicy",
    "TaskSystemToolProvider",
    "register_task_tools",
]
