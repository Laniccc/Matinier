from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.captions.models import CaptionEvent, CaptionStatus  # noqa: E402
from app.captions.runtime import WorkerCaptionRuntime  # noqa: E402
from app.main import create_app  # noqa: E402
from app.persistence.database import Database  # noqa: E402
from app.persistence.models import SessionRecord  # noqa: E402
from app.persistence.segments import SegmentRepository  # noqa: E402
from app.persistence.sessions import SessionRepository  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.transcription.models import (  # noqa: E402
    ASREvent,
    ASREventType,
    TranscriptionMetrics,
)


class VerificationPublisher:
    def __init__(self) -> None:
        self.captions: list[CaptionEvent] = []
        self.statuses: list[str] = []
        self.progress: list[int] = []
        self.metrics: list[dict[str, int | float | None]] = []
        self.errors: list[dict[str, str]] = []

    async def publish_caption(self, caption: CaptionEvent) -> None:
        self.captions.append(caption)

    async def publish_status(self, status: str) -> None:
        self.statuses.append(status)

    async def publish_progress(self, audio_time_ms: int) -> None:
        self.progress.append(audio_time_ms)

    async def publish_metrics(self, metrics: TranscriptionMetrics) -> None:
        self.metrics.append(metrics.log_fields())

    async def publish_error(self, *, error_code: str, message: str) -> None:
        self.errors.append({"error_code": error_code, "message": message})


def event(
    event_type: ASREventType,
    event_id: str,
    *,
    text: str | None = None,
    end_time_ms: int | None = None,
) -> ASREvent:
    return ASREvent(
        event_type=event_type,
        provider_event_id=event_id,
        segment_id="segment-1" if text is not None else None,
        text=text,
        begin_time_ms=0 if text is not None else None,
        end_time_ms=end_time_ms,
        confidence=0.96 if text is not None else None,
        received_at_ms=1_750_000_000_000,
    )


def verifier_settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="stage3-local-key",
        livekit_api_secret="stage3-local-secret-that-is-at-least-32-bytes",
        livekit_room_name="stage3-local",
        database_url=database_url,
        log_level="ERROR",
    )


async def verify() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="livecaption-stage3-") as temp_dir:
        database_path = Path(temp_dir) / "stage3.db"
        database_url = f"sqlite:///{database_path.as_posix()}"
        database = Database(database_url)
        database.create_schema()
        session_id = "stage3-local-session"
        db_session = Session(database.engine, expire_on_commit=False)
        db_session.add(
            SessionRecord(
                id=session_id,
                room_name="stage3-local-room",
                status="created",
                source_type="file",
                source_name="chinese-speech.wav",
                language="zh-CN",
            )
        )
        db_session.commit()

        publisher = VerificationPublisher()
        dispose_count = 0

        def dispose_database() -> None:
            nonlocal dispose_count
            dispose_count += 1
            database.dispose()

        runtime = WorkerCaptionRuntime(
            session_id=session_id,
            track_id="replay-audio",
            language="zh-CN",
            source_type="file",
            provider_name="fake",
            model_name="stage3-local",
            segment_repository=SegmentRepository(db_session),
            session_repository=SessionRepository(db_session),
            db_session=db_session,
            publisher=publisher,
            dispose_database=dispose_database,
        )
        await runtime.start_replay()
        await runtime.handle_asr_event(
            event(ASREventType.STREAM_STARTED, "started")
        )
        await runtime.handle_asr_event(
            event(
                ASREventType.PARTIAL_RESULT,
                "partial-1",
                text="实时字幕",
                end_time_ms=300,
            )
        )
        await runtime.handle_asr_event(
            event(
                ASREventType.PARTIAL_RESULT,
                "partial-2",
                text="实时字幕修订",
                end_time_ms=600,
            )
        )
        await runtime.handle_asr_event(
            event(
                ASREventType.FINAL_RESULT,
                "final-1",
                text="实时字幕修订完成",
                end_time_ms=900,
            )
        )
        await runtime.publish_progress(900)
        metrics = TranscriptionMetrics(
            final_result_count=1,
            first_partial_latency_ms=25.0,
            sent_audio_chunk_count=9,
            sent_audio_bytes=28_800,
        )
        metrics._final_latencies_ms.append(75.0)
        await runtime.begin_finalizing()
        await runtime.complete(metrics)
        await runtime.aclose()
        await runtime.aclose()

        recovery_database = Database(database_url)
        settings = verifier_settings(database_url)
        with TestClient(
            create_app(settings=settings, database=recovery_database)
        ) as client:
            session_response = client.get(f"/api/sessions/{session_id}")
            segments_response = client.get(
                f"/api/sessions/{session_id}/segments"
            )
            session_response.raise_for_status()
            segments_response.raise_for_status()
            recovered_session = session_response.json()
            recovered_segments = segments_response.json()

        draft_captions = [
            caption for caption in publisher.captions
            if caption.status is CaptionStatus.DRAFT
        ]
        final_captions = [
            caption for caption in publisher.captions
            if caption.status is CaptionStatus.FINAL
        ]
        partial_replaced = (
            [caption.text for caption in draft_captions]
            == ["实时字幕", "实时字幕修订"]
            and [caption.revision for caption in draft_captions] == [1, 2]
        )
        one_durable_final = (
            len(final_captions) == 1
            and final_captions[0].revision == 3
            and len(recovered_segments) == 1
            and recovered_segments[0]["display_text"] == "实时字幕修订完成"
        )
        completed = (
            publisher.statuses
            == ["running", "running", "finalizing", "completed"]
            and recovered_session["status"] == "completed"
        )
        metrics_published = publisher.metrics == [metrics.log_fields()]
        clean_shutdown = dispose_count == 1 and not publisher.errors
        success = all(
            (
                partial_replaced,
                one_durable_final,
                completed,
                metrics_published,
                clean_shutdown,
            )
        )
        return {
            "status": "ok" if success else "failed",
            "partial_replaced": partial_replaced,
            "published_revisions": [
                caption.revision for caption in publisher.captions
            ],
            "durable_final_count": len(recovered_segments),
            "snapshot_recovered": one_durable_final,
            "session_status": recovered_session["status"],
            "progress_ms": publisher.progress[-1] if publisher.progress else None,
            "metrics_published": metrics_published,
            "error_count": len(publisher.errors),
            "clean_shutdown": clean_shutdown,
        }


def main() -> None:
    result = asyncio.run(verify())
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
