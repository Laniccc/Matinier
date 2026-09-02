from __future__ import annotations

from app.export.models import (
    ExportDocument,
    format_timestamp,
    normalize_caption_text,
)


def export_srt(document: ExportDocument) -> bytes:
    cues: list[str] = []
    for index, segment in enumerate(document.timeline.segments, start=1):
        start = format_timestamp(segment.audio_start_ms, separator=",")
        end = format_timestamp(segment.audio_end_ms, separator=",")
        text = normalize_caption_text(segment.display_text)
        cues.append(f"{index}\n{start} --> {end}\n{text}")
    if not cues:
        return b""
    return ("\n\n".join(cues) + "\n").encode("utf-8")
