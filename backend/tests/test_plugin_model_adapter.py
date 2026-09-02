from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest

from app.persistence.database import Database
from app.plugins import bootstrap as plugin_bootstrap
from app.plugins.bootstrap import PluginHostRuntime
from app.plugins.broker import CapabilityExecutionContext
from app.plugins.capabilities import ModelInvokeInput
from app.plugins.container_runtime import DockerContainerRuntime, PluginIdentity
from app.plugins.model_adapter import (
    PluginModelCapabilityAdapter,
    PluginModelCapabilityError,
)
from app.settings import Settings
from app.text_processing.provider import (
    DeepSeekAuthError,
    DeepSeekConfigurationError,
    DeepSeekRequestError,
    StructuredCompletionRequest,
    StructuredCompletionResult,
)


class FakeStructuredProvider:
    provider_name = "fake-structured"
    model = "fake-json-v1"

    def __init__(self, outcome: StructuredCompletionResult | Exception) -> None:
        self.outcome = outcome
        self.requests: list[StructuredCompletionRequest] = []
        self._api_key = "provider-secret-must-not-leak"

    async def complete_structured(
        self,
        request: StructuredCompletionRequest,
    ) -> StructuredCompletionResult:
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _context() -> CapabilityExecutionContext:
    return CapabilityExecutionContext(
        plugin_id="com.matinier.course-organizer",
        plugin_version="1.0.0",
        generation=1,
        media_session_id="media-1",
        invocation_id="invoke-1",
    )


def _input(**updates: object) -> ModelInvokeInput:
    payload: dict[str, object] = {
        "input_category": "session_transcript",
        "system_prompt": "Return exactly one JSON object.",
        "user_prompt": "Classify this course window.",
        "input_payload": {
            "items": [{"item_id": "seg-1", "text": "A definition"}]
        },
        "response_format": "json_object",
        "max_output_tokens": 1_024,
        "timeout_seconds": 20,
    }
    payload.update(updates)
    return ModelInvokeInput.model_validate(payload)


def _adapter(
    outcome: StructuredCompletionResult | Exception,
    *,
    max_input_chars: int = 32_000,
    max_output_tokens: int = 4_096,
):
    provider = FakeStructuredProvider(outcome)
    adapter = PluginModelCapabilityAdapter(
        provider,
        max_input_chars=max_input_chars,
        max_output_tokens=max_output_tokens,
    )
    return provider, adapter


def test_model_adapter_preserves_structured_request_and_returns_json_object() -> None:
    provider, adapter = _adapter(
        StructuredCompletionResult(
            content='{"classification":"knowledge_candidate","score":0.91}',
            finish_reason="stop",
            output_tokens=37,
        )
    )

    result = asyncio.run(adapter(_context(), _input()))

    assert provider.requests == [
        StructuredCompletionRequest(
            system_prompt="Return exactly one JSON object.",
            user_prompt="Classify this course window.",
            input_payload={
                "items": [{"item_id": "seg-1", "text": "A definition"}]
            },
            max_output_tokens=1_024,
        )
    ]
    assert result.output == {
        "classification": "knowledge_candidate",
        "score": 0.91,
    }
    assert result.provider == "fake-structured"
    assert result.model == "fake-json-v1"
    assert result.output_tokens == 37
    assert "provider-secret" not in result.model_dump_json()


@pytest.mark.parametrize(
    "result",
    [
        StructuredCompletionResult(content="[]", finish_reason="stop"),
        StructuredCompletionResult(content="42", finish_reason="stop"),
        StructuredCompletionResult(content="not-json", finish_reason="stop"),
        StructuredCompletionResult(content='{"partial":true}', finish_reason="length"),
        StructuredCompletionResult(
            content=json.dumps({"text": "x" * (200 * 1024)}),
            finish_reason="stop",
        ),
    ],
)
def test_model_adapter_rejects_non_object_truncated_or_oversized_output(
    result: StructuredCompletionResult,
) -> None:
    _provider, adapter = _adapter(result)
    with pytest.raises(PluginModelCapabilityError) as exc_info:
        asyncio.run(adapter(_context(), _input()))
    assert exc_info.value.code in {
        "model_invalid_output",
        "model_output_truncated",
        "model_output_too_large",
    }


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (DeepSeekConfigurationError("raw configuration detail"), "model_unavailable"),
        (DeepSeekAuthError("raw auth detail"), "model_unavailable"),
        (
            DeepSeekRequestError("raw response body", retryable=True),
            "model_request_failed",
        ),
        (RuntimeError("unexpected provider secret"), "model_request_failed"),
    ],
)
def test_model_adapter_maps_provider_failures_without_leaking_details(
    error: Exception,
    code: str,
) -> None:
    _provider, adapter = _adapter(error)
    with pytest.raises(PluginModelCapabilityError) as exc_info:
        asyncio.run(adapter(_context(), _input()))
    assert exc_info.value.code == code
    assert "raw" not in str(exc_info.value)
    assert "secret" not in str(exc_info.value)


def test_model_adapter_enforces_host_input_and_token_limits() -> None:
    result = StructuredCompletionResult(content="{}", finish_reason="stop")
    _provider, small_input = _adapter(result, max_input_chars=40)
    with pytest.raises(PluginModelCapabilityError) as exc_info:
        asyncio.run(small_input(_context(), _input()))
    assert exc_info.value.code == "model_input_too_large"

    _provider, small_output = _adapter(result, max_output_tokens=128)
    with pytest.raises(PluginModelCapabilityError) as exc_info:
        asyncio.run(
            small_output(
                _context(),
                _input(max_output_tokens=129),
            )
        )
    assert exc_info.value.code == "model_output_limit_exceeded"


def test_runtime_registers_model_without_network_or_credential_inheritance(
    tmp_path,
) -> None:
    provider = FakeStructuredProvider(
        StructuredCompletionResult(content="{}", finish_reason="stop")
    )
    settings = Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="test-room",
        data_dir=tmp_path,
        deepseek_api_key="host-model-secret",
    )
    database = Database("sqlite://")
    runtime = PluginHostRuntime(
        settings,
        database,
        structured_provider=provider,
        container_runtime=DockerContainerRuntime(
            host_environment={
                "PATH": "C:\\Windows\\System32",
                "DEEPSEEK_API_KEY": "host-model-secret",
                "DATABASE_URL": "sqlite:///host-private.db",
            }
        ),
    )
    try:
        binding = runtime.capability_registry.require("model.invoke")
        assert binding.handler is not None
        assert provider.requests == []
        assert runtime.container_runtime.subprocess_environment == {
            "PATH": "C:\\Windows\\System32"
        }
    finally:
        database.dispose()


def test_runtime_serializes_plugin_capabilities_with_supervisor_writes(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[str] = []

    class RecordingBroker:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def invoke(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            events.append("capability-start")
            await asyncio.sleep(0.02)
            events.append("capability-end")
            return {}

    class RecordingRepository:
        def __init__(self, _session: object) -> None:
            pass

        def record_runtime_status(self, **_kwargs: object) -> None:
            events.append("status-write")

        def advance_binding_delivery(self, *_args: object, **_kwargs: object) -> None:
            events.append("delivery-write")

        def acknowledge_binding(self, *_args: object, **_kwargs: object) -> None:
            events.append("ack-write")

    class RecordingPeer:
        def __init__(self) -> None:
            self.handlers: dict[str, Any] = {}

        def register_handler(self, method: str, handler, **_kwargs: object) -> None:
            self.handlers[method] = handler

    settings = Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="test-room",
        data_dir=tmp_path,
    )
    database = Database("sqlite://")
    runtime = PluginHostRuntime(settings, database)
    peer = RecordingPeer()
    identity = PluginIdentity("com.matinier.diagnostic", "1.0.0")
    monkeypatch.setattr(plugin_bootstrap, "CapabilityBroker", RecordingBroker)
    monkeypatch.setattr(plugin_bootstrap, "PluginRepository", RecordingRepository)
    runtime._configure_peer(identity, peer, 1)

    async def scenario() -> None:
        invoke = peer.handlers["capability.invoke"]
        capability = asyncio.create_task(
            invoke({"capability": "state.get", "input": {}})
        )
        await asyncio.sleep(0)
        status = asyncio.create_task(runtime._record_status(identity, "ready", {}))
        delivery = asyncio.create_task(
            runtime._advance_binding_delivery("binding-1", sequence=4)
        )
        acknowledgment = asyncio.create_task(
            runtime._acknowledge_binding("binding-1", sequence=4)
        )
        await asyncio.gather(capability, status, delivery, acknowledgment)

    try:
        asyncio.run(scenario())
        assert events[:2] == ["capability-start", "capability-end"]
        assert set(events[2:]) == {"status-write", "delivery-write", "ack-write"}
    finally:
        database.dispose()
