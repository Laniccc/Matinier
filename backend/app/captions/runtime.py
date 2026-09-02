from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.errors import public_error_for
from app.captions.publisher import (
    CaptionEventPublisher,
    LiveKitCaptionEventPublisher,
)
from app.captions.reconciler import TranscriptReconciler
from app.persistence.database import Database
from app.persistence.models import SessionRecord
from app.persistence.segments import SegmentRepository
from app.persistence.sessions import SessionRepository
from app.persistence.translations import TranslationSegmentRepository
from app.sessions.state import TERMINAL_SESSION_STATES
from app.transcription.errors import ASRProviderError
from app.transcription.models import ASREvent, ASREventType, TranscriptionMetrics


logger = logging.getLogger(__name__)


class WorkerCaptionRuntime:
    """Durable caption state and reliable live events for one replay track."""

    def __init__(
        self,
        *,
        session_id: str,
        track_id: str,
        language: str,
        source_type: str,
        provider_name: str,
        model_name: str,
        segment_repository: SegmentRepository,
        session_repository: SessionRepository,
        db_session: Session,
        publisher: CaptionEventPublisher,
        translation_repository: TranslationSegmentRepository | None = None,
        dispose_database: Callable[[], None] | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        if not track_id:
            raise ValueError("track_id is required")
        if not language:
            raise ValueError("language is required")
        if not source_type:
            raise ValueError("source_type is required")
        if not provider_name:
            raise ValueError("provider_name is required")
        if not model_name:
            raise ValueError("model_name is required")
        self._session_id = session_id
        self._track_id = track_id
        self._language = language
        self._source_type = source_type
        self._provider_name = provider_name
        self._model_name = model_name
        self._segment_repository = segment_repository
        self._session_repository = session_repository
        self._db_session = db_session
        self._publisher = publisher
        self._translation_repository = translation_repository
        self._dispose_database = dispose_database
        self._reconciler = TranscriptReconciler(session_id=session_id)
        self._terminal = False
        self._closed = False
        self._source_cleanup_recorded = False
        self._first_partial_logged = False
        self._started_at_ms = time.monotonic() * 1_000

    @property
    def source_language(self) -> str:
        return self._language

    @property
    def source_type(self) -> str:
        return self._source_type

    async def start_replay(self) -> None:
        if self._terminal:
            return
        self._persist_replay_started()
        await self._publisher.publish_status("running")
        logger.info(
            "caption session running",
            extra=self._extra("session_running", status="running"),
        )

    async def handle_asr_event(self, event: ASREvent) -> None:
        if self._terminal:
            return
        if event.event_type is ASREventType.STREAM_STARTED:
            self._persist_transcribing()
            await self._publisher.publish_status("running")
            logger.info(
                "ASR connected",
                extra=self._extra(
                    "asr_connected",
                    status="running",
                    provider_request_id=event.provider_event_id,
                ),
            )
            return
        if event.event_type is ASREventType.STREAM_ERROR:
            await self.fail(ASRProviderError("provider reported a stream error"))
            return

        caption = self._reconciler.consume(event)
        if caption is None:
            return
        if not caption.is_final and not self._first_partial_logged:
            self._first_partial_logged = True
            logger.info(
                "first ASR partial received",
                extra=self._extra(
                    "first_partial",
                    status="running",
                    provider_request_id=caption.provider_event_id,
                    segment_id=caption.segment_id,
                    revision=caption.revision,
                    text_length=len(caption.text),
                ),
            )
        if caption.is_final:
            self._persist_final(caption)
            logger.info(
                "caption final persisted",
                extra=self._extra(
                    "final_persisted",
                    status="running",
                    provider_request_id=caption.provider_event_id,
                    segment_id=caption.segment_id,
                    revision=caption.revision,
                    text_length=len(caption.text),
                ),
            )
        await self._publisher.publish_caption(caption)
        if caption.is_final:
            logger.info(
                "caption final published",
                extra=self._extra(
                    "final_published",
                    status="running",
                    provider_request_id=caption.provider_event_id,
                    segment_id=caption.segment_id,
                    revision=caption.revision,
                    text_length=len(caption.text),
                ),
            )

    async def publish_progress(self, audio_time_ms: int) -> None:
        if self._terminal:
            return
        await self._publisher.publish_progress(audio_time_ms)

    async def complete(self, metrics: TranscriptionMetrics) -> None:
        if self._terminal:
            return
        if self._refresh_external_terminal_state():
            return
        self._persist_completion(metrics)
        self._terminal = True
        logger.info(
            "caption session completed",
            extra=self._extra(
                "session_completed",
                status="completed",
                **metrics.log_fields(),
            ),
        )
        await self._publisher.publish_metrics(metrics)
        await self._publisher.publish_status("completed")

    async def begin_finalizing(self) -> None:
        if self._terminal:
            return
        if self._refresh_external_terminal_state():
            return
        self._persist_finalizing()
        logger.info(
            "caption session finalizing",
            extra=self._extra("session_finalizing", status="finalizing"),
        )
        await self._publisher.publish_status("finalizing")

    async def fail(self, error: BaseException) -> None:
        if self._terminal:
            return
        error_code, message = self._public_error(error)
        self._persist_failure(error_code=error_code, message=message)
        logger.error(
            "caption session failed",
            extra=self._extra(
                "session_failed",
                status="failed",
                failure_code=error_code,
                error_type=type(error).__name__,
            ),
        )
        await self._publisher.publish_error(
            error_code=error_code,
            message=message,
        )
        await self._publisher.publish_status("failed")
        self._terminal = True

    async def cancel(self) -> None:
        if self._terminal:
            return
        self._persist_cancellation()
        logger.info(
            "caption session cancelled",
            extra=self._extra(
                "session_cancelled",
                status="cancelled",
                stop_reason="worker_cancelled",
            ),
        )
        await self._publisher.publish_status("cancelled")
        self._terminal = True

    async def complete_source_cleanup(self) -> None:
        """Persist Worker-owned source cleanup after local resources close."""

        if self._source_type == "hls" or self._source_cleanup_recorded:
            return
        try:
            self._session_repository.mark_source_stopped(
                self._session_id,
                cleanup_detail="Worker audio resources released.",
            )
            self._db_session.commit()
        except BaseException:
            self._db_session.rollback()
            raise
        self._source_cleanup_recorded = True

    async def fail_source_cleanup(self, error: BaseException) -> None:
        """Persist a Worker-owned source cleanup failure without changing run state."""

        if self._source_type == "hls" or self._source_cleanup_recorded:
            return
        try:
            self._session_repository.mark_source_cleanup_failed(
                self._session_id,
                cleanup_detail=(
                    "Worker audio resource cleanup failed "
                    f"({type(error).__name__})."
                ),
            )
            self._db_session.commit()
        except BaseException:
            self._db_session.rollback()
            raise
        self._source_cleanup_recorded = True

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._db_session.close()
        finally:
            if self._dispose_database is not None:
                self._dispose_database()

    def _persist_final(self, caption: Any) -> None:
        try:
            self._segment_repository.upsert_final(
                caption,
                track_id=self._track_id,
                language=self._language,
            )
            if self._translation_repository is not None:
                self._translation_repository.realign_session(
                    self._session_id
                )
            self._db_session.commit()
        except BaseException:
            self._db_session.rollback()
            raise

    def _persist_replay_started(self) -> None:
        try:
            self._session_repository.start_replay(self._session_id)
            if self._source_type != "hls":
                self._session_repository.mark_source_running(self._session_id)
            self._db_session.commit()
        except BaseException:
            self._db_session.rollback()
            raise

    def _persist_transcribing(self) -> None:
        try:
            self._session_repository.mark_transcribing(
                self._session_id,
                provider=self._provider_name,
                model=self._model_name,
            )
            self._db_session.commit()
        except BaseException:
            self._db_session.rollback()
            raise

    def _persist_finalizing(self) -> None:
        try:
            self._session_repository.begin_finalizing(self._session_id)
            self._db_session.commit()
        except BaseException:
            self._db_session.rollback()
            raise

    def _persist_completion(self, metrics: TranscriptionMetrics) -> None:
        try:
            self._session_repository.complete(
                self._session_id,
                **metrics.log_fields(),
            )
            self._db_session.commit()
        except BaseException:
            self._db_session.rollback()
            raise

    def _persist_failure(self, *, error_code: str, message: str) -> None:
        try:
            self._session_repository.fail(
                self._session_id,
                error_code=error_code,
                error_message=message,
            )
            self._db_session.commit()
        except BaseException:
            self._db_session.rollback()
            raise

    def _persist_cancellation(self) -> None:
        try:
            self._session_repository.cancel(self._session_id)
            self._db_session.commit()
        except BaseException:
            self._db_session.rollback()
            raise

    def _refresh_external_terminal_state(self) -> bool:
        record = self._session_repository.get_required(self._session_id)
        self._db_session.expire(record)
        self._db_session.refresh(record)
        if record.status not in TERMINAL_SESSION_STATES:
            return False
        self._terminal = True
        return True

    def _extra(self, event: str, **fields: Any) -> dict[str, Any]:
        return {
            "process_name": "worker",
            "session_id": self._session_id,
            "track_sid": self._track_id,
            "source_type": self._source_type,
            "provider": self._provider_name,
            "event": event,
            "elapsed_ms": round(
                time.monotonic() * 1_000 - self._started_at_ms,
                3,
            ),
            "error_type": fields.pop("error_type", None),
            **fields,
        }

    @staticmethod
    def _public_error(error: BaseException) -> tuple[str, str]:
        return public_error_for(
            error,
            default="asr_stream_error",
        )


def create_worker_caption_runtime(
    *,
    session_id: str,
    track_id: str,
    local_participant: Any,
    database_url: str,
    provider_name: str,
    model_name: str,
) -> WorkerCaptionRuntime:
    """Open the Worker-owned SQLite unit of work for a replay track."""

    database = Database(database_url)
    db_session = Session(database.engine, expire_on_commit=False)
    try:
        session_record = db_session.get(SessionRecord, session_id)
        if session_record is None:
            raise LookupError(f"Session not found: {session_id}")
        return WorkerCaptionRuntime(
            session_id=session_id,
            track_id=track_id,
            language=session_record.language,
            source_type=session_record.source_type,
            provider_name=provider_name,
            model_name=model_name,
            segment_repository=SegmentRepository(db_session),
            session_repository=SessionRepository(db_session),
            db_session=db_session,
            publisher=LiveKitCaptionEventPublisher(
                local_participant,
                session_id=session_id,
            ),
            translation_repository=TranslationSegmentRepository(db_session),
            dispose_database=database.dispose,
        )
    except BaseException:
        db_session.close()
        database.dispose()
        raise
