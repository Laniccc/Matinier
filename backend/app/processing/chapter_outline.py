from __future__ import annotations

import json
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
from app.packages.models import TranscriptItem, TranscriptPackage
from app.processing.contracts import ArtifactDraft, PackageReader
from app.processing.factual_evidence import FactualEvidenceBuilder
from app.processing.structured import chunk_items, complete_and_parse
from app.text_processing.parser import ScriptOutputError
from app.text_processing.provider import (
    StructuredCompletionRequest,
    StructuredTextProvider,
)


_CHAPTER_PROMPT = """Create a chronological chapter outline from the effective source
items of a frozen transcript Package. The supplied items are the only factual authority.
Use compact, descriptive chapter titles and factual summaries.

Return exactly one JSON object and no Markdown:
{
  "chapters": [
    {
      "title": "chapter title",
      "summary": "chapter summary",
      "evidence_item_ids": ["one or more supplied item_id values"]
    }
  ],
  "warnings": []
}
Chapters must follow source time order. Every chapter must cite non-empty evidence from
the supplied items. Do not output timestamps, excerpts, or Segment IDs; the server
derives them from Package evidence.
"""


class ChapterOutlineOptions(BaseModel):
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


class _Chapter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    evidence_item_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("title", "summary")
    @classmethod
    def text_must_be_visible(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("chapter title and summary must not be blank")
        return normalized

    @field_validator("evidence_item_ids")
    @classmethod
    def evidence_ids_must_be_unique(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("chapter repeats an evidence item ID")
        return value


class _ChapterOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chapters: tuple[_Chapter, ...] = Field(min_length=1)
    warnings: tuple[str, ...]


def _parse_chunk(
    content: str,
    items: Sequence[TranscriptItem],
) -> _ChapterOutput:
    if not content.strip():
        raise ScriptOutputError("structured provider returned empty content")
    try:
        result = _ChapterOutput.model_validate(json.loads(content))
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
        raise ScriptOutputError(
            "structured provider output is not valid chapter outline JSON"
        ) from error
    allowed_ids = {item.item_id for item in items}
    referenced_ids = {
        item_id
        for chapter in result.chapters
        for item_id in chapter.evidence_item_ids
    }
    foreign_ids = referenced_ids - allowed_ids
    if foreign_ids:
        raise ScriptOutputError(
            f"chapter outline references foreign Package items: {sorted(foreign_ids)}"
        )
    return result


class ChapterOutlineWorkflow:
    artifact_kind = "chapter_outline"
    workflow_version = "1.0"
    requires_complete_source = False
    allows_empty_evidence = False

    def __init__(
        self,
        provider: StructuredTextProvider,
        *,
        max_retries: int,
        max_input_chars: int,
        max_items_per_chunk: int,
        retry_delay_seconds: float = 0.2,
    ) -> None:
        if max_retries < 0 or retry_delay_seconds < 0:
            raise ValueError("retry settings must be non-negative")
        self._provider = provider
        self._max_retries = max_retries
        self._max_input_chars = max_input_chars
        self._max_items_per_chunk = max_items_per_chunk
        self._retry_delay_seconds = retry_delay_seconds

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name

    @property
    def model(self) -> str:
        return self._provider.model

    async def _generate_chunk(
        self,
        chunk: tuple[TranscriptItem, ...],
        options: ChapterOutlineOptions,
    ) -> _ChapterOutput:
        request = StructuredCompletionRequest(
            system_prompt=_CHAPTER_PROMPT,
            user_prompt="Outline these Package effective-source items:",
            input_payload={
                "style": options.style,
                "items": [
                    {"item_id": item.item_id, "text": item.text}
                    for item in chunk
                ],
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
            raise ValueError("chapter outline does not accept a target artifact")
        validated_options = ChapterOutlineOptions.model_validate(options)
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
        chapters = tuple(
            chapter for result in results for chapter in result.chapters
        )
        evidence_builder = FactualEvidenceBuilder(package)
        evidence = []
        content_chapters = []
        previous_start_ms = -1
        for index, chapter in enumerate(chapters):
            factual = evidence_builder.build(
                evidence_key=f"chapter:{index}",
                evidence_item_ids=chapter.evidence_item_ids,
            )
            if factual.evidence.start_ms < previous_start_ms:
                raise ScriptOutputError(
                    "chapter outline is not ordered by Package time"
                )
            previous_start_ms = factual.evidence.start_ms
            evidence.append(factual.evidence)
            content_chapters.append(
                {
                    "title": chapter.title,
                    "summary": chapter.summary,
                    "evidence_item_ids": list(factual.evidence_item_ids),
                    "source_segment_ids": list(
                        factual.evidence.source_segment_ids
                    ),
                    "start_ms": factual.evidence.start_ms,
                    "end_ms": factual.evidence.end_ms,
                    "time_ranges": list(factual.time_ranges),
                    "evidence_excerpts": list(factual.evidence_excerpts),
                }
            )
        warnings = list(
            dict.fromkeys(
                warning
                for result in results
                for warning in result.warnings
            )
        )
        return ArtifactDraft(
            provider=self._provider.provider_name,
            model=self._provider.model,
            options=validated_options.model_dump(mode="json", exclude_none=True),
            content={"chapters": content_chapters, "warnings": warnings},
            evidence=tuple(evidence),
        )
