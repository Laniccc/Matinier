from __future__ import annotations

import datetime as dt

import pytest

from app.persistence.models import SegmentRecord
from app.timeline.service import normalize_timeline


NOW = dt.datetime(2026, 7, 22, 12, tzinfo=dt.UTC)


def segment(
    segment_id: str,
    *,
    start_ms: int | None,
    end_ms: int | None,
    text: str | None = None,
    created_offset_ms: int = 0,
    status: str = "final",
) -> SegmentRecord:
    created_at = NOW + dt.timedelta(milliseconds=created_offset_ms)
    return SegmentRecord(
        id=f"row-{segment_id}",
        session_id="session-1",
        segment_id=segment_id,
        track_id="track-1",
        revision=3,
        language="zh-CN",
        raw_text=text or segment_id,
        display_text=text or segment_id,
        audio_start_ms=start_ms,
        audio_end_ms=end_ms,
        confidence=0.9,
        status=status,
        received_at_ms=1_700_000_000_000 + created_offset_ms,
        finalized_at=created_at,
        created_at=created_at,
        updated_at=created_at,
    )


def test_timeline_orders_by_audio_time_and_keeps_stable_ties() -> None:
    timeline = normalize_timeline(
        [
            segment("late", start_ms=3_000, end_ms=4_000),
            segment("tie-b", start_ms=1_000, end_ms=1_500, created_offset_ms=2),
            segment("tie-a", start_ms=1_000, end_ms=1_500, created_offset_ms=1),
            segment("unknown", start_ms=None, end_ms=None),
        ]
    )

    assert [item.segment_id for item in timeline.segments] == [
        "tie-a",
        "tie-b",
        "late",
        "unknown",
    ]
    assert timeline.segments[-1].audio_start_ms == 4_000
    assert timeline.segments[-1].audio_end_ms == 6_000
    assert timeline.duration_ms == 6_000


def test_timeline_normalizes_invalid_and_missing_times_conservatively() -> None:
    timeline = normalize_timeline(
        [
            segment("negative", start_ms=-500, end_ms=-100),
            segment("missing-end", start_ms=1_000, end_ms=None),
            segment("next", start_ms=2_500, end_ms=2_000),
            segment("last", start_ms=5_000, end_ms=None),
        ]
    )

    values = {
        item.segment_id: (item.audio_start_ms, item.audio_end_ms)
        for item in timeline.segments
    }
    assert values == {
        "negative": (0, 0),
        "missing-end": (1_000, 2_500),
        "next": (2_500, 2_500),
        "last": (5_000, 7_000),
    }


def test_timeline_preserves_overlap_unicode_newlines_and_source_metadata() -> None:
    value = "第一行 → 特殊字符\n第二行"
    timeline = normalize_timeline(
        [
            segment("first", start_ms=0, end_ms=2_000),
            segment("second", start_ms=1_500, end_ms=2_500, text=value),
        ]
    )

    second = timeline.segments[1]
    assert (second.audio_start_ms, second.audio_end_ms) == (1_500, 2_500)
    assert second.display_text == value
    assert second.raw_text == value
    assert second.revision == 3
    assert second.received_at_ms == 1_700_000_000_000
    assert second.finalized_at == NOW


def test_timeline_empty_input_and_draft_rejection_are_explicit() -> None:
    empty = normalize_timeline([])
    assert empty.segments == ()
    assert empty.duration_ms == 0

    with pytest.raises(ValueError, match="Final"):
        normalize_timeline(
            [segment("draft", start_ms=0, end_ms=100, status="draft")]
        )
