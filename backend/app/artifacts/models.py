from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ArtifactKind = Literal[
    "clean_script",
    "refined_translation",
    "summary",
    "chapter_outline",
    "timeline_fact_review",
]
ArtifactStatus = Literal[
    "generated",
    "reviewed",
    "approved",
    "superseded",
    "failed",
]
ArtifactCreator = Literal["model", "human"]


def artifact_identity_key(
    artifact_kind: ArtifactKind,
    *,
    target_language: str | None = None,
    target_artifact_id: str | None = None,
) -> str:
    """Return the stable identity whose versions share one approval slot."""

    if artifact_kind == "refined_translation":
        if not target_language:
            raise ValueError("refined translation identity requires a language")
        return f"refined_translation:{target_language}"
    if artifact_kind == "timeline_fact_review":
        if not target_artifact_id:
            raise ValueError("fact review identity requires a target artifact")
        return f"timeline_fact_review:{target_artifact_id}"
    return artifact_kind


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactEvidence(FrozenModel):
    evidence_key: str = Field(min_length=1)
    source_item_ids: tuple[str, ...] = Field(min_length=1)
    source_segment_ids: tuple[str, ...] = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_timing(self) -> ArtifactEvidence:
        if self.end_ms < self.start_ms:
            raise ValueError("evidence end must not precede start")
        return self


class DerivedArtifact(FrozenModel):
    artifact_id: str = Field(min_length=1)
    package_id: str = Field(min_length=1)
    package_version: int = Field(ge=1)
    package_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_kind: ArtifactKind
    identity_key: str = Field(min_length=1, max_length=256)
    artifact_version: int = Field(ge=1)
    target_language: str | None
    status: ArtifactStatus
    provider: str | None
    model: str | None
    workflow_version: str = Field(min_length=1)
    options: dict[str, Any]
    content: dict[str, Any]
    evidence: tuple[ArtifactEvidence, ...]
    parent_artifact_id: str | None
    created_by: ArtifactCreator
    created_at: dt.datetime
    approved_at: dt.datetime | None
