from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.assistant.action_scheduler import ActionRunScheduler
from app.assistant.repository import AssistantRepository
from app.assistant.tools import ToolRegistry
from app.persistence.database import Database
from app.persistence.models import AssistantExecutionRecord


class RecoveryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enqueued_execution_ids: tuple[str, ...]
    reset_execution_count: int = Field(ge=0)
    reconciled_execution_count: int = Field(ge=0)
    reset_subagent_count: int = Field(ge=0)
    inspected_claim_count: int = Field(ge=0)


class ActionRunRecovery:
    """Repairs durable startup state without repeating an unknown mutation."""

    _ACTIVE_STATUSES = (
        "queued",
        "planning",
        "executing",
        "observing",
        "waiting_external",
        "reconciling",
    )

    def __init__(
        self,
        database: Database,
        registry: ToolRegistry,
        scheduler: ActionRunScheduler,
        *, execution_guard=None, tool_executor=None,
    ) -> None:
        self._database = database
        self._registry = registry
        self._scheduler = scheduler
        self._execution_guard = execution_guard
        self._tool_executor = tool_executor

    async def recover_startup(self) -> RecoveryReport:
        enqueue_ids: list[str] = []
        reset_executions = 0
        reconciled_executions = 0
        reset_subagents = 0
        read_only_ids = []
        with self._database.session() as db_session:
            if self._execution_guard is not None:
                from app.plugins.host_actions import begin_write
                begin_write(db_session)
            repository = AssistantRepository(db_session)
            executions = repository.list_action_executions(
                statuses=self._ACTIVE_STATUSES
            )
            claims = repository.list_external_action_claims(
                statuses=("reserved", "requesting", "unknown")
            )
            orphan_execution_ids: set[str] = set()
            for claim in claims:
                tool_call = (
                    repository.get_tool_call(claim.tool_call_id)
                    if claim.tool_call_id is not None
                    else None
                )
                if (
                    tool_call is not None
                    and tool_call.execution_id == claim.holder_execution_id
                    and tool_call.capability == claim.capability
                    and tool_call.logical_action_key == claim.logical_action_key
                ):
                    continue
                execution = repository.get_execution(claim.holder_execution_id)
                if (
                    execution is None
                    or execution.status not in self._ACTIVE_STATUSES
                ):
                    continue
                self._move_to_needs_input(
                    repository,
                    execution,
                    error_code="orphan_external_action_claim",
                    question=(
                        "Inspect the external system before continuing: a durable "
                        "action claim has no matching tool call."
                    ),
                )
                orphan_execution_ids.add(execution.id)
            for execution in executions:
                if self._execution_guard is not None:
                    from app.assistant.plugin_repository import MeetingPluginDenied
                    try:
                        self._execution_guard.check_db(db_session, execution.id)
                        # A model may have completed before the process died.
                        # Without a durable answer, do not rebill automatically.
                        if execution.status not in {"queued", "waiting_external", "reconciling"}:
                            raise MeetingPluginDenied("interrupted model requires confirmation")
                    except MeetingPluginDenied:
                        read_only_ids.append(execution.id)
                        continue
                for branch in repository.list_subagent_runs(execution.id):
                    if branch.status == "running":
                        repository.transition_subagent_run(
                            branch.id,
                            expected_status="running",
                            status="queued",
                            result=branch.result_json,
                            error_code=None,
                            error_message=None,
                        )
                        reset_subagents += 1

                if execution.id in orphan_execution_ids:
                    continue

                external_calls = repository.list_tool_calls(
                    execution.id,
                    statuses=(
                        "requesting",
                        "pending",
                        "unknown",
                        "reconciling",
                    ),
                    effect="external_write",
                )
                if external_calls:
                    unverifiable = False
                    for call in external_calls:
                        registered = self._registry.get(call.tool_name)
                        if registered.spec.supports_reconciliation:
                            if call.status == "requesting":
                                call = repository.update_tool_call(
                                    call.id,
                                    expected_status="requesting",
                                    status="unknown",
                                    result=call.result_json,
                                    external_reference=call.external_reference_json,
                                    error_code=call.error_code or "tool_outcome_unknown_after_restart",
                                    error_message=call.error_message or "The external outcome requires reconciliation after restart.",
                                )
                            repository.update_tool_call(
                                call.id,
                                expected_status=call.status,
                                status="reconciling",
                                result=call.result_json,
                                external_reference=call.external_reference_json,
                                error_code=call.error_code,
                                error_message=call.error_message,
                            )
                        else:
                            repository.update_tool_call(
                                call.id,
                                expected_status=call.status,
                                status="unknown",
                                result=call.result_json,
                                external_reference=call.external_reference_json,
                                error_code="unverifiable_external_side_effect",
                                error_message=(
                                    "The external outcome cannot be verified automatically."
                                ),
                            )
                            unverifiable = True
                    execution = self._move_to_reconciling(repository, execution)
                    reconciled_executions += 1
                    if unverifiable:
                        execution = repository.transition_execution(
                            execution.id,
                            expected_version=execution.state_version,
                            target_status="needs_input",
                            event_type="action.needs_input",
                            summary=(
                                "Action Run needs input for an unverifiable external outcome"
                            ),
                            result={
                                "question": (
                                    "Inspect the external system before continuing this action."
                                ),
                                "choices": [],
                                "error_code": "unverifiable_external_side_effect",
                            },
                        )
                    else:
                        enqueue_ids.append(execution.id)
                    continue

                if execution.status == "executing":
                    interrupted_calls = repository.list_tool_calls(
                        execution.id,
                        statuses=("prepared", "requesting", "pending"),
                    )
                    for call in (
                        value
                        for value in interrupted_calls
                        if value.effect != "external_write"
                    ):
                        repository.update_tool_call(
                            call.id,
                            expected_status=call.status,
                            status="failed",
                            result={
                                "status": "failed",
                                "output": None,
                                "external_reference": (
                                    call.external_reference_json
                                ),
                                "error_code": "tool_interrupted_before_recovery",
                                "error_message": (
                                    "The non-external tool was interrupted and was not "
                                    "automatically repeated."
                                ),
                                "retryable": True,
                            },
                            external_reference=call.external_reference_json,
                            error_code="tool_interrupted_before_recovery",
                            error_message=(
                                "The non-external tool was interrupted and was not "
                                "automatically repeated."
                            ),
                        )
                        repository.append_observation(
                            execution_id=execution.id,
                            source="system",
                            source_ref=call.id,
                            observation={
                                "tool_name": call.tool_name,
                                "status": "failed",
                                "error_code": "tool_interrupted_before_recovery",
                            },
                        )
                    execution = repository.transition_execution(
                        execution.id,
                        expected_version=execution.state_version,
                        target_status="observing",
                        event_type="action.recovered_observing",
                        summary="Action Run recovered interrupted local work",
                    )

                if execution.status in {"planning", "observing"}:
                    execution = repository.transition_execution(
                        execution.id,
                        expected_version=execution.state_version,
                        target_status="queued",
                        event_type="action.recovered_queued",
                        summary="Action Run returned to the durable queue",
                    )
                    reset_executions += 1
                if execution.status in {
                    "queued",
                    "waiting_external",
                    "reconciling",
                }:
                    enqueue_ids.append(execution.id)
            db_session.commit()

        for execution_id in read_only_ids:
            if await self._execution_guard.reconcile_read_only(execution_id, self._tool_executor):
                reconciled_executions += 1

        unique_ids = tuple(dict.fromkeys(enqueue_ids))
        for execution_id in unique_ids:
            await self._scheduler.enqueue(execution_id)
        return RecoveryReport(
            enqueued_execution_ids=unique_ids,
            reset_execution_count=reset_executions,
            reconciled_execution_count=reconciled_executions,
            reset_subagent_count=reset_subagents,
            inspected_claim_count=len(claims),
        )

    @staticmethod
    def _move_to_reconciling(
        repository: AssistantRepository,
        execution: AssistantExecutionRecord,
    ) -> AssistantExecutionRecord:
        if execution.status == "reconciling":
            return execution
        if execution.status == "waiting_external":
            return repository.transition_execution(
                execution.id,
                expected_version=execution.state_version,
                target_status="reconciling",
                event_type="action.recovered_reconciling",
                summary="Action Run resumed external reconciliation",
            )
        if execution.status == "observing":
            execution = repository.transition_execution(
                execution.id,
                expected_version=execution.state_version,
                target_status="queued",
                event_type="action.recovered_queued",
                summary="Action Run returned to the durable queue",
            )
        if execution.status == "queued":
            execution = repository.transition_execution(
                execution.id,
                expected_version=execution.state_version,
                target_status="planning",
                event_type="action.recovered_planning",
                summary="Action Run restored its planning phase",
            )
        if execution.status == "planning":
            execution = repository.transition_execution(
                execution.id,
                expected_version=execution.state_version,
                target_status="executing",
                event_type="action.recovered_execution",
                summary="Action Run restored its external execution phase",
            )
        if execution.status != "executing":
            raise RuntimeError(
                f"cannot recover external operation from {execution.status}"
            )
        return repository.transition_execution(
            execution.id,
            expected_version=execution.state_version,
            target_status="reconciling",
            event_type="action.recovered_reconciling",
            summary="Action Run will reconcile the external operation",
        )

    @staticmethod
    def _move_to_needs_input(
        repository: AssistantRepository,
        execution: AssistantExecutionRecord,
        *,
        error_code: str,
        question: str,
    ) -> AssistantExecutionRecord:
        if execution.status == "queued":
            execution = repository.transition_execution(
                execution.id,
                expected_version=execution.state_version,
                target_status="planning",
                event_type="action.recovered_planning",
                summary="Action Run restored its planning phase",
            )
        elif execution.status == "executing":
            execution = repository.transition_execution(
                execution.id,
                expected_version=execution.state_version,
                target_status="observing",
                event_type="action.recovered_observing",
                summary="Action Run isolated an orphan external action claim",
            )
        elif execution.status == "waiting_external":
            execution = repository.transition_execution(
                execution.id,
                expected_version=execution.state_version,
                target_status="reconciling",
                event_type="action.recovered_reconciling",
                summary="Action Run isolated an orphan external action claim",
            )
        if execution.status == "observing":
            execution = repository.transition_execution(
                execution.id,
                expected_version=execution.state_version,
                target_status="planning",
                event_type="action.recovered_planning",
                summary="Action Run restored its planning phase",
            )
        if execution.status not in {"planning", "reconciling"}:
            raise RuntimeError(
                f"cannot isolate orphan action claim from {execution.status}"
            )
        return repository.transition_execution(
            execution.id,
            expected_version=execution.state_version,
            target_status="needs_input",
            event_type="action.needs_input",
            summary="Action Run needs input for an orphan external action claim",
            result={
                "question": question,
                "choices": [],
                "error_code": error_code,
            },
            error_code=error_code,
            error_message=question,
        )


__all__ = ["ActionRunRecovery", "RecoveryReport"]
