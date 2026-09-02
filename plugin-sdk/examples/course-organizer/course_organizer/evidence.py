from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import EvidenceItem, KnowledgeItem, RealtimeNote


_NOTE_KEYS = {
    "id",
    "note_type",
    "title",
    "body",
    "evidence_item_ids",
    "confidence_status",
    "language",
    "related_note_ids",
}
_KNOWLEDGE_KEYS = {
    "id",
    "category",
    "topic_path",
    "title",
    "statement",
    "explanation",
    "evidence_item_ids",
    "related_item_ids",
    "confirmation_status",
}


def parse_realtime_notes(
    raw: object,
    *,
    allowed_evidence: Mapping[str, EvidenceItem],
) -> tuple[RealtimeNote, ...]:
    items = _items(raw, "notes")
    notes: list[RealtimeNote] = []
    ids: set[str] = set()
    for raw_item in items:
        _exact_keys(raw_item, _NOTE_KEYS)
        item_id = _string(raw_item, "id")
        if item_id in ids:
            raise ValueError("duplicate realtime note ID")
        ids.add(item_id)
        evidence_ids = _string_tuple(raw_item, "evidence_item_ids", required=True)
        evidence = _resolve(evidence_ids, allowed_evidence)
        confidence_status = _string(raw_item, "confidence_status")
        _prevent_upgrade(evidence, confidence_status)
        notes.append(
            RealtimeNote(
                id=item_id,
                note_type=_string(raw_item, "note_type"),  # type: ignore[arg-type]
                title=_string(raw_item, "title"),
                body=_string(raw_item, "body"),
                start_ms=min(item.start_ms for item in evidence),
                end_ms=max(item.end_ms for item in evidence),
                source_segment_ids=_source_ids(evidence),
                evidence_item_ids=evidence_ids,
                confidence_status=confidence_status,  # type: ignore[arg-type]
                language=_string(raw_item, "language"),
                related_note_ids=_string_tuple(raw_item, "related_note_ids"),
            )
        )
    for note in notes:
        if any(item not in ids for item in note.related_note_ids):
            raise ValueError("realtime note references an unknown related note")
    return tuple(notes)


def parse_knowledge_items(
    raw: object,
    *,
    allowed_evidence: Mapping[str, EvidenceItem],
) -> tuple[KnowledgeItem, ...]:
    items = _items(raw, "items")
    knowledge: list[KnowledgeItem] = []
    ids: set[str] = set()
    for raw_item in items:
        _exact_keys(raw_item, _KNOWLEDGE_KEYS)
        item_id = _string(raw_item, "id")
        if item_id in ids:
            raise ValueError("duplicate knowledge item ID")
        ids.add(item_id)
        evidence_ids = _string_tuple(raw_item, "evidence_item_ids", required=True)
        evidence = _resolve(evidence_ids, allowed_evidence)
        confirmation_status = _string(raw_item, "confirmation_status")
        _prevent_upgrade(evidence, confirmation_status)
        knowledge.append(
            KnowledgeItem(
                id=item_id,
                category=_string(raw_item, "category"),
                topic_path=_string_tuple(raw_item, "topic_path", required=True),
                title=_string(raw_item, "title"),
                statement=_string(raw_item, "statement"),
                explanation=_string(raw_item, "explanation"),
                evidence_item_ids=evidence_ids,
                source_segment_ids=_source_ids(evidence),
                time_ranges=tuple((item.start_ms, item.end_ms) for item in evidence),
                related_item_ids=_string_tuple(raw_item, "related_item_ids"),
                confirmation_status=confirmation_status,  # type: ignore[arg-type]
            )
        )
    for item in knowledge:
        if any(related not in ids for related in item.related_item_ids):
            raise ValueError("knowledge item references an unknown related item")
    return tuple(knowledge)


def _items(raw: object, key: str) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or set(raw) != {key}:
        raise ValueError("model output has an invalid envelope")
    values = raw[key]
    if not isinstance(values, list):
        raise ValueError("model output items must be an array")
    if len(values) > 500:
        raise ValueError("model output contains too many items")
    if any(not isinstance(item, dict) for item in values):
        raise ValueError("model output item must be an object")
    return values


def _exact_keys(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError("model output item fields do not match the contract")


def _string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"model output {key} must be a non-empty string")
    return result


def _string_tuple(
    value: dict[str, Any],
    key: str,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    result = value.get(key)
    if not isinstance(result, list) or any(
        not isinstance(item, str) or not item for item in result
    ):
        raise ValueError(f"model output {key} must be a string array")
    output = tuple(result)
    if required and not output:
        raise ValueError(f"model output {key} must not be empty")
    if len(output) != len(set(output)):
        raise ValueError(f"model output {key} contains duplicates")
    return output


def _resolve(
    evidence_ids: tuple[str, ...],
    allowed: Mapping[str, EvidenceItem],
) -> tuple[EvidenceItem, ...]:
    try:
        return tuple(allowed[item_id] for item_id in evidence_ids)
    except KeyError:
        raise ValueError("model cited evidence outside the current chunk") from None


def _prevent_upgrade(
    evidence: tuple[EvidenceItem, ...],
    confirmation_status: str,
) -> None:
    if (
        confirmation_status == "confirmed"
        and any(item.confirmation_status == "needs_confirmation" for item in evidence)
    ):
        raise ValueError("uncertain evidence cannot be upgraded to a confirmed fact")


def _source_ids(evidence: tuple[EvidenceItem, ...]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for item in evidence:
        for segment_id in item.source_segment_ids:
            if segment_id not in seen:
                seen.add(segment_id)
                output.append(segment_id)
    return tuple(output)


__all__ = ["parse_knowledge_items", "parse_realtime_notes"]
