from __future__ import annotations

from app.captions.models import CaptionEvent, CaptionStatus
from app.transcription.models import ASREvent, ASREventType


class TranscriptReconciler:
    """Deterministically reconciles normalized ASR revisions by segment ID."""

    def __init__(self, *, session_id: str) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        self._session_id = session_id
        self._active: dict[str, CaptionEvent] = {}
        self._final: dict[str, CaptionEvent] = {}
        self._last_revision: dict[str, int] = {}
        self._seen_provider_event_ids: set[str] = set()
        self._seen_fingerprints: set[tuple[object, ...]] = set()
        self._final_order: dict[str, int] = {}
        self._next_final_order = 0

    @property
    def active_draft_segments(self) -> tuple[CaptionEvent, ...]:
        return tuple(sorted(self._active.values(), key=self._sort_key))

    @property
    def final_segments(self) -> tuple[CaptionEvent, ...]:
        return tuple(
            sorted(
                self._final.values(),
                key=lambda caption: (
                    self._time_key(caption.audio_start_ms),
                    self._time_key(caption.audio_end_ms),
                    self._final_order[caption.segment_id],
                ),
            )
        )

    def consume(self, event: ASREvent) -> CaptionEvent | None:
        if event.event_type not in {
            ASREventType.PARTIAL_RESULT,
            ASREventType.FINAL_RESULT,
        }:
            return None
        if not event.segment_id:
            raise ValueError("caption ASR event requires a stable segment_id")
        if event.text is None:
            raise ValueError("caption ASR event requires text")
        if not event.text.strip():
            return None
        if event.provider_event_id in self._seen_provider_event_ids:
            return None

        self._seen_provider_event_ids.add(event.provider_event_id)
        fingerprint = (
            event.event_type,
            event.segment_id,
            event.text,
            event.begin_time_ms,
            event.end_time_ms,
            event.confidence,
        )
        if fingerprint in self._seen_fingerprints:
            return None
        self._seen_fingerprints.add(fingerprint)

        # A provider Final is authoritative for the remainder of the stream.
        if event.segment_id in self._final:
            return None

        revision = self._last_revision.get(event.segment_id, 0) + 1
        status = (
            CaptionStatus.FINAL
            if event.event_type is ASREventType.FINAL_RESULT
            else CaptionStatus.DRAFT
        )
        caption = CaptionEvent(
            session_id=self._session_id,
            segment_id=event.segment_id,
            revision=revision,
            status=status,
            text=event.text,
            audio_start_ms=event.begin_time_ms,
            audio_end_ms=event.end_time_ms,
            confidence=event.confidence,
            provider_event_id=event.provider_event_id,
            received_at_ms=event.received_at_ms,
        )
        self._last_revision[event.segment_id] = revision

        if caption.is_final:
            self._active.pop(caption.segment_id, None)
            self._final[caption.segment_id] = caption
            self._next_final_order += 1
            self._final_order[caption.segment_id] = self._next_final_order
        else:
            self._active[caption.segment_id] = caption
        return caption

    @classmethod
    def _sort_key(cls, caption: CaptionEvent) -> tuple[float, float, str]:
        return (
            cls._time_key(caption.audio_start_ms),
            cls._time_key(caption.audio_end_ms),
            caption.segment_id,
        )

    @staticmethod
    def _time_key(value: int | None) -> float:
        return float("inf") if value is None else float(value)
