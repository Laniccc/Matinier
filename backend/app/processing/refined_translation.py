from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from app.artifacts.models import DerivedArtifact
from app.packages.models import (
    LiveTranslationDocument,
    TranscriptItem,
    TranscriptPackage,
)
from app.processing.contracts import (
    ArtifactDraft,
    EvidenceValidator,
    PackageReader,
)
from app.processing.structured import chunk_items, complete_and_parse
from app.text_processing.parser import ScriptOutputError
from app.text_processing.provider import (
    StructuredCompletionRequest,
    StructuredTextProvider,
)


_LANGUAGE_PATTERN = re.compile(
    r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"
)

_REFINED_TRANSLATION_PROMPT = """You create a publication-quality translation from
the effective source document of a frozen transcript Package. The source items are the
only factual authority. A live_translation_reference may be provided as fallible wording
help; correct it whenever it conflicts with the source. Preserve names, numbers, dates,
times, product names, technical terms, uncertainty, and meaning. Apply the supplied
glossary exactly when context permits and follow the requested style.

Return exactly one JSON object and no Markdown:
{
  "sections": [
    {
      "source_item_ids": ["an input item_id"],
      "translated_text": "target-language text",
      "notes": []
    }
  ],
  "warnings": []
}
Every input item_id must occur exactly once. Context items and live translations must
never be cited as source_item_ids. Do not output timestamps or Segment IDs; the server
derives them from Package evidence.
"""


def normalize_language(value: str) -> str:
    value = value.strip()
    if not _LANGUAGE_PATTERN.fullmatch(value):
        raise ValueError("target_language must be a BCP-47-like language tag")
    parts = value.split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        elif len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


class RefinedTranslationOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_language: str = Field(min_length=2, max_length=32)
    glossary: dict[str, str] = Field(default_factory=dict)
    style: str | None = Field(default=None, max_length=100)
    context_window_items: int = Field(default=2, ge=0, le=20)

    @field_validator("target_language")
    @classmethod
    def target_language_must_be_valid(cls, value: str) -> str:
        return normalize_language(value)

    @field_validator("glossary")
    @classmethod
    def glossary_must_be_bounded(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 200:
            raise ValueError("glossary must contain at most 200 entries")
        normalized: dict[str, str] = {}
        for source, target in value.items():
            clean_source = source.strip()
            clean_target = target.strip()
            if not clean_source or not clean_target:
                raise ValueError("glossary terms must not be blank")
            normalized[clean_source] = clean_target
        return normalized

    @field_validator("style")
    @classmethod
    def style_must_be_visible(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("style must not be blank")
        return normalized


class _TranslationSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_item_ids: tuple[str, ...] = Field(min_length=1)
    translated_text: str = Field(min_length=1)
    notes: tuple[str, ...]

    @field_validator("source_item_ids")
    @classmethod
    def item_ids_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("translation section repeats a source item ID")
        return value

    @field_validator("translated_text")
    @classmethod
    def text_must_be_visible(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("translated_text must not be blank")
        return normalized


class _TranslationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sections: tuple[_TranslationSection, ...] = Field(min_length=1)
    warnings: tuple[str, ...]


def _parse_chunk(
    content: str,
    items: Sequence[TranscriptItem],
) -> _TranslationOutput:
    if not content.strip():
        raise ScriptOutputError("structured provider returned empty content")
    try:
        result = _TranslationOutput.model_validate(json.loads(content))
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
        raise ScriptOutputError(
            "structured provider output is not valid refined translation JSON"
        ) from error
    expected = {item.item_id for item in items}
    referenced = [
        item_id
        for section in result.sections
        for item_id in section.source_item_ids
    ]
    counts = Counter(referenced)
    if set(referenced) != expected or any(count != 1 for count in counts.values()):
        raise ScriptOutputError(
            "every Package source item must be translated exactly once"
        )
    return result


def _item_payload(item: TranscriptItem) -> dict[str, object]:
    return {"item_id": item.item_id, "text": item.text}


class RefinedTranslationWorkflow:
    artifact_kind = "refined_translation"
    workflow_version = "1.0"
    requires_complete_source = True
    allows_empty_evidence = False

    def __init__(
        self,
        provider: StructuredTextProvider,
        *,
        max_retries: int,
        max_input_chars: int,
        max_items_per_chunk: int,
        retry_delay_seconds: float = 0.2,
        evidence_validator: EvidenceValidator | None = None,
    ) -> None:
        if max_retries < 0 or retry_delay_seconds < 0:
            raise ValueError("retry settings must be non-negative")
        self._provider = provider
        self._max_retries = max_retries
        self._max_input_chars = max_input_chars
        self._max_items_per_chunk = max_items_per_chunk
        self._retry_delay_seconds = retry_delay_seconds
        self._evidence_validator = evidence_validator or EvidenceValidator()

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name

    @property
    def model(self) -> str:
        return self._provider.model

    @staticmethod
    def _live_reference(
        package: TranscriptPackage,
        target_language: str,
        chunk: Sequence[TranscriptItem],
    ) -> list[dict[str, object]]:
        source_segment_ids = {
            segment_id
            for item in chunk
            for segment_id in item.source_segment_ids
        }
        references: list[dict[str, object]] = []
        for document in package.documents:
            if not isinstance(document, LiveTranslationDocument):
                continue
            if normalize_language(document.language) != target_language:
                continue
            for item in document.content.items:
                if source_segment_ids.intersection(item.source_segment_ids):
                    references.append(
                        {
                            "text": item.text,
                            "source_segment_ids": list(item.source_segment_ids),
                        }
                    )
        return references

    async def _generate_chunk(
        self,
        package: TranscriptPackage,
        all_items: tuple[TranscriptItem, ...],
        chunk: tuple[TranscriptItem, ...],
        *,
        chunk_start: int,
        options: RefinedTranslationOptions,
    ) -> _TranslationOutput:
        chunk_end = chunk_start + len(chunk)
        window = options.context_window_items
        context_before = all_items[max(0, chunk_start - window) : chunk_start]
        context_after = all_items[chunk_end : chunk_end + window]
        request = StructuredCompletionRequest(
            system_prompt=_REFINED_TRANSLATION_PROMPT,
            user_prompt="Translate these Package effective-source items:",
            input_payload={
                "target_language": options.target_language,
                "glossary": options.glossary,
                "style": options.style,
                "items": [_item_payload(item) for item in chunk],
                "context_before": [_item_payload(item) for item in context_before],
                "context_after": [_item_payload(item) for item in context_after],
                "live_translation_reference": self._live_reference(
                    package,
                    options.target_language,
                    chunk,
                ),
            },
        )
        return await complete_and_parse(
            self._provider,
            request,
            parse=lambda content: _parse_chunk(content, chunk),
            max_retries=self._max_retries,
            retry_delay_seconds=self._retry_delay_seconds,
        )

    async def run(
        self,
        package: TranscriptPackage,
        options: Mapping[str, Any],
        target_artifact: DerivedArtifact | None = None,
    ) -> ArtifactDraft:
        if target_artifact is not None:
            raise ValueError("refined translation does not accept a target artifact")
        validated_options = RefinedTranslationOptions.model_validate(options)
        items = PackageReader.effective_source(package).content.items
        if not items:
            raise ValueError("Package effective source contains no items")
        chunks = chunk_items(
            items,
            text_of=lambda item: item.text,
            max_items=self._max_items_per_chunk,
            max_chars=self._max_input_chars,
        )
        results: list[_TranslationOutput] = []
        cursor = 0
        for chunk in chunks:
            results.append(
                await self._generate_chunk(
                    package,
                    items,
                    chunk,
                    chunk_start=cursor,
                    options=validated_options,
                )
            )
            cursor += len(chunk)
        sections = tuple(
            section for result in results for section in result.sections
        )
        evidence = tuple(
            self._evidence_validator.derive(
                package,
                evidence_key=f"section:{index}",
                source_item_ids=section.source_item_ids,
            )
            for index, section in enumerate(sections)
        )
        source_items_by_id = {item.item_id: item for item in items}
        content_sections = [
            {
                "source_item_ids": list(section.source_item_ids),
                "source_segment_ids": list(evidence_entry.source_segment_ids),
                "start_ms": evidence_entry.start_ms,
                "end_ms": evidence_entry.end_ms,
                "source_text": " ".join(
                    source_items_by_id[item_id].text
                    for item_id in section.source_item_ids
                ),
                "translated_text": section.translated_text,
                "notes": list(section.notes),
            }
            for section, evidence_entry in zip(sections, evidence, strict=True)
        ]
        normalized_options = validated_options.model_dump(
            mode="json",
            exclude_none=True,
        )
        return ArtifactDraft(
            provider=self._provider.provider_name,
            model=self._provider.model,
            target_language=validated_options.target_language,
            options=normalized_options,
            content={
                "target_language": validated_options.target_language,
                "sections": content_sections,
                "warnings": [
                    warning for result in results for warning in result.warnings
                ],
            },
            evidence=evidence,
        )
