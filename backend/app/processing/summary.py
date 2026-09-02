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


_SUMMARY_PROMPT = """Create a factual summary from the effective source items of a
frozen transcript Package. The supplied items are the only factual authority. Preserve
names, numbers, dates, products, technical terms, uncertainty, and meaning.

Return exactly one JSON object and no Markdown:
{
  "brief": "concise summary for this chunk",
  "key_points": [
    {
      "text": "one factual key point",
      "evidence_item_ids": ["one or more supplied item_id values"]
    }
  ],
  "warnings": []
}
Every key point must cite non-empty evidence from the supplied items. Do not output
timestamps, excerpts, or Segment IDs; the server derives them from Package evidence.
"""


class SummaryOptions(BaseModel):
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


class _SummaryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    evidence_item_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def text_must_be_visible(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("summary key point text must not be blank")
        return normalized

    @field_validator("evidence_item_ids")
    @classmethod
    def evidence_ids_must_be_unique(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("summary key point repeats an evidence item ID")
        return value


class _SummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    brief: str = Field(min_length=1)
    key_points: tuple[_SummaryPoint, ...] = Field(min_length=1)
    warnings: tuple[str, ...]

    @field_validator("brief")
    @classmethod
    def brief_must_be_visible(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("summary brief must not be blank")
        return normalized


def _parse_chunk(
    content: str,
    items: Sequence[TranscriptItem],
) -> _SummaryOutput:
    if not content.strip():
        raise ScriptOutputError("structured provider returned empty content")
    try:
        result = _SummaryOutput.model_validate(json.loads(content))
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
        raise ScriptOutputError(
            "structured provider output is not valid summary JSON"
        ) from error
    allowed_ids = {item.item_id for item in items}
    referenced_ids = {
        item_id
        for point in result.key_points
        for item_id in point.evidence_item_ids
    }
    foreign_ids = referenced_ids - allowed_ids
    if foreign_ids:
        raise ScriptOutputError(
            f"summary references foreign Package items: {sorted(foreign_ids)}"
        )
    return result


class SummaryWorkflow:
    artifact_kind = "summary"
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
        options: SummaryOptions,
    ) -> _SummaryOutput:
        request = StructuredCompletionRequest(
            system_prompt=_SUMMARY_PROMPT,
            user_prompt="Summarize these Package effective-source items:",
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
            raise ValueError("summary does not accept a target artifact")
        validated_options = SummaryOptions.model_validate(options)
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
        points = tuple(
            point for result in results for point in result.key_points
        )
        evidence_builder = FactualEvidenceBuilder(package)
        evidence = []
        content_points = []
        for index, point in enumerate(points):
            factual = evidence_builder.build(
                evidence_key=f"key_point:{index}",
                evidence_item_ids=point.evidence_item_ids,
            )
            evidence.append(factual.evidence)
            content_points.append(
                {
                    "text": point.text,
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
            content={
                "brief": "\n\n".join(result.brief for result in results),
                "key_points": content_points,
                "warnings": warnings,
            },
            evidence=tuple(evidence),
        )
