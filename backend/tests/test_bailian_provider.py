from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.transcription.bailian import (
    BailianConfig,
    BailianSpeechRecognitionProvider,
    build_bailian_endpoint,
)
from app.transcription.errors import (
    ASRAuthenticationError,
    ASRConfigurationError,
    ASRProtocolError,
    ASRProviderError,
    ASRTimeoutError,
)
from app.transcription.models import ASREvent, ASREventType


_CLOSED = object()


def server_event(
    event: str,
    *,
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> str:
    header: dict[str, Any] = {"event": event}
    if event_id is not None:
        header["event_id"] = event_id
    return json.dumps({"header": header, "payload": payload or {}})


class ScriptedWebSocket:
    def __init__(self, mode: str = "normal") -> None:
        self.mode = mode
        self.incoming: asyncio.Queue[object] = asyncio.Queue()
        self.sent: list[str | bytes] = []
        self.closed = False
        self.close_calls = 0
        self._results_sent = False

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)
        if isinstance(message, str):
            action = json.loads(message)["header"]["action"]
            if action == "run-task":
                if self.mode == "task_failed":
                    await self.incoming.put(
                        server_event(
                            "task-failed",
                            payload={
                                "error_code": "InvalidParameter",
                                "error_message": "bad model",
                            },
                            event_id="failed-1",
                        )
                    )
                elif self.mode != "silent":
                    await self.incoming.put(
                        server_event("task-started", event_id="started-1")
                    )
            elif action == "finish-task" and self.mode in {
                "normal",
                "missing_segment_ids",
            }:
                await self.incoming.put(
                    server_event("task-finished", event_id="finished-1")
                )
        elif self.mode == "malformed_after_audio":
            await self.incoming.put("{not-json")
        elif self.mode == "normal" and not self._results_sent:
            self._results_sent = True
            await self.incoming.put(
                server_event(
                    "result-generated",
                    payload={
                        "output": {
                            "sentence": {
                                "sentence_id": 3,
                                "text": "你好",
                                "begin_time": 100,
                                "end_time": 320,
                                "sentence_end": False,
                            }
                        }
                    },
                    event_id="result-1",
                )
            )
            await self.incoming.put(
                server_event(
                    "result-generated",
                    payload={
                        "output": {
                            "sentence": {
                                "sentence_id": 3,
                                "text": "你好世界",
                                "begin_time": 100,
                                "end_time": 680,
                                "sentence_end": True,
                                "confidence": 0.91,
                            }
                        }
                    },
                    event_id="result-2",
                )
            )
        elif self.mode == "missing_segment_ids" and not self._results_sent:
            self._results_sent = True
            for event_id, text, sentence_end in (
                ("generated-1", "one", False),
                ("generated-2", "one revised", False),
                ("generated-3", "one final", True),
                ("generated-4", "two", False),
            ):
                await self.incoming.put(
                    server_event(
                        "result-generated",
                        payload={
                            "output": {
                                "sentence": {
                                    "text": text,
                                    "begin_time": 100,
                                    "sentence_end": sentence_end,
                                }
                            }
                        },
                        event_id=event_id,
                    )
                )

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self

    async def __anext__(self) -> str | bytes:
        message = await self.incoming.get()
        if message is _CLOSED:
            raise StopAsyncIteration
        assert isinstance(message, (str, bytes))
        return message

    async def close(self) -> None:
        self.close_calls += 1
        if self.closed:
            return
        self.closed = True
        await self.incoming.put(_CLOSED)


class RecordingConnector:
    def __init__(
        self,
        websocket: ScriptedWebSocket | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.websocket = websocket
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, uri: str, **kwargs: Any) -> ScriptedWebSocket:
        self.calls.append((uri, kwargs))
        if self.error is not None:
            raise self.error
        assert self.websocket is not None
        return self.websocket


class FakeHandshakeError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"handshake status {status_code}")
        self.status_code = status_code


async def collect_events(
    provider: BailianSpeechRecognitionProvider,
) -> list[ASREvent]:
    return [event async for event in provider.events()]


def make_config(**overrides: Any) -> BailianConfig:
    values: dict[str, Any] = {
        "api_key": "test-secret-key",
        "workspace_id": "workspace-123",
        "region": "beijing",
        "model": "fun-asr-realtime",
        "start_timeout_seconds": 0.2,
        "finish_timeout_seconds": 0.2,
    }
    values.update(overrides)
    return BailianConfig(**values)


def test_region_endpoints_and_override() -> None:
    assert build_bailian_endpoint("wid", "beijing") == (
        "wss://wid.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    )
    assert build_bailian_endpoint("wid", "singapore") == (
        "wss://wid.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/inference"
    )
    assert (
        build_bailian_endpoint("wid", "custom", override="wss://example.test/asr")
        == "wss://example.test/asr"
    )

    with pytest.raises(ASRConfigurationError, match="region"):
        build_bailian_endpoint("wid", "unknown")


def test_provider_sends_protocol_and_normalizes_results() -> None:
    async def scenario() -> None:
        websocket = ScriptedWebSocket()
        connector = RecordingConnector(websocket)
        provider = BailianSpeechRecognitionProvider(
            make_config(language="zh-CN"),
            connector=connector,
        )

        await provider.start()
        await provider.send_audio(b"\x01\x02" * 1_600)
        await provider.finish()
        events = await collect_events(provider)

        uri, kwargs = connector.calls[0]
        assert uri == (
            "wss://workspace-123.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
        )
        assert kwargs["additional_headers"] == {
            "Authorization": "Bearer test-secret-key",
            "X-DashScope-WorkSpace": "workspace-123",
        }
        assert kwargs["open_timeout"] == 0.2
        assert kwargs["close_timeout"] == 0.2
        assert kwargs["proxy"] is None

        run_task = json.loads(websocket.sent[0])
        assert run_task["header"]["action"] == "run-task"
        assert run_task["header"]["streaming"] == "duplex"
        assert run_task["header"]["task_id"]
        assert run_task["payload"] == {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": "fun-asr-realtime",
            "parameters": {
                "format": "pcm",
                "sample_rate": 16_000,
                "language_hints": ["zh"],
            },
            "input": {},
        }
        assert websocket.sent[1] == b"\x01\x02" * 1_600
        finish_task = json.loads(websocket.sent[2])
        assert finish_task["header"] == {
            "action": "finish-task",
            "task_id": run_task["header"]["task_id"],
            "streaming": "duplex",
        }

        assert [event.event_type for event in events] == [
            ASREventType.STREAM_STARTED,
            ASREventType.PARTIAL_RESULT,
            ASREventType.FINAL_RESULT,
            ASREventType.STREAM_COMPLETED,
        ]
        partial, final = events[1], events[2]
        assert partial.provider_event_id == "result-1"
        assert partial.segment_id == "3"
        assert partial.text == "你好"
        assert partial.is_final is False
        assert partial.begin_time_ms == 100
        assert partial.end_time_ms == 320
        assert partial.raw_payload is not None
        assert final.provider_event_id == "result-2"
        assert final.text == "你好世界"
        assert final.is_final is True
        assert final.confidence == 0.91
        assert websocket.closed is True

    asyncio.run(scenario())


def test_provider_generates_stable_segment_ids_when_bailian_omits_them() -> None:
    async def scenario() -> None:
        websocket = ScriptedWebSocket(mode="missing_segment_ids")
        provider = BailianSpeechRecognitionProvider(
            make_config(),
            connector=RecordingConnector(websocket),
        )

        await provider.start()
        await provider.send_audio(b"\x00\x00" * 320)
        await provider.finish()
        events = await collect_events(provider)

        task_id = json.loads(websocket.sent[0])["header"]["task_id"]
        captions = [
            event
            for event in events
            if event.event_type
            in {ASREventType.PARTIAL_RESULT, ASREventType.FINAL_RESULT}
        ]
        assert [event.segment_id for event in captions] == [
            f"{task_id}:segment:1",
            f"{task_id}:segment:1",
            f"{task_id}:segment:1",
            f"{task_id}:segment:2",
        ]
        assert all(
            caption.text not in (caption.segment_id or "") for caption in captions
        )

    asyncio.run(scenario())


def test_handshake_authentication_failure_is_classified_without_key_leak() -> None:
    async def scenario() -> None:
        connector = RecordingConnector(error=FakeHandshakeError(401))
        provider = BailianSpeechRecognitionProvider(
            make_config(),
            connector=connector,
        )

        with pytest.raises(ASRAuthenticationError) as exc_info:
            await provider.start()

        assert "test-secret-key" not in str(exc_info.value)
        assert connector.calls

    asyncio.run(scenario())


def test_start_timeout_is_finite_and_closes_transport() -> None:
    async def scenario() -> None:
        websocket = ScriptedWebSocket(mode="silent")
        provider = BailianSpeechRecognitionProvider(
            make_config(start_timeout_seconds=0.01),
            connector=RecordingConnector(websocket),
        )

        with pytest.raises(ASRTimeoutError, match="start"):
            await provider.start()

        assert websocket.closed is True

    asyncio.run(scenario())


def test_task_failed_becomes_provider_error_and_stream_error_event() -> None:
    async def scenario() -> None:
        websocket = ScriptedWebSocket(mode="task_failed")
        provider = BailianSpeechRecognitionProvider(
            make_config(),
            connector=RecordingConnector(websocket),
        )

        with pytest.raises(ASRProviderError) as exc_info:
            await provider.start()
        events = await collect_events(provider)

        assert exc_info.value.provider_code == "InvalidParameter"
        assert [event.event_type for event in events] == [
            ASREventType.STREAM_ERROR
        ]
        assert events[0].raw_payload is not None
        assert websocket.closed is True

    asyncio.run(scenario())


def test_malformed_server_message_becomes_protocol_error_event() -> None:
    async def scenario() -> None:
        websocket = ScriptedWebSocket(mode="malformed_after_audio")
        provider = BailianSpeechRecognitionProvider(
            make_config(),
            connector=RecordingConnector(websocket),
        )

        await provider.start()
        await provider.send_audio(b"\x00\x00")
        events = await asyncio.wait_for(collect_events(provider), timeout=0.2)

        assert [event.event_type for event in events] == [
            ASREventType.STREAM_STARTED,
            ASREventType.STREAM_ERROR,
        ]
        assert events[-1].raw_payload == {"message": "{not-json"}
        with pytest.raises(ASRProtocolError):
            await provider.finish()

    asyncio.run(scenario())


def test_close_is_idempotent() -> None:
    async def scenario() -> None:
        websocket = ScriptedWebSocket()
        provider = BailianSpeechRecognitionProvider(
            make_config(),
            connector=RecordingConnector(websocket),
        )
        await provider.start()

        await provider.aclose()
        await provider.aclose()

        assert websocket.close_calls == 1

    asyncio.run(scenario())
