from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from websockets.asyncio.client import connect as websocket_connect

from app.languages import provider_language_code, validate_translation_pair
from app.translation.errors import (
    TranslationAuthenticationError,
    TranslationConfigurationError,
    TranslationError,
    TranslationProtocolError,
    TranslationProviderError,
    TranslationStateError,
    TranslationTimeoutError,
)
from app.translation.models import TranslationEvent, TranslationEventType
from app.translation.provider import SpeechTranslationProvider


Connector = Callable[..., Awaitable[Any]]
_EVENTS_CLOSED = object()
_REGION_CODES = {
    "beijing": "cn-beijing",
    "cn-beijing": "cn-beijing",
    "singapore": "ap-southeast-1",
    "ap-southeast-1": "ap-southeast-1",
}


def build_livetranslate_endpoint(
    workspace_id: str,
    region: str,
    model: str,
    *,
    override: str | None = None,
) -> str:
    if override:
        if not override.startswith(("ws://", "wss://")):
            raise TranslationConfigurationError(
                "LiveTranslate endpoint override must use ws:// or wss://"
            )
        return override
    if not workspace_id or not workspace_id.strip():
        raise TranslationConfigurationError("Bailian workspace_id is required")
    if not model or not model.strip():
        raise TranslationConfigurationError("LiveTranslate model is required")
    region_code = _REGION_CODES.get(region.strip().lower())
    if region_code is None:
        raise TranslationConfigurationError(
            "Unsupported Bailian region; use beijing or singapore"
        )
    return (
        f"wss://{workspace_id.strip()}.{region_code}.maas.aliyuncs.com"
        f"/api-ws/v1/realtime?model={quote(model.strip(), safe='.-_')}"
    )


@dataclass(frozen=True, slots=True)
class BailianLiveTranslateConfig:
    api_key: str | None
    workspace_id: str | None
    source_language: str
    target_language: str
    region: str = "beijing"
    model: str = "qwen3.5-livetranslate-flash-realtime"
    websocket_url: str | None = None
    proxy_url: str | None = None
    sample_rate: int = 16_000
    start_timeout_seconds: float = 10.0
    finish_timeout_seconds: float = 20.0

    def validate(self) -> None:
        if not self.api_key or not self.api_key.strip():
            raise TranslationConfigurationError("Bailian api_key is required")
        if not self.workspace_id or not self.workspace_id.strip():
            raise TranslationConfigurationError("Bailian workspace_id is required")
        if self.sample_rate != 16_000:
            raise TranslationConfigurationError(
                "LiveTranslate input sample_rate must be 16000"
            )
        if self.start_timeout_seconds <= 0:
            raise TranslationConfigurationError(
                "LiveTranslate start timeout must be positive"
            )
        if self.finish_timeout_seconds <= 0:
            raise TranslationConfigurationError(
                "LiveTranslate finish timeout must be positive"
            )
        try:
            validate_translation_pair(
                self.source_language,
                self.target_language,
            )
        except ValueError as error:
            raise TranslationConfigurationError(str(error)) from error
        build_livetranslate_endpoint(
            self.workspace_id,
            self.region,
            self.model,
            override=self.websocket_url,
        )

    @property
    def endpoint(self) -> str:
        return build_livetranslate_endpoint(
            self.workspace_id or "",
            self.region,
            self.model,
            override=self.websocket_url,
        )

    @property
    def source_code(self) -> str:
        value = provider_language_code(self.source_language, allow_auto=False)
        assert value is not None
        return value

    @property
    def target_code(self) -> str:
        value = provider_language_code(self.target_language, allow_auto=False)
        assert value is not None
        return value


class BailianLiveTranslateProvider(SpeechTranslationProvider):
    """Qwen3.5 LiveTranslate realtime text stream over WebSocket."""

    def __init__(
        self,
        config: BailianLiveTranslateConfig,
        *,
        connector: Connector = websocket_connect,
    ) -> None:
        self._config = config
        self._connector = connector
        self._websocket: Any | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._start_signal: asyncio.Future[TranslationError | None] | None = None
        self._finish_signal: asyncio.Future[TranslationError | None] | None = None
        self._session_created_signal: asyncio.Future[TranslationError | None] | None = None
        self._events: asyncio.Queue[TranslationEvent | object] = asyncio.Queue()
        self._state = "idle"
        self._event_sequence = 0
        self._terminal_error: TranslationError | None = None
        self._terminal_event_emitted = False
        self._events_closed = False
        self._transport_closed = False
        self._close_called = False
        self._audio_time_ms = 0
        self._last_final_end_ms = 0
        self._segment_start_ms: dict[str, int] = {}

    async def start(self) -> None:
        if self._state != "idle":
            raise TranslationStateError(
                "LiveTranslate provider can only be started once"
            )
        self._config.validate()
        loop = asyncio.get_running_loop()
        self._session_created_signal = loop.create_future()
        self._start_signal = loop.create_future()
        self._finish_signal = loop.create_future()
        self._state = "connecting"
        try:
            self._websocket = await self._connector(
                self._config.endpoint,
                additional_headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                },
                open_timeout=self._config.start_timeout_seconds,
                close_timeout=min(2.0, self._config.finish_timeout_seconds),
                ping_interval=20,
                ping_timeout=20,
                # Keep proxy behavior deterministic across long-lived Worker
                # processes. None means a direct connection in websockets.
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

        self._state = "creating"
        self._receiver_task = asyncio.create_task(
            self._receive_messages(),
            name="bailian-livetranslate-receiver",
        )
        try:
            created = await asyncio.wait_for(
                asyncio.shield(self._session_created_signal),
                timeout=self._config.start_timeout_seconds,
            )
            if created is not None:
                raise created
            assert self._websocket is not None
            self._state = "configuring"
            await self._websocket.send(self._session_update_message())
            started = await asyncio.wait_for(
                asyncio.shield(self._start_signal),
                timeout=self._config.start_timeout_seconds,
            )
            if started is not None:
                raise started
            if self._state != "started":
                raise TranslationProtocolError(
                    "LiveTranslate session updated in an invalid state"
                )
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except TimeoutError as error:
            normalized = TranslationTimeoutError(
                "LiveTranslate session start timed out"
            )
            await self._publish_failure(normalized)
            await self._close_transport()
            raise normalized from error
        except TranslationError:
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
            raise TranslationStateError(
                "LiveTranslate session is not accepting audio"
            )
        if not isinstance(pcm, bytes):
            raise TypeError("pcm must be bytes")
        if not pcm:
            return
        assert self._websocket is not None
        encoded = base64.b64encode(pcm).decode("ascii")
        try:
            await self._websocket.send(
                json.dumps(
                    {
                        "event_id": self._next_event_id(),
                        "type": "input_audio_buffer.append",
                        "audio": encoded,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            self._audio_time_ms += len(pcm) * 1_000 // (
                self._config.sample_rate * 2
            )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            normalized = self._normalize_runtime_error(error)
            await self._publish_failure(normalized)
            raise normalized from error

    async def finish(self) -> None:
        if self._terminal_error is not None:
            raise self._terminal_error
        if self._state == "completed":
            return
        if self._state != "started":
            raise TranslationStateError(
                "LiveTranslate session cannot finish before it starts"
            )
        assert self._websocket is not None
        assert self._finish_signal is not None
        self._state = "finishing"
        try:
            await self._websocket.send(
                json.dumps(
                    {
                        "event_id": self._next_event_id(),
                        "type": "session.finish",
                    },
                    separators=(",", ":"),
                )
            )
            result = await asyncio.wait_for(
                asyncio.shield(self._finish_signal),
                timeout=self._config.finish_timeout_seconds,
            )
            if result is not None:
                raise result
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except TimeoutError as error:
            normalized = TranslationTimeoutError(
                "LiveTranslate session finish timed out"
            )
            await self._publish_failure(normalized)
            raise normalized from error
        finally:
            await self._close_transport()

    async def events(self) -> AsyncIterator[TranslationEvent]:
        while True:
            item = await self._events.get()
            if item is _EVENTS_CLOSED:
                return
            assert isinstance(item, TranslationEvent)
            yield item

    async def aclose(self) -> None:
        if self._close_called:
            return
        self._close_called = True
        if self._state not in {"completed", "failed"}:
            self._state = "closed"
        closed = TranslationStateError("provider closed")
        self._resolve_signal(self._session_created_signal, closed)
        self._resolve_signal(self._start_signal, closed)
        self._resolve_signal(self._finish_signal, closed)
        self._close_events()
        await self._close_transport()

    async def _receive_messages(self) -> None:
        assert self._websocket is not None
        try:
            async for message in self._websocket:
                if await self._handle_server_message(message):
                    return
            if self._state not in {"completed", "failed", "closed"}:
                await self._publish_failure(
                    TranslationProviderError(
                        "LiveTranslate WebSocket closed before completion"
                    )
                )
        except asyncio.CancelledError:
            raise
        except TranslationError as error:
            await self._publish_failure(error)
        except BaseException as error:
            await self._publish_failure(self._normalize_runtime_error(error))

    async def _handle_server_message(self, message: str | bytes) -> bool:
        if isinstance(message, bytes):
            raise TranslationProtocolError(
                "LiveTranslate returned unexpected binary data"
            )
        try:
            data = json.loads(message)
        except (TypeError, json.JSONDecodeError) as error:
            raise TranslationProtocolError(
                "LiveTranslate returned invalid JSON"
            ) from error
        if not isinstance(data, dict):
            raise TranslationProtocolError(
                "LiveTranslate payload must be a JSON object"
            )
        event_type = data.get("type")
        if not isinstance(event_type, str):
            raise TranslationProtocolError(
                "LiveTranslate payload is missing an event type"
            )

        if event_type == "session.created":
            if self._state != "creating":
                raise TranslationProtocolError(
                    "LiveTranslate session.created arrived out of order"
                )
            self._resolve_signal(self._session_created_signal, None)
            return False

        if event_type == "session.updated":
            if self._state != "configuring":
                raise TranslationProtocolError(
                    "LiveTranslate session.updated arrived out of order"
                )
            self._state = "started"
            await self._publish_event(
                TranslationEvent(
                    event_type=TranslationEventType.STREAM_STARTED,
                    provider_event_id=self._provider_event_id(data),
                    target_language=self._config.target_code,
                    raw_payload=data,
                )
            )
            self._resolve_signal(self._start_signal, None)
            return False

        if event_type == "response.text.text":
            if self._state not in {"started", "finishing"}:
                raise TranslationProtocolError(
                    "LiveTranslate result arrived before session.updated"
                )
            text = data.get("text")
            stash = data.get("stash", "")
            if not isinstance(text, str) or not isinstance(stash, str):
                raise TranslationProtocolError(
                    "LiveTranslate response text fields must be strings"
                )
            segment_id = self._segment_id(data)
            await self._publish_event(
                TranslationEvent(
                    event_type=TranslationEventType.PARTIAL_RESULT,
                    provider_event_id=self._provider_event_id(data),
                    target_language=self._config.target_code,
                    segment_id=segment_id,
                    text=text + stash,
                    begin_time_ms=self._segment_start(segment_id),
                    end_time_ms=self._audio_time_ms,
                    raw_payload=data,
                )
            )
            return False

        if event_type == "response.text.done":
            text = data.get("text")
            if not isinstance(text, str):
                raise TranslationProtocolError(
                    "LiveTranslate final text must be a string"
                )
            segment_id = self._segment_id(data)
            end_time_ms = self._audio_time_ms
            await self._publish_event(
                TranslationEvent(
                    event_type=TranslationEventType.FINAL_RESULT,
                    provider_event_id=self._provider_event_id(data),
                    target_language=self._config.target_code,
                    segment_id=segment_id,
                    text=text,
                    begin_time_ms=self._segment_start(segment_id),
                    end_time_ms=end_time_ms,
                    raw_payload=data,
                )
            )
            self._last_final_end_ms = end_time_ms
            self._segment_start_ms.pop(segment_id, None)
            return False

        if event_type == "session.finished":
            self._state = "completed"
            await self._publish_event(
                TranslationEvent(
                    event_type=TranslationEventType.STREAM_COMPLETED,
                    provider_event_id=self._provider_event_id(data),
                    target_language=self._config.target_code,
                    raw_payload=data,
                )
            )
            self._terminal_event_emitted = True
            self._resolve_signal(self._finish_signal, None)
            self._close_events()
            return True

        if event_type == "error":
            error_payload = data.get("error")
            provider_code = None
            message_text = "LiveTranslate request failed"
            if isinstance(error_payload, dict):
                if error_payload.get("code") is not None:
                    provider_code = str(error_payload["code"])
                if error_payload.get("message") is not None:
                    message_text = str(error_payload["message"])
            await self._publish_failure(
                TranslationProviderError(
                    message_text,
                    provider_code=provider_code,
                ),
                raw_payload=data,
            )
            return True

        return False

    def _session_update_message(self) -> str:
        return json.dumps(
            {
                "event_id": self._next_event_id(),
                "type": "session.update",
                "session": {
                    "modalities": ["text"],
                    "sample_rate": self._config.sample_rate,
                    "input_audio_format": "pcm",
                    "input_audio_transcription": {
                        "language": self._config.source_code,
                    },
                    "translation": {
                        "language": self._config.target_code,
                    },
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    async def _publish_event(self, event: TranslationEvent) -> None:
        if not self._events_closed:
            await self._events.put(event)

    async def _publish_failure(
        self,
        error: TranslationError,
        *,
        raw_payload: dict[str, Any] | None = None,
    ) -> None:
        if self._terminal_event_emitted:
            return
        self._terminal_error = error
        self._state = "failed"
        self._terminal_event_emitted = True
        await self._publish_event(
            TranslationEvent(
                event_type=TranslationEventType.STREAM_ERROR,
                provider_event_id=self._next_event_id(),
                target_language=self._config.target_code,
                text=str(error),
                raw_payload=raw_payload
                or {
                    "error_code": error.code,
                    "error_message": str(error),
                },
            )
        )
        self._resolve_signal(self._session_created_signal, error)
        self._resolve_signal(self._start_signal, error)
        self._resolve_signal(self._finish_signal, error)
        self._close_events()

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

    def _segment_id(self, data: dict[str, Any]) -> str:
        for key in ("response_id", "item_id"):
            value = data.get(key)
            if value is not None:
                return str(value)
        return f"translation:{self._event_sequence + 1}"

    def _segment_start(self, segment_id: str) -> int:
        return self._segment_start_ms.setdefault(
            segment_id,
            self._last_final_end_ms,
        )

    def _provider_event_id(self, data: dict[str, Any]) -> str:
        value = data.get("event_id")
        if value is not None:
            return str(value)
        return self._next_event_id()

    def _next_event_id(self) -> str:
        self._event_sequence += 1
        return f"event_{uuid.uuid4().hex}_{self._event_sequence}"

    @staticmethod
    def _resolve_signal(
        signal: asyncio.Future[TranslationError | None] | None,
        result: TranslationError | None,
    ) -> None:
        if signal is not None and not signal.done():
            signal.set_result(result)

    @staticmethod
    def _normalize_connection_error(
        error: BaseException,
    ) -> TranslationError:
        status_code = getattr(error, "status_code", None)
        response = getattr(error, "response", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        if status_code in {401, 403}:
            return TranslationAuthenticationError(
                "LiveTranslate authentication failed"
            )
        if isinstance(error, TimeoutError):
            return TranslationTimeoutError(
                "LiveTranslate WebSocket connection timed out"
            )
        return TranslationProviderError(
            "LiveTranslate WebSocket connection failed"
        )

    @staticmethod
    def _normalize_runtime_error(error: BaseException) -> TranslationError:
        if isinstance(error, TranslationError):
            return error
        if isinstance(error, TimeoutError):
            return TranslationTimeoutError(
                "LiveTranslate WebSocket operation timed out"
            )
        return TranslationProviderError(
            "LiveTranslate WebSocket operation failed"
        )
