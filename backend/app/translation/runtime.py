from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from sqlalchemy.orm import Session

from app.captions.publisher import (
    CaptionEventPublisher,
    LiveKitCaptionEventPublisher,
)
from app.persistence.database import Database
from app.persistence.models import SessionRecord
from app.persistence.sessions import SessionRepository
from app.persistence.translations import TranslationSegmentRepository
from app.translation.errors import TranslationError
from app.translation.models import TranslationEvent, TranslationEventType
from app.translation.reconciler import TranslationReconciler


logger = logging.getLogger(__name__)

_PUBLIC_MESSAGES = {
    "translation_auth_error": "Realtime translation authentication failed.",
    "translation_configuration_error": "Realtime translation is not configured.",
    "translation_timeout_error": "Realtime translation timed out.",
    "translation_backpressure_error": "Realtime translation could not keep up with audio.",
    "translation_protocol_error": "Realtime translation returned an invalid response.",
    "translation_state_error": "Realtime translation stopped unexpectedly.",
    "translation_stream_error": "Realtime translation stopped unexpectedly.",
}


class WorkerTranslationRuntime:
    """Independent persisted lifecycle for the optional translation route."""

    def __init__(
        self,
        *,
        session_id: str,
        source_language: str,
        target_language: str,
        provider_name: str,
        model_name: str,
        session_repository: SessionRepository,
        translation_repository: TranslationSegmentRepository,
        db_session: Session,
        publisher: CaptionEventPublisher,
        dispose_database: Callable[[], None] | None = None,
        draft_publish_interval_ms: float = 250.0,
        clock_ms: Callable[[], float] | None = None,
        log_context: dict[str, Any] | None = None,
    ) -> None:
        if draft_publish_interval_ms < 0:
            raise ValueError("draft publish interval must be non-negative")
        self.session_id = session_id
        self.source_language = source_language
        self.target_language = target_language
        self.provider_name = provider_name
        self.model_name = model_name
        self._session_repository = session_repository
        self._translation_repository = translation_repository
        self._db_session = db_session
        self._publisher = publisher
        self._dispose_database = dispose_database
        self._draft_publish_interval_ms = draft_publish_interval_ms
        self._clock_ms = clock_ms or (lambda: time.monotonic() * 1_000)
        self._started_at_ms = time.monotonic() * 1_000
        self._log_context = dict(log_context or {})
        self._terminal = False
        self._closed = False
        self._summary_logged = False
        self._last_draft_publish_at_ms: dict[str, float] = {}
        self._provider_revision_count = 0
        self._published_draft_count = 0
        self._published_final_count = 0
        self._reconciler = TranslationReconciler(
            session_id=session_id,
            source_language=source_language,
            target_language=target_language,
        )

    async def start(self) -> None:
        if self._terminal:
            return
        self._write(
            lambda: self._session_repository.mark_translation_starting(
                self.session_id,
                provider=self.provider_name,
                model=self.model_name,
            )
        )
        await self._publisher.publish_translation_status(
            status="starting",
            source_language=self.source_language,
            target_language=self.target_language,
        )
        logger.info(
            "translation starting",
            extra=self._extra("translation_starting", status="starting"),
        )

    async def handle_event(self, event: TranslationEvent) -> None:
        if self._terminal:
            return
        if event.event_type is TranslationEventType.STREAM_STARTED:
            self._write(
                lambda: self._session_repository.mark_translation_active(
                    self.session_id
                )
            )
            await self._publisher.publish_translation_status(
                status="running",
                source_language=self.source_language,
                target_language=self.target_language,
            )
            logger.info(
                "translation stream active",
                extra=self._extra(
                    "translation_connected",
                    status="running",
                    provider_request_id=event.provider_event_id,
                ),
            )
        elif event.event_type is TranslationEventType.STREAM_ERROR:
            await self.fail(
                TranslationError(event.text or "translation stream failed")
            )
            return

        caption = self._reconciler.consume(event)
        if caption is None:
            return
        self._provider_revision_count += 1
        if caption.is_final:
            record = self._persist_final(caption)
            caption = replace(
                caption,
                source_segment_ids=tuple(record.source_segment_ids),
            )
            self._last_draft_publish_at_ms.pop(caption.segment_id, None)
            self._published_final_count += 1
            logger.info(
                "translation final persisted",
                extra=self._extra(
                    "translation_final_persisted",
                    status="running",
                    provider_request_id=caption.provider_event_id,
                    segment_id=caption.segment_id,
                    revision=caption.revision,
                    text_length=len(caption.text),
                ),
            )
            await self._publisher.publish_translation(caption)
            logger.info(
                "translation final published",
                extra=self._extra(
                    "translation_final_published",
                    status="running",
                    provider_request_id=caption.provider_event_id,
                    segment_id=caption.segment_id,
                    revision=caption.revision,
                    text_length=len(caption.text),
                    begin_time_ms=caption.audio_start_ms,
                    end_time_ms=caption.audio_end_ms,
                ),
            )
            return

        now_ms = self._clock_ms()
        last_published_at = self._last_draft_publish_at_ms.get(
            caption.segment_id
        )
        if (
            last_published_at is not None
            and now_ms - last_published_at
            < self._draft_publish_interval_ms
        ):
            return
        self._last_draft_publish_at_ms[caption.segment_id] = now_ms
        self._published_draft_count += 1
        await self._publisher.publish_translation(caption)
        logger.debug(
            "translation draft published",
            extra=self._extra(
                "translation_partial_result",
                segment_id=caption.segment_id,
                revision=caption.revision,
                text_length=len(caption.text),
                begin_time_ms=caption.audio_start_ms,
                end_time_ms=caption.audio_end_ms,
            ),
        )

    async def begin_finalizing(self) -> None:
        if self._terminal:
            return
        self._write(
            lambda: self._session_repository.begin_translation_finalizing(
                self.session_id
            )
        )
        await self._publisher.publish_translation_status(
            status="running",
            source_language=self.source_language,
            target_language=self.target_language,
        )
        logger.info(
            "translation finalizing",
            extra=self._extra("translation_finalizing", status="running"),
        )

    async def complete(self) -> None:
        if self._terminal:
            return
        self._write(
            lambda: self._session_repository.complete_translation(
                self.session_id
            )
        )
        self._terminal = True
        await self._publisher.publish_translation_status(
            status="completed",
            source_language=self.source_language,
            target_language=self.target_language,
        )
        logger.info(
            "translation completed",
            extra=self._extra("translation_completed", status="completed"),
        )
        self._log_summary()

    async def fail(self, error: BaseException) -> None:
        if self._terminal:
            return
        code = (
            error.code
            if isinstance(error, TranslationError)
            else "translation_stream_error"
        )
        message = _PUBLIC_MESSAGES.get(
            code,
            _PUBLIC_MESSAGES["translation_stream_error"],
        )
        self._write(
            lambda: self._session_repository.fail_translation(
                self.session_id,
                error_code=code,
                error_message=message,
            )
        )
        self._terminal = True
        await self._publisher.publish_translation_status(
            status="failed",
            source_language=self.source_language,
            target_language=self.target_language,
            error_code=code,
            message=message,
        )
        logger.error(
            "translation failed",
            extra=self._extra(
                "translation_failed",
                error_type=type(error).__name__,
                translation_error_code=code,
                failure_code=code,
                status="failed",
            ),
        )
        self._log_summary()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._db_session.close()
        finally:
            if self._dispose_database is not None:
                self._dispose_database()
            self._log_summary()

    def _write(self, operation: Callable[[], object]) -> None:
        try:
            operation()
            self._db_session.commit()
        except BaseException:
            self._db_session.rollback()
            raise

    def _persist_final(self, caption: Any):
        try:
            record = self._translation_repository.upsert_final(caption)
            self._db_session.commit()
            return record
        except BaseException:
            self._db_session.rollback()
            raise

    def _log_summary(self) -> None:
        if self._summary_logged:
            return
        self._summary_logged = True
        logger.info(
            "translation session summary",
            extra=self._extra(
                "translation_summary",
                translation_provider_revision_count=(
                    self._provider_revision_count
                ),
                translation_published_draft_count=(
                    self._published_draft_count
                ),
                translation_published_final_count=(
                    self._published_final_count
                ),
            ),
        )

    def _extra(self, event: str, **fields: Any) -> dict[str, Any]:
        return {
            "process_name": "worker",
            "session_id": self.session_id,
            "room_name": self._log_context.get("room_name"),
            "participant_identity": self._log_context.get(
                "participant_identity"
            ),
            "track_sid": self._log_context.get("track_sid"),
            "source_type": self._log_context.get("source_type"),
            "provider": self.provider_name,
            "event": event,
            "elapsed_ms": round(
                time.monotonic() * 1_000 - self._started_at_ms,
                3,
            ),
            "error_type": fields.pop("error_type", None),
            "translation_provider": self.provider_name,
            "translation_model": self.model_name,
            "source_language": self.source_language,
            "target_language": self.target_language,
            **fields,
        }


def create_worker_translation_runtime(
    *,
    session_id: str,
    database_url: str,
    provider_name: str,
    model_name: str,
    local_participant: Any,
    log_context: dict[str, Any] | None = None,
) -> WorkerTranslationRuntime | None:
    database = Database(database_url)
    db_session = Session(database.engine, expire_on_commit=False)
    try:
        record = db_session.get(SessionRecord, session_id)
        if record is None:
            raise LookupError(f"Session not found: {session_id}")
        if record.target_language is None:
            db_session.close()
            database.dispose()
            return None
        return WorkerTranslationRuntime(
            session_id=session_id,
            source_language=record.language,
            target_language=record.target_language,
            provider_name=provider_name,
            model_name=model_name,
            session_repository=SessionRepository(db_session),
            translation_repository=TranslationSegmentRepository(db_session),
            db_session=db_session,
            publisher=LiveKitCaptionEventPublisher(
                local_participant,
                session_id=session_id,
            ),
            dispose_database=database.dispose,
            log_context=log_context,
        )
    except BaseException:
        db_session.close()
        database.dispose()
        raise
