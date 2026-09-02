from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.captions.models import CaptionEvent, CaptionStatus
from app.persistence.database import Database
from app.persistence.models import SessionRecord, TranslationSegmentRecord
from app.persistence.segments import SegmentRepository
from app.persistence.translations import TranslationSegmentRepository
from app.translation.models import (
    TranslationCaption,
    TranslationCaptionStatus,
)


def test_translation_final_is_separate_and_aligned_to_source_segments() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with Session(database.engine) as db_session:
            db_session.add(
                SessionRecord(
                    id="session-1",
                    room_name="room-1",
                    status="running",
                    source_type="file",
                    source_name="speech.wav",
                    language="zh-CN",
                    target_language="en-US",
                    translation_status="running",
                )
            )
            source_repository = SegmentRepository(db_session)
            for segment_id, text, start_ms, end_ms in (
                ("source-1", "你好", 0, 500),
                ("source-2", "世界", 500, 1_000),
            ):
                source_repository.upsert_final(
                    CaptionEvent(
                        session_id="session-1",
                        segment_id=segment_id,
                        revision=1,
                        status=CaptionStatus.FINAL,
                        text=text,
                        audio_start_ms=start_ms,
                        audio_end_ms=end_ms,
                        confidence=0.9,
                        provider_event_id=f"event-{segment_id}",
                        received_at_ms=1_000,
                    ),
                    track_id="track-1",
                    language="zh-CN",
                )

            repository = TranslationSegmentRepository(db_session)
            saved = repository.upsert_final(
                TranslationCaption(
                    session_id="session-1",
                    segment_id="translation-1",
                    revision=2,
                    status=TranslationCaptionStatus.FINAL,
                    text="Hello world",
                    source_language="zh-CN",
                    target_language="en-US",
                    audio_start_ms=100,
                    audio_end_ms=900,
                    source_segment_ids=(),
                    provider_event_id="translation-final",
                    received_at_ms=2_000,
                )
            )
            db_session.commit()

            assert saved.text == "Hello world"
            assert saved.source_segment_ids == ["source-1", "source-2"]
            assert [item.segment_id for item in repository.list_final("session-1")] == [
                "translation-1"
            ]
            assert source_repository.list_final("session-1")[0].raw_text == "你好"
    finally:
        database.dispose()


def test_translation_final_upsert_is_revision_aware_and_idempotent() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with Session(database.engine) as db_session:
            db_session.add(
                SessionRecord(
                    id="session-1",
                    room_name="room-1",
                    status="running",
                    source_type="file",
                    source_name="speech.wav",
                    language="zh-CN",
                    target_language="en-US",
                )
            )
            repository = TranslationSegmentRepository(db_session)

            def caption(
                revision: int,
                text: str,
                status: TranslationCaptionStatus = (
                    TranslationCaptionStatus.FINAL
                ),
            ) -> TranslationCaption:
                return TranslationCaption(
                    session_id="session-1",
                    segment_id="translation-1",
                    revision=revision,
                    status=status,
                    text=text,
                    source_language="zh-CN",
                    target_language="en-US",
                    audio_start_ms=100,
                    audio_end_ms=900,
                    source_segment_ids=(),
                    provider_event_id=f"translation-{revision}",
                    received_at_ms=2_000 + revision,
                )

            created = repository.upsert_final(caption(2, "stable v2"))
            duplicate = repository.upsert_final(caption(2, "must not replace"))
            stale = repository.upsert_final(caption(1, "stale"))
            updated = repository.upsert_final(caption(3, "stable v3"))
            with pytest.raises(ValueError, match="Final"):
                repository.upsert_final(
                    caption(4, "draft", TranslationCaptionStatus.DRAFT)
                )
            db_session.commit()

            rows = list(db_session.scalars(select(TranslationSegmentRecord)))
            assert len(rows) == 1
            assert created.id == duplicate.id == stale.id == updated.id
            assert rows[0].revision == 3
            assert rows[0].text == "stable v3"
            assert rows[0].status == TranslationCaptionStatus.FINAL.value
    finally:
        database.dispose()
