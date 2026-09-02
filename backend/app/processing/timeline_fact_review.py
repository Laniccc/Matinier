from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
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


FactReviewStatus = Literal[
    "supported",
    "partially_supported",
    "contradicted",
    "unsupported",
    "ambiguous",
]
_ALLOWED_TARGET_KINDS = {
    "summary",
    "clean_script",
    "refined_translation",
    "chapter_outline",
}
_FACT_REVIEW_PROMPT = """Review each target Artifact claim only for fidelity to the
supplied frozen transcript Package. This is not external fact checking. Do not use or
request web search, outside knowledge, or unstated facts.

Return exactly one JSON object and no Markdown:
{
  "reviews": [
    {
      "claim_id": "one supplied claim_id",
      "status": "supported | partially_supported | contradicted | unsupported | ambiguous",
      "evidence_item_ids": ["zero or more supplied Package item_id values"],
      "explanation": "short Package-internal assessment or null"
    }
  ],
  "warnings": []
}
Return every claim_id exactly once. A supported claim must cite evidence. A contradicted
claim must cite conflicting evidence or include an explicit explanation. Unsupported
and ambiguous claims may have no evidence. Do not output timestamps, excerpts, or
Segment IDs; the server derives them from Package evidence.
"""


@dataclass(frozen=True, slots=True)
class _TargetClaim:
    claim_id: str
    target_path: str
    text: str
    target_evidence_item_ids: tuple[str, ...]


class TimelineFactReviewOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _FactReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str = Field(min_length=1)
    status: FactReviewStatus
    evidence_item_ids: tuple[str, ...]
    explanation: str | None = Field(default=None, max_length=2_000)

    @field_validator("evidence_item_ids")
    @classmethod
    def evidence_ids_must_be_unique(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("fact review repeats an evidence item ID")
        return value

    @field_validator("explanation")
    @classmethod
    def explanation_must_be_visible(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def status_must_have_required_support(self) -> _FactReview:
        if self.status == "supported" and not self.evidence_item_ids:
            raise ValueError("supported fact review must cite Package evidence")
        if (
            self.status == "contradicted"
            and not self.evidence_item_ids
            and self.explanation is None
        ):
            raise ValueError(
                "contradicted fact review needs evidence or an explanation"
            )
        return self


class _FactReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reviews: tuple[_FactReview, ...] = Field(min_length=1)
    warnings: tuple[str, ...]


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"target Artifact {field} must be visible text")
    return value.strip()


def _item_ids(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError("target Artifact evidence item IDs are invalid")
    return tuple(value)


def _target_claims(target: DerivedArtifact) -> tuple[_TargetClaim, ...]:
    kind = target.artifact_kind
    if kind not in _ALLOWED_TARGET_KINDS:
        raise ValueError(f"Artifact kind cannot be fact reviewed: {kind}")
    if kind == "summary":
        raw_claims = target.content.get("key_points")
        path_prefix = "key_points"
        text_of = lambda item: _text(item.get("text"), field="key point")
        ids_of = lambda item: _item_ids(item.get("evidence_item_ids"))
    elif kind == "chapter_outline":
        raw_claims = target.content.get("chapters")
        path_prefix = "chapters"
        text_of = lambda item: (
            f"{_text(item.get('title'), field='chapter title')}: "
            f"{_text(item.get('summary'), field='chapter summary')}"
        )
        ids_of = lambda item: _item_ids(item.get("evidence_item_ids"))
    else:
        raw_claims = target.content.get("sections")
        path_prefix = "sections"
        text_field = "clean_text" if kind == "clean_script" else "translated_text"
        text_of = lambda item: _text(item.get(text_field), field=text_field)
        ids_of = lambda item: _item_ids(item.get("source_item_ids"))
    if not isinstance(raw_claims, list) or not raw_claims:
        raise ValueError("target Artifact has no reviewable claims")
    claims = []
    for index, raw_claim in enumerate(raw_claims):
        if not isinstance(raw_claim, dict):
            raise ValueError("target Artifact claim must be an object")
        claims.append(
            _TargetClaim(
                claim_id=f"claim:{index}",
                target_path=f"{path_prefix}[{index}]",
                text=text_of(raw_claim),
                target_evidence_item_ids=ids_of(raw_claim),
            )
        )
    return tuple(claims)


def _parse_chunk(
    content: str,
    claims: Sequence[_TargetClaim],
    package_items: Sequence[TranscriptItem],
) -> _FactReviewOutput:
    if not content.strip():
        raise ScriptOutputError("structured provider returned empty content")
    try:
        result = _FactReviewOutput.model_validate(json.loads(content))
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
        raise ScriptOutputError(
            "structured provider output is not valid timeline fact review JSON"
        ) from error
    expected_claim_ids = {claim.claim_id for claim in claims}
    returned_claim_ids = [review.claim_id for review in result.reviews]
    counts = Counter(returned_claim_ids)
    if set(returned_claim_ids) != expected_claim_ids or any(
        count != 1 for count in counts.values()
    ):
        raise ScriptOutputError("fact review must return every claim exactly once")
    allowed_item_ids = {item.item_id for item in package_items}
    foreign_item_ids = {
        item_id
        for review in result.reviews
        for item_id in review.evidence_item_ids
        if item_id not in allowed_item_ids
    }
    if foreign_item_ids:
        raise ScriptOutputError(
            f"fact review references foreign Package items: {sorted(foreign_item_ids)}"
        )
    return result


class TimelineFactReviewWorkflow:
    artifact_kind = "timeline_fact_review"
    workflow_version = "1.0"
    requires_complete_source = False
    allows_empty_evidence = True

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
        chunk: tuple[_TargetClaim, ...],
        package_items: tuple[TranscriptItem, ...],
        target: DerivedArtifact,
    ) -> _FactReviewOutput:
        request = StructuredCompletionRequest(
            system_prompt=_FACT_REVIEW_PROMPT,
            user_prompt="Review these target Artifact claims against the Package:",
            input_payload={
                "review_scope": "package_internal_only",
                "target_artifact": {
                    "artifact_id": target.artifact_id,
                    "artifact_kind": target.artifact_kind,
                    "artifact_version": target.artifact_version,
                },
                "claims": [
                    {
                        "claim_id": claim.claim_id,
                        "text": claim.text,
                        "target_evidence_item_ids": list(
                            claim.target_evidence_item_ids
                        ),
                    }
                    for claim in chunk
                ],
                "package_items": [
                    {"item_id": item.item_id, "text": item.text}
                    for item in package_items
                ],
            },
        )
        return await complete_and_parse(
            self._provider,
            request,
            parse=lambda content: _parse_chunk(
                content,
                chunk,
                package_items,
            ),
            max_retries=self._max_retries,
            retry_delay_seconds=self._retry_delay_seconds,
        )

    async def run(
        self,
        package: TranscriptPackage,
        options: Mapping[str, Any],
        target_artifact: DerivedArtifact | None = None,
    ) -> ArtifactDraft:
        TimelineFactReviewOptions.model_validate(options)
        if target_artifact is None:
            raise ValueError("timeline fact review requires a target Artifact")
        if (
            target_artifact.package_id != package.package_id
            or target_artifact.package_version != package.package_version
            or target_artifact.package_content_hash != package.content_hash
        ):
            raise ValueError("target Artifact does not belong to the input Package")
        claims = _target_claims(target_artifact)
        package_items = PackageReader.effective_source(package).content.items
        if not package_items:
            raise ValueError("Package effective source contains no items")
        chunks = chunk_items(
            claims,
            text_of=lambda claim: claim.text,
            max_items=self._max_items_per_chunk,
            max_chars=self._max_input_chars,
        )
        results = [
            await self._generate_chunk(chunk, package_items, target_artifact)
            for chunk in chunks
        ]
        reviews_by_id = {
            review.claim_id: review
            for result in results
            for review in result.reviews
        }
        evidence_builder = FactualEvidenceBuilder(package)
        evidence = []
        content_reviews = []
        for index, claim in enumerate(claims):
            review = reviews_by_id[claim.claim_id]
            if review.evidence_item_ids:
                factual = evidence_builder.build(
                    evidence_key=f"claim:{index}",
                    evidence_item_ids=review.evidence_item_ids,
                )
                evidence.append(factual.evidence)
                evidence_item_ids = list(factual.evidence_item_ids)
                source_segment_ids = list(factual.evidence.source_segment_ids)
                start_ms: int | None = factual.evidence.start_ms
                end_ms: int | None = factual.evidence.end_ms
                time_ranges = list(factual.time_ranges)
                evidence_excerpts = list(factual.evidence_excerpts)
            else:
                evidence_item_ids = []
                source_segment_ids = []
                start_ms = None
                end_ms = None
                time_ranges = []
                evidence_excerpts = []
            content_reviews.append(
                {
                    "claim_id": claim.claim_id,
                    "target_path": claim.target_path,
                    "claim_text": claim.text,
                    "status": review.status,
                    "explanation": review.explanation,
                    "evidence_item_ids": evidence_item_ids,
                    "source_segment_ids": source_segment_ids,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "time_ranges": time_ranges,
                    "evidence_excerpts": evidence_excerpts,
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
            options={
                "target_artifact_id": target_artifact.artifact_id,
                "target_artifact_version": target_artifact.artifact_version,
            },
            content={
                "target_artifact": {
                    "artifact_id": target_artifact.artifact_id,
                    "artifact_kind": target_artifact.artifact_kind,
                    "artifact_version": target_artifact.artifact_version,
                    "package_id": target_artifact.package_id,
                    "package_content_hash": target_artifact.package_content_hash,
                },
                "reviews": content_reviews,
                "warnings": warnings,
            },
            evidence=tuple(evidence),
        )
