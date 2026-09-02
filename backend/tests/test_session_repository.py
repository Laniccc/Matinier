from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.persistence.database import Database
from app.persistence.models import SessionRecord
from app.persistence.sessions import SessionRepository
from app.sessions.state import InvalidSessionTransition


@pytest.fixture
def database() -> Database:
    value = Database("sqlite://")
    value.create_schema()
    try:
        yield value
    finally:
        value.dispose()


def add_session(db_session: Session, session_id: str = "session-1") -> None:
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


def test_repository_persists_full_state_chain_and_terminal_metrics(
    database: Database,
) -> None:
    with Session(database.engine) as db_session:
        add_session(db_session)
        repository = SessionRepository(db_session)
        replay_started_at = dt.datetime(2026, 7, 28, 10, tzinfo=dt.UTC)
        completed_at = replay_started_at + dt.timedelta(seconds=12)

        assert repository.transition("session-1", "starting").status == "starting"
        running = repository.transition(
            "session-1",
            "running",
            changed_at=replay_started_at,
        )
        transcribing = repository.mark_transcribing(
            "session-1",
            provider="bailian",
            model="fun-asr-realtime",
        )
        assert repository.transition("session-1", "finalizing").status == "finalizing"
        completed = repository.complete(
            "session-1",
            final_result_count=2,
            first_partial_latency_ms=125.5,
            average_final_latency_ms=640.25,
            provider_error_count=0,
            sent_audio_chunk_count=120,
            sent_audio_bytes=384_000,
            changed_at=completed_at,
        )
        db_session.commit()

        assert running.started_at is not None
        assert running.started_at.replace(tzinfo=dt.UTC) == replay_started_at
        assert transcribing.asr_provider == "bailian"
        assert transcribing.asr_model == "fun-asr-realtime"
        assert completed.status == "completed"
        assert completed.final_result_count == 2
        assert completed.average_final_latency_ms == 640.25
        assert completed.sent_audio_bytes == 384_000
        assert completed.error_code is None
        assert completed.error_message is None
        assert completed.failure_code is None
        assert completed.failure_detail is None
        assert completed.ended_at is not None
        assert completed.ended_at.replace(tzinfo=dt.UTC) == completed_at


def test_repository_failure_is_sanitized_and_terminal(database: Database) -> None:
    with Session(database.engine) as db_session:
        add_session(db_session)
        repository = SessionRepository(db_session)

        failed = repository.fail(
            "session-1",
            error_code="asr_auth_error",
            error_message="Speech recognition authentication failed.",
        )
        db_session.commit()

        assert failed.status == "failed"
        assert failed.error_code == "asr_auth_error"
        assert failed.failure_code == "asr_auth_error"
        assert (
            failed.error_message
            == "Speech recognition authentication failed."
        )
        assert failed.ended_at is not None
        with pytest.raises(InvalidSessionTransition):
            repository.transition("session-1", "starting")


def test_repository_cancel_is_idempotent_and_does_not_invent_metrics(
    database: Database,
) -> None:
    with Session(database.engine) as db_session:
        add_session(db_session)
        repository = SessionRepository(db_session)

        cancelled = repository.cancel("session-1")
        same = repository.cancel("session-1")
        db_session.commit()

        assert same is cancelled
        assert cancelled.status == "cancelled"
        assert cancelled.error_code is None
        assert cancelled.error_message is None
        assert cancelled.stop_reason == "worker_cancelled"
        assert cancelled.final_result_count is None
        assert cancelled.provider_error_count is None


def test_repository_rejects_unknown_sessions_and_invalid_metrics(
    database: Database,
) -> None:
    with Session(database.engine) as db_session:
        add_session(db_session)
        repository = SessionRepository(db_session)

        with pytest.raises(LookupError, match="missing"):
            repository.transition("missing", "starting")
        repository.transition("session-1", "starting")
        repository.transition("session-1", "running")
        repository.mark_transcribing(
            "session-1",
            provider="bailian",
            model="fun-asr-realtime",
        )
        repository.transition("session-1", "finalizing")
        with pytest.raises(ValueError, match="non-negative"):
            repository.complete(
                "session-1",
                final_result_count=-1,
                first_partial_latency_ms=None,
                average_final_latency_ms=None,
                provider_error_count=0,
                sent_audio_chunk_count=0,
                sent_audio_bytes=0,
            )


def test_repository_tracks_source_cleanup_independently_from_terminal_session(
    database: Database,
) -> None:
    with Session(database.engine) as db_session:
        repository = SessionRepository(db_session)
        record = repository.create(
            session_id="session-hls",
            room_name="room-hls",
            source_type="hls",
            source_name="https://media.example/live.m3u8",
            language="zh-CN",
        )
        assert record.source_status == "starting"
        assert record.cleanup_status == "not_started"

        repository.mark_source_running("session-hls")
        repository.fail(
            "session-hls",
            error_code="asr_stream_error",
            error_message="Speech recognition stopped before completion.",
        )
        stopping = repository.mark_source_stopping("session-hls")
        same_stopping = repository.mark_source_stopping("session-hls")
        stopped_at = dt.datetime(2026, 8, 1, 3, 0, tzinfo=dt.UTC)
        stopped = repository.mark_source_stopped(
            "session-hls",
            cleanup_detail="decoder=stopped,track=already_unpublished",
            changed_at=stopped_at,
        )
        same_stopped = repository.mark_source_stopped(
            "session-hls",
            cleanup_detail="decoder=stopped,track=already_unpublished",
            changed_at=stopped_at,
        )
        db_session.commit()

        assert same_stopping is stopping
        assert stopped.status == "failed"
        assert same_stopped is stopped
        assert stopped.source_status == "stopped"
        assert stopped.cleanup_status == "completed"
        assert stopped.cleanup_detail == "decoder=stopped,track=already_unpublished"
        assert stopped.source_ended_at is not None
        assert stopped.source_ended_at.replace(tzinfo=dt.UTC) == stopped_at


def test_repository_records_cleanup_failure_without_overwriting_primary_error(
    database: Database,
) -> None:
    with Session(database.engine) as db_session:
        add_session(db_session, "session-failed-cleanup")
        repository = SessionRepository(db_session)
        repository.fail(
            "session-failed-cleanup",
            error_code="asr_auth_error",
            error_message="Speech recognition authentication failed.",
        )

        record = repository.mark_source_cleanup_failed(
            "session-failed-cleanup",
            cleanup_detail="internal abort request timed out",
        )
        db_session.commit()

        assert record.status == "failed"
        assert record.error_code == "asr_auth_error"
        assert record.source_status == "failed"
        assert record.cleanup_status == "failed"
        assert record.cleanup_detail == "internal abort request timed out"


def test_file_database_applies_shared_sqlite_connection_policy(tmp_path) -> None:
    database_path = tmp_path / "runtime.db"
    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        settings = database.sqlite_runtime_settings()
    finally:
        database.dispose()

    assert settings == {
        "journal_mode": "wal",
        "synchronous": 1,
        "foreign_keys": 1,
        "busy_timeout_ms": 5_000,
    }
