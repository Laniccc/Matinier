from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence

from pydantic import ValidationError

from app.text_processing.models import (
    ProcessedScriptContent,
    SourceSegmentSnapshot,
)


class ScriptOutputError(ValueError):
    """The text provider returned output that cannot be saved safely."""


def parse_script_content(
    content: str,
    source_segments: Sequence[SourceSegmentSnapshot],
) -> ProcessedScriptContent:
    if not content.strip():
        raise ScriptOutputError("DeepSeek returned empty content")
    if not source_segments:
        raise ScriptOutputError("source segment snapshot is empty")

    try:
        payload = json.loads(content)
        parsed = ProcessedScriptContent.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
        raise ScriptOutputError("DeepSeek output is not valid script JSON") from error

    sources_by_id = {segment.segment_id: segment for segment in source_segments}
    if len(sources_by_id) != len(source_segments):
        raise ScriptOutputError("source snapshot contains duplicate segment IDs")

    referenced_ids = [
        segment_id
        for section in parsed.sections
        for segment_id in section.source_segment_ids
    ]
    counts = Counter(referenced_ids)
    if any(segment_id not in sources_by_id for segment_id in referenced_ids):
        raise ScriptOutputError("script references a foreign source segment")
    if any(count != 1 for count in counts.values()):
        raise ScriptOutputError("source segments must not be referenced more than once")
    if set(referenced_ids) != set(sources_by_id):
        raise ScriptOutputError("every source segment must be referenced exactly once")

    for section in parsed.sections:
        referenced = [
            sources_by_id[segment_id]
            for segment_id in section.source_segment_ids
        ]
        expected_start_ms = min(segment.audio_start_ms for segment in referenced)
        expected_end_ms = max(segment.audio_end_ms for segment in referenced)
        if (
            section.start_ms != expected_start_ms
            or section.end_ms != expected_end_ms
        ):
            raise ScriptOutputError(
                "script section timing does not match its source references"
            )

    return parsed
