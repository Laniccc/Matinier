from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.artifacts.models import DerivedArtifact
from app.packages.models import TranscriptItem, TranscriptPackage
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


_CLEAN_SCRIPT_PROMPT = """You edit the effective source document of a frozen transcript
Package without translating it. Return exactly one JSON object and no Markdown.
Add natural punctuation, merge adjacent fragments where useful, and remove only
meaningless verbal repetition. Preserve every fact, name, number, time, product name,
technical term, uncertainty, and source language. Use notes for material editorial
choices and warnings for ambiguity that cannot be resolved from the source.
Never invent, omit, duplicate, reorder, or modify source_item_ids.
The output schema is:
{
  "title": "short title",
  "sections": [
    {
      "source_item_ids": ["an input item_id"],
      "clean_text": "cleaned source-language text",
      "notes": []
    }
  ],
  "warnings": []
}
Every input item_id must occur exactly once. Do not output timestamps or Segment IDs;
the server derives them from Package evidence.
"""


class CleanScriptOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    style: str | None = Field(default=None, max_length=100)

    @field_validator("style")
    @classmethod
    def style_must_be_visible(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("style must not be blank")
        return normalized


class _ModelSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_item_ids: tuple[str, ...] = Field(min_length=1)
    clean_text: str = Field(min_length=1)
    notes: tuple[str, ...]

    @field_validator("source_item_ids")
    @classmethod
    def item_ids_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("section repeats a source item ID")
        return value

    @field_validator("clean_text")
    @classmethod
    def text_must_be_visible(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("clean_text must not be blank")
        return value


class _ModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1)
    sections: tuple[_ModelSection, ...] = Field(min_length=1)
    warnings: tuple[str, ...]

    @field_validator("title")
    @classmethod
    def title_must_be_visible(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value


def _parse_chunk(content: str, items: Sequence[TranscriptItem]) -> _ModelOutput:
    if not content.strip():
        raise ScriptOutputError("structured provider returned empty content")
    try:
        result = _ModelOutput.model_validate(json.loads(content))
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
        raise ScriptOutputError(
            "structured provider output is not valid clean script JSON"
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
            "every Package source item must be referenced exactly once"
        )
    return result


class CleanScriptWorkflow:
    artifact_kind = "clean_script"
    workflow_version = "2.0"
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

    async def _generate_chunk(
        self,
        chunk: tuple[TranscriptItem, ...],
        options: CleanScriptOptions,
    ) -> _ModelOutput:
        request = StructuredCompletionRequest(
            system_prompt=_CLEAN_SCRIPT_PROMPT,
            user_prompt="Clean these Package effective-source items:",
            input_payload={
                "style": options.style,
                "items": [
                    {"item_id": item.item_id, "text": item.text}
                    for item in chunk
                ]
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
            raise ValueError("clean script does not accept a target artifact")
        validated_options = CleanScriptOptions.model_validate(options)
        items = PackageReader.effective_source(package).content.items
        if not items:
            raise ValueError("Package effective source contains no items")
        chunks = chunk_items(
            items,
            text_of=lambda item: item.text,
            max_items=self._max_items_per_chunk,
            max_chars=self._max_input_chars,
        )
        results = [
            await self._generate_chunk(chunk, validated_options)
            for chunk in chunks
        ]
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
        content_sections = [
            {
                "source_item_ids": list(section.source_item_ids),
                "source_segment_ids": list(evidence_entry.source_segment_ids),
                "start_ms": evidence_entry.start_ms,
                "end_ms": evidence_entry.end_ms,
                "clean_text": section.clean_text,
                "notes": list(section.notes),
            }
            for section, evidence_entry in zip(sections, evidence, strict=True)
        ]
        return ArtifactDraft(
            provider=self._provider.provider_name,
            model=self._provider.model,
            content={
                "title": results[0].title,
                "sections": content_sections,
                "warnings": [
                    warning for result in results for warning in result.warnings
                ],
            },
            evidence=evidence,
            options=validated_options.model_dump(
                mode="json",
                exclude_none=True,
            ),
        )
