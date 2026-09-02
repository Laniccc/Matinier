from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable

from app.plugins.contracts import CapabilityEffect


class PermissionDeniedError(PermissionError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.public_message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PluginPrincipal:
    plugin_id: str
    version: str


@dataclass(frozen=True, slots=True)
class PluginGrant:
    grant_id: str
    plugin_id: str
    plugin_version: str
    media_session_id: str | None
    capability: str
    effect: CapabilityEffect
    scope: Mapping[str, object]
    status: str
    expires_at: dt.datetime
    revoked_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    principal: PluginPrincipal
    capability: str
    effect: CapabilityEffect
    media_session_id: str | None
    requested_scope: Mapping[str, object]
    call_count: int = 0


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    capability: str
    grant_id: str | None
    authorized_scope: Mapping[str, object]


class PermissionEvaluator:
    """Pure default-deny evaluation over install permissions and ephemeral grants."""

    def __init__(self, *, now: Callable[[], dt.datetime] | None = None) -> None:
        self._now = now or (lambda: dt.datetime.now(dt.UTC))

    def authorize(
        self,
        request: AuthorizationRequest,
        *,
        base_permissions: frozenset[str],
        grants: tuple[PluginGrant, ...],
    ) -> AuthorizationDecision:
        if request.capability not in base_permissions:
            raise PermissionDeniedError(
                "permission_missing",
                "The plugin did not request this capability at installation.",
            )
        if request.call_count < 0:
            raise PermissionDeniedError("invalid_call_count", "Invalid capability call count.")
        if not self._requires_session_grant(request):
            return AuthorizationDecision(request.capability, None, {})

        now = _as_utc(self._now())
        matching_identity = tuple(
            candidate
            for candidate in grants
            if candidate.plugin_id == request.principal.plugin_id
            and candidate.plugin_version == request.principal.version
            and candidate.capability == request.capability
            and candidate.effect == request.effect
            and candidate.media_session_id == request.media_session_id
        )
        active = tuple(
            candidate
            for candidate in matching_identity
            if candidate.status == "active"
            and candidate.revoked_at is None
            and _as_utc(candidate.expires_at) > now
        )
        within_scope = tuple(
            candidate
            for candidate in active
            if _scope_value_allows(candidate.scope, request.requested_scope)
        )
        for candidate in within_scope:
            max_calls = candidate.scope.get("max_calls")
            if isinstance(max_calls, int) and not isinstance(max_calls, bool):
                if request.call_count >= max_calls:
                    continue
            return AuthorizationDecision(
                capability=request.capability,
                grant_id=candidate.grant_id,
                authorized_scope=candidate.scope,
            )
        if within_scope and all(
            isinstance(item.scope.get("max_calls"), int)
            and request.call_count >= int(item.scope["max_calls"])
            for item in within_scope
        ):
            raise PermissionDeniedError(
                "grant_call_budget_exhausted",
                "The capability grant call budget is exhausted.",
            )
        raise PermissionDeniedError(
            "session_grant_required",
            "This capability requires an active, matching session grant.",
        )

    @staticmethod
    def _requires_session_grant(request: AuthorizationRequest) -> bool:
        return (
            request.capability.startswith("media.")
            or request.capability == "meeting.execution.input"
            or request.effect in {"network", "external_write"}
        )


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


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
        return any(_scope_value_allows(item, requested) for item in authorized)
    return authorized == requested


__all__ = [
    "AuthorizationDecision",
    "AuthorizationRequest",
    "PermissionDeniedError",
    "PermissionEvaluator",
    "PluginGrant",
    "PluginPrincipal",
]
