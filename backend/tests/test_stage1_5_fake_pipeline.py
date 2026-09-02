from __future__ import annotations

import asyncio
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.captions.events import LIVE_CAPTION_TOPIC
from app.captions.runtime import create_worker_caption_runtime
from app.persistence.database import Database
from app.persistence.models import (
    SegmentRecord,
    SessionRecord,
    TranslationSegmentRecord,
)
from app.transcription.fake import FakeASRConfig, FakeASRProvider
from app.transcription.session import TranscriptionSession
from app.translation.fake import (
    FakeTranslationConfig,
    FakeTranslationProvider,
)
from app.translation.runtime import create_worker_translation_runtime
from app.translation.session import TranslationSession
from app.worker.entrypoint import consume_replay_audio


class Frame:
    sample_rate = 16_000
    num_channels = 1
    samples_per_channel = 320
    data = b"\x01\x00" * 320


class Event:
    frame = Frame()


class Stream:
    def __init__(self, frame_count: int) -> None:
        self._events = iter(Event() for _ in range(frame_count))
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration as error:
            raise StopAsyncIteration from error

    async def aclose(self) -> None:
        self.closed = True


class RecordingParticipant:
    def __init__(self) -> None:
        self.packets: list[dict] = []

    async def publish_data(
        self,
        data: bytes,
        *,
        reliable: bool,
        topic: str,
    ) -> None:
        assert reliable is True
        assert topic == LIVE_CAPTION_TOPIC
        self.packets.append(json.loads(data))


def test_fake_pipeline_uses_reconcilers_sqlite_and_livekit_events(tmp_path) -> None:
    database_path = tmp_path / "fake-pipeline.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    database = Database(database_url)
    database.create_schema()
    session_id = "stage1-5-fake-pipeline"
    with Session(database.engine) as db_session:
        db_session.add(
            SessionRecord(
                id=session_id,
                room_name="fake-room",
                status="created",
                source_type="file",
                source_name="fake.pcm",
                language="zh-CN",
                target_language="en-US",
            )
        )
        db_session.commit()

    participant = RecordingParticipant()
    stream = Stream(frame_count=15)

    async def scenario() -> None:
        caption_runtime = create_worker_caption_runtime(
            session_id=session_id,
            track_id="fake-track",
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
        )
        assert translation_runtime is not None

        def transcription_factory(**kwargs) -> TranscriptionSession:
            return TranscriptionSession(
                provider_factory=lambda: FakeASRProvider(
                    FakeASRConfig(
                        emit_duplicate_final=True,
                        emit_stale_partial=True,
                    )
                ),
                queue_max_chunks=5,
                event_handler=kwargs["event_handler"],
            )

        def translation_factory(**kwargs) -> TranslationSession:
            return TranslationSession(
                provider_factory=lambda: FakeTranslationProvider(
                    FakeTranslationConfig(
                        target_language=kwargs["target_language"],
                        emit_duplicate_final=True,
                        emit_stale_partial=True,
                    )
                ),
                queue_max_chunks=5,
                event_handler=kwargs["event_handler"],
            )

        stats = await consume_replay_audio(
            track="track",
            room_name="fake-room",
            participant_identity="replay-stage1-5-fake-pipeline",
            session_id=session_id,
            stream_factory=lambda *args, **kwargs: stream,
            transcription_session_factory=transcription_factory,
            caption_runtime=caption_runtime,
            translation_session_factory=translation_factory,
            translation_runtime=translation_runtime,
        )
        assert stats.frame_count == 15

    try:
        asyncio.run(scenario())
        with Session(database.engine) as db_session:
            source_rows = list(db_session.scalars(select(SegmentRecord)))
            translation_rows = list(
                db_session.scalars(select(TranslationSegmentRecord))
            )
            session = db_session.get(SessionRecord, session_id)

        assert stream.closed is True
        assert len(source_rows) == 1
        assert source_rows[0].revision == 3
        assert source_rows[0].raw_text == "fake transcript 1 v3"
        assert len(translation_rows) == 1
        assert translation_rows[0].revision == 3
        assert translation_rows[0].text == "fake translation 1 v3"
        assert translation_rows[0].source_segment_ids == [
            source_rows[0].segment_id
        ]
        assert session is not None
        assert session.status == "completed"
        assert session.translation_status == "completed"

        source_events = [
            packet
            for packet in participant.packets
            if packet["type"] == "caption.upsert"
        ]
        translation_events = [
            packet
            for packet in participant.packets
            if packet["type"] == "translation.upsert"
        ]
        assert [event["payload"]["status"] for event in source_events] == [
            "draft",
            "draft",
            "final",
        ]
        assert [
            event["payload"]["status"] for event in translation_events
        ] == ["draft", "final"]
    finally:
        database.dispose()
