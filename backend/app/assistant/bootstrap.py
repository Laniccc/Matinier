from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError

from app.assistant.action_runner import (
    ActionInvocationPolicy,
    ActionPlanner,
    ActionRunBudget,
    ActionRunRunner,
)
from app.assistant.action_scheduler import ActionRunScheduler
from app.assistant.critic import Critic, EvidenceBoundCritic
from app.assistant.fast_runner import FastTurnBudget, FastTurnRunner
from app.assistant.handoff import HandoffService
from app.assistant.models import ContextSnapshot, SubagentRole
from app.assistant.planner import (
    AvailableTool,
    BoundedPlanner,
    PlanningObservation,
)
from app.assistant.recovery import ActionRunRecovery, RecoveryReport
from app.assistant.subagents import (
    SUBAGENT_ROLES,
    SubagentAnalysis,
    SubagentAnalyzer,
    SubagentCoordinator,
)
from app.assistant.tools import ToolExecutor, ToolRegistry
from app.meeting_state.bootstrap import build_meeting_state_projector
from app.meeting_state.projector import MeetingStateProjector
from app.persistence.database import Database
from app.settings import Settings
from app.task_system.bootstrap import (
    build_task_system_adapter,
    validate_task_system_adapter,
)
from app.task_system.contracts import TaskSystemAdapter
from app.task_system.service import TaskSystemService
from app.task_system.tools import (
    TaskSystemInvocationPolicy,
    register_task_tools,
)
from app.text_processing.deepseek_provider import DeepSeekCompletionProvider
from app.text_processing.provider import (
    StructuredCompletionRequest,
    StructuredTextProvider,
)


_SUBAGENT_SYSTEM_PROMPT = """You are one read-only analysis branch inside a
private meeting Action Run. Return exactly one JSON object with fields summary,
payload, evidence_refs, model_call_count, and blocks_external_write. Never return
hidden reasoning or Markdown. Cite only supplied evidence IDs. Do not claim that a
tool ran; when outside data is needed, place a concise recommended read in payload.
The summary must be short and user-auditable. model_call_count must be exactly 1.
evidence_refs must contain only IDs from available_evidence_refs; observation IDs
are never evidence IDs. Treat user observations as authoritative clarifications.
If the user explicitly confirms an optional identity may remain unassigned, do not
block the write solely because that identity has no external-system ID. Set
blocks_external_write only when a remaining unresolved condition actually prevents
the requested write after applying all observations."""


def _normalize_subagent_payload(
    payload: object,
    *,
    available_evidence_refs: Sequence[str],
) -> object:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    normalized["model_call_count"] = 1
    evidence_refs = normalized.get("evidence_refs")
    if isinstance(evidence_refs, list):
        allowed = set(available_evidence_refs)
        normalized["evidence_refs"] = [
            evidence_ref
            for evidence_ref in evidence_refs
            if isinstance(evidence_ref, str) and evidence_ref in allowed
        ]
    return normalized


class _StructuredSubagentAnalyzer:
    """One bounded provider call for each read-only specialist branch."""

    def __init__(self, provider: StructuredTextProvider) -> None:
        self._provider = provider

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
    ) -> SubagentAnalysis:
        completion = await self._provider.complete_structured(
            StructuredCompletionRequest(
                system_prompt=_SUBAGENT_SYSTEM_PROMPT,
                user_prompt=(
                    f"Perform the {role} branch mission using only the supplied "
                    "frozen context and observations."
                ),
                input_payload={
                    "role": role,
                    "mission": dict(task),
                    "goal": goal,
                    "snapshot": snapshot.model_dump(mode="json"),
                    "available_evidence_refs": list(snapshot.evidence_refs),
                    "available_read_tools": [
                        tool.model_dump(mode="json") for tool in available_tools
                    ],
                    "observations": [
                        observation.model_dump(mode="json")
                        for observation in observations[-20:]
                    ],
                    "remaining_model_calls": remaining_model_calls,
                },
            )
        )
        if completion.finish_reason == "length":
            raise ValueError("Subagent output was truncated")
        try:
            payload = json.loads(completion.content)
            analysis = SubagentAnalysis.model_validate(
                _normalize_subagent_payload(
                    payload,
                    available_evidence_refs=snapshot.evidence_refs,
                )
            )
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
            raise ValueError("Subagent output is not a valid analysis") from error
        return analysis.model_copy(update={"model_call_count": 1})


@dataclass(frozen=True, slots=True)
class AssistantActionRuntime:
    runner: ActionRunRunner
    scheduler: ActionRunScheduler
    recovery: ActionRunRecovery

    async def start(self) -> RecoveryReport:
        await self.scheduler.start()
        try:
            return await self.recovery.recover_startup()
        except Exception:
            await self.scheduler.stop()
            raise

    async def stop(self) -> None:
        await self.scheduler.stop()


@dataclass(frozen=True, slots=True)
class AssistantRuntime:
    projector: MeetingStateProjector
    planner: BoundedPlanner
    registry: ToolRegistry
    task_adapter: TaskSystemAdapter
    task_service: TaskSystemService
    fast_runner: FastTurnRunner
    action_runtime: AssistantActionRuntime
    operation_worker: Any = None

    async def start(self) -> RecoveryReport:
        await validate_task_system_adapter(self.task_adapter)
        await self.projector.start()
        try:
            report = await self.action_runtime.start()
            if self.operation_worker is not None:
                await self.operation_worker.start()
            return report
        except Exception:
            try:
                if self.operation_worker is not None:
                    await self.operation_worker.stop()
            finally:
                try:
                    await self.action_runtime.stop()
                finally:
                    await self.projector.stop()
            raise

    async def stop(self) -> None:
        try:
            if self.operation_worker is not None:
                await self.operation_worker.stop()
        finally:
            try:
                await self.action_runtime.stop()
            finally:
                await self.projector.stop()


def build_action_runtime(
    *,
    database: Database,
    settings: Settings,
    planner: ActionPlanner,
    registry: ToolRegistry,
    subagent_analyzers: Mapping[SubagentRole, SubagentAnalyzer],
    projector: MeetingStateProjector | None = None,
    tool_executor: ToolExecutor | None = None,
    critic: Critic | None = None,
    invocation_policy: ActionInvocationPolicy | None = None,
    execution_guard=None,
) -> AssistantActionRuntime:
    executor = tool_executor or ToolExecutor(database, registry, execution_guard=execution_guard)
    subagents = SubagentCoordinator(
        database,
        subagent_analyzers,
        max_subagents_per_run=settings.assistant_subagents_per_run,
        global_concurrency=settings.assistant_subagent_global_concurrency,
        execution_guard=execution_guard,
    )
    runner = ActionRunRunner(
        database,
        planner,
        registry,
        executor,
        subagents,
        critic or EvidenceBoundCritic(),
        projector=projector,
        invocation_policy=invocation_policy,
        execution_guard=execution_guard,
        budget=ActionRunBudget(
            max_steps=settings.assistant_action_max_steps,
            max_model_calls=settings.assistant_action_max_model_calls,
            max_planning_rounds=settings.assistant_action_max_planning_rounds,
            reconciliation_delays_seconds=(
                settings.assistant_reconciliation_delay_list
            ),
        ),
    )
    scheduler = ActionRunScheduler(
        runner,
        concurrency=settings.assistant_action_concurrency,
        resume_delay_seconds=settings.assistant_reconciliation_delay_list[0],
    )
    recovery = ActionRunRecovery(database, registry, scheduler,
        execution_guard=execution_guard, tool_executor=executor)
    return AssistantActionRuntime(
        runner=runner,
        scheduler=scheduler,
        recovery=recovery,
    )


def build_assistant_runtime(
    settings: Settings,
    database: Database,
    *,
    provider: StructuredTextProvider | None = None,
    task_adapter: TaskSystemAdapter | None = None,
    subagent_analyzers: Mapping[SubagentRole, SubagentAnalyzer] | None = None,
    meeting_operations=None,
    meeting_policy=None,
) -> AssistantRuntime | None:
    """Compose both Agent paths with the configured task-system Adapter."""

    if not settings.assistant_enabled:
        return None
    structured_provider = provider or DeepSeekCompletionProvider(
        api_key=settings.deepseek_api_key,
        base_url=str(settings.deepseek_base_url),
        model=settings.deepseek_model,
        timeout_seconds=settings.deepseek_request_timeout_seconds,
        temperature=settings.deepseek_temperature,
        max_output_tokens=settings.deepseek_max_output_tokens,
    )
    resolved_task_adapter = task_adapter or build_task_system_adapter(settings)

    projector = build_meeting_state_projector(
        settings,
        database,
        provider=structured_provider,
        policy=meeting_policy,
    )
    if projector is None:
        raise RuntimeError("Assistant projector was not built while enabled")
    registry = ToolRegistry()
    task_service = TaskSystemService(database, resolved_task_adapter)
    if resolved_task_adapter.provider_name != "disabled":
        register_task_tools(
            registry,
            task_service,
            timeout_seconds=settings.assistant_tool_timeout_seconds,
        )
    from app.plugins.host_actions import HostActions
    from app.assistant.plugin_operations import MeetingPluginOperations
    from app.assistant.plugin_execution_guard import MeetingExecutionGuard
    from app.assistant.plugin_runtime import MeetingPluginOperationWorker
    operations = meeting_operations or MeetingPluginOperations(HostActions(database, settings))
    execution_guard = MeetingExecutionGuard(operations)
    planner = BoundedPlanner(structured_provider, execution_guard=execution_guard)
    tool_executor = ToolExecutor(database, registry, execution_guard=execution_guard)
    analyzers = dict(subagent_analyzers or {})
    default_analyzer = _StructuredSubagentAnalyzer(structured_provider)
    for role in SUBAGENT_ROLES:
        analyzers.setdefault(role, default_analyzer)
    action_runtime = build_action_runtime(
        database=database,
        settings=settings,
        planner=planner,
        registry=registry,
        subagent_analyzers=analyzers,
        projector=projector,
        tool_executor=tool_executor,
        invocation_policy=TaskSystemInvocationPolicy(task_service),
        execution_guard=execution_guard,
    )
    handoff_service = HandoffService(
        database,
        enqueue=action_runtime.scheduler.enqueue,
        execution_guard=execution_guard,
    )
    fast_runner = FastTurnRunner(
        database,
        planner,
        handoff_service,
        projector=projector,
        registry=registry,
        tool_executor=tool_executor,
        execution_guard=execution_guard,
        budget=FastTurnBudget(
            timeout_seconds=settings.assistant_fast_timeout_seconds,
            max_model_rounds=settings.assistant_fast_max_model_rounds,
            max_parallel_read_tools=(
                settings.assistant_fast_max_parallel_read_tools
            ),
        ),
    )
    return AssistantRuntime(
        projector=projector,
        planner=planner,
        registry=registry,
        task_adapter=resolved_task_adapter,
        task_service=task_service,
        fast_runner=fast_runner,
        action_runtime=action_runtime,
        operation_worker=MeetingPluginOperationWorker(operations,
            SimpleNamespace(fast_runner=fast_runner, action_runtime=action_runtime)),
    )


__all__ = [
    "AssistantActionRuntime",
    "AssistantRuntime",
    "build_action_runtime",
    "build_assistant_runtime",
]
