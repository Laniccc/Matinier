from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.persistence.models import (
    MediaSessionRecord,
    PluginDocumentRecord,
    PluginPackageRecord,
    ResultPackageRecord,
    utc_now,
)
from app.plugins.document_contracts import PluginDocumentPublishInput


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class PluginDocumentRepository:
    """Transaction-scoped, append-only storage for user-owned plugin documents."""

    def __init__(self, db_session: Session) -> None:
        self._db = db_session

    def publish(
        self,
        *,
        plugin_id: str,
        plugin_version: str,
        plugin_package_id: str,
        media_session_id: str,
        source_package_id: str,
        value: PluginDocumentPublishInput,
    ) -> PluginDocumentRecord:
        plugin_package = self.require_plugin_package(plugin_package_id)
        if (
            plugin_package.plugin_id != plugin_id
            or plugin_package.version != plugin_version
        ):
            raise ValueError("plugin package identity does not match publisher")
        self.require_media_session(media_session_id)
        source_package = self.require_source_package(source_package_id)
        if value.source_package_id != source_package_id:
            raise ValueError("document source Package identity does not match request")
        if source_package.content_hash is None or source_package.status not in {
            "frozen",
            "superseded",
        }:
            raise ValueError("source Package is not frozen")

        content_hash = self.compute_content_hash(
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            source_package=source_package,
            value=value,
        )
        latest = self._db.scalar(
            select(PluginDocumentRecord)
            .where(
                PluginDocumentRecord.plugin_id == plugin_id,
                PluginDocumentRecord.media_session_id == media_session_id,
                PluginDocumentRecord.identity_key == value.identity_key,
            )
            .order_by(PluginDocumentRecord.document_version.desc())
            .limit(1)
            .with_for_update()
        )
        if latest is not None and latest.content_hash == content_hash:
            return latest

        created_at = utc_now()
        previous_created_at = self._db.scalar(
            select(func.max(PluginDocumentRecord.created_at)).where(
                PluginDocumentRecord.media_session_id == media_session_id
            )
        )
        if previous_created_at is not None:
            if (
                previous_created_at.tzinfo is None
                or previous_created_at.utcoffset() is None
            ):
                previous_created_at = previous_created_at.replace(tzinfo=dt.UTC)
            if created_at <= previous_created_at:
                created_at = previous_created_at + dt.timedelta(microseconds=1)

        record = PluginDocumentRecord(
            id=str(uuid.uuid4()),
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            plugin_package_id=plugin_package.id,
            media_session_id=media_session_id,
            source_package_id=source_package.id,
            identity_key=value.identity_key,
            document_version=(latest.document_version if latest else 0) + 1,
            schema_name=value.schema_name,
            schema_version=value.schema_version,
            language=value.language,
            trigger=value.trigger,
            completeness=value.completeness,
            status="published",
            source_package_version=source_package.version,
            source_package_hash=source_package.content_hash,
            content_json=value.content,
            evidence_refs_json=[
                item.model_dump(mode="json") for item in value.evidence_refs
            ],
            markdown_text=value.markdown,
            content_hash=content_hash,
            created_at=created_at,
        )
        self._db.add(record)
        self._db.flush()
        return record

    @staticmethod
    def compute_content_hash(
        *,
        plugin_id: str,
        plugin_version: str,
        source_package: ResultPackageRecord,
        value: PluginDocumentPublishInput,
    ) -> str:
        canonical = {
            "plugin_id": plugin_id,
            "plugin_version": plugin_version,
            "source_package_id": source_package.id,
            "source_package_version": source_package.version,
            "source_package_hash": source_package.content_hash,
            "document": value.model_dump(mode="json"),
        }
        return hashlib.sha256(_canonical_json(canonical)).hexdigest()

    def get(self, document_id: str) -> PluginDocumentRecord | None:
        return self._db.get(PluginDocumentRecord, document_id)

    def list_for_session(
        self,
        media_session_id: str,
        *,
        plugin_id: str | None = None,
        identity_key: str | None = None,
        language: str | None = None,
    ) -> list[PluginDocumentRecord]:
        query = select(PluginDocumentRecord).where(
            PluginDocumentRecord.media_session_id == media_session_id
        )
        if plugin_id is not None:
            query = query.where(PluginDocumentRecord.plugin_id == plugin_id)
        if identity_key is not None:
            query = query.where(PluginDocumentRecord.identity_key == identity_key)
        if language is not None:
            query = query.where(PluginDocumentRecord.language == language)
        return list(
            self._db.scalars(
                query.order_by(
                    PluginDocumentRecord.created_at.desc(),
                    PluginDocumentRecord.id.desc(),
                )
            )
        )

    def require_plugin_package(self, package_id: str) -> PluginPackageRecord:
        record = self._db.get(PluginPackageRecord, package_id)
        if record is None:
            raise LookupError(f"plugin package does not exist: {package_id}")
        return record

    def require_media_session(self, media_session_id: str) -> MediaSessionRecord:
        record = self._db.get(MediaSessionRecord, media_session_id)
        if record is None:
            raise LookupError(f"MediaSession does not exist: {media_session_id}")
        return record

    def require_source_package(self, package_id: str) -> ResultPackageRecord:
        record = self._db.get(ResultPackageRecord, package_id)
        if record is None:
            raise LookupError(f"source Package does not exist: {package_id}")
        return record


__all__ = ["PluginDocumentRepository"]
