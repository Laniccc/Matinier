from __future__ import annotations

import asyncio
import inspect
import threading
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.media.projector import MediaEventProjector, ProjectorConfig
from app.persistence.database import Database
from app.persistence.models import (
    MediaEventRecord,
    SegmentRecord,
    SessionRecord,
    TranslationSegmentRecord,
)


def seed_session(database: Database) -> None:
    now = datetime.now(UTC)
    with Session(database.engine) as db_session:
        db_session.add(
            SessionRecord(
                id="legacy-1",
                room_name="room-1",
                status="completed",
                stop_reason="source_ended",
                source_type="browser-tab",
                source_name="Shared tab",
                language="en-US",
                target_language="zh-CN",
                ended_at=now,
            )
        )
        db_session.flush()
        db_session.add_all(
            [
                SegmentRecord(
                    id="row-1",
                    session_id="legacy-1",
                    segment_id="segment-1",
                    track_id="track-en",
                    revision=1,
                    language="en-US",
                    raw_text="hello",
                    display_text="Hello.",
                    audio_start_ms=100,
                    audio_end_ms=700,
                    confidence=0.98,
                    status="final",
                    received_at_ms=1_000,
                    finalized_at=now,
                    created_at=now,
                    updated_at=now,
                ),
                SegmentRecord(
                    id="row-2",
                    session_id="legacy-1",
                    segment_id="segment-2",
                    track_id="track-en",
                    revision=3,
                    language="en-US",
                    raw_text="world",
                    display_text="World.",
                    audio_start_ms=800,
                    audio_end_ms=1_300,
                    confidence=0.92,
                    status="final",
                    received_at_ms=1_500,
                    finalized_at=now,
                    created_at=now,
                    updated_at=now,
                ),
                SegmentRecord(
                    id="row-draft",
                    session_id="legacy-1",
                    segment_id="segment-draft",
                    track_id="track-en",
                    revision=4,
                    language="en-US",
                    raw_text="partial",
                    display_text="Partial",
                    audio_start_ms=1_400,
                    audio_end_ms=1_700,
                    confidence=None,
                    status="draft",
                    received_at_ms=1_700,
                    finalized_at=now,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        db_session.add(
            TranslationSegmentRecord(
                id="translation-row-1",
                session_id="legacy-1",
                segment_id="translation-1",
                revision=2,
                source_language="en-US",
                target_language="zh-CN",
                text="你好，世界。",
                audio_start_ms=100,
                audio_end_ms=1_300,
                source_segment_ids=["segment-1", "segment-2"],
                status="final",
                received_at_ms=1_800,
                finalized_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db_session.commit()


def test_projector_emits_final_transcript_translation_and_terminal_events() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        seed_session(database)
        projector = MediaEventProjector(
            database,
            config=ProjectorConfig(poll_interval_ms=10, batch_size=50),
        )

        result = asyncio.run(projector.request_catch_up("legacy-1"))
        assert result.projected_event_count == 4

        with Session(database.engine) as db_session:
            events = list(
                db_session.scalars(
                    select(MediaEventRecord).order_by(MediaEventRecord.sequence)
                )
            )
        assert [event.event_type for event in events] == [
            "transcript.final",
            "transcript.final",
            "translation.final",
            "session.completed",
        ]
        assert [event.sequence for event in events] == [1, 2, 3, 4]
        assert events[0].payload_json == {
            "text": "Hello.",
            "language": "en-US",
            "track_id": "track-en",
            "confidence": 0.98,
            "evidence": {
                "legacy_session_id": "legacy-1",
                "segment_id": "segment-1",
                "record_id": "row-1",
            },
        }
        assert events[0].media_time_ms == 100
        assert events[0].duration_ms == 600
        assert events[2].payload_json["source_segment_ids"] == [
            "segment-1",
            "segment-2",
        ]
        assert events[2].payload_json["target_language"] == "zh-CN"
        assert events[2].payload_json["language"] == "zh-CN"
        assert events[2].payload_json["confidence"] is None
        assert all("segment-draft" not in str(event.payload_json) for event in events)
    finally:
        database.dispose()


def test_projector_is_idempotent_and_accepts_higher_final_revision() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        seed_session(database)
        projector = MediaEventProjector(database, config=ProjectorConfig())
        first = asyncio.run(projector.request_catch_up("legacy-1"))
        repeated = asyncio.run(projector.request_catch_up("legacy-1"))
        assert first.projected_event_count == 4
        assert repeated.projected_event_count == 0

        with Session(database.engine) as db_session:
            segment = db_session.scalar(
                select(SegmentRecord).where(SegmentRecord.segment_id == "segment-1")
            )
            assert segment is not None
            segment.revision = 2
            segment.display_text = "Hello again."
            segment.raw_text = "hello again"
            segment.updated_at = datetime.now(UTC)
            db_session.commit()

        revised = asyncio.run(projector.request_catch_up("legacy-1"))
        assert revised.projected_event_count == 1
        with Session(database.engine) as db_session:
            revisions = list(
                db_session.scalars(
                    select(MediaEventRecord)
                    .where(MediaEventRecord.logical_id == "transcript:segment-1")
                    .order_by(MediaEventRecord.sequence)
                )
            )
        assert [event.revision for event in revisions] == [1, 2]
        assert revisions[-1].payload_json["text"] == "Hello again."
    finally:
        database.dispose()


def test_projector_lifecycle_is_idempotent_and_has_no_model_or_plugin_dependency() -> None:
    database = Database("sqlite://")
    database.create_schema()
    projector = MediaEventProjector(
        database,
        config=ProjectorConfig(poll_interval_ms=10),
    )

    async def scenario() -> None:
        await projector.start()
        await projector.start()
        assert projector.started is True
        await asyncio.sleep(0.03)
        await projector.stop()
        await projector.stop()
        assert projector.started is False

    try:
        asyncio.run(scenario())
        source = inspect.getsource(inspect.getmodule(MediaEventProjector))
        assert "app.plugins" not in source
        assert "text_processing" not in source
    finally:
        database.dispose()


def test_projector_serializes_concurrent_catch_up_requests() -> None:
    database = Database("sqlite://")
    database.create_schema()
    seed_session(database)
    projector = MediaEventProjector(database, config=ProjectorConfig())
    original_project = projector._project_session_to_completion
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def tracked_project(legacy_session_id: str):
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.05)
            return original_project(legacy_session_id)
        finally:
            with state_lock:
                active -= 1

    projector._project_session_to_completion = tracked_project

    async def scenario() -> None:
        await asyncio.gather(
            projector.request_catch_up("legacy-1"),
            projector.request_catch_up("legacy-1"),
        )

    try:
        asyncio.run(scenario())
        assert maximum_active == 1
    finally:
        database.dispose()
