from __future__ import annotations

import datetime as dt

from app.export.models import format_timestamp, one_line
from app.text_processing.models import ProcessedScriptContent


def render_processed_script_markdown(
    content: ProcessedScriptContent,
    *,
    provider: str,
    model: str,
    version: int,
    created_at: dt.datetime,
) -> str:
    generated_at = (
        created_at
        if created_at.tzinfo is not None
        else created_at.replace(tzinfo=dt.UTC)
    )
    lines = [
        f"# {one_line(content.title)}",
        "",
        f"Provider: {one_line(provider)}",
        f"Model: {one_line(model)}",
        f"Version: {version}",
        f"Generated: {generated_at.isoformat()}",
        "",
    ]
    for section in content.sections:
        start = format_timestamp(section.start_ms)
        end = format_timestamp(section.end_ms)
        source_ids = ", ".join(
            f"`{one_line(segment_id)}`"
            for segment_id in section.source_segment_ids
        )
        lines.extend(
            [
                f"[{start} - {end}]",
                section.clean_text,
                "",
                f"Source segments: {source_ids}",
            ]
        )
        if section.notes:
            lines.append("Notes: " + "; ".join(section.notes))
        lines.append("")

    if content.warnings:
        lines.extend(
            [
                "## Warnings",
                "",
                *(f"- {warning}" for warning in content.warnings),
                "",
            ]
        )
    return "\n".join(lines)
