from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field


PACKAGE_SCHEMA = "matinier.transcript-package"
PACKAGE_SCHEMA_VERSION = "1.0"
PackageStatus = Literal["building", "frozen", "superseded", "invalid"]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Language = Annotated[str, Field(min_length=2, max_length=32)]


class FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class TranscriptItem(FrozenModel):
    item_id: str = Field(min_length=1)
    source_segment_ids: tuple[str, ...] = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str
    raw_text: str | None = None
    confidence: float | None = None
    speaker: str | None = None
    locked: bool = True


class TranscriptDocumentContent(FrozenModel):
    items: tuple[TranscriptItem, ...]


class TimelineIndexEntry(FrozenModel):
    item_id: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    source_document_id: str = Field(min_length=1)


class TimelineIndexContent(FrozenModel):
    duration_ms: int = Field(ge=0)
    items: tuple[TimelineIndexEntry, ...]


class EvidenceEntry(FrozenModel):
    item_id: str = Field(min_length=1)
    source_segment_ids: tuple[str, ...] = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    raw_text: str
    effective_text: str
    source_document_id: str = Field(min_length=1)


class EvidenceIndexContent(FrozenModel):
    items: tuple[EvidenceEntry, ...]


class SessionSnapshot(FrozenModel):
    session_id: str = Field(min_length=1)
    room_name: str = Field(min_length=1)
    status: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    source_language: Language
    target_languages: tuple[Language, ...]
    created_at: dt.datetime
    started_at: dt.datetime | None
    ended_at: dt.datetime | None
    source_ended_at: dt.datetime | None
    translation_ended_at: dt.datetime | None
    stop_reason: str | None
    failure_code: str | None


class ProviderSnapshot(FrozenModel):
    asr_provider: str | None
    asr_model: str | None
    translation_provider: str | None
    translation_model: str | None
    translation_status: str


class MetricsSnapshot(FrozenModel):
    final_result_count: int | None
    first_partial_latency_ms: float | None
    average_final_latency_ms: float | None
    provider_error_count: int | None
    sent_audio_chunk_count: int | None
    sent_audio_bytes: int | None


class DocumentBase(FrozenModel):
    document_id: str = Field(min_length=1)
    language: Language | None
    content_hash: Digest


class SourceRawDocument(DocumentBase):
    document_kind: Literal["source_raw"]
    language: Language
    content: TranscriptDocumentContent


class SourceApprovedDocument(DocumentBase):
    document_kind: Literal["source_approved"]
    language: Language
    content: TranscriptDocumentContent


class LiveTranslationDocument(DocumentBase):
    document_kind: Literal["live_translation"]
    language: Language
    content: TranscriptDocumentContent


class TimelineIndexDocument(DocumentBase):
    document_kind: Literal["timeline_index"]
    language: None = None
    content: TimelineIndexContent


class EvidenceIndexDocument(DocumentBase):
    document_kind: Literal["evidence_index"]
    language: None = None
    content: EvidenceIndexContent


class SessionMetadataDocument(DocumentBase):
    document_kind: Literal["session_metadata"]
    language: None = None
    content: SessionSnapshot


class ProviderMetadataDocument(DocumentBase):
    document_kind: Literal["provider_metadata"]
    language: None = None
    content: ProviderSnapshot


class MetricsMetadataDocument(DocumentBase):
    document_kind: Literal["metrics_metadata"]
    language: None = None
    content: MetricsSnapshot


PackageDocument: TypeAlias = Annotated[
    SourceRawDocument
    | SourceApprovedDocument
    | LiveTranslationDocument
    | TimelineIndexDocument
    | EvidenceIndexDocument
    | SessionMetadataDocument
    | ProviderMetadataDocument
    | MetricsMetadataDocument,
    Field(discriminator="document_kind"),
]


class DocumentReference(FrozenModel):
    document_id: str = Field(min_length=1)
    document_kind: str = Field(min_length=1)
    language: Language | None
    content_hash: Digest


class PackageManifest(FrozenModel):
    schema_name: Literal["matinier.transcript-package"] = Field(
        default=PACKAGE_SCHEMA,
        alias="schema",
        serialization_alias="schema",
    )
    schema_version: Literal["1.0"] = PACKAGE_SCHEMA_VERSION
    package_id: str = Field(min_length=1)
    package_version: int = Field(ge=1)
    session_id: str = Field(min_length=1)
    status: Literal["frozen"] = "frozen"
    source_language: Language
    target_languages: tuple[Language, ...]
    effective_source_document_id: str = Field(min_length=1)
    source_revision_id: str | None
    created_at: dt.datetime
    content_hash: Digest
    documents: tuple[DocumentReference, ...]
    session_snapshot: SessionSnapshot
    provider_snapshot: ProviderSnapshot
    metrics_snapshot: MetricsSnapshot


class TranscriptPackage(FrozenModel):
    package_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    package_version: int = Field(ge=1)
    schema_name: Literal["matinier.transcript-package"] = PACKAGE_SCHEMA
    schema_version: Literal["1.0"] = PACKAGE_SCHEMA_VERSION
    status: PackageStatus
    source_revision_id: str | None
    manifest: PackageManifest
    documents: tuple[PackageDocument, ...]
    content_hash: Digest
    created_at: dt.datetime
    frozen_at: dt.datetime | None
    superseded_at: dt.datetime | None


class PackageValidationResult(FrozenModel):
    valid: bool
    package_id: str
    content_hash: str | None
    errors: tuple[str, ...]


def document_identity(document: PackageDocument) -> tuple[str, str]:
    return document.document_kind, document.language or ""
