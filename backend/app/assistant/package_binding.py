from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.meeting_state.models import (
    CaptionEvidenceMessage,
    EvidenceMessageSnapshot,
)
from app.packages.models import (
    SourceApprovedDocument,
    SourceRawDocument,
    TranscriptPackage,
)
from app.persistence.models import (
    AssistantContextSnapshotRecord,
    AssistantExecutionRecord,
    AssistantPackageBindingRecord,
    utc_now,
)


_EVIDENCE_MESSAGES = TypeAdapter(tuple[EvidenceMessageSnapshot, ...])


class FrozenBindingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssistantPackageBinding(FrozenBindingModel):
    binding_id: str = Field(min_length=1, max_length=36)
    execution_id: str = Field(min_length=1, max_length=36)
    snapshot_id: str = Field(min_length=1, max_length=36)
    package_id: str = Field(min_length=1, max_length=36)
    package_version: int = Field(ge=1)
    package_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    segment_item_mapping: dict[str, tuple[str, ...]]
    created_at: dt.datetime


class PackageBindingError(ValueError):
    pass


class PackageBindingService:
    """Attach unbound execution snapshots to one immutable evidence Package."""

    def __init__(self, db_session: Session) -> None:
        self._db_session = db_session

    def bind_unbound_executions(
        self,
        package: TranscriptPackage,
    ) -> tuple[AssistantPackageBinding, ...]:
        if package.status != "frozen" or package.frozen_at is None:
            raise PackageBindingError("Assistant bindings require a frozen Package")
        effective_items = self._effective_source_items(package)
        item_ids_by_segment: dict[str, list[str]] = {}
        for item in effective_items:
            for segment_id in item.source_segment_ids:
                values = item_ids_by_segment.setdefault(segment_id, [])
                if item.item_id not in values:
                    values.append(item.item_id)

        rows = self._db_session.execute(
            select(AssistantExecutionRecord, AssistantContextSnapshotRecord)
            .join(
                AssistantContextSnapshotRecord,
                AssistantExecutionRecord.snapshot_id
                == AssistantContextSnapshotRecord.id,
            )
            .outerjoin(
                AssistantPackageBindingRecord,
                AssistantPackageBindingRecord.execution_id
                == AssistantExecutionRecord.id,
            )
            .where(
                AssistantExecutionRecord.session_id == package.session_id,
                AssistantContextSnapshotRecord.session_id == package.session_id,
                AssistantPackageBindingRecord.id.is_(None),
            )
            .order_by(
                AssistantExecutionRecord.created_at,
                AssistantExecutionRecord.id,
            )
        ).all()

        created: list[AssistantPackageBinding] = []
        for execution, snapshot in rows:
            mapping = self._snapshot_mapping(
                snapshot,
                item_ids_by_segment=item_ids_by_segment,
            )
            record = AssistantPackageBindingRecord(
                id=str(uuid.uuid4()),
                execution_id=execution.id,
                snapshot_id=snapshot.id,
                package_id=package.package_id,
                package_version=package.package_version,
                package_content_hash=package.content_hash,
                segment_item_mapping_json={
                    segment_id: list(item_ids)
                    for segment_id, item_ids in mapping.items()
                },
                created_at=utc_now(),
            )
            self._db_session.add(record)
            created.append(self._to_domain(record))
        self._db_session.flush()
        return tuple(created)

    def get_for_execution(
        self,
        execution_id: str,
    ) -> AssistantPackageBinding | None:
        record = self._db_session.scalar(
            select(AssistantPackageBindingRecord).where(
                AssistantPackageBindingRecord.execution_id == execution_id
            )
        )
        return self._to_domain(record) if record is not None else None

    @staticmethod
    def _effective_source_items(package: TranscriptPackage):
        document = next(
            (
                item
                for item in package.documents
                if item.document_id
                == package.manifest.effective_source_document_id
            ),
            None,
        )
        if not isinstance(
            document,
            (SourceRawDocument, SourceApprovedDocument),
        ):
            raise PackageBindingError(
                "Package effective source document is unavailable"
            )
        return document.content.items

    @staticmethod
    def _snapshot_mapping(
        snapshot: AssistantContextSnapshotRecord,
        *,
        item_ids_by_segment: dict[str, list[str]],
    ) -> dict[str, tuple[str, ...]]:
        messages = _EVIDENCE_MESSAGES.validate_python(
            snapshot.evidence_messages_json
        )
        segment_ids = tuple(
            dict.fromkeys(
                message.segment_id
                for message in messages
                if isinstance(message, CaptionEvidenceMessage)
            )
        )
        missing = [
            segment_id
            for segment_id in segment_ids
            if segment_id not in item_ids_by_segment
        ]
        if missing:
            raise PackageBindingError(
                "Frozen Package does not cover Snapshot segments: "
                + ", ".join(missing)
            )
        return {
            segment_id: tuple(item_ids_by_segment[segment_id])
            for segment_id in segment_ids
        }

    @staticmethod
    def _to_domain(
        record: AssistantPackageBindingRecord,
    ) -> AssistantPackageBinding:
        return AssistantPackageBinding(
            binding_id=record.id,
            execution_id=record.execution_id,
            snapshot_id=record.snapshot_id,
            package_id=record.package_id,
            package_version=record.package_version,
            package_content_hash=record.package_content_hash,
            segment_item_mapping={
                segment_id: tuple(item_ids)
                for segment_id, item_ids in record.segment_item_mapping_json.items()
            },
            created_at=record.created_at,
        )


__all__ = [
    "AssistantPackageBinding",
    "PackageBindingError",
    "PackageBindingService",
]
