from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.plugins.capabilities import CapabilitySpec, CapabilityRegistry
from app.plugins.permissions import (
    AuthorizationRequest,
    PermissionDeniedError,
    PermissionEvaluator,
    PluginGrant,
    PluginPrincipal,
)
from pydantic import BaseModel


NOW = datetime(2026, 8, 27, 8, tzinfo=UTC)
PRINCIPAL = PluginPrincipal("com.example.viewer", "1.0.0")


def grant(**overrides: object) -> PluginGrant:
    values: dict[str, object] = {
        "grant_id": "grant-1",
        "plugin_id": PRINCIPAL.plugin_id,
        "plugin_version": PRINCIPAL.version,
        "media_session_id": "media-one",
        "capability": "media.query",
        "effect": "read",
        "scope": {"event_types": ["transcript.final"], "max_calls": 2},
        "status": "active",
        "expires_at": NOW + timedelta(minutes=5),
        "revoked_at": None,
    }
    values.update(overrides)
    return PluginGrant(**values)  # type: ignore[arg-type]


def request(**overrides: object) -> AuthorizationRequest:
    values: dict[str, object] = {
        "principal": PRINCIPAL,
        "capability": "media.query",
        "effect": "read",
        "media_session_id": "media-one",
        "requested_scope": {"event_types": ["transcript.final"]},
        "call_count": 0,
    }
    values.update(overrides)
    return AuthorizationRequest(**values)  # type: ignore[arg-type]


def test_base_permissions_are_accepted_but_sensitive_media_is_session_only() -> None:
    evaluator = PermissionEvaluator(now=lambda: NOW)
    allowed = evaluator.authorize(
        request(
            capability="ui.publish",
            effect="local_write",
            media_session_id=None,
            requested_scope={},
        ),
        base_permissions=frozenset({"ui.publish"}),
        grants=(),
    )
    assert allowed.grant_id is None

    with pytest.raises(PermissionDeniedError, match="session grant"):
        evaluator.authorize(
            request(),
            base_permissions=frozenset({"media.query"}),
            grants=(),
        )
    session_allowed = evaluator.authorize(
        request(),
        base_permissions=frozenset({"media.query"}),
        grants=(grant(),),
    )
    assert session_allowed.grant_id == "grant-1"


def test_authorization_denies_mismatch_expiry_revocation_and_scope_expansion() -> None:
    evaluator = PermissionEvaluator(now=lambda: NOW)
    cases = (
        grant(plugin_id="com.example.other"),
        grant(plugin_version="2.0.0"),
        grant(media_session_id="media-two"),
        grant(expires_at=NOW),
        grant(status="revoked", revoked_at=NOW - timedelta(seconds=1)),
        grant(scope={"event_types": ["transcript.final"], "methods": ["GET"]}),
    )
    requests = (
        request(),
        request(),
        request(),
        request(),
        request(),
        request(requested_scope={"methods": ["POST"]}),
    )
    for candidate, attempted in zip(cases, requests, strict=True):
        with pytest.raises(PermissionDeniedError):
            evaluator.authorize(
                attempted,
                base_permissions=frozenset({"media.query"}),
                grants=(candidate,),
            )

    with pytest.raises(PermissionDeniedError, match="call budget"):
        evaluator.authorize(
            request(call_count=2),
            base_permissions=frozenset({"media.query"}),
            grants=(grant(),),
        )


class EmptyInput(BaseModel):
    pass


class EmptyOutput(BaseModel):
    ok: bool


async def handler(_context: object, _value: EmptyInput) -> EmptyOutput:
    return EmptyOutput(ok=True)


def test_capability_registry_is_host_owned_and_immutable() -> None:
    registry = CapabilityRegistry()
    spec = CapabilitySpec(
        name="ui.publish",
        version="1.0",
        effect="local_write",
        input_model=EmptyInput,
        output_model=EmptyOutput,
        timeout_seconds=1,
        requires_action_grant=False,
        supports_idempotency=False,
        supports_reconciliation=False,
    )
    registry.register_host(spec, handler)
    assert registry.require("ui.publish").spec is spec
    with pytest.raises(ValueError, match="already registered"):
        registry.register_host(spec, handler)
    with pytest.raises(PermissionError, match="host"):
        registry.register_plugin(spec, handler)

