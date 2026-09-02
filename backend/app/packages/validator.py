from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from app.packages.canonical import (
    document_content_hash,
    package_content_hash,
)
from app.packages.models import (
    PACKAGE_SCHEMA,
    PACKAGE_SCHEMA_VERSION,
    EvidenceIndexDocument,
    LiveTranslationDocument,
    PackageValidationResult,
    SourceApprovedDocument,
    SourceRawDocument,
    TimelineIndexDocument,
    TranscriptPackage,
    document_identity,
)


class PackageValidationError(ValueError):
    pass


_FORBIDDEN_KEYS = {
    "api_key",
    "authorization",
    "fetch_url",
    "password",
    "provider_payload",
    "raw_payload",
    "secret",
    "token",
}


def _contains_sensitive_value(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in _FORBIDDEN_KEYS or normalized.endswith("_secret"):
                return True
            if _contains_sensitive_value(item):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_sensitive_value(item) for item in value)
    if isinstance(value, str):
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https", "ws", "wss"} and bool(
            parsed.query or parsed.username or parsed.password
        )
    return False


class PackageValidator:
    def validate(self, package: TranscriptPackage) -> PackageValidationResult:
        errors: list[str] = []
        documents = sorted(package.documents, key=document_identity)
        identities = [document_identity(item) for item in documents]
        if len(set(identities)) != len(identities):
            errors.append("package contains duplicate document identities")
        if package.schema_name != PACKAGE_SCHEMA:
            errors.append("package schema name is unsupported")
        if package.schema_version != PACKAGE_SCHEMA_VERSION:
            errors.append("package schema version is unsupported")
        if package.status not in {"frozen", "superseded"}:
            errors.append("package is not frozen")

        for document in documents:
            if document_content_hash(document) != document.content_hash:
                errors.append(
                    f"document hash mismatch: {document.document_kind}"
                )

        expected_package_hash = package_content_hash(
            schema_name=package.schema_name,
            schema_version=package.schema_version,
            package_version=package.package_version,
            session_id=package.session_id,
            source_revision_id=package.source_revision_id,
            documents=documents,
        )
        if expected_package_hash != package.content_hash:
            errors.append("package content hash mismatch")
        manifest = package.manifest
        if manifest.content_hash != package.content_hash:
            errors.append("manifest content hash mismatch")
        if manifest.package_id != package.package_id:
            errors.append("manifest package ID mismatch")
        if manifest.package_version != package.package_version:
            errors.append("manifest package version mismatch")
        if manifest.session_id != package.session_id:
            errors.append("manifest session ID mismatch")
        if manifest.source_revision_id != package.source_revision_id:
            errors.append("manifest source revision ID mismatch")

        expected_references = tuple(
            (
                item.document_id,
                item.document_kind,
                item.language,
                item.content_hash,
            )
            for item in documents
        )
        actual_references = tuple(
            (
                item.document_id,
                item.document_kind,
                item.language,
                item.content_hash,
            )
            for item in manifest.documents
        )
        if expected_references != actual_references:
            errors.append("manifest document index mismatch")

        source_documents = [
            item for item in documents if isinstance(item, SourceRawDocument)
        ]
        approved_documents = [
            item for item in documents if isinstance(item, SourceApprovedDocument)
        ]
        timeline_documents = [
            item for item in documents if isinstance(item, TimelineIndexDocument)
        ]
        evidence_documents = [
            item for item in documents if isinstance(item, EvidenceIndexDocument)
        ]
        required_counts = {
            "source_raw": len(source_documents),
            "timeline_index": len(timeline_documents),
            "evidence_index": len(evidence_documents),
            "session_metadata": sum(
                item.document_kind == "session_metadata" for item in documents
            ),
            "provider_metadata": sum(
                item.document_kind == "provider_metadata" for item in documents
            ),
            "metrics_metadata": sum(
                item.document_kind == "metrics_metadata" for item in documents
            ),
        }
        for kind, count in required_counts.items():
            if count != 1:
                errors.append(f"package requires exactly one {kind} document")
        if len(approved_documents) > 1:
            errors.append("package allows at most one source_approved document")

        source_candidates = [*source_documents, *approved_documents]
        effective_documents = [
            item
            for item in source_candidates
            if item.document_id == manifest.effective_source_document_id
        ]
        if len(effective_documents) != 1:
            errors.append("effective source document ID mismatch")
        if package.source_revision_id is None:
            if approved_documents:
                errors.append("baseline package must not contain source_approved")
            if effective_documents and not isinstance(
                effective_documents[0], SourceRawDocument
            ):
                errors.append("baseline package effective source must be source_raw")
        else:
            if len(approved_documents) != 1:
                errors.append("revision package requires source_approved")
            if effective_documents and not isinstance(
                effective_documents[0], SourceApprovedDocument
            ):
                errors.append(
                    "revision package effective source must be source_approved"
                )

        if effective_documents and timeline_documents and evidence_documents:
            source = effective_documents[0]
            timeline = timeline_documents[0]
            evidence = evidence_documents[0]
            source_items = source.content.items
            source_ids = [item.item_id for item in source_items]
            if len(set(source_ids)) != len(source_ids):
                errors.append("source document contains duplicate item IDs")
            for item in source_items:
                if item.end_ms < item.start_ms:
                    errors.append(f"source item has invalid timing: {item.item_id}")
            expected_timeline = tuple(
                (item.item_id, item.start_ms, item.end_ms, source.document_id)
                for item in source_items
            )
            actual_timeline = tuple(
                (
                    item.item_id,
                    item.start_ms,
                    item.end_ms,
                    item.source_document_id,
                )
                for item in timeline.content.items
            )
            if expected_timeline != actual_timeline:
                errors.append("timeline index does not match source document")
            expected_evidence = tuple(
                (
                    item.item_id,
                    item.source_segment_ids,
                    item.start_ms,
                    item.end_ms,
                    item.raw_text or item.text,
                    item.text,
                    source.document_id,
                )
                for item in source_items
            )
            actual_evidence = tuple(
                (
                    item.item_id,
                    item.source_segment_ids,
                    item.start_ms,
                    item.end_ms,
                    item.raw_text,
                    item.effective_text,
                    item.source_document_id,
                )
                for item in evidence.content.items
            )
            if expected_evidence != actual_evidence:
                errors.append("evidence index does not match source document")
            duration = max((item.end_ms for item in source_items), default=0)
            if timeline.content.duration_ms != duration:
                errors.append("timeline duration does not match source document")

        if source_documents and approved_documents:
            raw_source_ids = {
                source_id
                for item in source_documents[0].content.items
                for source_id in item.source_segment_ids
            }
            approved_source_ids = {
                source_id
                for item in approved_documents[0].content.items
                for source_id in item.source_segment_ids
            }
            if approved_source_ids != raw_source_ids:
                errors.append(
                    "source_approved does not preserve raw source segment IDs"
                )

        translation_languages = tuple(
            sorted(
                item.language
                for item in documents
                if isinstance(item, LiveTranslationDocument)
            )
        )
        if translation_languages != tuple(sorted(manifest.target_languages)):
            errors.append("manifest target languages do not match documents")
        if _contains_sensitive_value(package.model_dump(mode="json")):
            errors.append("package contains a sensitive key or URL")

        return PackageValidationResult(
            valid=not errors,
            package_id=package.package_id,
            content_hash=package.content_hash,
            errors=tuple(errors),
        )

    def validate_or_raise(self, package: TranscriptPackage) -> None:
        result = self.validate(package)
        if not result.valid:
            raise PackageValidationError("; ".join(result.errors))
