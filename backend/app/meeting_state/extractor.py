from __future__ import annotations

import json
import re
from collections.abc import Sequence
from copy import deepcopy

from pydantic import ValidationError

from app.meeting_state.contracts import MeetingStateDelta, ProjectionSegment
from app.meeting_state.models import MeetingState
from app.text_processing.parser import ScriptOutputError
from app.text_processing.provider import (
    StructuredCompletionRequest,
    StructuredTextProvider,
)


_SYSTEM_PROMPT = """You incrementally extract a private Meeting State from Final
caption segments. Return exactly one JSON object and no Markdown. Every extracted
fact and every candidate field must cite one or more supplied segment_id values.
Never invent IDs. Never infer a due date, priority, or resolved identity that the
captions do not establish. An unresolved spoken assignee remains the original text.

The object has these fields:
{
  "topics": [{"text": "...", "source_segment_ids": ["..."]}],
  "entities": [{"text": "...", "entity_type": "person|team|project|other",
    "source_segment_ids": ["..."]}],
  "decisions": [{"text": "...", "source_segment_ids": ["..."]}],
  "highlights": [{"text": "...", "source_segment_ids": ["..."]}],
  "conflicts": [{"text": "...", "source_segment_ids": ["..."]}],
  "action_operations": [],
  "warnings": []
}

action_operations may contain only create, revise, merge, split, or cancel.
create: {"kind":"create","content":CONTENT,"change_summary":null}
revise: {"kind":"revise","candidate_id":"existing server ID",
  "expected_revision":1,"content":CONTENT,"change_summary":"..."}
merge: {"kind":"merge","candidate_ids":["id1","id2"],
  "expected_revisions":{"id1":1,"id2":1},"source_segment_ids":["..."],
  "change_summary":"..."}
split: {"kind":"split","candidate_id":"id","expected_revision":1,
  "parts":[CONTENT,CONTENT],"source_segment_ids":["..."],
  "change_summary":"..."}
cancel: {"kind":"cancel","candidate_id":"id","expected_revision":1,
  "source_segment_ids":["..."],"reason":"..."}

CONTENT has title, deliverable, assignee, due_at, priority, and
blocking_conflict_segment_ids. Exactly the first five fields are grounded objects:
{"value": value-or-null, "resolution":"known|missing|ambiguous|conflicting",
 "source_segment_ids":["..."], "confidence":0.0, "explanation":null}.
blocking_conflict_segment_ids is always a plain JSON array of segment IDs, never a
grounded object. due_at uses a full ISO-8601 datetime with timezone when known, for
example "2026-08-28T00:00:00+08:00"; a date-only string is not valid. priority is
low, normal, high, or urgent when known.
Use current candidates only to propose revision-aware operations. Do not copy old
Meeting State items merely because they appear in current_state.
"""

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _compact_state(state: MeetingState) -> dict[str, object]:
    return {
        "version": state.version,
        "topics": [item.model_dump(mode="json") for item in state.topics],
        "entities": [item.model_dump(mode="json") for item in state.entities],
        "decisions": [item.model_dump(mode="json") for item in state.decisions],
        "highlights": [item.model_dump(mode="json") for item in state.highlights],
        "conflicts": [item.model_dump(mode="json") for item in state.conflicts],
        "action_candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "current_revision": candidate.current_revision,
                "content_status": candidate.content_status,
                "execution_status": candidate.execution_status,
                "content": candidate.content.model_dump(
                    mode="json",
                    exclude={"evidence_messages"},
                ),
            }
            for candidate in state.action_candidates
        ],
    }


def parse_meeting_state_delta(
    content: str,
    *,
    session_id: str,
    segments: Sequence[ProjectionSegment],
) -> MeetingStateDelta:
    if not content.strip():
        raise ScriptOutputError("structured provider returned empty content")
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ScriptOutputError(
            "structured provider output is not valid Meeting State JSON"
        ) from error
    if not isinstance(payload, dict):
        raise ScriptOutputError("Meeting State output must be a JSON object")
    if "session_id" in payload or "source_segment_ids" in payload:
        raise ScriptOutputError(
            "Meeting State output attempted to set server-owned fields"
        )

    allowed_ids = tuple(segment.segment_id for segment in segments)
    normalized = _normalize_model_payload(payload)
    try:
        delta = MeetingStateDelta.model_validate(
            {
                **normalized,
                "session_id": session_id,
                "source_segment_ids": allowed_ids,
            }
        )
    except (ValidationError, TypeError, ValueError) as error:
        raise ScriptOutputError(
            "structured provider output violates Meeting State contract"
        ) from error

    foreign_ids = delta.cited_segment_ids() - frozenset(allowed_ids)
    if foreign_ids:
        raise ScriptOutputError(
            "Meeting State output references foreign Segments: "
            + ", ".join(sorted(foreign_ids))
        )
    return delta


def _normalize_model_payload(payload: dict[str, object]) -> dict[str, object]:
    """Normalize bounded JSON-shape quirks without inventing meeting facts."""

    normalized = deepcopy(payload)
    operations = normalized.get("action_operations")
    if not isinstance(operations, list):
        return normalized
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        content = operation.get("content")
        if not isinstance(content, dict):
            continue
        conflicts = content.get("blocking_conflict_segment_ids")
        if isinstance(conflicts, dict):
            value = conflicts.get("value")
            if value is None or isinstance(value, list):
                content["blocking_conflict_segment_ids"] = value or []
        due_at = content.get("due_at")
        if not isinstance(due_at, dict):
            continue
        due_value = due_at.get("value")
        if isinstance(due_value, str) and _DATE_ONLY.fullmatch(due_value):
            due_at["value"] = f"{due_value}T00:00:00+00:00"
    return normalized


class MeetingStateExtractor:
    def __init__(self, provider: StructuredTextProvider) -> None:
        self._provider = provider

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name

    @property
    def model(self) -> str:
        return self._provider.model

    async def extract(
        self,
        *,
        state: MeetingState,
        segments: Sequence[ProjectionSegment],
    ) -> MeetingStateDelta:
        if not segments:
            raise ValueError("Meeting State extraction requires Final Segments")
        session_ids = {segment.session_id for segment in segments}
        if session_ids != {state.session_id}:
            raise ValueError("projection batch must belong to the Meeting State Session")

        input_payload = {
            "current_state": _compact_state(state),
            "segments": [
                {
                    "segment_id": segment.segment_id,
                    "revision": segment.revision,
                    "language": segment.language,
                    "text": segment.display_text,
                    "audio_start_ms": segment.audio_start_ms,
                    "audio_end_ms": segment.audio_end_ms,
                }
                for segment in segments
            ],
        }
        completion = await self._provider.complete_structured(
            StructuredCompletionRequest(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt="Project only the supplied new or revised Final segments.",
                input_payload=input_payload,
            )
        )
        if completion.finish_reason == "length":
            raise ScriptOutputError("Meeting State provider output was truncated")
        try:
            return parse_meeting_state_delta(
                completion.content,
                session_id=state.session_id,
                segments=segments,
            )
        except ScriptOutputError:
            repair = await self._provider.complete_structured(
                StructuredCompletionRequest(
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=(
                        "Repair invalid_output into exactly one contract-compliant "
                        "Meeting State JSON object. Preserve only facts and Segment "
                        "IDs present in the supplied input."
                    ),
                    input_payload={
                        **input_payload,
                        "invalid_output": completion.content,
                    },
                )
            )
            if repair.finish_reason == "length":
                raise ScriptOutputError("Meeting State repair output was truncated")
            return parse_meeting_state_delta(
                repair.content,
                session_id=state.session_id,
                segments=segments,
            )
