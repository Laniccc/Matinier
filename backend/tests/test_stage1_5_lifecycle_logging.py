from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.captions.events import LIVE_CAPTION_TOPIC
from app.captions.runtime import create_worker_caption_runtime
from app.hls.decoder import HLSDecodeError
from app.hls.manager import HLSInputManager, HLSStartRequest
from app.logging import JsonFormatter, STABLE_CONTEXT_FIELDS
from app.persistence.database import Database
from app.persistence.models import SessionRecord
from app.persistence.sessions import SessionRepository
from app.transcription.fake import FakeASRConfig, FakeASRProvider
from app.transcription.session import TranscriptionSession
from app.translation.fake import FakeTranslationConfig, FakeTranslationProvider
from app.translation.runtime import create_worker_translation_runtime
from app.translation.session import TranslationSession
from app.worker.entrypoint import consume_replay_audio


class _Frame:
    sample_rate = 16_000
    num_channels = 1
    samples_per_channel = 320
    data = b"\x01\x00" * 320


class _AudioEvent:
    frame = _Frame()


class _Stream:
    def __init__(self, frame_count: int = 15) -> None:
        self._events = iter(_AudioEvent() for _ in range(frame_count))

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration as error:
            raise StopAsyncIteration from error

    async def aclose(self) -> None:
        return None


class _Participant:
    async def publish_data(
        self,
        _data: bytes,
        *,
        reliable: bool,
        topic: str,
    ) -> None:
        assert reliable is True
        assert topic == LIVE_CAPTION_TOPIC


class _FailingHLSSource:
    cleanup_errors: tuple[str, ...] = ()
    cleanup_detail = "decoder=stopped,track=not_published,room=stopped"

    async def run(self, _url: str, _token: str, *, on_source_failed) -> None:
        error = HLSDecodeError("injected upstream failure")
        await on_source_failed(error)
        raise error

    async def aclose(self) -> None:
        return None


def _formatted_records(
    records: list[logging.LogRecord],
    session_id: str,
) -> list[dict[str, object]]:
    formatter = JsonFormatter()
    return [
        json.loads(formatter.format(record))
        for record in records
        if getattr(record, "session_id", None) == session_id
    ]


def _assert_event_order(
    records: list[dict[str, object]],
    expected: list[str],
) -> None:
    events = [str(record["event"]) for record in records]
    cursor = 0
    for event in expected:
        cursor = events.index(event, cursor) + 1


async def _run_caption_scenario(
    database_url: str,
    *,
    session_id: str,
    translation_failure: bool,
) -> SessionRecord:
    database = Database(database_url)
    database.create_schema()
    with Session(database.engine) as db_session:
        SessionRepository(db_session).create(
            session_id=session_id,
            room_name=f"room-{session_id}",
            source_type="file",
            source_name="fake.pcm",
            language="zh-CN",
            target_language="en-US",
        )
        db_session.commit()

    participant = _Participant()
    caption_runtime = create_worker_caption_runtime(
        session_id=session_id,
        track_id=f"track-{session_id}",
        local_participant=participant,
        database_url=database_url,
        provider_name="fake",
        model_name="fake-asr-v1",
    )
    translation_runtime = create_worker_translation_runtime(
        session_id=session_id,
        database_url=database_url,
        provider_name="fake",
        model_name="fake-translation-v1",
        local_participant=participant,
        log_context={
            "room_name": f"room-{session_id}",
            "participant_identity": f"replay-{session_id}",
            "track_sid": f"track-{session_id}",
            "source_type": "file",
        },
    )
    assert translation_runtime is not None

    def transcription_factory(**kwargs) -> TranscriptionSession:
        return TranscriptionSession(
            provider_factory=lambda: FakeASRProvider(FakeASRConfig()),
            queue_max_chunks=5,
            event_handler=kwargs["event_handler"],
        )

    def translation_factory(**kwargs) -> TranslationSession:
        return TranslationSession(
            provider_factory=lambda: FakeTranslationProvider(
                FakeTranslationConfig(
                    target_language=kwargs["target_language"],
                    fail_after_chunks=2 if translation_failure else None,
                )
            ),
            queue_max_chunks=5,
            event_handler=kwargs["event_handler"],
        )

    await consume_replay_audio(
        track="track",
        track_sid=f"track-{session_id}",
        room_name=f"room-{session_id}",
        participant_identity=f"replay-{session_id}",
        session_id=session_id,
        stream_factory=lambda *args, **kwargs: _Stream(),
        transcription_session_factory=transcription_factory,
        caption_runtime=caption_runtime,
        translation_session_factory=translation_factory,
        translation_runtime=translation_runtime,
    )
    with Session(database.engine) as db_session:
        record = db_session.get(SessionRecord, session_id)
        assert record is not None
        db_session.expunge(record)
    database.dispose()
    return record


def test_required_lifecycle_logs_reconstruct_three_d4_scenarios(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)

    success_id = "d4-success"
    success_record = asyncio.run(
        _run_caption_scenario(
            f"sqlite:///{(tmp_path / 'success.db').as_posix()}",
            session_id=success_id,
            translation_failure=False,
        )
    )
    success_logs = _formatted_records(caplog.records, success_id)
    assert success_record.status == "completed"
    assert success_record.translation_status == "completed"
    _assert_event_order(
        success_logs,
        [
            "session_running",
            "asr_connected",
            "first_partial",
            "session_finalizing",
            "final_persisted",
            "final_published",
            "session_completed",
            "worker_cleanup_completed",
        ],
    )

    degraded_id = "d4-translation-degraded"
    degraded_record = asyncio.run(
        _run_caption_scenario(
            f"sqlite:///{(tmp_path / 'degraded.db').as_posix()}",
            session_id=degraded_id,
            translation_failure=True,
        )
    )
    degraded_logs = _formatted_records(caplog.records, degraded_id)
    degraded_events = [record["event"] for record in degraded_logs]
    assert degraded_record.status == "completed"
    assert degraded_record.translation_status == "failed"
    assert "translation_failed" in degraded_events
    assert "session_completed" in degraded_events
    assert "session_failed" not in degraded_events
    _assert_event_order(
        degraded_logs,
        [
            "translation_connected",
            "translation_failed",
            "session_completed",
            "worker_cleanup_completed",
        ],
    )

    source_failure_id = "d4-source-failed"

    async def source_failure_scenario() -> None:
        manager = HLSInputManager(
            livekit_url="ws://livekit.test",
            token_factory=lambda _request: "local-test-token",
            source_factory=lambda _request: _FailingHLSSource(),
        )
        await manager.start(
            HLSStartRequest(
                session_id=source_failure_id,
                room_id="room-d4-source-failed",
                room_name="room-d4-source-failed",
                fetch_url="https://media.example/live.m3u8?token=private",
            )
        )
        await asyncio.sleep(0)
        await manager.aclose()

    asyncio.run(source_failure_scenario())
    source_logs = _formatted_records(caplog.records, source_failure_id)
    _assert_event_order(
        source_logs,
        [
            "source_started",
            "source_failed",
            "session_failed",
            "source_stopped",
        ],
    )
    source_terminal = source_logs[-1]
    assert source_terminal["failure_code"] == "hls_stream_error"
    assert source_terminal["cleanup_status"] == "completed"

    for record in [*success_logs, *degraded_logs, *source_logs]:
        assert set(STABLE_CONTEXT_FIELDS).issubset(record)
