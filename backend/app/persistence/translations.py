from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.persistence.models import (
    SegmentRecord,
    TranslationSegmentRecord,
    utc_now,
)
from app.translation.models import (
    TranslationCaption,
    TranslationCaptionStatus,
)


class TranslationSegmentRepository:
    def __init__(self, db_session: Session) -> None:
        self._db_session = db_session

    def upsert_final(
        self,
        caption: TranslationCaption,
    ) -> TranslationSegmentRecord:
        if caption.status is not TranslationCaptionStatus.FINAL:
            raise ValueError("only Final translations can be persisted")
        existing = self._db_session.scalar(
            select(TranslationSegmentRecord).where(
                TranslationSegmentRecord.session_id == caption.session_id,
                TranslationSegmentRecord.target_language
                == caption.target_language,
                TranslationSegmentRecord.segment_id == caption.segment_id,
            )
        )
        if existing is not None and caption.revision <= existing.revision:
            return existing

        source_segment_ids = self._align_source_segments(
            session_id=caption.session_id,
            start_ms=caption.audio_start_ms,
            end_ms=caption.audio_end_ms,
        )
        finalized_at = dt.datetime.fromtimestamp(
            caption.received_at_ms / 1_000,
            tz=dt.UTC,
        )
        now = utc_now()
        if existing is None:
            existing = TranslationSegmentRecord(
                id=str(uuid.uuid4()),
                session_id=caption.session_id,
                segment_id=caption.segment_id,
                revision=caption.revision,
                source_language=caption.source_language,
                target_language=caption.target_language,
                text=caption.text,
                audio_start_ms=caption.audio_start_ms,
                audio_end_ms=caption.audio_end_ms,
                source_segment_ids=source_segment_ids,
                status=TranslationCaptionStatus.FINAL.value,
                received_at_ms=caption.received_at_ms,
                finalized_at=finalized_at,
                created_at=now,
                updated_at=now,
            )
            self._db_session.add(existing)
        else:
            existing.revision = caption.revision
            existing.source_language = caption.source_language
            existing.text = caption.text
            existing.audio_start_ms = caption.audio_start_ms
            existing.audio_end_ms = caption.audio_end_ms
            existing.source_segment_ids = source_segment_ids
            existing.status = TranslationCaptionStatus.FINAL.value
            existing.received_at_ms = caption.received_at_ms
            existing.finalized_at = finalized_at
            existing.updated_at = now
        self._db_session.flush()
        return existing

    def list_final(
        self,
        session_id: str,
    ) -> list[TranslationSegmentRecord]:
        return list(
            self._db_session.scalars(
                select(TranslationSegmentRecord)
                .where(
                    TranslationSegmentRecord.session_id == session_id,
                    TranslationSegmentRecord.status
                    == TranslationCaptionStatus.FINAL.value,
                )
                .order_by(
                    TranslationSegmentRecord.audio_start_ms.is_(None),
                    TranslationSegmentRecord.audio_start_ms,
                    TranslationSegmentRecord.audio_end_ms.is_(None),
                    TranslationSegmentRecord.audio_end_ms,
                    TranslationSegmentRecord.created_at,
                    TranslationSegmentRecord.id,
                )
            )
        )

    def realign_session(self, session_id: str) -> None:
        for record in self.list_final(session_id):
            aligned = self._align_source_segments(
                session_id=session_id,
                start_ms=record.audio_start_ms,
                end_ms=record.audio_end_ms,
            )
            if aligned != record.source_segment_ids:
                record.source_segment_ids = aligned
                record.updated_at = utc_now()
        self._db_session.flush()

    def _align_source_segments(
        self,
        *,
        session_id: str,
        start_ms: int | None,
        end_ms: int | None,
    ) -> list[str]:
        source = list(
            self._db_session.scalars(
                select(SegmentRecord)
                .where(
                    SegmentRecord.session_id == session_id,
                    SegmentRecord.status == "final",
                )
                .order_by(
                    SegmentRecord.audio_start_ms.is_(None),
                    SegmentRecord.audio_start_ms,
                    SegmentRecord.created_at,
                    SegmentRecord.id,
                )
            )
        )
        if not source:
            return []
        if start_ms is None or end_ms is None:
            return [source[-1].segment_id]
        overlaps = [
            record.segment_id
            for record in source
            if record.audio_start_ms is not None
            and record.audio_end_ms is not None
            and record.audio_end_ms >= start_ms
            and record.audio_start_ms <= end_ms
        ]
        if overlaps:
            return overlaps
        prior = [
            record
            for record in source
            if record.audio_start_ms is not None
            and record.audio_start_ms <= end_ms
        ]
        return [(prior[-1] if prior else source[0]).segment_id]
