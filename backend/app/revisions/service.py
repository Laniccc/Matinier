from __future__ import annotations

from app.packages.canonical import canonical_sha256
from app.packages.models import SourceApprovedDocument, SourceRawDocument, TranscriptPackage
from app.packages.repository import PackageRepository
from app.revisions.models import RevisionItem, TranscriptRevision, TranscriptRevisionContent
from app.revisions.repository import RevisionRepository


class RevisionError(ValueError):
    pass


def revision_content_hash(
    language: str,
    content: TranscriptRevisionContent,
) -> str:
    return canonical_sha256(
        {"language": language, "content": content.model_dump(mode="json")}
    )


def _effective_source(package: TranscriptPackage) -> SourceRawDocument | SourceApprovedDocument:
    for document in package.documents:
        if (
            document.document_id == package.manifest.effective_source_document_id
            and isinstance(document, (SourceRawDocument, SourceApprovedDocument))
        ):
            return document
    raise RevisionError("Package effective source document is missing")


def _raw_source(package: TranscriptPackage) -> SourceRawDocument:
    for document in package.documents:
        if isinstance(document, SourceRawDocument):
            return document
    raise RevisionError("Package raw source document is missing")


class RevisionService:
    def __init__(self, db_session) -> None:
        self._packages = PackageRepository(db_session)
        self._revisions = RevisionRepository(db_session)

    def create_from_package(
        self,
        package_id: str,
        *,
        change_summary: str | None = None,
    ) -> TranscriptRevision:
        package = self._packages.load(package_id)
        if package.status not in {"frozen", "superseded"}:
            raise RevisionError("Revision requires a frozen Package")
        source = _effective_source(package)
        if not source.content.items:
            raise RevisionError("Package effective source has no items")
        content = TranscriptRevisionContent(
            items=tuple(
                RevisionItem(
                    item_id=item.item_id,
                    source_segment_ids=item.source_segment_ids,
                    start_ms=item.start_ms,
                    end_ms=item.end_ms,
                    text=item.text,
                )
                for item in source.content.items
            )
        )
        self._validate_against_package(content, package)
        return self._revisions.create(
            session_id=package.session_id,
            parent_revision_id=None,
            base_package_id=package.package_id,
            language=source.language,
            content=content,
            content_hash=revision_content_hash(source.language, content),
            change_summary=change_summary or "Created from Package effective source",
        )

    def create_version(
        self,
        parent_revision_id: str,
        *,
        content: TranscriptRevisionContent,
        change_summary: str | None = None,
    ) -> TranscriptRevision:
        parent = self._revisions.get(parent_revision_id)
        if parent is None:
            raise LookupError(f"Revision not found: {parent_revision_id}")
        package = self._packages.load(parent.base_package_id)
        self._validate_against_package(content, package)
        return self._revisions.create(
            session_id=parent.session_id,
            parent_revision_id=parent.revision_id,
            base_package_id=parent.base_package_id,
            language=parent.language,
            content=content,
            content_hash=revision_content_hash(parent.language, content),
            change_summary=change_summary,
        )

    def approve(self, revision_id: str) -> TranscriptRevision:
        return self._revisions.approve(revision_id)

    @staticmethod
    def _validate_against_package(
        content: TranscriptRevisionContent,
        package: TranscriptPackage,
    ) -> None:
        raw = _raw_source(package)
        expected_source_ids = {
            segment_id
            for item in raw.content.items
            for segment_id in item.source_segment_ids
        }
        actual_source_ids = {
            segment_id
            for item in content.items
            for segment_id in item.source_segment_ids
        }
        foreign = actual_source_ids - expected_source_ids
        missing = expected_source_ids - actual_source_ids
        if foreign:
            raise RevisionError("Revision contains foreign source segment IDs")
        if missing:
            raise RevisionError("Revision dropped source segment IDs")
