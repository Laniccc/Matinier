from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.persistence.models import TranscriptRevisionRecord, utc_now
from app.revisions.models import TranscriptRevision, TranscriptRevisionContent


class RevisionRepository:
    def __init__(self, db_session: Session) -> None:
        self._db_session = db_session

    def next_version(self, session_id: str, language: str) -> int:
        current = self._db_session.scalar(
            select(func.max(TranscriptRevisionRecord.version)).where(
                TranscriptRevisionRecord.session_id == session_id,
                TranscriptRevisionRecord.language == language,
            )
        )
        return (current or 0) + 1

    def create(
        self,
        *,
        session_id: str,
        parent_revision_id: str | None,
        base_package_id: str,
        language: str,
        content: TranscriptRevisionContent,
        content_hash: str,
        change_summary: str | None,
        created_at: dt.datetime | None = None,
    ) -> TranscriptRevision:
        record = TranscriptRevisionRecord(
            id=str(uuid.uuid4()),
            session_id=session_id,
            version=self.next_version(session_id, language),
            parent_revision_id=parent_revision_id,
            base_package_id=base_package_id,
            language=language,
            content_json=content.model_dump(mode="json"),
            content_hash=content_hash,
            change_summary=change_summary,
            status="saved",
            created_at=created_at or utc_now(),
            approved_at=None,
        )
        self._db_session.add(record)
        self._db_session.flush()
        return self._to_domain(record)

    def get_record(self, revision_id: str) -> TranscriptRevisionRecord | None:
        return self._db_session.get(TranscriptRevisionRecord, revision_id)

    def get(self, revision_id: str) -> TranscriptRevision | None:
        record = self.get_record(revision_id)
        return self._to_domain(record) if record is not None else None

    def list_for_session(self, session_id: str) -> list[TranscriptRevision]:
        records = self._db_session.scalars(
            select(TranscriptRevisionRecord)
            .where(TranscriptRevisionRecord.session_id == session_id)
            .order_by(
                TranscriptRevisionRecord.version.desc(),
                TranscriptRevisionRecord.id.desc(),
            )
        )
        return [self._to_domain(item) for item in records]

    def approve(
        self,
        revision_id: str,
        *,
        approved_at: dt.datetime | None = None,
    ) -> TranscriptRevision:
        target = self.get_record(revision_id)
        if target is None:
            raise LookupError(f"Revision not found: {revision_id}")
        if target.status == "superseded":
            raise ValueError("a superseded revision cannot be approved")
        if target.status == "approved":
            return self._to_domain(target)
        timestamp = approved_at or utc_now()
        current = list(
            self._db_session.scalars(
                select(TranscriptRevisionRecord).where(
                    TranscriptRevisionRecord.session_id == target.session_id,
                    TranscriptRevisionRecord.language == target.language,
                    TranscriptRevisionRecord.status == "approved",
                    TranscriptRevisionRecord.id != target.id,
                )
            )
        )
        for item in current:
            item.status = "superseded"
        if current:
            self._db_session.flush()
        target.status = "approved"
        target.approved_at = timestamp
        self._db_session.flush()
        return self._to_domain(target)

    @staticmethod
    def _to_domain(record: TranscriptRevisionRecord) -> TranscriptRevision:
        return TranscriptRevision(
            revision_id=record.id,
            session_id=record.session_id,
            version=record.version,
            parent_revision_id=record.parent_revision_id,
            base_package_id=record.base_package_id,
            language=record.language,
            content=TranscriptRevisionContent.model_validate(record.content_json),
            content_hash=record.content_hash,
            change_summary=record.change_summary,
            status=record.status,
            created_at=record.created_at,
            approved_at=record.approved_at,
        )
