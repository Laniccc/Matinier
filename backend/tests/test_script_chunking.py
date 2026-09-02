from __future__ import annotations

from app.text_processing.chunking import chunk_source_segments
from app.text_processing.models import SourceSegmentSnapshot


def segment(
    index: int,
    text: str,
    *,
    start_ms: int | None = None,
) -> SourceSegmentSnapshot:
    actual_start = index * 1_000 if start_ms is None else start_ms
    return SourceSegmentSnapshot(
        segment_id=f"seg-{index}",
        revision=1,
        language="zh-CN",
        raw_text=text,
        display_text=text,
        audio_start_ms=actual_start,
        audio_end_ms=actual_start + 900,
    )


def test_chunking_preserves_order_and_segment_limit() -> None:
    chunks = chunk_source_segments(
        tuple(segment(index, f"字幕{index}") for index in range(5)),
        max_segments=2,
        max_chars=1_000,
    )

    assert [[item.segment_id for item in chunk] for chunk in chunks] == [
        ["seg-0", "seg-1"],
        ["seg-2", "seg-3"],
        ["seg-4"],
    ]


def test_chunking_obeys_unicode_character_budget_without_rewriting_text() -> None:
    segments = (
        segment(0, "中文\n一"),
        segment(1, "欢迎"),
        segment(2, "🙂结束"),
    )

    chunks = chunk_source_segments(
        segments,
        max_segments=10,
        max_chars=6,
    )

    assert [[item.display_text for item in chunk] for chunk in chunks] == [
        ["中文\n一", "欢迎"],
        ["🙂结束"],
    ]
    assert chunks[0][0].display_text == "中文\n一"


def test_single_oversized_segment_is_kept_as_one_chunk() -> None:
    oversized = segment(0, "超" * 20)

    chunks = chunk_source_segments(
        (oversized,),
        max_segments=3,
        max_chars=5,
    )

    assert chunks == ((oversized,),)


def test_empty_segments_return_no_chunks() -> None:
    assert (
        chunk_source_segments((), max_segments=10, max_chars=100)
        == ()
    )
