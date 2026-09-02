from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.captions.models import CaptionEvent, CaptionStatus
from app.persistence.models import SegmentRecord, utc_now


class SegmentRepository:
    def __init__(self, db_session: Session) -> None:
        self._db_session = db_session

    def upsert_final(
        self,
        caption: CaptionEvent,
        *,
        track_id: str,
        language: str,
    ) -> SegmentRecord:
        if caption.status is not CaptionStatus.FINAL:
            raise ValueError("only Final captions can be persisted")
        if not track_id:
            raise ValueError("track_id is required")
        if not language:
            raise ValueError("language is required")

        existing = self._db_session.scalar(
            select(SegmentRecord).where(
                SegmentRecord.session_id == caption.session_id,
                SegmentRecord.segment_id == caption.segment_id,
            )
        )
        if existing is not None and caption.revision <= existing.revision:
            return existing

        finalized_at = dt.datetime.fromtimestamp(
            caption.received_at_ms / 1_000,
            tz=dt.UTC,
        )
        now = utc_now()
        if existing is None:
            existing = SegmentRecord(
                id=str(uuid.uuid4()),
                session_id=caption.session_id,
                segment_id=caption.segment_id,
                track_id=track_id,
                revision=caption.revision,
                language=language,
                raw_text=caption.text,
                display_text=caption.text,
                audio_start_ms=caption.audio_start_ms,
                audio_end_ms=caption.audio_end_ms,
                confidence=caption.confidence,
                status=CaptionStatus.FINAL.value,
                received_at_ms=caption.received_at_ms,
                finalized_at=finalized_at,
                created_at=now,
                updated_at=now,
            )
            self._db_session.add(existing)
        else:
            existing.track_id = track_id
            existing.revision = caption.revision
            existing.language = language
            existing.raw_text = caption.text
            existing.display_text = caption.text
            existing.audio_start_ms = caption.audio_start_ms
            existing.audio_end_ms = caption.audio_end_ms
            existing.confidence = caption.confidence
            existing.status = CaptionStatus.FINAL.value
            existing.received_at_ms = caption.received_at_ms
            existing.finalized_at = finalized_at
            existing.updated_at = now
        self._db_session.flush()
        return existing

    def list_final(self, session_id: str) -> list[SegmentRecord]:
        return list(
            self._db_session.scalars(
                select(SegmentRecord)
                .where(
                    SegmentRecord.session_id == session_id,
                    SegmentRecord.status == CaptionStatus.FINAL.value,
                )
                .order_by(
                    SegmentRecord.audio_start_ms.is_(None),
                    SegmentRecord.audio_start_ms,
                    SegmentRecord.audio_end_ms.is_(None),
                    SegmentRecord.audio_end_ms,
                    SegmentRecord.created_at,
                    SegmentRecord.id,
                )
            )
        )
