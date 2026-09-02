from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.captions.models import CaptionEvent  # noqa: E402
from app.captions.runtime import WorkerCaptionRuntime  # noqa: E402
from app.main import create_app  # noqa: E402
from app.persistence.database import Database  # noqa: E402
from app.persistence.models import SegmentRecord  # noqa: E402
from app.persistence.segments import SegmentRepository  # noqa: E402
from app.persistence.sessions import SessionRepository  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.transcription.models import (  # noqa: E402
    ASREvent,
    ASREventType,
    TranscriptionMetrics,
)
from app.transcription.provider import SpeechRecognitionProvider  # noqa: E402
from app.transcription.session import TranscriptionSession  # noqa: E402


_EVENTS_DONE = object()


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class FakeASRProvider(SpeechRecognitionProvider):
    def __init__(self, *, final_text: str) -> None:
        self.final_text = final_text
        self.audio_chunks: list[bytes] = []
        self.closed = False
        self._events: asyncio.Queue[ASREvent | object] = asyncio.Queue()

    async def start(self) -> None:
        await self._events.put(
            ASREvent(
                event_type=ASREventType.STREAM_STARTED,
                provider_event_id="fake-started",
            )
        )

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_chunks.append(pcm)
        partials = ("实时", "实时字幕")
        if len(self.audio_chunks) <= len(partials):
            await self._events.put(
                ASREvent(
                    event_type=ASREventType.PARTIAL_RESULT,
                    provider_event_id=f"fake-partial-{len(self.audio_chunks)}",
                    segment_id="segment-1",
                    text=partials[len(self.audio_chunks) - 1],
                    begin_time_ms=0,
                    end_time_ms=len(self.audio_chunks) * 100,
                    confidence=0.8,
                    received_at_ms=1_750_000_000_000
                    + len(self.audio_chunks),
                )
            )

    async def finish(self) -> None:
        await self._events.put(
            ASREvent(
                event_type=ASREventType.FINAL_RESULT,
                provider_event_id="fake-final",
                segment_id="segment-1",
                text=self.final_text,
                begin_time_ms=0,
                end_time_ms=200,
                confidence=0.95,
                received_at_ms=1_750_000_000_003,
            )
        )
        await self._events.put(
            ASREvent(
                event_type=ASREventType.STREAM_COMPLETED,
                provider_event_id="fake-completed",
            )
        )
        await self._events.put(_EVENTS_DONE)

    async def events(self) -> AsyncIterator[ASREvent]:
        while True:
            item = await self._events.get()
            if item is _EVENTS_DONE:
                return
            assert isinstance(item, ASREvent)
            yield item

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._events.put(_EVENTS_DONE)


class VerificationPublisher:
    def __init__(self) -> None:
        self.captions: list[CaptionEvent] = []
        self.statuses: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self.asr_running = asyncio.Event()

    async def publish_caption(self, caption: CaptionEvent) -> None:
        self.captions.append(caption)

    async def publish_status(self, status: str) -> None:
        self.statuses.append(status)
        if status == "running" and self.statuses.count("running") >= 2:
            self.asr_running.set()

    async def publish_progress(self, audio_time_ms: int) -> None:
        del audio_time_ms

    async def publish_metrics(self, metrics: TranscriptionMetrics) -> None:
        del metrics

    async def publish_error(self, *, error_code: str, message: str) -> None:
        self.errors.append((error_code, message))


def verifier_settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="stage6-local-key",
        livekit_api_secret="stage6-local-secret-that-is-long-enough",
        livekit_room_name="stage6-local",
        database_url=database_url,
        log_level="ERROR",
    )


async def run_pipeline(
    database: Database,
    *,
    session_id: str,
    final_text: str,
) -> tuple[VerificationPublisher, FakeASRProvider, bool]:
    db_session = Session(database.engine, expire_on_commit=False)
    session_repository = SessionRepository(db_session)
    session_repository.create(
        session_id=session_id,
        room_name=f"room-{session_id}",
        source_type="file",
        source_name=f"{session_id}.wav",
        language="zh-CN",
    )
    db_session.commit()

    publisher = VerificationPublisher()
    provider = FakeASRProvider(final_text=final_text)
    runtime = WorkerCaptionRuntime(
        session_id=session_id,
        track_id=f"track-{session_id}",
        language="zh-CN",
        source_type="file",
        provider_name="fake",
        model_name="fake-asr-stage6",
        segment_repository=SegmentRepository(db_session),
        session_repository=session_repository,
        db_session=db_session,
        publisher=publisher,
    )
    transcription = TranscriptionSession(
        provider_factory=lambda: provider,
        queue_max_chunks=4,
        chunk_duration_ms=100,
        startup_retries=0,
        event_handler=runtime.handle_asr_event,
        log_context={"session_id": session_id},
    )
    try:
        await runtime.start_replay()
        await transcription.start()
        for _ in range(10):
            transcription.send_frame(b"\x00\x00" * 320)
        await asyncio.wait_for(publisher.asr_running.wait(), timeout=1.0)
        await runtime.begin_finalizing()
        metrics = await transcription.finish()
        await runtime.complete(metrics)
    finally:
        await transcription.aclose()
        await runtime.aclose()

    await asyncio.sleep(0)
    active_names = {
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    }
    clean = (
        provider.closed
        and not publisher.errors
        and not any(name.startswith("transcription-") for name in active_names)
    )
    return publisher, provider, clean


async def verify() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="livecaption-stage6-") as temp_dir:
        database_path = Path(temp_dir) / "stage6.db"
        database_url = f"sqlite:///{database_path.as_posix()}"
        database = Database(database_url)
        database.create_schema()

        first_text = "实时字幕集成验证完成"
        second_text = "第二次运行保持隔离"
        first, first_provider, first_clean = await run_pipeline(
            database,
            session_id="stage6-local-1",
            final_text=first_text,
        )
        second, second_provider, second_clean = await run_pipeline(
            database,
            session_id="stage6-local-2",
            final_text=second_text,
        )

        app = create_app(
            settings=verifier_settings(database_url),
            database=database,
        )
        with TestClient(app) as client:
            first_export_response = client.get(
                "/api/sessions/stage6-local-1/export",
                params={"format": "json"},
            )
            second_export_response = client.get(
                "/api/sessions/stage6-local-2/export",
                params={"format": "json"},
            )
            ensure(first_export_response.status_code == 200, "first export failed")
            ensure(second_export_response.status_code == 200, "second export failed")
            first_export = first_export_response.json()
            second_export = second_export_response.json()

        with Session(database.engine) as db_session:
            durable_count = int(
                db_session.scalar(
                    select(func.count()).select_from(SegmentRecord)
                )
                or 0
            )
            draft_count = int(
                db_session.scalar(
                    select(func.count())
                    .select_from(SegmentRecord)
                    .where(SegmentRecord.status != "final")
                )
                or 0
            )

        expected_states = [
            "running",
            "running",
            "finalizing",
            "completed",
        ]
        first_revisions = [caption.revision for caption in first.captions]
        second_revisions = [caption.revision for caption in second.captions]
        isolated = (
            first_export["timeline"]["segment_count"] == 1
            and second_export["timeline"]["segment_count"] == 1
            and first_export["segments"][0]["display_text"] == first_text
            and second_export["segments"][0]["display_text"] == second_text
            and first_export["session"]["id"] != second_export["session"]["id"]
        )
        success = all(
            (
                first.statuses == expected_states,
                second.statuses == expected_states,
                first_revisions == [1, 2, 3],
                second_revisions == [1, 2, 3],
                durable_count == 2,
                draft_count == 0,
                isolated,
                first_clean,
                second_clean,
                first_provider.closed,
                second_provider.closed,
            )
        )
        result = {
            "status": "ok" if success else "failed",
            "session_count": 2,
            "state_sequence": expected_states,
            "published_revisions": first_revisions,
            "durable_final_count": durable_count,
            "draft_row_count": draft_count,
            "json_export_segment_counts": [
                first_export["timeline"]["segment_count"],
                second_export["timeline"]["segment_count"],
            ],
            "runs_isolated": isolated,
            "providers_closed": first_provider.closed and second_provider.closed,
            "clean_shutdown": first_clean and second_clean,
        }
        database.dispose()
        return result


def main() -> None:
    result = asyncio.run(verify())
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
