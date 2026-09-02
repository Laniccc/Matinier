from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from app.assistant.models import ActionGrant, ToolEffect
from app.persistence.models import ActionGrantRecord, utc_now


class GrantAuthorizationError(RuntimeError):
    """A bounded, user-safe authorization rejection."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.public_message = message
        super().__init__(message)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _scope_value_allows(authorized: object, requested: object) -> bool:
    if isinstance(authorized, Mapping):
        if not isinstance(requested, Mapping):
            return False
        return all(
            key in authorized
            and _scope_value_allows(authorized[key], requested_value)
            for key, requested_value in requested.items()
        )
    if isinstance(authorized, (list, tuple, set, frozenset)):
        if isinstance(requested, (list, tuple, set, frozenset)):
            return all(
                any(
                    _scope_value_allows(allowed_value, requested_value)
                    for allowed_value in authorized
                )
                for requested_value in requested
            )
        return any(
            _scope_value_allows(allowed_value, requested)
            for allowed_value in authorized
        )
    return authorized == requested


def action_grant_from_record(record: ActionGrantRecord) -> ActionGrant:
    return ActionGrant(
        grant_id=record.id,
        session_id=record.session_id,
        actor_id=record.actor_id,
        goal=record.goal,
        capabilities=frozenset(record.capabilities_json),
        resource_scope=dict(record.resource_scope_json),
        candidate_ids=frozenset(record.candidate_ids_json),
        linear_team_id=record.linear_team_id,
        max_side_effects=record.max_side_effects,
        used_side_effects=record.used_side_effects,
        unresolved_identity_policy=record.unresolved_identity_policy,
        expires_at=record.expires_at,
        status=record.status,
        created_at=record.created_at,
    )


def authorize_tool_use(
    grant: ActionGrant | None,
    *,
    effect: ToolEffect,
    capability: str,
    session_id: str,
    execution_goal: str,
    candidate_id: str | None,
    requested_resource_scope: Mapping[str, Any],
    now: dt.datetime | None = None,
) -> ActionGrant | None:
    """Validate one invocation without mutating the side-effect budget."""

    if effect != "external_write":
        return grant
    if grant is None:
        raise GrantAuthorizationError(
            "grant_required",
            "This external action requires an active authorization grant.",
        )
    checked_at = now or utc_now()
    if grant.status != "active" or _as_utc(grant.expires_at) <= _as_utc(checked_at):
        raise GrantAuthorizationError(
            "grant_inactive",
            "The authorization grant is no longer active.",
        )
    if grant.session_id != session_id:
        raise GrantAuthorizationError(
            "grant_session_mismatch",
            "The authorization grant belongs to another meeting.",
        )
    if _normalized_text(grant.goal) != _normalized_text(execution_goal):
        raise GrantAuthorizationError(
            "grant_goal_mismatch",
            "The authorization grant does not cover this action goal.",
        )
    if capability not in grant.capabilities:
        raise GrantAuthorizationError(
            "grant_capability_missing",
            "The authorization grant does not include this capability.",
        )
    if candidate_id is not None and candidate_id not in grant.candidate_ids:
        raise GrantAuthorizationError(
            "grant_candidate_mismatch",
            "The authorization grant does not include this action candidate.",
        )
    if grant.candidate_ids and candidate_id is None:
        raise GrantAuthorizationError(
            "grant_candidate_required",
            "This authorization grant requires a selected action candidate.",
        )
    if not _scope_value_allows(
        grant.resource_scope,
        dict(requested_resource_scope),
    ):
        raise GrantAuthorizationError(
            "grant_resource_scope_mismatch",
            "The requested resource is outside the authorization scope.",
        )
    if grant.used_side_effects >= grant.max_side_effects:
        raise GrantAuthorizationError(
            "grant_budget_exhausted",
            "The authorization grant has no remaining external actions.",
        )
    return grant


__all__ = [
    "GrantAuthorizationError",
    "action_grant_from_record",
    "authorize_tool_use",
]
