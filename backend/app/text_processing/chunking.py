from __future__ import annotations

from collections.abc import Sequence

from app.text_processing.models import SourceSegmentSnapshot


def chunk_source_segments(
    segments: Sequence[SourceSegmentSnapshot],
    *,
    max_segments: int,
    max_chars: int,
) -> tuple[tuple[SourceSegmentSnapshot, ...], ...]:
    if max_segments <= 0:
        raise ValueError("max_segments must be positive")
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    chunks: list[tuple[SourceSegmentSnapshot, ...]] = []
    current: list[SourceSegmentSnapshot] = []
    current_chars = 0
    for segment in segments:
        segment_chars = len(segment.display_text)
        exceeds_count = len(current) >= max_segments
        exceeds_chars = bool(current) and current_chars + segment_chars > max_chars
        if exceeds_count or exceeds_chars:
            chunks.append(tuple(current))
            current = []
            current_chars = 0
        current.append(segment)
        current_chars += segment_chars

    if current:
        chunks.append(tuple(current))
    return tuple(chunks)
