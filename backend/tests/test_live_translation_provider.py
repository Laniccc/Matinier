from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any

from app.translation.bailian import (
    BailianLiveTranslateConfig,
    BailianLiveTranslateProvider,
)
from app.translation.models import TranslationEventType


_CLOSED = object()


class ScriptedLiveTranslateWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[object] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)
        data = json.loads(message)
        if data["type"] == "session.update":
            await self.incoming.put(
                json.dumps(
                    {
                        "event_id": "updated-1",
                        "type": "session.updated",
                        "session": {"id": "session-remote"},
                    }
                )
            )
        elif data["type"] == "input_audio_buffer.append":
            await self.incoming.put(
                json.dumps(
                    {
                        "event_id": "draft-1",
                        "type": "response.text.text",
                        "response_id": "response-1",
                        "item_id": "item-1",
                        "text": "Hello",
                        "stash": " world",
                    }
                )
            )
            await self.incoming.put(
                json.dumps(
                    {
                        "event_id": "final-1",
                        "type": "response.text.done",
                        "response_id": "response-1",
                        "item_id": "item-1",
                        "text": "Hello world",
                    }
                )
            )
        elif data["type"] == "session.finish":
            await self.incoming.put(
                json.dumps(
                    {
                        "event_id": "finished-1",
                        "type": "session.finished",
                    }
                )
            )

    def __aiter__(self) -> AsyncIterator[str]:
        return self

    async def __anext__(self) -> str:
        item = await self.incoming.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        assert isinstance(item, str)
        return item

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.incoming.put(_CLOSED)


def test_livetranslate_protocol_normalizes_streaming_text() -> None:
    async def scenario() -> None:
        websocket = ScriptedLiveTranslateWebSocket()
        calls: list[tuple[str, dict[str, Any]]] = []

        async def connect(uri: str, **kwargs: Any):
            calls.append((uri, kwargs))
            await websocket.incoming.put(
                json.dumps(
                    {
                        "event_id": "created-1",
                        "type": "session.created",
                        "session": {"id": "session-remote"},
                    }
                )
            )
            return websocket

        provider = BailianLiveTranslateProvider(
            BailianLiveTranslateConfig(
                api_key="secret",
                workspace_id="workspace-123",
                source_language="zh-CN",
                target_language="en-US",
                start_timeout_seconds=0.2,
                finish_timeout_seconds=0.2,
            ),
            connector=connect,
        )

        await provider.start()
        pcm = b"\x01\x02" * 1_600
        await provider.send_audio(pcm)
        await provider.finish()
        events = [event async for event in provider.events()]

        assert calls[0][0] == (
            "wss://workspace-123.cn-beijing.maas.aliyuncs.com/"
            "api-ws/v1/realtime?model=qwen3.5-livetranslate-flash-realtime"
        )
        assert calls[0][1]["additional_headers"] == {
            "Authorization": "Bearer secret"
        }
        assert calls[0][1]["proxy"] is None
        update = json.loads(websocket.sent[0])
        assert update["type"] == "session.update"
        assert update["session"] == {
            "modalities": ["text"],
            "sample_rate": 16_000,
            "input_audio_format": "pcm",
            "input_audio_transcription": {"language": "zh"},
            "translation": {"language": "en"},
        }
        audio = json.loads(websocket.sent[1])
        assert audio["type"] == "input_audio_buffer.append"
        assert base64.b64decode(audio["audio"]) == pcm
        assert json.loads(websocket.sent[2])["type"] == "session.finish"

        assert [event.event_type for event in events] == [
            TranslationEventType.STREAM_STARTED,
            TranslationEventType.PARTIAL_RESULT,
            TranslationEventType.FINAL_RESULT,
            TranslationEventType.STREAM_COMPLETED,
        ]
        assert events[1].segment_id == "response-1"
        assert events[1].text == "Hello world"
        assert events[2].text == "Hello world"
        assert events[2].target_language == "en"
        assert events[2].begin_time_ms == 0
        assert events[2].end_time_ms == 100
        assert websocket.closed is True

    asyncio.run(scenario())
