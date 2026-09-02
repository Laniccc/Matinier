from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.packages import PackageRepository
from app.packages.models import EvidenceIndexDocument
from app.persistence.models import MediaSessionRecord
from app.plugins.broker import CapabilityExecutionContext
from app.plugins.document_contracts import (
    PluginDocumentPublishInput,
    PluginDocumentPublishOutput,
)
from app.plugins.document_repository import PluginDocumentRepository
from app.plugins.repository import PluginRepository


class PluginDocumentCapabilityAdapter:
    """Validate Package-closed evidence before publishing a user document."""

    def __init__(self, db_session: Session, *, max_document_bytes: int) -> None:
        if max_document_bytes < 1:
            raise ValueError("max_document_bytes must be positive")
        self._db = db_session
        self._max_document_bytes = max_document_bytes

    async def __call__(
        self,
        context: CapabilityExecutionContext,
        value: PluginDocumentPublishInput,
    ) -> PluginDocumentPublishOutput:
        encoded = json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > self._max_document_bytes:
            raise ValueError("document exceeds the Host byte limit")
        if context.media_session_id is None:
            raise ValueError("document.publish is session-bound")

        media_session = self._db.get(MediaSessionRecord, context.media_session_id)
        if media_session is None:
            raise LookupError("MediaSession does not exist")
        package = PackageRepository(self._db).load(value.source_package_id)
        if (
            media_session.legacy_session_id is None
            or package.session_id != media_session.legacy_session_id
        ):
            raise ValueError("Package does not belong to the current MediaSession")

        evidence_document = next(
            (
                document
                for document in package.documents
                if isinstance(document, EvidenceIndexDocument)
            ),
            None,
        )
        if evidence_document is None:
            raise ValueError("source Package has no evidence index")
        if not value.evidence_refs:
            raise ValueError("document evidence is required")
        evidence_by_id = {
            item.item_id: item for item in evidence_document.content.items
        }
        for reference in value.evidence_refs:
            source = evidence_by_id.get(reference.item_id)
            if source is None:
                raise ValueError("document evidence item is not in the source Package")
            if (
                reference.source_segment_ids != source.source_segment_ids
                or reference.start_ms != source.start_ms
                or reference.end_ms != source.end_ms
            ):
                raise ValueError("document evidence does not match the source Package")

        plugin_package = PluginRepository(self._db).get_package(
            context.plugin_id,
            context.plugin_version,
        )
        assert plugin_package is not None
        record = PluginDocumentRepository(self._db).publish(
            plugin_id=context.plugin_id,
            plugin_version=context.plugin_version,
            plugin_package_id=plugin_package.id,
            media_session_id=context.media_session_id,
            source_package_id=package.package_id,
            value=value,
        )
        return PluginDocumentPublishOutput(
            document_id=record.id,
            document_version=record.document_version,
            identity_key=record.identity_key,
            content_hash=record.content_hash,
            language=record.language,
            trigger=record.trigger,
            completeness=record.completeness,
        )


__all__ = ["PluginDocumentCapabilityAdapter"]
