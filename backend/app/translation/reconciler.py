from __future__ import annotations

from app.translation.models import (
    TranslationCaption,
    TranslationCaptionStatus,
    TranslationEvent,
    TranslationEventType,
)


class TranslationReconciler:
    """Revision-aware reconciliation for one target-language stream."""

    def __init__(
        self,
        *,
        session_id: str,
        source_language: str,
        target_language: str,
    ) -> None:
        self._session_id = session_id
        self._source_language = source_language
        self._target_language = target_language
        self._last_revision: dict[str, int] = {}
        self._final: set[str] = set()
        self._seen_event_ids: set[str] = set()
        self._seen_fingerprints: set[tuple[object, ...]] = set()

    def consume(
        self,
        event: TranslationEvent,
    ) -> TranslationCaption | None:
        if event.event_type not in {
            TranslationEventType.PARTIAL_RESULT,
            TranslationEventType.FINAL_RESULT,
        }:
            return None
        if not event.segment_id:
            raise ValueError("translation event requires a segment_id")
        if event.text is None:
            raise ValueError("translation event requires text")
        if not event.text.strip():
            return None
        if event.provider_event_id in self._seen_event_ids:
            return None
        self._seen_event_ids.add(event.provider_event_id)
        fingerprint = (
            event.event_type,
            event.segment_id,
            event.text,
            event.begin_time_ms,
            event.end_time_ms,
        )
        if fingerprint in self._seen_fingerprints:
            return None
        self._seen_fingerprints.add(fingerprint)
        if event.segment_id in self._final:
            return None

        revision = self._last_revision.get(event.segment_id, 0) + 1
        status = (
            TranslationCaptionStatus.FINAL
            if event.event_type is TranslationEventType.FINAL_RESULT
            else TranslationCaptionStatus.DRAFT
        )
        caption = TranslationCaption(
            session_id=self._session_id,
            segment_id=event.segment_id,
            revision=revision,
            status=status,
            text=event.text,
            source_language=self._source_language,
            target_language=self._target_language,
            audio_start_ms=event.begin_time_ms,
            audio_end_ms=event.end_time_ms,
            source_segment_ids=(),
            provider_event_id=event.provider_event_id,
            received_at_ms=event.received_at_ms,
        )
        self._last_revision[event.segment_id] = revision
        if caption.is_final:
            self._final.add(event.segment_id)
        return caption
