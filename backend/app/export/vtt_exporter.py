from __future__ import annotations

import html

from app.export.models import (
    ExportDocument,
    format_timestamp,
    normalize_caption_text,
)


def export_vtt(document: ExportDocument) -> bytes:
    cues: list[str] = []
    for segment in document.timeline.segments:
        start = format_timestamp(segment.audio_start_ms)
        end = format_timestamp(segment.audio_end_ms)
        text = html.escape(normalize_caption_text(segment.display_text), quote=False)
        cues.append(f"{start} --> {end}\n{text}")
    header = "WEBVTT\n\n"
    if document.partial_export:
        header += (
            f"NOTE session_status={document.session.status} "
            f"exported_at={document.exported_at.isoformat()} "
            "partial_export=true\n\n"
        )
    if not cues:
        return header.encode("utf-8")
    return (header + "\n\n".join(cues) + "\n").encode("utf-8")
