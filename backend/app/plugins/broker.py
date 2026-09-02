from __future__ import annotations

import asyncio
import ipaddress
import uuid
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Protocol
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel

from app.plugins.capabilities import (
    ActionExecuteInput,
    CapabilityAdapter,
    CapabilityBinding,
    CapabilityRegistry,
    DeliveryPrepareInput,
    DeliveryPrepareOutput,
    DeliveryQueryInput,
    DeliveryQueryOutput,
    MediaQueryInput,
    ModelInvokeInput,
    NetworkFetchInput,
    NetworkFetchOutput,
    StateGetInput,
    StateGetOutput,
    StatePutInput,
    StatePutOutput,
    UIViewPublishInput,
    UIViewPublishOutput,
)
from app.plugins.document_contracts import (
    PluginDocumentPublishInput,
    PluginDocumentPublishOutput,
)
from app.plugins.permissions import (
    AuthorizationDecision,
    AuthorizationRequest,
    PermissionDeniedError,
    PermissionEvaluator,
    PluginGrant,
    PluginPrincipal,
)
from app.plugins.repository import PluginRepository


class OutcomeUnknownError(RuntimeError):
    """An external effect may have happened and must be reconciled before retry."""


@dataclass(frozen=True, slots=True)
class BrokerConnection:
    plugin_id: str
    plugin_version: str
    generation: int
    session_scopes: Mapping[str, str]

    @property
    def principal(self) -> PluginPrincipal:
        return PluginPrincipal(self.plugin_id, self.plugin_version)

    def resolve_session(self, scope: str | None) -> str | None:
        if scope is None:
            return None
        media_session_id = self.session_scopes.get(scope)
        if media_session_id is None:
            raise PermissionDeniedError(
                "session_scope_invalid",
                "The session scope is stale or belongs to another plugin process.",
            )
        return media_session_id


@dataclass(frozen=True, slots=True)
class CapabilityExecutionContext:
    plugin_id: str
    plugin_version: str
    generation: int
    media_session_id: str | None
    invocation_id: str


@dataclass(frozen=True, slots=True)
class NetworkResponse:
    status: int
    mime_type: str
    body: bytes
    headers: Mapping[str, str]


NetworkResolver = Callable[[str], Awaitable[list[str]]]


class DeliveryCapabilityAdapterLike(Protocol):
    async def prepare(
        self,
        context: CapabilityExecutionContext,
        value: DeliveryPrepareInput,
    ) -> DeliveryPrepareOutput: ...

    async def query(
        self,
        context: CapabilityExecutionContext,
        value: DeliveryQueryInput,
    ) -> DeliveryQueryOutput: ...


class CapabilityBroker:
    """The sole plugin-to-host capability boundary for one authenticated process."""

    def __init__(
        self,
        *,
        connection: BrokerConnection,
        repository: PluginRepository,
        registry: CapabilityRegistry,
        permission_evaluator: PermissionEvaluator,
        network_resolver: NetworkResolver,
        delivery_adapter: DeliveryCapabilityAdapterLike | None = None,
        document_adapter: CapabilityAdapter | None = None,
        meeting_adapter=None,
        state_quota_bytes: int = 2 * 1024 * 1024,
        model_concurrency: int = 2,
        model_semaphore: asyncio.Semaphore | None = None,
        model_call_scope: Callable[[], AbstractAsyncContextManager] | None = None,
        max_redirects: int = 3,
    ) -> None:
        if state_quota_bytes <= 0 or model_concurrency <= 0 or max_redirects < 0:
            raise ValueError("invalid broker limits")
        self._connection = connection
        self._repository = repository
        self._registry = registry
        self._permissions = permission_evaluator
        self._network_resolver = network_resolver
        self._delivery_adapter = delivery_adapter
        self._document_adapter = document_adapter
        self._meeting_adapter = meeting_adapter
        self._state_quota_bytes = state_quota_bytes
        self._model_slots = model_semaphore or asyncio.Semaphore(model_concurrency)
        self._model_call_scope = model_call_scope or nullcontext
        self._max_redirects = max_redirects

    @property
    def registry(self) -> CapabilityRegistry:
        return self._registry

    async def invoke(
        self,
        capability: str,
        raw_params: dict[str, object],
        *,
        session_scope: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        binding = self._registry.require(capability)
        if capability.startswith("meeting."):
            from app.assistant.plugin_repository import MEETING_PLUGIN_ID
            if self._connection.plugin_id != MEETING_PLUGIN_ID:
                raise PermissionDeniedError("meeting_owner_invalid", "This capability is reserved for the meeting plugin.")
        media_session_id: str | None = None
        invocation = None
        self._audit(
            media_session_id=None,
            event_type="capability.requested",
            severity="info",
            payload={"capability": capability},
        )
        try:
            input_value = binding.spec.input_model.model_validate(raw_params)
            media_session_id = self._connection.resolve_session(session_scope)
            if binding.spec.supports_idempotency and not idempotency_key:
                raise ValueError(f"{binding.spec.name} requires an idempotency key")
            if idempotency_key is not None and len(idempotency_key.encode("utf-8")) > 255:
                raise ValueError("idempotency key exceeds 255 bytes")

            if isinstance(input_value, NetworkFetchInput):
                await self._validate_network_target(str(input_value.url))

            stored_request = input_value.model_dump(mode="json")
            meeting_scope = None
            if capability.startswith("meeting."):
                if self._meeting_adapter is None:
                    raise RuntimeError("meeting capability has no scoped Host adapter")
                meeting_scope = self._meeting_adapter.preflight(
                    self._context(media_session_id, "preflight"), capability, input_value)
                if "intent_token" in stored_request:
                    from app.plugins.host_actions import token_hash
                    stored_request["intent_token_hash"] = token_hash(stored_request.pop("intent_token"))

            effective_key = idempotency_key or f"call-{uuid.uuid4()}"
            existing = self._repository.get_capability_invocation(
                plugin_id=self._connection.plugin_id,
                idempotency_key=effective_key,
            )
            if existing is not None:
                checked = self._repository.create_capability_invocation(
                    plugin_id=self._connection.plugin_id,
                    version=self._connection.plugin_version,
                    media_session_id=media_session_id,
                    capability=binding.spec.name,
                    effect=binding.spec.effect,
                    idempotency_key=effective_key,
                    request=stored_request,
                )
                return await self._resume_existing(
                    binding,
                    input_value,
                    checked,
                    media_session_id,
                    effective_key,
                )

            requested_scope = meeting_scope if meeting_scope is not None else _requested_scope(input_value)
            call_count = self._repository.count_capability_invocations(
                plugin_id=self._connection.plugin_id,
                version=self._connection.plugin_version,
                media_session_id=media_session_id,
                capability=capability,
            )
            decision = self._permissions.authorize(
                AuthorizationRequest(
                    principal=self._connection.principal,
                    capability=capability,
                    effect=binding.spec.effect,
                    media_session_id=media_session_id,
                    requested_scope=requested_scope,
                    call_count=call_count,
                ),
                base_permissions=self._repository.list_base_permissions(
                    plugin_id=self._connection.plugin_id,
                    version=self._connection.plugin_version,
                ),
                grants=self._grants(capability),
            )
            invocation = self._repository.create_capability_invocation(
                plugin_id=self._connection.plugin_id,
                version=self._connection.plugin_version,
                media_session_id=media_session_id,
                capability=capability,
                effect=binding.spec.effect,
                idempotency_key=effective_key,
                request=stored_request,
            )
            context = self._context(media_session_id, invocation.id)
            output = await self._execute(binding, context, input_value, decision)
            serialized = output.model_dump(mode="json")
            self._repository.complete_capability_invocation(
                invocation.id,
                result=serialized,
            )
            self._audit(
                media_session_id=media_session_id,
                event_type="capability.completed",
                severity="info",
                payload={"capability": capability, "invocation_id": invocation.id},
            )
            return serialized
        except OutcomeUnknownError:
            if invocation is not None:
                self._repository.fail_capability_invocation(
                    invocation.id,
                    error_code="outcome_unknown",
                    outcome_unknown=True,
                )
            self._audit(
                media_session_id=media_session_id,
                event_type="capability.outcome_unknown",
                severity="warning",
                payload={"capability": capability},
            )
            raise OutcomeUnknownError(
                "External action outcome is unknown; reconciliation is required."
            ) from None
        except PermissionDeniedError as error:
            self._audit(
                media_session_id=media_session_id,
                event_type="capability.denied",
                severity="warning",
                payload={"capability": capability, "code": error.code},
            )
            raise
        except Exception as error:
            if invocation is not None:
                self._repository.fail_capability_invocation(
                    invocation.id,
                    error_code="capability_failed",
                )
            self._audit(
                media_session_id=media_session_id,
                event_type="capability.failed",
                severity="warning",
                payload={
                    "capability": capability,
                    "error_type": type(error).__name__,
                },
            )
            raise

    async def _execute(
        self,
        binding: CapabilityBinding,
        context: CapabilityExecutionContext,
        input_value: BaseModel,
        decision: AuthorizationDecision,
    ) -> BaseModel:
        if binding.spec.name.startswith("meeting."):
            return _validate_output(binding, self._meeting_adapter.invoke(
                context, binding.spec.name, input_value, decision))
        if isinstance(input_value, StateGetInput):
            record = self._repository.get_state(
                plugin_id=context.plugin_id,
                version=context.plugin_version,
                namespace=self._state_namespace(context.media_session_id),
                key=input_value.key,
            )
            return StateGetOutput(
                value=record.value_json if record is not None else None,
                version=record.version if record is not None else 0,
            )
        if isinstance(input_value, StatePutInput):
            record = self._repository.put_state(
                plugin_id=context.plugin_id,
                version=context.plugin_version,
                namespace=self._state_namespace(context.media_session_id),
                key=input_value.key,
                value=input_value.value,
                expected_version=input_value.expected_version,
                quota_bytes=self._state_quota_bytes,
            )
            return StatePutOutput(version=record.version)
        if isinstance(input_value, UIViewPublishInput) and binding.handler is None:
            from app.plugins.ui_schema import RepositoryUIViewCapabilityAdapter

            def allowed_commands(plugin_id: str, version: str) -> frozenset[str]:
                package = self._repository.get_package(plugin_id, version)
                assert package is not None
                commands = package.manifest_json.get("commands", [])
                return frozenset(
                    str(item) for item in commands if isinstance(item, str)
                )

            adapter = RepositoryUIViewCapabilityAdapter(
                self._repository,
                allowed_commands=allowed_commands,
            )
            result = await adapter(context, input_value)
            if not isinstance(result, UIViewPublishOutput):
                raise ValueError("UI adapter returned an invalid response")
            return result
        if isinstance(input_value, DeliveryPrepareInput):
            if self._delivery_adapter is None:
                raise RuntimeError("delivery capability has no session adapter")
            raw_output = await asyncio.wait_for(
                self._delivery_adapter.prepare(context, input_value),
                timeout=binding.spec.timeout_seconds,
            )
            return _validate_output(binding, raw_output)
        if isinstance(input_value, DeliveryQueryInput):
            if self._delivery_adapter is None:
                raise RuntimeError("delivery capability has no session adapter")
            raw_output = await asyncio.wait_for(
                self._delivery_adapter.query(context, input_value),
                timeout=binding.spec.timeout_seconds,
            )
            return _validate_output(binding, raw_output)
        if isinstance(input_value, PluginDocumentPublishInput):
            if self._document_adapter is None:
                raise RuntimeError("document capability has no session adapter")
            raw_output = await asyncio.wait_for(
                self._document_adapter(context, input_value),
                timeout=binding.spec.timeout_seconds,
            )
            output = _validate_output(binding, raw_output)
            if not isinstance(output, PluginDocumentPublishOutput):
                raise ValueError("document adapter returned an invalid response")
            return output
        if isinstance(input_value, NetworkFetchInput):
            return await self._network_fetch(binding, context, input_value, decision)
        if binding.handler is None:
            raise RuntimeError("capability has no host adapter")

        async def call() -> object:
            return await binding.handler(context, input_value)

        timeout = binding.spec.timeout_seconds
        if isinstance(input_value, ModelInvokeInput):
            timeout = min(timeout, input_value.timeout_seconds)
            async with self._model_call_scope():
                async with self._model_slots:
                    raw_output = await asyncio.wait_for(call(), timeout=timeout)
        else:
            raw_output = await asyncio.wait_for(call(), timeout=timeout)
        return _validate_output(binding, raw_output)

    async def _network_fetch(
        self,
        binding: CapabilityBinding,
        context: CapabilityExecutionContext,
        input_value: NetworkFetchInput,
        decision: AuthorizationDecision,
    ) -> NetworkFetchOutput:
        if binding.handler is None:
            raise RuntimeError("network capability has no host adapter")
        current = input_value
        for redirect_count in range(self._max_redirects + 1):
            await self._validate_network_target(str(current.url))
            hostname = (urlsplit(str(current.url)).hostname or "").casefold()
            allowed_destinations = decision.authorized_scope.get("destinations", ())
            if not _contains_text(allowed_destinations, hostname):
                raise PermissionDeniedError(
                    "network_destination_out_of_scope",
                    "The network destination is outside the active grant.",
                )
            raw = await asyncio.wait_for(
                binding.handler(context, current),
                timeout=binding.spec.timeout_seconds,
            )
            if not isinstance(raw, NetworkResponse):
                raise ValueError("network adapter returned an invalid response")
            location = _header(raw.headers, "location")
            if raw.status in {301, 302, 303, 307, 308} and location:
                if redirect_count >= self._max_redirects:
                    raise ValueError("network redirect limit exceeded")
                redirected = urljoin(str(current.url), location)
                current = NetworkFetchInput.model_validate(
                    {**current.model_dump(mode="json"), "url": redirected}
                )
                continue
            mime = raw.mime_type.split(";", 1)[0].strip().casefold()
            if not any(_mime_matches(allowed, mime) for allowed in current.accepted_mime_types):
                raise ValueError("network response MIME type is not allowed")
            if len(raw.body) > current.max_response_bytes:
                raise ValueError("network response exceeds the byte limit")
            return NetworkFetchOutput(
                status=raw.status,
                mime_type=mime,
                body=raw.body.decode("utf-8", errors="replace"),
            )
        raise RuntimeError("unreachable redirect state")

    async def _resume_existing(
        self,
        binding: CapabilityBinding,
        input_value: BaseModel,
        invocation: object,
        media_session_id: str | None,
        idempotency_key: str,
    ) -> dict[str, object]:
        if invocation.status == "completed" and invocation.result_json is not None:
            return dict(invocation.result_json)
        if not invocation.outcome_unknown:
            raise ValueError("idempotent capability invocation is still pending or failed")
        if binding.reconciler is None:
            raise OutcomeUnknownError("capability has no reconciliation adapter")
        context = self._context(media_session_id, invocation.id)
        if binding.spec.name.startswith("meeting."):
            raw = self._meeting_adapter.reconcile(context, binding.spec.name, input_value)
        else:
            raw = await asyncio.wait_for(
                binding.reconciler(context, input_value, idempotency_key),
                timeout=binding.spec.timeout_seconds,
            )
        output = _validate_output(binding, raw)
        serialized = output.model_dump(mode="json")
        self._repository.complete_capability_invocation(invocation.id, result=serialized)
        self._audit(
            media_session_id=media_session_id,
            event_type="capability.reconciled",
            severity="info",
            payload={"capability": binding.spec.name, "invocation_id": invocation.id},
        )
        return serialized

    async def _validate_network_target(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme.casefold() != "https":
            raise ValueError("network.fetch requires HTTPS")
        if parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise ValueError("network.fetch URL authority is not allowed")
        hostname = (parsed.hostname or "").casefold()
        if not hostname:
            raise ValueError("network.fetch URL has no hostname")
        addresses = await self._network_resolver(hostname)
        if not addresses:
            raise ValueError("network destination did not resolve")
        for address in addresses:
            try:
                parsed_address = ipaddress.ip_address(address)
            except ValueError as error:
                raise ValueError("network resolver returned an invalid address") from error
            if not parsed_address.is_global:
                raise ValueError("network destination must resolve only to public addresses")

    def _grants(self, capability: str) -> tuple[PluginGrant, ...]:
        records = self._repository.list_capability_grants(
            plugin_id=self._connection.plugin_id,
            version=self._connection.plugin_version,
            capability=capability,
        )
        return tuple(
            PluginGrant(
                grant_id=record.id,
                plugin_id=record.plugin_id,
                plugin_version=record.plugin_version,
                media_session_id=record.media_session_id,
                capability=record.capability,
                effect=record.effect,
                scope=record.scope_json,
                status=record.status,
                expires_at=record.expires_at,
                revoked_at=record.revoked_at,
            )
            for record in records
        )

    def _context(
        self,
        media_session_id: str | None,
        invocation_id: str,
    ) -> CapabilityExecutionContext:
        return CapabilityExecutionContext(
            plugin_id=self._connection.plugin_id,
            plugin_version=self._connection.plugin_version,
            generation=self._connection.generation,
            media_session_id=media_session_id,
            invocation_id=invocation_id,
        )

    def _state_namespace(self, media_session_id: str | None) -> str:
        session = media_session_id or "global"
        return (
            f"plugin:{self._connection.plugin_id}:{self._connection.plugin_version}:"
            f"session:{session}"
        )

    def _audit(
        self,
        *,
        media_session_id: str | None,
        event_type: str,
        severity: str,
        payload: dict[str, object],
    ) -> None:
        self._repository.append_audit_event(
            plugin_id=self._connection.plugin_id,
            version=self._connection.plugin_version,
            media_session_id=media_session_id,
            event_type=event_type,
            severity=severity,
            payload=payload,
        )


def _requested_scope(input_value: BaseModel) -> dict[str, object]:
    if isinstance(input_value, MediaQueryInput):
        return {"event_types": list(input_value.event_types)}
    if isinstance(input_value, NetworkFetchInput):
        hostname = (urlsplit(str(input_value.url)).hostname or "").casefold()
        return {"destinations": [hostname], "methods": [input_value.method]}
    if isinstance(input_value, ActionExecuteInput):
        return {"actions": [input_value.action]}
    if isinstance(input_value, ModelInvokeInput):
        return {"input_categories": [input_value.input_category]}
    return {}


def _validate_output(binding: CapabilityBinding, raw: object) -> BaseModel:
    if isinstance(raw, BaseModel):
        raw = raw.model_dump(mode="json")
    return binding.spec.output_model.model_validate(raw)


def _contains_text(values: object, expected: str) -> bool:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return False
    return any(isinstance(item, str) and item.casefold() == expected for item in values)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.casefold() == name.casefold():
            return value
    return None


def _mime_matches(allowed: str, actual: str) -> bool:
    normalized = allowed.split(";", 1)[0].strip().casefold()
    if normalized == "*/*":
        return True
    if normalized.endswith("/*"):
        return actual.startswith(normalized[:-1])
    return normalized == actual


__all__ = [
    "BrokerConnection",
    "CapabilityBroker",
    "CapabilityExecutionContext",
    "NetworkResponse",
    "OutcomeUnknownError",
]
