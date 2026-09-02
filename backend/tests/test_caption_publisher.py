from __future__ import annotations

import asyncio
import json

import pytest

from app.captions.events import LIVE_CAPTION_TOPIC
from app.captions.models import CaptionEvent, CaptionStatus
from app.captions.publisher import LiveKitCaptionEventPublisher
from app.transcription.models import TranscriptionMetrics
from app.translation.models import (
    TranslationCaption,
    TranslationCaptionStatus,
)


class FakeLocalParticipant:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes | str, dict[str, object]]] = []

    async def publish_data(self, payload: bytes | str, **kwargs) -> None:
        self.calls.append((payload, kwargs))


def make_caption(*, text: str = "实时字幕") -> CaptionEvent:
    return CaptionEvent(
        session_id="session-1",
        segment_id="segment-1",
        revision=2,
        status=CaptionStatus.FINAL,
        text=text,
        audio_start_ms=120,
        audio_end_ms=860,
        confidence=0.93,
        provider_event_id="provider-2",
        received_at_ms=1_700_000_000_000,
    )


def decode_call(participant: FakeLocalParticipant, index: int) -> dict[str, object]:
    payload, kwargs = participant.calls[index]
    assert isinstance(payload, bytes)
    assert kwargs == {"reliable": True, "topic": LIVE_CAPTION_TOPIC}
    return json.loads(payload.decode("utf-8"))


def test_caption_is_published_as_small_reliable_versioned_json() -> None:
    async def scenario() -> None:
        participant = FakeLocalParticipant()
        publisher = LiveKitCaptionEventPublisher(
            participant,
            session_id="session-1",
            clock_ms=lambda: 1_234,
        )

        await publisher.publish_caption(make_caption())

        envelope = decode_call(participant, 0)
        assert envelope == {
            "schema_version": 1,
            "topic": "caption",
            "type": "caption.upsert",
            "session_id": "session-1",
            "sent_at_ms": 1_234,
            "payload": {
                "segment_id": "segment-1",
                "revision": 2,
                "status": "final",
                "text": "实时字幕",
                "audio_start_ms": 120,
                "audio_end_ms": 860,
                "confidence": 0.93,
                "provider_event_id": "provider-2",
                "received_at_ms": 1_700_000_000_000,
            },
        }
        raw_payload = participant.calls[0][0]
        assert isinstance(raw_payload, bytes)
        assert len(raw_payload) < 1_024

    asyncio.run(scenario())


def test_session_status_progress_metrics_and_error_envelopes() -> None:
    async def scenario() -> None:
        participant = FakeLocalParticipant()
        publisher = LiveKitCaptionEventPublisher(
            participant,
            session_id="session-1",
            clock_ms=lambda: 2_000,
        )
        metrics = TranscriptionMetrics(
            final_result_count=2,
            first_partial_latency_ms=125.0,
            provider_error_count=0,
            sent_audio_chunk_count=9,
            sent_audio_bytes=28_800,
        )
        metrics._final_latencies_ms.extend([500.0, 700.0])

        await publisher.publish_status("running")
        await publisher.publish_progress(1_500)
        await publisher.publish_metrics(metrics)
        await publisher.publish_error(
            error_code="asr_stream_error",
            message="Recognition stopped",
        )
        await publisher.publish_translation(
            TranslationCaption(
                session_id="session-1",
                segment_id="translation-1",
                revision=2,
                status=TranslationCaptionStatus.FINAL,
                text="Hello world",
                source_language="zh-CN",
                target_language="en-US",
                audio_start_ms=100,
                audio_end_ms=900,
                source_segment_ids=("source-1",),
                provider_event_id="translation-final-1",
                received_at_ms=2_100,
            )
        )
        await publisher.publish_translation_status(
            status="completed",
            source_language="zh-CN",
            target_language="en-US",
        )

        assert [decode_call(participant, index)["type"] for index in range(6)] == [
            "session.status",
            "session.progress",
            "session.metrics",
            "session.error",
            "translation.upsert",
            "translation.status",
        ]
        assert decode_call(participant, 0)["payload"] == {"status": "running"}
        assert decode_call(participant, 1)["payload"] == {"audio_time_ms": 1_500}
        assert decode_call(participant, 2)["payload"] == {
            "final_result_count": 2,
            "first_partial_latency_ms": 125.0,
            "average_final_latency_ms": 600.0,
            "provider_error_count": 0,
            "sent_audio_chunk_count": 9,
            "sent_audio_bytes": 28_800,
        }
        assert decode_call(participant, 3)["payload"] == {
            "error_code": "asr_stream_error",
            "message": "Recognition stopped",
        }
        assert decode_call(participant, 4)["payload"] == {
            "segment_id": "translation-1",
            "revision": 2,
            "status": "final",
            "text": "Hello world",
            "source_language": "zh-CN",
            "target_language": "en-US",
            "audio_start_ms": 100,
            "audio_end_ms": 900,
            "source_segment_ids": ["source-1"],
            "provider_event_id": "translation-final-1",
            "received_at_ms": 2_100,
        }
        assert decode_call(participant, 5)["payload"] == {
            "status": "completed",
            "source_language": "zh-CN",
            "target_language": "en-US",
            "error_code": None,
            "message": None,
        }
        serialized = b"".join(
            payload
            for payload, _ in participant.calls
            if isinstance(payload, bytes)
        ).lower()
        assert b"authorization" not in serialized
        assert b"api_key" not in serialized

    asyncio.run(scenario())


def test_publisher_rejects_wrong_session_and_oversized_payload() -> None:
    async def scenario() -> None:
        participant = FakeLocalParticipant()
        publisher = LiveKitCaptionEventPublisher(
            participant,
            session_id="session-1",
            max_payload_bytes=512,
        )

        wrong_session = CaptionEvent(
            session_id="session-2",
            segment_id="segment-1",
            revision=1,
            status=CaptionStatus.DRAFT,
            text="text",
            audio_start_ms=0,
            audio_end_ms=None,
            confidence=None,
            provider_event_id="event-1",
            received_at_ms=1,
        )
        with pytest.raises(ValueError, match="session"):
            await publisher.publish_caption(wrong_session)
        with pytest.raises(ValueError, match="size"):
            await publisher.publish_caption(make_caption(text="x" * 2_000))

        assert participant.calls == []

    asyncio.run(scenario())


def test_publisher_rejects_unknown_status() -> None:
    async def scenario() -> None:
        participant = FakeLocalParticipant()
        publisher = LiveKitCaptionEventPublisher(
            participant,
            session_id="session-1",
        )

        with pytest.raises(ValueError, match="status"):
            await publisher.publish_status("unknown")  # type: ignore[arg-type]

    asyncio.run(scenario())
