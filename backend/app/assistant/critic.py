from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.assistant.models import ContextSnapshot, SubagentRole
from app.assistant.planner import PlanningObservation
from app.assistant.subagents import SUBAGENT_ROLES, SubagentBranchResult


class FrozenCriticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CriticReview(FrozenCriticModel):
    decision_summary: str = Field(min_length=1, max_length=1_000)
    observation: PlanningObservation
    external_writes_allowed: bool
    incomplete_roles: tuple[SubagentRole, ...] = Field(default_factory=tuple)
    model_call_count: int = Field(default=0, ge=0, le=2)


class Critic(Protocol):
    async def review(
        self,
        *,
        execution_id: str,
        planning_round: int,
        goal: str,
        snapshot: ContextSnapshot,
        branches: Sequence[SubagentBranchResult],
        observations: Sequence[PlanningObservation],
        remaining_model_calls: int,
    ) -> CriticReview: ...


class EvidenceBoundCritic:
    """Deterministic join gate; it never invents evidence or hidden reasoning."""

    async def review(
        self,
        *,
        execution_id: str,
        planning_round: int,
        goal: str,
        snapshot: ContextSnapshot,
        branches: Sequence[SubagentBranchResult],
        observations: Sequence[PlanningObservation],
        remaining_model_calls: int,
    ) -> CriticReview:
        del goal, observations, remaining_model_calls
        by_role = {branch.role: branch for branch in branches}
        incomplete = tuple(
            role
            for role in SUBAGENT_ROLES
            if role not in by_role or by_role[role].status != "completed"
        )
        evidence_branch = by_role.get("evidence")
        evidence_ready = (
            evidence_branch is not None
            and evidence_branch.status == "completed"
            and evidence_branch.analysis is not None
            and bool(evidence_branch.analysis.evidence_refs)
            and not evidence_branch.analysis.blocks_external_write
        )
        blocking_roles = tuple(
            branch.role
            for branch in branches
            if branch.analysis is not None
            and branch.analysis.blocks_external_write
        )
        cited = tuple(
            dict.fromkeys(
                evidence_ref
                for branch in branches
                if branch.analysis is not None
                for evidence_ref in branch.analysis.evidence_refs
            )
        )
        if not cited and snapshot.evidence_refs:
            cited = (snapshot.evidence_refs[0],)
        external_writes_allowed = (
            evidence_ready and not incomplete and not blocking_roles
        )
        summary = (
            "Critic joined all read-only branches and confirmed the evidence gate."
            if not incomplete and external_writes_allowed
            else "Critic joined available branches; external writes remain evidence-gated."
        )
        return CriticReview(
            decision_summary=summary,
            observation=PlanningObservation(
                observation_id=(
                    str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"matinier:critic:{execution_id}:{planning_round}",
                        )
                    )
                ),
                source="system",
                summary=summary,
                payload={
                    "branch_statuses": {
                        role: (
                            by_role[role].status if role in by_role else "missing"
                        )
                        for role in SUBAGENT_ROLES
                    },
                    "external_writes_allowed": external_writes_allowed,
                    "blocking_roles": list(blocking_roles),
                },
                evidence_refs=cited,
            ),
            external_writes_allowed=external_writes_allowed,
            incomplete_roles=incomplete,
        )


__all__ = ["Critic", "CriticReview", "EvidenceBoundCritic"]
