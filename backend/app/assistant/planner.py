from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.assistant.models import ContextSnapshot, ToolEffect
from app.assistant.parser import (
    AgentDecision,
    HandoffDecision,
    InvokeToolsDecision,
    NeedsInputDecision,
    parse_agent_decision,
)
from app.assistant.prompts import planner_system_prompt, planner_user_prompt
from app.assistant.state_machine import ExecutionProfile
from app.text_processing.parser import ScriptOutputError
from app.text_processing.provider import (
    StructuredCompletionRequest,
    StructuredTextProvider,
)


class FrozenPlannerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AvailableTool(FrozenPlannerModel):
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    capability: str = Field(min_length=1, max_length=128)
    effect: ToolEffect
    description: str = Field(min_length=1, max_length=1_000)
    input_schema: dict[str, Any]

    @field_validator("name", "version", "capability", "description")
    @classmethod
    def visible_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("tool metadata must not be blank")
        return normalized


class PlanningObservation(FrozenPlannerModel):
    observation_id: str = Field(min_length=1, max_length=255)
    source: Literal["tool", "model", "system", "user"]
    summary: str = Field(min_length=1, max_length=2_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=64)

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("observation summary must not be blank")
        return normalized


class PlannerLimits(FrozenPlannerModel):
    max_input_chars: int = Field(default=24_000, ge=4_000, le=100_000)
    max_observations: int = Field(default=20, ge=0, le=100)
    max_tools: int = Field(default=20, ge=0, le=100)
    fast_max_tool_calls: int = Field(default=2, ge=0, le=4)
    action_max_tool_calls: int = Field(default=4, ge=0, le=4)


class PlannerOutcome(FrozenPlannerModel):
    decision: AgentDecision
    model_call_count: int = Field(ge=1, le=2)
    repair_attempted: bool = False


def _candidate_ids(snapshot: ContextSnapshot) -> frozenset[str]:
    meeting_state = snapshot.state_slice.get("meeting_state", {})
    if not isinstance(meeting_state, Mapping):
        return frozenset()
    values = meeting_state.get("action_candidates", ())
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return frozenset()
    identifiers: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            candidate_id = value.get("candidate_id")
            if isinstance(candidate_id, str):
                identifiers.add(candidate_id)
    return frozenset(identifiers)


def _json_chars(payload: Mapping[str, object]) -> int:
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


class BoundedPlanner:
    """One-decision planner with profile-specific repair and tool boundaries."""

    def __init__(
        self,
        provider: StructuredTextProvider,
        *,
        limits: PlannerLimits | None = None,
        execution_guard=None,
    ) -> None:
        self._provider = provider
        self._limits = limits or PlannerLimits()
        self._execution_guard = execution_guard

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name

    @property
    def model(self) -> str:
        return self._provider.model

    async def decide(
        self,
        *,
        profile: ExecutionProfile,
        goal: str,
        snapshot: ContextSnapshot,
        available_tools: Sequence[AvailableTool] = (),
        observations: Sequence[PlanningObservation] = (),
        remaining_budget: Mapping[str, object] | None = None,
    ) -> PlannerOutcome:
        normalized_goal = " ".join(goal.split())
        if not normalized_goal:
            raise ValueError("planner goal is required")
        tools = self._bounded_tools(profile, available_tools)
        bounded_observations = (
            tuple(observations[-self._limits.max_observations :])
            if self._limits.max_observations
            else ()
        )
        allowed_evidence = frozenset(
            (*snapshot.evidence_refs, *(value.observation_id for value in bounded_observations))
        )
        payload: dict[str, object] = {
            "profile": profile,
            "goal": normalized_goal,
            "snapshot": snapshot.model_dump(mode="json"),
            "observations": [
                value.model_dump(mode="json") for value in bounded_observations
            ],
            "available_tools": [value.model_dump(mode="json") for value in tools],
            "available_evidence_refs": sorted(allowed_evidence),
            "allowed_candidate_ids": sorted(_candidate_ids(snapshot)),
            "remaining_budget": dict(remaining_budget or {}),
        }
        if _json_chars(payload) > self._limits.max_input_chars:
            raise ValueError("bounded planner input exceeds its character limit")

        def check_authority():
            if self._execution_guard is not None:
                execution_id = (remaining_budget or {}).get("execution_id")
                self._execution_guard.check(execution_id, allow_unowned_read=profile == "fast_turn")

        check_authority()
        completion = await self._provider.complete_structured(
            StructuredCompletionRequest(
                system_prompt=planner_system_prompt(profile),
                user_prompt=planner_user_prompt(profile),
                input_payload=payload,
            )
        )
        model_call_count = 1
        try:
            decision = self._parse_and_validate(
                content=completion.content,
                finish_reason=completion.finish_reason,
                profile=profile,
                tools=tools,
                allowed_evidence=allowed_evidence,
                allowed_candidate_ids=_candidate_ids(snapshot),
            )
        except ScriptOutputError:
            remaining_model_calls = int(
                (remaining_budget or {}).get("model_calls", 2)
            )
            if profile == "fast_turn" or remaining_model_calls < 2:
                raise
            repair_payload = self._repair_payload(payload, completion.content)
            check_authority()
            repair = await self._provider.complete_structured(
                StructuredCompletionRequest(
                    system_prompt=planner_system_prompt(profile),
                    user_prompt=planner_user_prompt(profile, repair=True),
                    input_payload=repair_payload,
                )
            )
            model_call_count += 1
            decision = self._parse_and_validate(
                content=repair.content,
                finish_reason=repair.finish_reason,
                profile=profile,
                tools=tools,
                allowed_evidence=allowed_evidence,
                allowed_candidate_ids=_candidate_ids(snapshot),
            )
        return PlannerOutcome(
            decision=decision,
            model_call_count=model_call_count,
            repair_attempted=model_call_count > 1,
        )

    def _bounded_tools(
        self,
        profile: ExecutionProfile,
        available_tools: Sequence[AvailableTool],
    ) -> tuple[AvailableTool, ...]:
        names: set[str] = set()
        values: list[AvailableTool] = []
        for tool in available_tools:
            if tool.name in names:
                raise ValueError(f"duplicate available tool name: {tool.name}")
            names.add(tool.name)
            if profile == "fast_turn" and tool.effect == "external_write":
                continue
            values.append(tool)
        if len(values) > self._limits.max_tools:
            raise ValueError("available tools exceed the bounded planner limit")
        return tuple(values)

    def _parse_and_validate(
        self,
        *,
        content: str,
        finish_reason: str | None,
        profile: ExecutionProfile,
        tools: Sequence[AvailableTool],
        allowed_evidence: frozenset[str],
        allowed_candidate_ids: frozenset[str],
    ) -> AgentDecision:
        if finish_reason == "length":
            raise ScriptOutputError("planner JSON output was truncated")
        decision = parse_agent_decision(
            content,
            allowed_evidence_refs=allowed_evidence,
            available_tool_names=frozenset(tool.name for tool in tools),
            allowed_candidate_ids=allowed_candidate_ids,
        )
        if profile == "fast_turn" and isinstance(decision, NeedsInputDecision):
            raise ScriptOutputError("Fast Turn cannot enter durable NeedsInput")
        if profile == "action_run" and isinstance(decision, HandoffDecision):
            raise ScriptOutputError("Action Run cannot hand off to another run")
        if isinstance(decision, InvokeToolsDecision):
            limit = (
                self._limits.fast_max_tool_calls
                if profile == "fast_turn"
                else self._limits.action_max_tool_calls
            )
            if len(decision.tool_calls) > limit:
                raise ScriptOutputError("planner requested too many tools in one decision")
            effects = {tool.name: tool.effect for tool in tools}
            if profile == "fast_turn" and any(
                effects[call.tool_name] == "external_write"
                for call in decision.tool_calls
            ):
                raise ScriptOutputError("Fast Turn cannot request external writes")
        return decision

    def _repair_payload(
        self,
        payload: Mapping[str, object],
        invalid_content: str,
    ) -> dict[str, object]:
        invalid_digest = hashlib.sha256(invalid_content.encode("utf-8")).hexdigest()
        repaired = {
            **payload,
            "repair": {
                "invalid_output_digest": invalid_digest,
                "invalid_output_excerpt": invalid_content[:1_000],
                "instruction": "Return a replacement object; do not explain the error.",
            },
        }
        if _json_chars(repaired) <= self._limits.max_input_chars:
            return repaired
        repaired["repair"] = {
            "invalid_output_digest": invalid_digest,
            "instruction": "Return a replacement object; do not explain the error.",
        }
        if _json_chars(repaired) > self._limits.max_input_chars:
            raise ScriptOutputError("planner repair input exceeds its character limit")
        return repaired


Planner = BoundedPlanner


__all__ = [
    "AvailableTool",
    "BoundedPlanner",
    "Planner",
    "PlannerLimits",
    "PlannerOutcome",
    "PlanningObservation",
]
