from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from app.text_processing.parser import ScriptOutputError


class FrozenDecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceClaim(FrozenDecisionModel):
    text: str = Field(min_length=1, max_length=4_000)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("claim text must not be blank")
        return normalized

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("claim evidence references must be unique")
        return value


class DecisionBase(FrozenDecisionModel):
    decision_summary: str = Field(min_length=1, max_length=1_000)

    @field_validator("decision_summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("decision summary must not be blank")
        return normalized


class RespondDecision(DecisionBase):
    kind: Literal["respond"] = "respond"
    claims: tuple[EvidenceClaim, ...] = Field(min_length=1, max_length=20)

    @property
    def response_text(self) -> str:
        return "\n".join(claim.text for claim in self.claims)


class ProposedToolCall(FrozenDecisionModel):
    tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]
    purpose: str = Field(min_length=1, max_length=500)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("tool_name", "purpose")
    @classmethod
    def normalize_visible_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("tool name and purpose must not be blank")
        return normalized

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("tool-call evidence references must be unique")
        return value


class InvokeToolsDecision(DecisionBase):
    kind: Literal["invoke_tools"] = "invoke_tools"
    tool_calls: tuple[ProposedToolCall, ...] = Field(min_length=1, max_length=4)


class HandoffDecision(DecisionBase):
    kind: Literal["handoff"] = "handoff"
    handoff_goal: str = Field(min_length=1, max_length=2_000)
    reason: EvidenceClaim
    required_capabilities: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=20,
    )
    candidate_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=50)

    @field_validator("handoff_goal")
    @classmethod
    def normalize_goal(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("handoff goal must not be blank")
        return normalized

    @field_validator("required_capabilities", "candidate_ids")
    @classmethod
    def unique_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("handoff identifiers must be unique")
        if any(not item.strip() for item in value):
            raise ValueError("handoff identifiers must not be blank")
        return value


class NeedsInputDecision(DecisionBase):
    kind: Literal["needs_input"] = "needs_input"
    question: str = Field(min_length=1, max_length=2_000)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    choices: tuple[str, ...] = Field(default_factory=tuple, max_length=5)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("input question must not be blank")
        return normalized

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("input evidence references must be unique")
        return value

    @field_validator("choices")
    @classmethod
    def normalize_choices(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(" ".join(choice.split()) for choice in value)
        if any(not choice for choice in normalized):
            raise ValueError("input choices must not be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("input choices must be unique")
        return normalized


class CompleteDecision(DecisionBase):
    kind: Literal["complete"] = "complete"
    claims: tuple[EvidenceClaim, ...] = Field(min_length=1, max_length=20)
    completion_status: Literal["completed", "partial"] = "completed"

    @property
    def response_text(self) -> str:
        return "\n".join(claim.text for claim in self.claims)


AgentDecision = Annotated[
    RespondDecision
    | InvokeToolsDecision
    | HandoffDecision
    | NeedsInputDecision
    | CompleteDecision,
    Field(discriminator="kind"),
]


_DECISION_ADAPTER = TypeAdapter(AgentDecision)


def decision_evidence_refs(decision: AgentDecision) -> frozenset[str]:
    if isinstance(decision, (RespondDecision, CompleteDecision)):
        return frozenset(
            evidence_ref
            for claim in decision.claims
            for evidence_ref in claim.evidence_refs
        )
    if isinstance(decision, InvokeToolsDecision):
        return frozenset(
            evidence_ref
            for call in decision.tool_calls
            for evidence_ref in call.evidence_refs
        )
    if isinstance(decision, HandoffDecision):
        return frozenset(decision.reason.evidence_refs)
    return frozenset(decision.evidence_refs)


def parse_agent_decision(
    content: str,
    *,
    allowed_evidence_refs: frozenset[str],
    available_tool_names: frozenset[str],
    allowed_candidate_ids: frozenset[str] = frozenset(),
) -> AgentDecision:
    """Parse exactly one server-constrained decision object."""

    if not content.strip():
        raise ScriptOutputError("planner returned empty content")
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ScriptOutputError("planner output is not one valid JSON object") from error
    if not isinstance(payload, dict):
        raise ScriptOutputError("planner output must be one JSON object")
    try:
        decision = _DECISION_ADAPTER.validate_python(payload)
    except (ValidationError, TypeError, ValueError) as error:
        raise ScriptOutputError("planner output violates the decision schema") from error

    foreign_evidence = decision_evidence_refs(decision) - allowed_evidence_refs
    if foreign_evidence:
        raise ScriptOutputError(
            "planner output cites unavailable evidence: "
            + ", ".join(sorted(foreign_evidence))
        )
    if isinstance(decision, InvokeToolsDecision):
        unknown_tools = {
            call.tool_name
            for call in decision.tool_calls
            if call.tool_name not in available_tool_names
        }
        if unknown_tools:
            raise ScriptOutputError(
                "planner output requests unavailable tools: "
                + ", ".join(sorted(unknown_tools))
            )
    if isinstance(decision, HandoffDecision):
        unknown_candidates = set(decision.candidate_ids) - allowed_candidate_ids
        if unknown_candidates:
            raise ScriptOutputError(
                "planner output references unavailable Candidates: "
                + ", ".join(sorted(unknown_candidates))
            )
    return decision


__all__ = [
    "AgentDecision",
    "CompleteDecision",
    "EvidenceClaim",
    "HandoffDecision",
    "InvokeToolsDecision",
    "NeedsInputDecision",
    "ProposedToolCall",
    "RespondDecision",
    "decision_evidence_refs",
    "parse_agent_decision",
]
