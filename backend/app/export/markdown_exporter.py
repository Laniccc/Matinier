from __future__ import annotations

from app.export.models import (
    ExportDocument,
    format_timestamp,
    normalize_caption_text,
    one_line,
)


def export_markdown(document: ExportDocument) -> bytes:
    session = document.session
    lines = [
        f"# Session {one_line(session.id)}",
        "",
        f"Source: {one_line(session.source_name)}",
        f"Language: {one_line(session.language)}",
        f"Duration: {format_timestamp(document.timeline.duration_ms)}",
        f"Provider: {one_line(session.asr_provider or '—')}",
        f"Model: {one_line(session.asr_model or '—')}",
        "",
    ]
    if document.partial_export:
        lines[1:1] = [
            "",
            f"Session status: {one_line(session.status)}",
            f"Exported at: {document.exported_at.isoformat()}",
            "Partial export: true",
        ]
    if not document.timeline.segments:
        lines.extend(["_No Final captions._", ""])
    else:
        for segment in document.timeline.segments:
            start = format_timestamp(segment.audio_start_ms)
            end = format_timestamp(segment.audio_end_ms)
            lines.extend(
                [
                    f"[{start} - {end}]",
                    normalize_caption_text(segment.display_text),
                    "",
                ]
            )
    return "\n".join(lines).encode("utf-8")
