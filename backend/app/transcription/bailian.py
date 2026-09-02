from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect as websocket_connect

from app.transcription.errors import (
    ASRAuthenticationError,
    ASRConfigurationError,
    ASRError,
    ASRProtocolError,
    ASRProviderError,
    ASRStateError,
    ASRTimeoutError,
)
from app.transcription.models import ASREvent, ASREventType
from app.transcription.provider import SpeechRecognitionProvider
from app.languages import provider_language_code


Connector = Callable[..., Awaitable[Any]]
_EVENTS_CLOSED = object()
_REGION_CODES = {
    "beijing": "cn-beijing",
    "cn-beijing": "cn-beijing",
    "singapore": "ap-southeast-1",
    "ap-southeast-1": "ap-southeast-1",
}


def build_bailian_endpoint(
    workspace_id: str,
    region: str,
    *,
    override: str | None = None,
) -> str:
    if override:
        if not override.startswith(("ws://", "wss://")):
            raise ASRConfigurationError(
                "Bailian WebSocket endpoint override must use ws:// or wss://"
            )
        return override
    if not workspace_id or not workspace_id.strip():
        raise ASRConfigurationError("Bailian workspace_id is required")
    region_code = _REGION_CODES.get(region.strip().lower())
    if region_code is None:
        raise ASRConfigurationError(
            "Unsupported Bailian region; use beijing or singapore"
        )
    return (
        f"wss://{workspace_id.strip()}.{region_code}.maas.aliyuncs.com"
        "/api-ws/v1/inference"
    )


@dataclass(frozen=True, slots=True)
class BailianConfig:
    api_key: str | None
    workspace_id: str | None
    region: str = "beijing"
    model: str = "fun-asr-realtime"
    websocket_url: str | None = None
    proxy_url: str | None = None
    sample_rate: int = 16_000
    start_timeout_seconds: float = 10.0
    finish_timeout_seconds: float = 15.0
    language: str = "auto"

    def validate(self) -> None:
        if not self.api_key or not self.api_key.strip():
            raise ASRConfigurationError("Bailian api_key is required")
        if not self.workspace_id or not self.workspace_id.strip():
            raise ASRConfigurationError("Bailian workspace_id is required")
        if not self.model or not self.model.strip():
            raise ASRConfigurationError("Bailian ASR model is required")
        if self.sample_rate != 16_000:
            raise ASRConfigurationError("Bailian stage 2 input sample_rate must be 16000")
        if self.start_timeout_seconds <= 0:
            raise ASRConfigurationError("Bailian start timeout must be positive")
        if self.finish_timeout_seconds <= 0:
            raise ASRConfigurationError("Bailian finish timeout must be positive")
        try:
            provider_language_code(self.language, allow_auto=True)
        except ValueError as error:
            raise ASRConfigurationError(str(error)) from error
        build_bailian_endpoint(
            self.workspace_id,
            self.region,
            override=self.websocket_url,
        )

    @property
    def endpoint(self) -> str:
        return build_bailian_endpoint(
            self.workspace_id or "",
            self.region,
            override=self.websocket_url,
        )


class BailianSpeechRecognitionProvider(SpeechRecognitionProvider):
    """One duplex Bailian real-time ASR task over an async WebSocket."""

    def __init__(
        self,
        config: BailianConfig,
        *,
        connector: Connector = websocket_connect,
    ) -> None:
        self._config = config
        self._connector = connector
        self._websocket: Any | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._start_signal: asyncio.Future[ASRError | None] | None = None
        self._finish_signal: asyncio.Future[ASRError | None] | None = None
        self._events: asyncio.Queue[ASREvent | object] = asyncio.Queue()
        self._task_id: str | None = None
        self._state = "idle"
        self._event_sequence = 0
        self._terminal_error: ASRError | None = None
        self._terminal_event_emitted = False
        self._events_closed = False
        self._transport_closed = False
        self._close_called = False
        self._generated_segment_sequence = 0
        self._active_generated_segment_id: str | None = None

    async def start(self) -> None:
        if self._state != "idle":
            raise ASRStateError("Bailian provider can only be started once")
        self._config.validate()
        self._task_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        self._start_signal = loop.create_future()
        self._finish_signal = loop.create_future()
        self._state = "connecting"

        try:
            self._websocket = await self._connector(
                self._config.endpoint,
                additional_headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                    "X-DashScope-WorkSpace": self._config.workspace_id,
                },
                open_timeout=self._config.start_timeout_seconds,
                # Task completion has its own finish timeout above. Keep the
                # transport close handshake short so a completed ASR stream
                # cannot hold a LiveKit room open for another full 15 seconds.
                close_timeout=min(2.0, self._config.finish_timeout_seconds),
                ping_interval=20,
                ping_timeout=20,
                # websockets defaults to proxy=True and automatically reads
                # process / OS proxy settings. A long-lived Worker may retain
                # a stale workstation proxy after that proxy has stopped, so
                # Bailian is direct by default and only uses an explicit URL.
                proxy=self._config.proxy_url,
            )
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except BaseException as error:
            normalized = self._normalize_connection_error(error)
            await self._publish_failure(normalized)
            await self._close_transport()
            raise normalized from error

        self._state = "starting"
        self._receiver_task = asyncio.create_task(
            self._receive_messages(),
            name=f"bailian-asr-receiver-{self._task_id}",
        )
        try:
            await self._websocket.send(self._run_task_message())
            signal = await asyncio.wait_for(
                asyncio.shield(self._start_signal),
                timeout=self._config.start_timeout_seconds,
            )
            if signal is not None:
                raise signal
            if self._state != "started":
                raise ASRProtocolError(
                    "Bailian task start completed in an invalid state"
                )
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except TimeoutError as error:
            normalized = ASRTimeoutError("Bailian task start timed out")
            await self._publish_failure(normalized)
            await self._close_transport()
            raise normalized from error
        except ASRError:
            await self._close_transport()
            raise
        except BaseException as error:
            normalized = self._normalize_runtime_error(error)
            await self._publish_failure(normalized)
            await self._close_transport()
            raise normalized from error

    async def send_audio(self, pcm: bytes) -> None:
        if self._state not in {"started", "finishing"}:
            if self._terminal_error is not None:
                raise self._terminal_error
            raise ASRStateError("Bailian task is not ready for audio")
        if not isinstance(pcm, bytes):
            raise TypeError("pcm must be bytes")
        if not pcm:
            return
        assert self._websocket is not None
        try:
            await self._websocket.send(pcm)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            normalized = self._normalize_runtime_error(error)
            await self._publish_failure(normalized)
            await self._close_transport()
            raise normalized from error

    async def finish(self) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error
        if self._state != "started":
            if self._state == "completed":
                return
            raise ASRStateError("Bailian task cannot finish before it starts")
        assert self._websocket is not None
        assert self._finish_signal is not None
        self._state = "finishing"
        try:
            await self._websocket.send(self._finish_task_message())
            signal = await asyncio.wait_for(
                asyncio.shield(self._finish_signal),
                timeout=self._config.finish_timeout_seconds,
            )
            if signal is not None:
                raise signal
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except TimeoutError as error:
            normalized = ASRTimeoutError("Bailian task finish timed out")
            await self._publish_failure(normalized)
            raise normalized from error
        except ASRError:
            raise
        except BaseException as error:
            normalized = self._normalize_runtime_error(error)
            await self._publish_failure(normalized)
            raise normalized from error
        finally:
            await self._close_transport()

    async def events(self) -> AsyncIterator[ASREvent]:
        while True:
            item = await self._events.get()
            if item is _EVENTS_CLOSED:
                return
            assert isinstance(item, ASREvent)
            yield item

    async def aclose(self) -> None:
        if self._close_called:
            return
        self._close_called = True
        if self._state not in {"completed", "failed"}:
            self._state = "closed"
        self._resolve_signal(self._start_signal, ASRStateError("provider closed"))
        self._resolve_signal(self._finish_signal, ASRStateError("provider closed"))
        self._close_events()
        await self._close_transport()

    async def _receive_messages(self) -> None:
        assert self._websocket is not None
        try:
            async for message in self._websocket:
                should_stop = await self._handle_server_message(message)
                if should_stop:
                    return
            if self._state not in {"completed", "failed", "closed"}:
                await self._publish_failure(
                    ASRProviderError("Bailian WebSocket closed before task completion")
                )
        except asyncio.CancelledError:
            raise
        except ASRError as error:
            await self._publish_failure(error, raw_payload=self._raw_message(message=None))
        except BaseException as error:
            await self._publish_failure(self._normalize_runtime_error(error))

    async def _handle_server_message(self, message: str | bytes) -> bool:
        if isinstance(message, bytes):
            raise ASRProtocolError("Bailian server returned unexpected binary data")
        try:
            data = json.loads(message)
        except (TypeError, json.JSONDecodeError) as error:
            protocol_error = ASRProtocolError("Bailian server returned invalid JSON")
            await self._publish_failure(
                protocol_error,
                raw_payload={"message": message},
            )
            return True
        if not isinstance(data, dict):
            raise ASRProtocolError("Bailian server payload must be a JSON object")
        header = data.get("header")
        payload = data.get("payload", {})
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise ASRProtocolError("Bailian server payload is missing header or payload")
        event_name = header.get("event")
        if not isinstance(event_name, str):
            raise ASRProtocolError("Bailian server payload is missing an event name")

        if event_name == "task-started":
            if self._state != "starting":
                raise ASRProtocolError("Bailian task-started arrived out of order")
            self._state = "started"
            await self._publish_event(
                ASREvent(
                    event_type=ASREventType.STREAM_STARTED,
                    provider_event_id=self._provider_event_id(header),
                    raw_payload=data,
                )
            )
            self._resolve_signal(self._start_signal, None)
            return False

        if event_name == "result-generated":
            if self._state not in {"started", "finishing"}:
                raise ASRProtocolError("Bailian result arrived before task-started")
            sentence = payload.get("output", {}).get("sentence")
            if not isinstance(sentence, dict):
                raise ASRProtocolError("Bailian result is missing output.sentence")
            if sentence.get("heartbeat") is True:
                return False
            text = sentence.get("text")
            if not isinstance(text, str):
                raise ASRProtocolError("Bailian result sentence text must be a string")
            is_final = sentence.get("sentence_end") is True
            confidence = sentence.get("confidence")
            segment_id = self._segment_id(sentence, is_final=is_final)
            await self._publish_event(
                ASREvent(
                    event_type=(
                        ASREventType.FINAL_RESULT
                        if is_final
                        else ASREventType.PARTIAL_RESULT
                    ),
                    provider_event_id=self._provider_event_id(header),
                    segment_id=segment_id,
                    text=text,
                    is_final=is_final,
                    begin_time_ms=self._optional_int(sentence.get("begin_time")),
                    end_time_ms=self._optional_int(sentence.get("end_time")),
                    confidence=self._optional_float(confidence),
                    raw_payload=data,
                )
            )
            return False

        if event_name == "task-finished":
            self._state = "completed"
            await self._publish_event(
                ASREvent(
                    event_type=ASREventType.STREAM_COMPLETED,
                    provider_event_id=self._provider_event_id(header),
                    raw_payload=data,
                )
            )
            self._terminal_event_emitted = True
            self._resolve_signal(self._finish_signal, None)
            self._close_events()
            return True

        if event_name == "task-failed":
            provider_code = self._first_string(
                payload.get("error_code"),
                header.get("error_code"),
            )
            provider_message = self._first_string(
                payload.get("error_message"),
                header.get("error_message"),
            )
            error = ASRProviderError(
                provider_message or "Bailian task failed",
                provider_code=provider_code,
            )
            await self._publish_failure(error, raw_payload=data)
            return True

        return False

    async def _publish_event(self, event: ASREvent) -> None:
        if not self._events_closed:
            await self._events.put(event)

    async def _publish_failure(
        self,
        error: ASRError,
        *,
        raw_payload: dict[str, Any] | None = None,
    ) -> None:
        if self._terminal_event_emitted:
            return
        self._terminal_error = error
        self._state = "failed"
        self._terminal_event_emitted = True
        await self._publish_event(
            ASREvent(
                event_type=ASREventType.STREAM_ERROR,
                provider_event_id=self._next_generated_event_id(),
                text=str(error),
                raw_payload=raw_payload
                or {
                    "error_code": error.code,
                    "error_message": str(error),
                },
            )
        )
        self._resolve_signal(self._start_signal, error)
        self._resolve_signal(self._finish_signal, error)
        self._close_events()

    def _run_task_message(self) -> str:
        assert self._task_id is not None
        parameters: dict[str, object] = {
            "format": "pcm",
            "sample_rate": self._config.sample_rate,
        }
        language_hint = provider_language_code(
            self._config.language,
            allow_auto=True,
        )
        if language_hint is not None:
            parameters["language_hints"] = [language_hint]
        return json.dumps(
            {
                "header": {
                    "action": "run-task",
                    "task_id": self._task_id,
                    "streaming": "duplex",
                },
                "payload": {
                    "task_group": "audio",
                    "task": "asr",
                    "function": "recognition",
                    "model": self._config.model,
                    "parameters": parameters,
                    "input": {},
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _finish_task_message(self) -> str:
        assert self._task_id is not None
        return json.dumps(
            {
                "header": {
                    "action": "finish-task",
                    "task_id": self._task_id,
                    "streaming": "duplex",
                },
                "payload": {"input": {}},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    async def _close_transport(self) -> None:
        if self._transport_closed:
            return
        self._transport_closed = True
        receiver = self._receiver_task
        if (
            receiver is not None
            and receiver is not asyncio.current_task()
            and not receiver.done()
        ):
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        if self._websocket is not None:
            await self._websocket.close()

    def _close_events(self) -> None:
        if self._events_closed:
            return
        self._events_closed = True
        self._events.put_nowait(_EVENTS_CLOSED)

    def _provider_event_id(self, header: dict[str, Any]) -> str:
        for key in ("event_id", "message_id"):
            value = header.get(key)
            if value is not None:
                return str(value)
        return self._next_generated_event_id()

    def _next_generated_event_id(self) -> str:
        self._event_sequence += 1
        return f"{self._task_id or 'unstarted'}:{self._event_sequence}"

    def _segment_id(
        self,
        sentence: dict[str, Any],
        *,
        is_final: bool,
    ) -> str:
        provider_segment_id = sentence.get("sentence_id")
        if provider_segment_id is not None:
            if is_final:
                self._active_generated_segment_id = None
            return str(provider_segment_id)

        if self._active_generated_segment_id is None:
            self._generated_segment_sequence += 1
            self._active_generated_segment_id = (
                f"{self._task_id or 'unstarted'}:segment:"
                f"{self._generated_segment_sequence}"
            )
        segment_id = self._active_generated_segment_id
        if is_final:
            self._active_generated_segment_id = None
        return segment_id

    @staticmethod
    def _resolve_signal(
        signal: asyncio.Future[ASRError | None] | None,
        result: ASRError | None,
    ) -> None:
        if signal is not None and not signal.done():
            signal.set_result(result)

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as error:
            raise ASRProtocolError("Bailian timestamp must be an integer") from error

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as error:
            raise ASRProtocolError("Bailian confidence must be numeric") from error

    @staticmethod
    def _first_string(*values: Any) -> str | None:
        for value in values:
            if value is not None:
                return str(value)
        return None

    @staticmethod
    def _raw_message(*, message: str | bytes | None) -> dict[str, Any] | None:
        if message is None:
            return None
        if isinstance(message, bytes):
            return {"binary_bytes": len(message)}
        return {"message": message}

    @staticmethod
    def _normalize_connection_error(error: BaseException) -> ASRError:
        status_code = getattr(error, "status_code", None)
        response = getattr(error, "response", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        if status_code in {401, 403}:
            return ASRAuthenticationError("Bailian authentication failed")
        if isinstance(error, TimeoutError):
            return ASRTimeoutError("Bailian WebSocket connection timed out")
        return ASRProviderError("Bailian WebSocket connection failed")

    @staticmethod
    def _normalize_runtime_error(error: BaseException) -> ASRError:
        if isinstance(error, ASRError):
            return error
        if isinstance(error, TimeoutError):
            return ASRTimeoutError("Bailian WebSocket operation timed out")
        return ASRProviderError("Bailian WebSocket operation failed")
