from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.media.repository import MediaRepository
from app.persistence.database import Database
from app.persistence.models import (
    MediaEventRecord,
    SessionRecord,
)


@pytest.fixture
def database() -> Database:
    value = Database("sqlite://")
    value.create_schema()
    try:
        yield value
    finally:
        value.dispose()


def seed_legacy_session(db_session: Session, session_id: str) -> None:
    db_session.add(
        SessionRecord(
            id=session_id,
            room_name=f"room-{session_id}",
            status="running",
            source_type="browser-tab",
            source_name="Shared tab",
            language="en-US",
            target_language="zh-CN",
        )
    )
    db_session.flush()


def append_final(
    repository: MediaRepository,
    media_session_id: str,
    *,
    logical_id: str,
    revision: int,
) -> MediaEventRecord:
    return repository.append_event(
        media_session_id=media_session_id,
        event_type="transcript.final",
        media_time_ms=1_000,
        duration_ms=500,
        logical_id=logical_id,
        revision=revision,
        finality="final",
        source="host.transcription",
        payload={"text": f"text-r{revision}", "language": "en-US"},
        created_at=datetime.now(UTC),
    )


def test_legacy_bridge_is_one_to_one_and_idempotent(database: Database) -> None:
    with Session(database.engine) as db_session:
        seed_legacy_session(db_session, "legacy-1")
        repository = MediaRepository(db_session)

        created = repository.ensure_legacy_session_bridge("legacy-1")
        repeated = repository.ensure_legacy_session_bridge("legacy-1")
        db_session.commit()

        assert repeated.id == created.id
        assert created.legacy_session_id == "legacy-1"
        assert created.mode == "live"
        assert created.source_kind == "browser-tab"
        assert created.next_sequence == 1


def test_event_sequences_are_monotonic_and_source_revisions_are_idempotent(
    database: Database,
) -> None:
    with Session(database.engine) as db_session:
        seed_legacy_session(db_session, "legacy-1")
        repository = MediaRepository(db_session)
        media_session = repository.ensure_legacy_session_bridge("legacy-1")

        first = append_final(repository, media_session.id, logical_id="segment:1", revision=1)
        duplicate = append_final(
            repository,
            media_session.id,
            logical_id="segment:1",
            revision=1,
        )
        revised = append_final(
            repository,
            media_session.id,
            logical_id="segment:1",
            revision=2,
        )
        second_segment = append_final(
            repository,
            media_session.id,
            logical_id="segment:2",
            revision=1,
        )
        db_session.commit()

        assert duplicate.id == first.id
        assert [first.sequence, revised.sequence, second_segment.sequence] == [1, 2, 3]
        rows = list(
            db_session.scalars(
                select(MediaEventRecord).order_by(MediaEventRecord.sequence)
            )
        )
        assert [row.revision for row in rows] == [1, 2, 1]


def test_listing_is_ordered_bounded_and_session_isolated(database: Database) -> None:
    with Session(database.engine) as db_session:
        seed_legacy_session(db_session, "legacy-1")
        seed_legacy_session(db_session, "legacy-2")
        repository = MediaRepository(db_session)
        first_session = repository.ensure_legacy_session_bridge("legacy-1")
        other_session = repository.ensure_legacy_session_bridge("legacy-2")
        for number in range(1, 5):
            append_final(
                repository,
                first_session.id,
                logical_id=f"segment:{number}",
                revision=1,
            )
        append_final(
            repository,
            other_session.id,
            logical_id="segment:other",
            revision=1,
        )
        db_session.commit()

        rows = repository.list_events_after(first_session.id, after_sequence=1, limit=2)
        assert [row.sequence for row in rows] == [2, 3]
        assert {row.media_session_id for row in rows} == {first_session.id}
        with pytest.raises(ValueError, match="limit"):
            repository.list_events_after(first_session.id, after_sequence=0, limit=0)


def test_consumer_cursor_acknowledgement_never_moves_backwards(database: Database) -> None:
    with Session(database.engine) as db_session:
        seed_legacy_session(db_session, "legacy-1")
        repository = MediaRepository(db_session)
        media_session = repository.ensure_legacy_session_bridge("legacy-1")
        append_final(repository, media_session.id, logical_id="segment:1", revision=1)
        append_final(repository, media_session.id, logical_id="segment:2", revision=1)
        cursor = repository.get_or_create_cursor(media_session.id, "diagnostic-plugin")
        assert cursor.last_delivered_sequence == 0
        assert cursor.last_acknowledged_sequence == 0

        repository.list_events_after(
            media_session.id,
            after_sequence=0,
            limit=10,
            consumer_id="diagnostic-plugin",
        )
        advanced = repository.acknowledge_cursor(
            media_session.id,
            "diagnostic-plugin",
            sequence=2,
        )
        unchanged = repository.acknowledge_cursor(
            media_session.id,
            "diagnostic-plugin",
            sequence=1,
        )
        db_session.commit()

        assert advanced.last_delivered_sequence == 2
        assert unchanged.last_acknowledged_sequence == 2
        with pytest.raises(ValueError, match="delivered"):
            repository.acknowledge_cursor(
                media_session.id,
                "diagnostic-plugin",
                sequence=3,
            )


def test_bridge_offsets_only_advance(database: Database) -> None:
    with Session(database.engine) as db_session:
        seed_legacy_session(db_session, "legacy-1")
        repository = MediaRepository(db_session)
        media_session = repository.ensure_legacy_session_bridge("legacy-1")

        created = repository.advance_bridge_offset(
            media_session.id,
            source_table="segments",
            source_type="transcript.final",
            source_logical_id="segment:1",
            processed_revision=2,
        )
        stale = repository.advance_bridge_offset(
            media_session.id,
            source_table="segments",
            source_type="transcript.final",
            source_logical_id="segment:1",
            processed_revision=1,
        )
        db_session.commit()

        assert created.id == stale.id
        assert stale.processed_revision == 2
