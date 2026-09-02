from __future__ import annotations

from sqlalchemy.orm import Session

from app.packages import PackageBuilder, PackageRepository
from app.persistence.models import MediaSessionRecord
from app.plugins.broker import CapabilityExecutionContext
from app.plugins.capabilities import (
    DeliveryPrepareInput,
    DeliveryPrepareOutput,
    DeliveryQueryInput,
    DeliveryQueryOutput,
)


class DeliveryCapabilityAdapter:
    """Session-scoped access to immutable Stage 2 delivery packages."""

    def __init__(self, db_session: Session, *, max_page_items: int) -> None:
        if max_page_items < 1 or max_page_items > 1_000:
            raise ValueError("max_page_items must be between 1 and 1000")
        self._db = db_session
        self._max_page_items = max_page_items

    async def prepare(
        self,
        context: CapabilityExecutionContext,
        value: DeliveryPrepareInput,
    ) -> DeliveryPrepareOutput:
        legacy_session_id = self._legacy_session_id(context)
        package = PackageBuilder(self._db).build_baseline(legacy_session_id)
        return DeliveryPrepareOutput(
            package_id=package.package_id,
            package_version=package.package_version,
            content_hash=package.content_hash,
            source_language=package.manifest.source_language,
            target_languages=package.manifest.target_languages,
            final_sequence=value.final_sequence,
        )

    async def query(
        self,
        context: CapabilityExecutionContext,
        value: DeliveryQueryInput,
    ) -> DeliveryQueryOutput:
        legacy_session_id = self._legacy_session_id(context)
        package, items, next_after_item = PackageRepository(self._db).delivery_page(
            value.package_id,
            document_kinds=value.document_kinds,
            language=value.language,
            after_item=value.after_item,
            limit=value.limit,
            max_page_items=self._max_page_items,
        )
        if package.session_id != legacy_session_id:
            raise ValueError("Package does not belong to the current MediaSession")
        return DeliveryQueryOutput(
            package_id=package.package_id,
            package_version=package.package_version,
            content_hash=package.content_hash,
            items=items,
            next_after_item=next_after_item,
        )

    def _legacy_session_id(self, context: CapabilityExecutionContext) -> str:
        if context.media_session_id is None:
            raise ValueError("delivery capability is session-bound")
        media_session = self._db.get(MediaSessionRecord, context.media_session_id)
        if media_session is None:
            raise LookupError("MediaSession does not exist")
        if media_session.legacy_session_id is None:
            raise ValueError("MediaSession has no legacy Session bridge")
        return media_session.legacy_session_id


__all__ = ["DeliveryCapabilityAdapter"]
