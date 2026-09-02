from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict

from sqlalchemy.orm import Session

from app.assistant.package_binding import (
    PackageBindingError,
    PackageBindingService,
)
from app.packages.canonical import (
    document_content_hash,
    package_content_hash,
)
from app.packages.models import (
    PACKAGE_SCHEMA,
    PACKAGE_SCHEMA_VERSION,
    DocumentReference,
    EvidenceEntry,
    EvidenceIndexContent,
    EvidenceIndexDocument,
    LiveTranslationDocument,
    MetricsMetadataDocument,
    MetricsSnapshot,
    PackageDocument,
    PackageManifest,
    ProviderMetadataDocument,
    ProviderSnapshot,
    SessionMetadataDocument,
    SessionSnapshot,
    SourceApprovedDocument,
    SourceRawDocument,
    TimelineIndexContent,
    TimelineIndexDocument,
    TimelineIndexEntry,
    TranscriptDocumentContent,
    TranscriptItem,
    TranscriptPackage,
    document_identity,
)
from app.packages.repository import PackageRepository
from app.packages.validator import PackageValidator
from app.persistence.models import SessionRecord, utc_now
from app.persistence.segments import SegmentRepository
from app.persistence.translations import TranslationSegmentRepository
from app.timeline.service import (
    normalize_timeline,
    normalize_translation_timeline,
)
from app.revisions.models import TranscriptRevision


class PackageBuildError(ValueError):
    pass


def _stable_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "matinier:" + ":".join(parts)))


class PackageBuilder:
    """The only Stage 2 boundary allowed to read Stage 1 Final tables."""

    def __init__(
        self,
        db_session: Session,
        *,
        validator: PackageValidator | None = None,
    ) -> None:
        self._db_session = db_session
        self._repository = PackageRepository(db_session)
        self._validator = validator or PackageValidator()

    def build_baseline(self, session_id: str) -> TranscriptPackage:
        session = self._db_session.get(SessionRecord, session_id)
        if session is None:
            raise LookupError(f"Session not found: {session_id}")

        source_records = SegmentRepository(self._db_session).list_final(session_id)
        translation_records = TranslationSegmentRepository(
            self._db_session
        ).list_final(session_id)
        if not source_records:
            raise PackageBuildError("Session has no Final captions")

        package_id = str(uuid.uuid4())
        version = self._repository.next_version(session_id)
        created_at = utc_now()
        record = self._repository.create_building(
            package_id=package_id,
            session_id=session_id,
            version=version,
            source_revision_id=None,
            created_at=created_at,
        )
        source_timeline = normalize_timeline(source_records)
        source_document_id = _stable_id(package_id, "source_raw", session.language)
        source_items = tuple(
            TranscriptItem(
                item_id=_stable_id(session_id, "source", item.segment_id),
                source_segment_ids=(item.segment_id,),
                start_ms=item.audio_start_ms,
                end_ms=item.audio_end_ms,
                text=item.display_text,
                raw_text=item.raw_text,
                confidence=item.confidence,
            )
            for item in source_timeline.segments
        )
        documents: list[PackageDocument] = []
        source_document = SourceRawDocument(
            document_id=source_document_id,
            document_kind="source_raw",
            language=session.language,
            content=TranscriptDocumentContent(items=source_items),
            content_hash="0" * 64,
        )
        documents.append(
            source_document.model_copy(
                update={"content_hash": document_content_hash(source_document)}
            )
        )

        translations_by_language = defaultdict(list)
        for item in translation_records:
            translations_by_language[item.target_language].append(item)
        for language in sorted(translations_by_language):
            records = translations_by_language[language]
            timeline = normalize_translation_timeline(records)
            records_by_segment = {item.segment_id: item for item in records}
            translation_items = tuple(
                TranscriptItem(
                    item_id=_stable_id(
                        session_id,
                        "live_translation",
                        language,
                        item.segment_id,
                    ),
                    source_segment_ids=tuple(
                        records_by_segment[item.segment_id].source_segment_ids
                    )
                    or (item.segment_id,),
                    start_ms=item.audio_start_ms,
                    end_ms=item.audio_end_ms,
                    text=item.display_text,
                )
                for item in timeline.segments
            )
            translation_document = LiveTranslationDocument(
                document_id=_stable_id(
                    package_id,
                    "live_translation",
                    language,
                ),
                document_kind="live_translation",
                language=language,
                content=TranscriptDocumentContent(items=translation_items),
                content_hash="0" * 64,
            )
            documents.append(
                translation_document.model_copy(
                    update={
                        "content_hash": document_content_hash(
                            translation_document
                        )
                    }
                )
            )

        timeline_document = TimelineIndexDocument(
            document_id=_stable_id(package_id, "timeline_index"),
            document_kind="timeline_index",
            content=TimelineIndexContent(
                duration_ms=source_timeline.duration_ms,
                items=tuple(
                    TimelineIndexEntry(
                        item_id=item.item_id,
                        start_ms=item.start_ms,
                        end_ms=item.end_ms,
                        source_document_id=source_document_id,
                    )
                    for item in source_items
                ),
            ),
            content_hash="0" * 64,
        )
        documents.append(
            timeline_document.model_copy(
                update={"content_hash": document_content_hash(timeline_document)}
            )
        )
        evidence_document = EvidenceIndexDocument(
            document_id=_stable_id(package_id, "evidence_index"),
            document_kind="evidence_index",
            content=EvidenceIndexContent(
                items=tuple(
                    EvidenceEntry(
                        item_id=item.item_id,
                        source_segment_ids=item.source_segment_ids,
                        start_ms=item.start_ms,
                        end_ms=item.end_ms,
                        raw_text=item.raw_text or item.text,
                        effective_text=item.text,
                        source_document_id=source_document_id,
                    )
                    for item in source_items
                )
            ),
            content_hash="0" * 64,
        )
        documents.append(
            evidence_document.model_copy(
                update={"content_hash": document_content_hash(evidence_document)}
            )
        )

        target_languages = tuple(sorted(translations_by_language))
        session_snapshot = SessionSnapshot(
            session_id=session.id,
            room_name=session.room_name,
            status=session.status,
            source_type=session.source_type,
            source_language=session.language,
            target_languages=target_languages,
            created_at=session.created_at,
            started_at=session.started_at,
            ended_at=session.ended_at,
            source_ended_at=session.source_ended_at,
            translation_ended_at=session.translation_ended_at,
            stop_reason=session.stop_reason,
            failure_code=session.failure_code or session.error_code,
        )
        provider_snapshot = ProviderSnapshot(
            asr_provider=session.asr_provider,
            asr_model=session.asr_model,
            translation_provider=session.translation_provider,
            translation_model=session.translation_model,
            translation_status=session.translation_status,
        )
        metrics_snapshot = MetricsSnapshot(
            final_result_count=session.final_result_count,
            first_partial_latency_ms=session.first_partial_latency_ms,
            average_final_latency_ms=session.average_final_latency_ms,
            provider_error_count=session.provider_error_count,
            sent_audio_chunk_count=session.sent_audio_chunk_count,
            sent_audio_bytes=session.sent_audio_bytes,
        )
        metadata = (
            SessionMetadataDocument(
                document_id=_stable_id(package_id, "session_metadata"),
                document_kind="session_metadata",
                content=session_snapshot,
                content_hash="0" * 64,
            ),
            ProviderMetadataDocument(
                document_id=_stable_id(package_id, "provider_metadata"),
                document_kind="provider_metadata",
                content=provider_snapshot,
                content_hash="0" * 64,
            ),
            MetricsMetadataDocument(
                document_id=_stable_id(package_id, "metrics_metadata"),
                document_kind="metrics_metadata",
                content=metrics_snapshot,
                content_hash="0" * 64,
            ),
        )
        documents.extend(
            item.model_copy(
                update={"content_hash": document_content_hash(item)}
            )
            for item in metadata
        )
        documents.sort(key=document_identity)
        content_hash = package_content_hash(
            schema_name=PACKAGE_SCHEMA,
            schema_version=PACKAGE_SCHEMA_VERSION,
            package_version=version,
            session_id=session.id,
            source_revision_id=None,
            documents=documents,
        )
        references = tuple(
            DocumentReference(
                document_id=item.document_id,
                document_kind=item.document_kind,
                language=item.language,
                content_hash=item.content_hash,
            )
            for item in documents
        )
        manifest = PackageManifest(
            package_id=package_id,
            package_version=version,
            session_id=session.id,
            source_language=session.language,
            target_languages=target_languages,
            effective_source_document_id=source_document_id,
            source_revision_id=None,
            created_at=created_at,
            content_hash=content_hash,
            documents=references,
            session_snapshot=session_snapshot,
            provider_snapshot=provider_snapshot,
            metrics_snapshot=metrics_snapshot,
        )
        package = TranscriptPackage(
            package_id=package_id,
            session_id=session.id,
            package_version=version,
            status="frozen",
            source_revision_id=None,
            manifest=manifest,
            documents=tuple(documents),
            content_hash=content_hash,
            created_at=created_at,
            frozen_at=created_at,
            superseded_at=None,
        )
        self._validator.validate_or_raise(package)
        for document in documents:
            self._repository.add_document(
                package_id,
                document,
                created_at=created_at,
            )
        self._repository.freeze(
            record,
            manifest=manifest,
            content_hash=content_hash,
            frozen_at=created_at,
        )
        return self._load_and_bind(package_id)

    def build_from_revision(
        self,
        revision: TranscriptRevision,
    ) -> TranscriptPackage:
        """Build a new frozen Package without rereading Stage 1 evidence tables."""

        if revision.status != "approved":
            raise PackageBuildError("Revision must be approved")
        base = self._repository.load(revision.base_package_id)
        if base.status not in {"frozen", "superseded"}:
            raise PackageBuildError("Base Package is not frozen")
        if base.session_id != revision.session_id:
            raise PackageBuildError("Revision and Package Session do not match")
        if base.manifest.source_language != revision.language:
            raise PackageBuildError("Revision language does not match Package")

        raw_base = next(
            (
                document
                for document in base.documents
                if isinstance(document, SourceRawDocument)
            ),
            None,
        )
        if raw_base is None:
            raise PackageBuildError("Base Package has no raw source document")
        raw_items_by_segment = {
            source_id: item
            for item in raw_base.content.items
            for source_id in item.source_segment_ids
        }

        package_id = str(uuid.uuid4())
        version = self._repository.next_version(revision.session_id)
        created_at = utc_now()
        record = self._repository.create_building(
            package_id=package_id,
            session_id=revision.session_id,
            version=version,
            source_revision_id=revision.revision_id,
            created_at=created_at,
        )
        raw_document = SourceRawDocument(
            document_id=_stable_id(package_id, "source_raw", revision.language),
            document_kind="source_raw",
            language=revision.language,
            content=raw_base.content,
            content_hash="0" * 64,
        )
        raw_document = raw_document.model_copy(
            update={"content_hash": document_content_hash(raw_document)}
        )
        approved_document_id = _stable_id(
            package_id,
            "source_approved",
            revision.language,
        )
        approved_items = tuple(
            TranscriptItem(
                item_id=item.item_id,
                source_segment_ids=item.source_segment_ids,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                text=item.text,
                raw_text=" ".join(
                    (
                        raw_items_by_segment[source_id].raw_text
                        or raw_items_by_segment[source_id].text
                    )
                    for source_id in item.source_segment_ids
                    if source_id in raw_items_by_segment
                ),
                confidence=None,
                speaker=None,
                locked=True,
            )
            for item in revision.content.items
        )
        approved_document = SourceApprovedDocument(
            document_id=approved_document_id,
            document_kind="source_approved",
            language=revision.language,
            content=TranscriptDocumentContent(items=approved_items),
            content_hash="0" * 64,
        )
        approved_document = approved_document.model_copy(
            update={
                "content_hash": document_content_hash(approved_document)
            }
        )

        documents: list[PackageDocument] = [raw_document, approved_document]
        for base_document in base.documents:
            if not isinstance(base_document, LiveTranslationDocument):
                continue
            document = LiveTranslationDocument(
                document_id=_stable_id(
                    package_id,
                    "live_translation",
                    base_document.language,
                ),
                document_kind="live_translation",
                language=base_document.language,
                content=base_document.content,
                content_hash="0" * 64,
            )
            documents.append(
                document.model_copy(
                    update={"content_hash": document_content_hash(document)}
                )
            )

        duration_ms = max((item.end_ms for item in approved_items), default=0)
        timeline_document = TimelineIndexDocument(
            document_id=_stable_id(package_id, "timeline_index"),
            document_kind="timeline_index",
            content=TimelineIndexContent(
                duration_ms=duration_ms,
                items=tuple(
                    TimelineIndexEntry(
                        item_id=item.item_id,
                        start_ms=item.start_ms,
                        end_ms=item.end_ms,
                        source_document_id=approved_document_id,
                    )
                    for item in approved_items
                ),
            ),
            content_hash="0" * 64,
        )
        evidence_document = EvidenceIndexDocument(
            document_id=_stable_id(package_id, "evidence_index"),
            document_kind="evidence_index",
            content=EvidenceIndexContent(
                items=tuple(
                    EvidenceEntry(
                        item_id=item.item_id,
                        source_segment_ids=item.source_segment_ids,
                        start_ms=item.start_ms,
                        end_ms=item.end_ms,
                        raw_text=item.raw_text or item.text,
                        effective_text=item.text,
                        source_document_id=approved_document_id,
                    )
                    for item in approved_items
                )
            ),
            content_hash="0" * 64,
        )
        documents.extend(
            item.model_copy(
                update={"content_hash": document_content_hash(item)}
            )
            for item in (timeline_document, evidence_document)
        )

        session_snapshot = base.manifest.session_snapshot
        provider_snapshot = base.manifest.provider_snapshot
        metrics_snapshot = base.manifest.metrics_snapshot
        metadata = (
            SessionMetadataDocument(
                document_id=_stable_id(package_id, "session_metadata"),
                document_kind="session_metadata",
                content=session_snapshot,
                content_hash="0" * 64,
            ),
            ProviderMetadataDocument(
                document_id=_stable_id(package_id, "provider_metadata"),
                document_kind="provider_metadata",
                content=provider_snapshot,
                content_hash="0" * 64,
            ),
            MetricsMetadataDocument(
                document_id=_stable_id(package_id, "metrics_metadata"),
                document_kind="metrics_metadata",
                content=metrics_snapshot,
                content_hash="0" * 64,
            ),
        )
        documents.extend(
            item.model_copy(
                update={"content_hash": document_content_hash(item)}
            )
            for item in metadata
        )
        documents.sort(key=document_identity)
        content_hash = package_content_hash(
            schema_name=PACKAGE_SCHEMA,
            schema_version=PACKAGE_SCHEMA_VERSION,
            package_version=version,
            session_id=revision.session_id,
            source_revision_id=revision.revision_id,
            documents=documents,
        )
        manifest = PackageManifest(
            package_id=package_id,
            package_version=version,
            session_id=revision.session_id,
            source_language=revision.language,
            target_languages=base.manifest.target_languages,
            effective_source_document_id=approved_document_id,
            source_revision_id=revision.revision_id,
            created_at=created_at,
            content_hash=content_hash,
            documents=tuple(
                DocumentReference(
                    document_id=item.document_id,
                    document_kind=item.document_kind,
                    language=item.language,
                    content_hash=item.content_hash,
                )
                for item in documents
            ),
            session_snapshot=session_snapshot,
            provider_snapshot=provider_snapshot,
            metrics_snapshot=metrics_snapshot,
        )
        package = TranscriptPackage(
            package_id=package_id,
            session_id=revision.session_id,
            package_version=version,
            status="frozen",
            source_revision_id=revision.revision_id,
            manifest=manifest,
            documents=tuple(documents),
            content_hash=content_hash,
            created_at=created_at,
            frozen_at=created_at,
            superseded_at=None,
        )
        self._validator.validate_or_raise(package)
        for document in documents:
            self._repository.add_document(
                package_id,
                document,
                created_at=created_at,
            )
        self._repository.freeze(
            record,
            manifest=manifest,
            content_hash=content_hash,
            frozen_at=created_at,
        )
        return self._load_and_bind(package_id)

    def _load_and_bind(self, package_id: str) -> TranscriptPackage:
        package = self._repository.load(package_id)
        try:
            PackageBindingService(self._db_session).bind_unbound_executions(
                package
            )
        except PackageBindingError as error:
            raise PackageBuildError(str(error)) from error
        return package
