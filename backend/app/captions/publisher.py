from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any, Protocol
from typing import Literal

from app.errors import LiveKitOperationError
from app.captions.events import (
    LIVE_CAPTION_TOPIC,
    CaptionPayload,
    LiveEventEnvelope,
    SessionErrorPayload,
    SessionMetricsPayload,
    SessionProgressPayload,
    SessionStatus,
    SessionStatusPayload,
    StrictPayload,
    TranslationPayload,
    TranslationStatusPayload,
)
from app.captions.models import CaptionEvent
from app.translation.models import TranslationCaption
from app.transcription.models import TranscriptionMetrics


class CaptionEventPublisher(Protocol):
    async def publish_caption(self, caption: CaptionEvent) -> None: ...

    async def publish_status(self, status: SessionStatus) -> None: ...

    async def publish_progress(self, audio_time_ms: int) -> None: ...

    async def publish_metrics(self, metrics: TranscriptionMetrics) -> None: ...

    async def publish_error(self, *, error_code: str, message: str) -> None: ...

    async def publish_translation(
        self,
        caption: TranslationCaption,
    ) -> None: ...

    async def publish_translation_status(
        self,
        *,
        status: Literal[
            "starting",
            "running",
            "completed",
            "failed",
        ],
        source_language: str,
        target_language: str,
        error_code: str | None = None,
        message: str | None = None,
    ) -> None: ...


class LiveKitCaptionEventPublisher:
    def __init__(
        self,
        local_participant: Any,
        *,
        session_id: str,
        clock_ms: Callable[[], int] | None = None,
        max_payload_bytes: int = 12_000,
    ) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be positive")
        self._local_participant = local_participant
        self._session_id = session_id
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._max_payload_bytes = max_payload_bytes

    async def publish_caption(self, caption: CaptionEvent) -> None:
        if caption.session_id != self._session_id:
            raise ValueError("caption session does not match publisher session")
        payload = CaptionPayload(
            segment_id=caption.segment_id,
            revision=caption.revision,
            status=caption.status.value,
            text=caption.text,
            audio_start_ms=caption.audio_start_ms,
            audio_end_ms=caption.audio_end_ms,
            confidence=caption.confidence,
            provider_event_id=caption.provider_event_id,
            received_at_ms=caption.received_at_ms,
        )
        await self._publish("caption", "caption.upsert", payload)

    async def publish_status(self, status: SessionStatus) -> None:
        await self._publish(
            "session",
            "session.status",
            SessionStatusPayload(status=status),
        )

    async def publish_translation(
        self,
        caption: TranslationCaption,
    ) -> None:
        if caption.session_id != self._session_id:
            raise ValueError("translation session does not match publisher session")
        await self._publish(
            "translation",
            "translation.upsert",
            TranslationPayload(
                segment_id=caption.segment_id,
                revision=caption.revision,
                status=caption.status.value,
                text=caption.text,
                source_language=caption.source_language,
                target_language=caption.target_language,
                audio_start_ms=caption.audio_start_ms,
                audio_end_ms=caption.audio_end_ms,
                source_segment_ids=list(caption.source_segment_ids),
                provider_event_id=caption.provider_event_id,
                received_at_ms=caption.received_at_ms,
            ),
        )

    async def publish_translation_status(
        self,
        *,
        status: Literal[
            "starting",
            "running",
            "completed",
            "failed",
        ],
        source_language: str,
        target_language: str,
        error_code: str | None = None,
        message: str | None = None,
    ) -> None:
        await self._publish(
            "translation",
            "translation.status",
            TranslationStatusPayload(
                status=status,
                source_language=source_language,
                target_language=target_language,
                error_code=error_code,
                message=message,
            ),
        )

    async def publish_progress(self, audio_time_ms: int) -> None:
        await self._publish(
            "session",
            "session.progress",
            SessionProgressPayload(audio_time_ms=audio_time_ms),
        )

    async def publish_metrics(self, metrics: TranscriptionMetrics) -> None:
        await self._publish(
            "session",
            "session.metrics",
            SessionMetricsPayload(**metrics.log_fields()),
        )

    async def publish_error(self, *, error_code: str, message: str) -> None:
        await self._publish(
            "session",
            "session.error",
            SessionErrorPayload(error_code=error_code, message=message),
        )

    async def _publish(
        self,
        topic: str,
        event_type: str,
        payload: StrictPayload,
    ) -> None:
        envelope = LiveEventEnvelope(
            topic=topic,
            type=event_type,
            session_id=self._session_id,
            sent_at_ms=int(self._clock_ms()),
            payload=payload.model_dump(mode="json"),
        )
        encoded = json.dumps(
            envelope.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > self._max_payload_bytes:
            raise ValueError(
                "caption event payload exceeds the configured size limit"
            )
        try:
            await self._local_participant.publish_data(
                encoded,
                reliable=True,
                topic=LIVE_CAPTION_TOPIC,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise LiveKitOperationError(
                "LiveKit data publication failed"
            ) from error
