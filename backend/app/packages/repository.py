from __future__ import annotations

import datetime as dt
import json
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.packages.models import (
    PACKAGE_SCHEMA,
    PACKAGE_SCHEMA_VERSION,
    PackageDocument,
    PackageManifest,
    TranscriptPackage,
    LiveTranslationDocument,
    document_identity,
)
from app.persistence.models import (
    PackageDocumentRecord,
    ResultPackageRecord,
)


_document_adapter = TypeAdapter(PackageDocument)


class PackageRepository:
    def __init__(self, db_session: Session) -> None:
        self._db_session = db_session

    def next_version(self, session_id: str) -> int:
        current = self._db_session.scalar(
            select(func.max(ResultPackageRecord.version)).where(
                ResultPackageRecord.session_id == session_id
            )
        )
        return (current or 0) + 1

    def create_building(
        self,
        *,
        package_id: str,
        session_id: str,
        version: int,
        source_revision_id: str | None,
        created_at: dt.datetime,
    ) -> ResultPackageRecord:
        record = ResultPackageRecord(
            id=package_id,
            session_id=session_id,
            version=version,
            schema_name=PACKAGE_SCHEMA,
            schema_version=PACKAGE_SCHEMA_VERSION,
            source_revision_id=source_revision_id,
            status="building",
            content_hash=None,
            manifest_json={},
            created_at=created_at,
        )
        self._db_session.add(record)
        self._db_session.flush()
        return record

    def add_document(
        self,
        package_id: str,
        document: PackageDocument,
        *,
        created_at: dt.datetime,
    ) -> PackageDocumentRecord:
        record = PackageDocumentRecord(
            id=document.document_id,
            package_id=package_id,
            document_kind=document.document_kind,
            language=document.language,
            content_json=document.content.model_dump(mode="json"),
            content_hash=document.content_hash,
            created_at=created_at,
        )
        self._db_session.add(record)
        self._db_session.flush()
        return record

    def freeze(
        self,
        record: ResultPackageRecord,
        *,
        manifest: PackageManifest,
        content_hash: str,
        frozen_at: dt.datetime,
    ) -> ResultPackageRecord:
        if record.status != "building":
            raise ValueError("only a building package can be frozen")
        previous = list(
            self._db_session.scalars(
                select(ResultPackageRecord).where(
                    ResultPackageRecord.session_id == record.session_id,
                    ResultPackageRecord.id != record.id,
                    ResultPackageRecord.status == "frozen",
                )
            )
        )
        for item in previous:
            item.status = "superseded"
            item.superseded_at = frozen_at
        record.status = "frozen"
        record.content_hash = content_hash
        record.manifest_json = manifest.model_dump(mode="json", by_alias=True)
        record.frozen_at = frozen_at
        self._db_session.flush()
        return record

    def get(self, package_id: str) -> ResultPackageRecord | None:
        return self._db_session.get(ResultPackageRecord, package_id)

    def list_for_session(self, session_id: str) -> list[ResultPackageRecord]:
        return list(
            self._db_session.scalars(
                select(ResultPackageRecord)
                .where(ResultPackageRecord.session_id == session_id)
                .order_by(
                    ResultPackageRecord.version.desc(),
                    ResultPackageRecord.id.desc(),
                )
            )
        )

    def list_documents(self, package_id: str) -> list[PackageDocumentRecord]:
        return list(
            self._db_session.scalars(
                select(PackageDocumentRecord).where(
                    PackageDocumentRecord.package_id == package_id
                )
            )
        )

    def load(self, package_id: str) -> TranscriptPackage:
        record = self.get(package_id)
        if record is None:
            raise LookupError(f"Package not found: {package_id}")
        if record.content_hash is None or not record.manifest_json:
            raise ValueError("Package is not frozen")
        documents = [
            self._document_from_record(item)
            for item in self.list_documents(package_id)
        ]
        documents.sort(key=document_identity)
        return TranscriptPackage(
            package_id=record.id,
            session_id=record.session_id,
            package_version=record.version,
            schema_name=record.schema_name,
            schema_version=record.schema_version,
            status=record.status,
            source_revision_id=record.source_revision_id,
            manifest=PackageManifest.model_validate(record.manifest_json),
            documents=tuple(documents),
            content_hash=record.content_hash,
            created_at=record.created_at,
            frozen_at=record.frozen_at,
            superseded_at=record.superseded_at,
        )

    def delivery_page(
        self,
        package_id: str,
        *,
        document_kinds: tuple[str, ...],
        language: str | None,
        after_item: int,
        limit: int,
        max_page_items: int,
        max_page_bytes: int = 192 * 1024,
    ) -> tuple[TranscriptPackage, tuple[dict[str, object], ...], int | None]:
        """Read a deterministic page using frozen Package documents only."""

        if after_item < 0 or limit < 1 or max_page_items < 1:
            raise ValueError("invalid delivery page bounds")
        package = self.load(package_id)
        if package.status not in {"frozen", "superseded"}:
            raise ValueError("Package is not frozen")

        requested = set(document_kinds)
        flattened: list[dict[str, object]] = []
        for document in sorted(package.documents, key=document_identity):
            if document.document_kind not in requested:
                continue
            if (
                language is not None
                and isinstance(document, LiveTranslationDocument)
                and document.language != language
            ):
                continue
            content = document.content.model_dump(mode="json")
            common: dict[str, object] = {
                "document_id": document.document_id,
                "document_kind": document.document_kind,
                "language": document.language,
                "document_content_hash": document.content_hash,
            }
            document_items = content.get("items")
            if isinstance(document_items, list):
                context = {
                    key: value
                    for key, value in content.items()
                    if key != "items"
                }
                for item in document_items:
                    if not isinstance(item, dict):
                        raise ValueError("Package document item is malformed")
                    flattened.append(
                        {
                            **common,
                            **({"document_context": context} if context else {}),
                            **item,
                        }
                    )
            else:
                flattened.append({**common, "content": content})

        if after_item >= len(flattened):
            return package, (), None
        page: list[dict[str, object]] = []
        hard_limit = min(limit, max_page_items)
        for item in flattened[after_item : after_item + hard_limit]:
            candidate = [*page, item]
            encoded = json.dumps(
                candidate,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > max_page_bytes:
                if not page:
                    raise ValueError("Package item exceeds delivery byte limit")
                break
            page.append(item)
        next_after_item = after_item + len(page)
        if next_after_item >= len(flattened):
            next_after_item = None
        return package, tuple(page), next_after_item

    @staticmethod
    def _document_from_record(record: PackageDocumentRecord) -> PackageDocument:
        value: dict[str, Any] = {
            "document_id": record.id,
            "document_kind": record.document_kind,
            "language": record.language,
            "content": record.content_json,
            "content_hash": record.content_hash,
        }
        return _document_adapter.validate_python(value)
