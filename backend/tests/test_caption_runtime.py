from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.captions.events import SessionStatus
from app.captions.models import CaptionEvent, CaptionStatus
from app.captions.runtime import WorkerCaptionRuntime
from app.transcription.errors import ASRAuthenticationError
from app.transcription.models import ASREvent, ASREventType, TranscriptionMetrics


class FakeRepository:
    def __init__(self, operations: list[object]) -> None:
        self.operations = operations
        self.record = SimpleNamespace(status="running")

    def get_required(self, _session_id: str) -> object:
        return self.record

    def start_replay(self, session_id: str) -> object:
        self.operations.append(("persist_replay", session_id))
        return object()

    def mark_source_running(self, session_id: str) -> object:
        self.operations.append(("persist_source_running", session_id))
        return object()

    def mark_source_stopped(
        self,
        session_id: str,
        *,
        cleanup_detail: str | None = None,
    ) -> object:
        self.operations.append(
            ("persist_source_stopped", session_id, cleanup_detail)
        )
        return object()

    def mark_source_cleanup_failed(
        self,
        session_id: str,
        *,
        cleanup_detail: str,
    ) -> object:
        self.operations.append(
            ("persist_source_cleanup_failed", session_id, cleanup_detail)
        )
        return object()

    def mark_transcribing(
        self,
        session_id: str,
        *,
        provider: str,
        model: str,
    ) -> object:
        self.operations.append(
            ("persist_transcribing", session_id, provider, model)
        )
        return object()

    def begin_finalizing(self, session_id: str) -> object:
        self.operations.append(("persist_finalizing", session_id))
        return object()

    def complete(self, session_id: str, **metrics: object) -> object:
        self.operations.append(("persist_completion", session_id, metrics))
        return object()

    def fail(
        self,
        session_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> object:
        self.operations.append(
            ("persist_failure", session_id, error_code, error_message)
        )
        return object()

    def cancel(self, session_id: str) -> object:
        self.operations.append(("persist_cancelled", session_id))
        return object()

    def upsert_final(
        self,
        caption: CaptionEvent,
        *,
        track_id: str,
        language: str,
    ) -> object:
        self.operations.append(
            (
                "persist_final",
                caption.segment_id,
                caption.revision,
                track_id,
                language,
            )
        )
        return object()


class FakeDatabaseSession:
    def __init__(
        self,
        operations: list[object],
        *,
        commit_error: Exception | None = None,
    ) -> None:
        self.operations = operations
        self.commit_error = commit_error
        self.closed = False

    def commit(self) -> None:
        self.operations.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.operations.append("rollback")

    def expire(self, _record: object) -> None:
        return None

    def refresh(self, _record: object) -> None:
        return None

    def close(self) -> None:
        self.operations.append("db_close")
        self.closed = True


class FakePublisher:
    def __init__(
        self,
        operations: list[object],
        *,
        caption_error: Exception | None = None,
    ) -> None:
        self.operations = operations
        self.caption_error = caption_error

    async def publish_caption(self, caption: CaptionEvent) -> None:
        self.operations.append(
            ("publish_caption", caption.segment_id, caption.revision, caption.status)
        )
        if self.caption_error is not None:
            raise self.caption_error

    async def publish_status(self, status: SessionStatus) -> None:
        self.operations.append(("publish_status", status))

    async def publish_progress(self, audio_time_ms: int) -> None:
        self.operations.append(("publish_progress", audio_time_ms))

    async def publish_metrics(self, metrics: TranscriptionMetrics) -> None:
        self.operations.append(("publish_metrics", metrics.final_result_count))

    async def publish_error(self, *, error_code: str, message: str) -> None:
        self.operations.append(("publish_error", error_code, message))


def make_runtime(
    operations: list[object],
    *,
    publisher: FakePublisher | None = None,
    db_session: FakeDatabaseSession | None = None,
    dispose: object | None = None,
    repository: FakeRepository | None = None,
) -> WorkerCaptionRuntime:
    runtime_repository = repository or FakeRepository(operations)
    return WorkerCaptionRuntime(
        session_id="session-1",
        track_id="track-1",
        language="zh-CN",
        source_type="file",
        provider_name="bailian",
        model_name="fun-asr-realtime",
        segment_repository=runtime_repository,
        session_repository=runtime_repository,
        db_session=db_session or FakeDatabaseSession(operations),
        publisher=publisher or FakePublisher(operations),
        dispose_database=dispose,
    )


def asr_event(
    event_type: ASREventType,
    event_id: str,
    *,
    text: str | None = None,
) -> ASREvent:
    return ASREvent(
        event_type=event_type,
        provider_event_id=event_id,
        segment_id="segment-1" if text is not None else None,
        text=text,
        begin_time_ms=100 if text is not None else None,
        end_time_ms=200 if text is not None else None,
        confidence=0.9 if text is not None else None,
        received_at_ms=1_750_000_000_000,
    )


def test_runtime_publishes_partial_without_persistence_and_final_after_commit() -> None:
    async def scenario() -> None:
        operations: list[object] = []
        runtime = make_runtime(operations)

        await runtime.start_replay()
        await runtime.handle_asr_event(
            asr_event(ASREventType.STREAM_STARTED, "started")
        )
        await runtime.handle_asr_event(
            asr_event(ASREventType.PARTIAL_RESULT, "partial", text="你")
        )
        await runtime.handle_asr_event(
            asr_event(ASREventType.FINAL_RESULT, "final", text="你好")
        )

        assert operations == [
            (
                "persist_replay",
                "session-1",
            ),
            ("persist_source_running", "session-1"),
            "commit",
            ("publish_status", "running"),
            (
                "persist_transcribing",
                "session-1",
                "bailian",
                "fun-asr-realtime",
            ),
            "commit",
            ("publish_status", "running"),
            ("publish_caption", "segment-1", 1, CaptionStatus.DRAFT),
            ("persist_final", "segment-1", 2, "track-1", "zh-CN"),
            "commit",
            ("publish_caption", "segment-1", 2, CaptionStatus.FINAL),
        ]

    asyncio.run(scenario())


def test_runtime_publishes_progress_then_metrics_and_completed_status() -> None:
    async def scenario() -> None:
        operations: list[object] = []
        runtime = make_runtime(operations)
        metrics = TranscriptionMetrics(
            final_result_count=3,
            first_partial_latency_ms=120.0,
            provider_error_count=0,
            sent_audio_chunk_count=12,
            sent_audio_bytes=38_400,
            _final_latencies_ms=[640.0, 760.0],
        )

        await runtime.start_replay()
        await runtime.handle_asr_event(
            asr_event(ASREventType.STREAM_STARTED, "started")
        )
        await runtime.publish_progress(500)
        await runtime.begin_finalizing()
        await runtime.complete(metrics)

        assert operations == [
            ("persist_replay", "session-1"),
            ("persist_source_running", "session-1"),
            "commit",
            ("publish_status", "running"),
            (
                "persist_transcribing",
                "session-1",
                "bailian",
                "fun-asr-realtime",
            ),
            "commit",
            ("publish_status", "running"),
            ("publish_progress", 500),
            ("persist_finalizing", "session-1"),
            "commit",
            ("publish_status", "finalizing"),
            (
                "persist_completion",
                "session-1",
                {
                    "final_result_count": 3,
                    "first_partial_latency_ms": 120.0,
                    "average_final_latency_ms": 700.0,
                    "provider_error_count": 0,
                    "sent_audio_chunk_count": 12,
                    "sent_audio_bytes": 38_400,
                },
            ),
            "commit",
            ("publish_metrics", 3),
            ("publish_status", "completed"),
        ]

    asyncio.run(scenario())


def test_runtime_failure_is_durable_visible_and_redacts_internal_message() -> None:
    async def scenario() -> None:
        operations: list[object] = []
        runtime = make_runtime(operations)

        await runtime.fail(
            ASRAuthenticationError("invalid api key sk-private-value")
        )

        assert operations[:2] == [
            (
                "persist_failure",
                "session-1",
                "asr_auth_error",
                "Speech recognition authentication failed.",
            ),
            "commit",
        ]
        assert operations[2][0:2] == ("publish_error", "asr_auth_error")
        assert operations[2][2] == "Speech recognition authentication failed."
        assert "sk-private-value" not in repr(operations)
        assert operations[3] == ("publish_status", "failed")

    asyncio.run(scenario())


def test_runtime_cancellation_is_durable_visible_and_idempotent() -> None:
    async def scenario() -> None:
        operations: list[object] = []
        runtime = make_runtime(operations)

        await runtime.cancel()
        await runtime.cancel()

        assert operations == [
            ("persist_cancelled", "session-1"),
            "commit",
            ("publish_status", "cancelled"),
        ]

    asyncio.run(scenario())


def test_runtime_keeps_database_and_publisher_failures_explicit() -> None:
    async def scenario() -> None:
        db_operations: list[object] = []
        db_runtime = make_runtime(
            db_operations,
            db_session=FakeDatabaseSession(
                db_operations,
                commit_error=RuntimeError("sqlite unavailable"),
            ),
        )
        with pytest.raises(RuntimeError, match="sqlite unavailable"):
            await db_runtime.handle_asr_event(
                asr_event(ASREventType.STREAM_STARTED, "started")
            )
        assert db_operations[-1] == "rollback"

        publish_operations: list[object] = []
        publish_runtime = make_runtime(
            publish_operations,
            publisher=FakePublisher(
                publish_operations,
                caption_error=RuntimeError("livekit unavailable"),
            ),
        )
        with pytest.raises(RuntimeError, match="livekit unavailable"):
            await publish_runtime.handle_asr_event(
                asr_event(ASREventType.FINAL_RESULT, "final", text="你好")
            )
        assert publish_operations[:2] == [
            ("persist_final", "segment-1", 1, "track-1", "zh-CN"),
            "commit",
        ]

    asyncio.run(scenario())


def test_runtime_close_is_idempotent_and_disposes_owned_database() -> None:
    async def scenario() -> None:
        operations: list[object] = []
        db_session = FakeDatabaseSession(operations)

        def dispose() -> None:
            operations.append("db_dispose")

        runtime = make_runtime(
            operations,
            db_session=db_session,
            dispose=dispose,
        )
        await runtime.complete_source_cleanup()
        await runtime.aclose()
        await runtime.aclose()

        assert operations == [
            (
                "persist_source_stopped",
                "session-1",
                "Worker audio resources released.",
            ),
            "commit",
            "db_close",
            "db_dispose",
        ]
        assert db_session.closed is True

    asyncio.run(scenario())


def test_runtime_preserves_a_terminal_state_written_by_another_process() -> None:
    async def scenario() -> None:
        operations: list[object] = []
        repository = FakeRepository(operations)
        runtime = make_runtime(operations, repository=repository)
        repository.record.status = "failed"
        metrics = TranscriptionMetrics(
            final_result_count=0,
            provider_error_count=0,
            sent_audio_chunk_count=1,
            sent_audio_bytes=640,
        )

        await runtime.begin_finalizing()
        await runtime.complete(metrics)

        assert operations == []
        assert repository.record.status == "failed"

    asyncio.run(scenario())
