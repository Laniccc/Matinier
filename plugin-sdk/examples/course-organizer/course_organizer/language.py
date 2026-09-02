from __future__ import annotations

import re
from typing import Protocol

from .models import RealtimeNote


_LANGUAGE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


class LanguageCapabilityClient(Protocol):
    async def capability(
        self,
        *,
        name: str,
        session_scope: str,
        input_value: dict[str, object],
        idempotency_key: str | None = None,
    ) -> dict[str, object]: ...


def validate_language(value: str) -> str:
    if not isinstance(value, str) or _LANGUAGE.fullmatch(value) is None:
        raise ValueError("invalid document language")
    return value


def choose_document_language(
    source_language: str,
    target_languages: tuple[str, ...],
    requested_language: str | None,
) -> str:
    validate_language(source_language)
    for item in target_languages:
        validate_language(item)
    if requested_language is not None:
        return validate_language(requested_language)
    return target_languages[0] if target_languages else source_language


async def localize_notes(
    capabilities: LanguageCapabilityClient,
    *,
    session_scope: str,
    notes: tuple[RealtimeNote, ...],
    target_language: str,
    chunk_size: int = 40,
) -> tuple[RealtimeNote, ...]:
    validate_language(target_language)
    if chunk_size < 1 or chunk_size > 100:
        raise ValueError("language chunk size must be between 1 and 100")
    localized: list[RealtimeNote] = []
    for offset in range(0, len(notes), chunk_size):
        chunk = notes[offset : offset + chunk_size]
        response = await capabilities.capability(
            name="model.invoke",
            session_scope=session_scope,
            input_value={
                "input_category": "plugin_state",
                "system_prompt": (
                    "Localize note titles and bodies only. Preserve every ID, "
                    "evidence reference, classification, and factual uncertainty."
                ),
                "user_prompt": f"Localize this note chunk to {target_language}.",
                "input_payload": {
                    "task": "course.localize_notes",
                    "target_language": target_language,
                    "items": [
                        {"id": item.id, "title": item.title, "body": item.body}
                        for item in chunk
                    ],
                },
                "response_format": "json_object",
                "max_output_tokens": 1_024,
                "timeout_seconds": 20,
            },
        )
        output = response.get("output")
        translated = _parse_localized(output, expected_ids=tuple(item.id for item in chunk))
        by_id = {item["id"]: item for item in translated}
        localized.extend(
            RealtimeNote(
                id=item.id,
                note_type=item.note_type,
                title=by_id[item.id]["title"],
                body=by_id[item.id]["body"],
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                source_segment_ids=item.source_segment_ids,
                evidence_item_ids=item.evidence_item_ids,
                confidence_status=item.confidence_status,
                language=target_language,
                related_note_ids=item.related_note_ids,
            )
            for item in chunk
        )
    return tuple(localized)


def _parse_localized(
    raw: object,
    *,
    expected_ids: tuple[str, ...],
) -> tuple[dict[str, str], ...]:
    if not isinstance(raw, dict) or set(raw) != {"notes"}:
        raise ValueError("invalid localized-note envelope")
    notes = raw["notes"]
    if not isinstance(notes, list):
        raise ValueError("localized notes must be an array")
    parsed: list[dict[str, str]] = []
    for item in notes:
        if not isinstance(item, dict) or set(item) != {"id", "title", "body"}:
            raise ValueError("invalid localized note")
        if any(not isinstance(item[key], str) or not item[key] for key in item):
            raise ValueError("localized note values must be non-empty strings")
        parsed.append({key: item[key] for key in ("id", "title", "body")})
    if tuple(item["id"] for item in parsed) != expected_ids:
        raise ValueError("localized note IDs changed")
    return tuple(parsed)


__all__ = ["choose_document_language", "localize_notes", "validate_language"]
