from __future__ import annotations

from collections.abc import Iterable

from app.persistence.models import SegmentRecord, TranslationSegmentRecord
from app.timeline.models import Timeline, TimelineSegment


_MISSING_END_DURATION_MS = 2_000


def _sort_key(record: SegmentRecord) -> tuple[object, ...]:
    start_ms = (
        max(0, record.audio_start_ms)
        if record.audio_start_ms is not None
        else 0
    )
    end_ms = (
        max(0, record.audio_end_ms)
        if record.audio_end_ms is not None
        else 0
    )
    return (
        record.audio_start_ms is None,
        start_ms,
        record.audio_end_ms is None,
        end_ms,
        record.created_at,
        record.id,
    )


def _next_known_start(
    records: list[SegmentRecord],
    index: int,
) -> int | None:
    for following in records[index + 1 :]:
        if following.audio_start_ms is not None:
            return max(0, following.audio_start_ms)
    return None


def normalize_timeline(segments: Iterable[SegmentRecord]) -> Timeline:
    """Return a deterministic Final-only timeline based on source audio time."""

    records = list(segments)
    if any(record.status != "final" for record in records):
        raise ValueError("only Final segments can be normalized")
    records.sort(key=_sort_key)

    normalized: list[TimelineSegment] = []
    previous_end_ms = 0
    duration_ms = 0
    for index, record in enumerate(records):
        if record.audio_start_ms is None:
            start_ms = previous_end_ms
        else:
            start_ms = max(0, record.audio_start_ms)

        if record.audio_end_ms is not None:
            end_ms = max(start_ms, max(0, record.audio_end_ms))
        else:
            next_start_ms = _next_known_start(records, index)
            end_ms = (
                next_start_ms
                if next_start_ms is not None and next_start_ms > start_ms
                else start_ms + _MISSING_END_DURATION_MS
            )

        normalized.append(
            TimelineSegment(
                id=record.id,
                session_id=record.session_id,
                segment_id=record.segment_id,
                track_id=record.track_id,
                revision=record.revision,
                language=record.language,
                raw_text=record.raw_text,
                display_text=record.display_text,
                audio_start_ms=start_ms,
                audio_end_ms=end_ms,
                confidence=record.confidence,
                received_at_ms=record.received_at_ms,
                finalized_at=record.finalized_at,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        )
        previous_end_ms = end_ms
        duration_ms = max(duration_ms, end_ms)

    return Timeline(segments=tuple(normalized), duration_ms=duration_ms)


def normalize_translation_timeline(
    segments: Iterable[TranslationSegmentRecord],
) -> Timeline:
    """Adapt persisted translation Finals to the shared export timeline."""

    source_records = [
        SegmentRecord(
            id=record.id,
            session_id=record.session_id,
            segment_id=record.segment_id,
            track_id="translation",
            revision=record.revision,
            language=record.target_language,
            raw_text=record.text,
            display_text=record.text,
            audio_start_ms=record.audio_start_ms,
            audio_end_ms=record.audio_end_ms,
            confidence=None,
            status=record.status,
            received_at_ms=record.received_at_ms,
            finalized_at=record.finalized_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        for record in segments
    ]
    return normalize_timeline(source_records)
