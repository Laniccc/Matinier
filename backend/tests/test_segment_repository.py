from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.captions.models import CaptionEvent, CaptionStatus
from app.persistence.database import Database
from app.persistence.models import SegmentRecord, SessionRecord
from app.persistence.segments import SegmentRepository


def caption(
    *,
    session_id: str = "session-1",
    segment_id: str = "segment-1",
    revision: int = 1,
    status: CaptionStatus = CaptionStatus.FINAL,
    text: str = "caption text",
    start_ms: int | None = 100,
    end_ms: int | None = 500,
) -> CaptionEvent:
    return CaptionEvent(
        session_id=session_id,
        segment_id=segment_id,
        revision=revision,
        status=status,
        text=text,
        audio_start_ms=start_ms,
        audio_end_ms=end_ms,
        confidence=0.9,
        provider_event_id=f"provider-{session_id}-{segment_id}-{revision}",
        received_at_ms=1_000 + revision,
    )


@pytest.fixture
def database() -> Database:
    value = Database("sqlite://")
    value.create_schema()
    try:
        yield value
    finally:
        value.dispose()


def add_session(db_session: Session, session_id: str) -> None:
    db_session.add(
        SessionRecord(
            id=session_id,
            room_name=f"room-{session_id}",
            status="created",
            source_type="file",
            source_name="speech.wav",
            language="zh-CN",
        )
    )
    db_session.flush()


def test_upsert_final_is_revision_aware_and_idempotent(database: Database) -> None:
    with Session(database.engine) as db_session:
        add_session(db_session, "session-1")
        repository = SegmentRepository(db_session)

        created = repository.upsert_final(
            caption(revision=2, text="stable v2"),
            track_id="replay-audio",
            language="zh-CN",
        )
        same_revision = repository.upsert_final(
            caption(revision=2, text="must not replace"),
            track_id="replay-audio",
            language="zh-CN",
        )
        stale = repository.upsert_final(
            caption(revision=1, text="stale"),
            track_id="replay-audio",
            language="zh-CN",
        )
        updated = repository.upsert_final(
            caption(revision=3, text="stable v3"),
            track_id="replay-audio",
            language="zh-CN",
        )
        db_session.commit()

        rows = list(db_session.scalars(select(SegmentRecord)))
        assert len(rows) == 1
        assert created.id == same_revision.id == stale.id == updated.id
        assert rows[0].revision == 3
        assert rows[0].raw_text == "stable v3"
        assert rows[0].display_text == "stable v3"
        assert rows[0].status == "final"
        assert rows[0].received_at_ms == 1_003
        assert rows[0].finalized_at is not None


def test_same_provider_segment_id_is_scoped_by_session(database: Database) -> None:
    with Session(database.engine) as db_session:
        add_session(db_session, "session-1")
        add_session(db_session, "session-2")
        repository = SegmentRepository(db_session)

        repository.upsert_final(
            caption(session_id="session-1", segment_id="shared"),
            track_id="track-1",
            language="zh-CN",
        )
        repository.upsert_final(
            caption(session_id="session-2", segment_id="shared"),
            track_id="track-2",
            language="en",
        )
        db_session.commit()

        rows = list(db_session.scalars(select(SegmentRecord)))
        assert {(row.session_id, row.segment_id) for row in rows} == {
            ("session-1", "shared"),
            ("session-2", "shared"),
        }


def test_repository_rejects_non_final_captions(database: Database) -> None:
    with Session(database.engine) as db_session:
        add_session(db_session, "session-1")
        repository = SegmentRepository(db_session)

        with pytest.raises(ValueError, match="Final"):
            repository.upsert_final(
                caption(status=CaptionStatus.DRAFT),
                track_id="replay-audio",
                language="zh-CN",
            )


def test_final_snapshot_orders_null_timings_last(database: Database) -> None:
    with Session(database.engine) as db_session:
        add_session(db_session, "session-1")
        repository = SegmentRepository(db_session)
        for value in (
            caption(segment_id="late", start_ms=900, end_ms=1_100),
            caption(segment_id="unknown", start_ms=None, end_ms=None),
            caption(segment_id="early", start_ms=100, end_ms=300),
        ):
            repository.upsert_final(
                value,
                track_id="replay-audio",
                language="zh-CN",
            )
        db_session.commit()

        assert [row.segment_id for row in repository.list_final("session-1")] == [
            "early",
            "late",
            "unknown",
        ]
