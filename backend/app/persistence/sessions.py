from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.persistence.models import SessionRecord, utc_now
from app.sessions.state import (
    SessionStatus,
    parse_session_status,
    transition_session_status,
)


class SessionRepository:
    def __init__(self, db_session: Session) -> None:
        self._db_session = db_session

    def create(
        self,
        *,
        room_name: str,
        room_id: str | None = None,
        source_type: str,
        source_name: str,
        language: str,
        target_language: str | None = None,
        session_id: str | None = None,
        created_at: dt.datetime | None = None,
    ) -> SessionRecord:
        if not room_name:
            raise ValueError("room_name is required")
        if not source_type:
            raise ValueError("source_type is required")
        if not source_name:
            raise ValueError("source_name is required")
        if not language:
            raise ValueError("language is required")
        record = SessionRecord(
            id=session_id or str(uuid.uuid4()),
            room_id=room_id,
            room_name=room_name,
            status="created",
            source_type=source_type,
            source_name=source_name,
            language=language,
            target_language=target_language,
            translation_status=(
                "starting" if target_language is not None else "disabled"
            ),
            source_status="starting",
            cleanup_status="not_started",
            created_at=created_at or utc_now(),
        )
        self._db_session.add(record)
        self._db_session.flush()
        return record

    def get(self, session_id: str) -> SessionRecord | None:
        return self._db_session.get(SessionRecord, session_id)

    def get_required(self, session_id: str) -> SessionRecord:
        record = self.get(session_id)
        if record is None:
            raise LookupError(f"Session not found: {session_id}")
        return record

    def list_newest_first(self) -> list[SessionRecord]:
        return list(
            self._db_session.scalars(
                select(SessionRecord).order_by(
                    SessionRecord.created_at.desc(),
                    SessionRecord.id.desc(),
                )
            )
        )

    def list_for_room(self, room_id: str) -> list[SessionRecord]:
        return list(
            self._db_session.scalars(
                select(SessionRecord)
                .where(SessionRecord.room_id == room_id)
                .order_by(
                    SessionRecord.created_at.desc(),
                    SessionRecord.id.desc(),
                )
            )
        )

    def get_active_for_room(self, room_id: str) -> SessionRecord | None:
        return self._db_session.scalar(
            select(SessionRecord)
            .where(
                SessionRecord.room_id == room_id,
                SessionRecord.status.not_in(("completed", "failed", "cancelled")),
            )
            .order_by(
                SessionRecord.created_at.desc(),
                SessionRecord.id.desc(),
            )
            .limit(1)
        )

    def transition(
        self,
        session_id: str,
        target: SessionStatus,
        *,
        changed_at: dt.datetime | None = None,
    ) -> SessionRecord:
        record = self.get_required(session_id)
        current = parse_session_status(record.status)
        next_status = transition_session_status(current, target)
        if current == next_status:
            return record

        timestamp = changed_at or utc_now()
        record.status = next_status
        if next_status == "running" and record.started_at is None:
            record.started_at = timestamp
        if next_status in {"completed", "failed", "cancelled"}:
            record.ended_at = timestamp
        self._db_session.flush()
        return record

    def mark_room_ready(self, session_id: str) -> SessionRecord:
        record = self.get_required(session_id)
        current = parse_session_status(record.status)
        if current == "created":
            return self.transition(session_id, "starting")
        return record

    def start_replay(
        self,
        session_id: str,
        *,
        changed_at: dt.datetime | None = None,
    ) -> SessionRecord:
        record = self.get_required(session_id)
        current = parse_session_status(record.status)
        if current == "created":
            self.transition(session_id, "starting", changed_at=changed_at)
            current = "starting"
        if current == "starting":
            return self.transition(
                session_id,
                "running",
                changed_at=changed_at,
            )
        if current == "running":
            return record
        raise ValueError(f"Session cannot start replay from {current}")

    def mark_transcribing(
        self,
        session_id: str,
        *,
        provider: str,
        model: str,
        changed_at: dt.datetime | None = None,
    ) -> SessionRecord:
        if not provider:
            raise ValueError("provider is required")
        if not model:
            raise ValueError("model is required")
        record = self.get_required(session_id)
        current = parse_session_status(record.status)
        if current == "created":
            self.transition(session_id, "starting", changed_at=changed_at)
            current = "starting"
        if current == "starting":
            record = self.transition(
                session_id,
                "running",
                changed_at=changed_at,
            )
        elif current != "running":
            raise ValueError(f"Session cannot start ASR from {current}")
        record.asr_provider = provider
        record.asr_model = model
        self._db_session.flush()
        return record

    def begin_finalizing(
        self,
        session_id: str,
        *,
        changed_at: dt.datetime | None = None,
    ) -> SessionRecord:
        return self.transition(
            session_id,
            "finalizing",
            changed_at=changed_at,
        )

    def complete(
        self,
        session_id: str,
        *,
        final_result_count: int,
        first_partial_latency_ms: float | None,
        average_final_latency_ms: float | None,
        provider_error_count: int,
        sent_audio_chunk_count: int,
        sent_audio_bytes: int,
        changed_at: dt.datetime | None = None,
        stop_reason: str | None = None,
    ) -> SessionRecord:
        integer_metrics = {
            "final_result_count": final_result_count,
            "provider_error_count": provider_error_count,
            "sent_audio_chunk_count": sent_audio_chunk_count,
            "sent_audio_bytes": sent_audio_bytes,
        }
        if any(value < 0 for value in integer_metrics.values()):
            raise ValueError("transcription metrics must be non-negative")
        latencies = (first_partial_latency_ms, average_final_latency_ms)
        if any(value is not None and value < 0 for value in latencies):
            raise ValueError("transcription metrics must be non-negative")

        record = self.transition(
            session_id,
            "completed",
            changed_at=changed_at,
        )
        record.final_result_count = final_result_count
        record.first_partial_latency_ms = first_partial_latency_ms
        record.average_final_latency_ms = average_final_latency_ms
        record.provider_error_count = provider_error_count
        record.sent_audio_chunk_count = sent_audio_chunk_count
        record.sent_audio_bytes = sent_audio_bytes
        record.error_code = None
        record.error_message = None
        record.failure_code = None
        record.failure_detail = None
        # A user/room stop request can be persisted before the Worker drains.
        # A later normal completion must not erase that already-known cause.
        if stop_reason is not None:
            record.stop_reason = stop_reason
        self._db_session.flush()
        return record

    def fail(
        self,
        session_id: str,
        *,
        error_code: str,
        error_message: str,
        changed_at: dt.datetime | None = None,
    ) -> SessionRecord:
        if not error_code:
            raise ValueError("error_code is required")
        if not error_message:
            raise ValueError("error_message is required")
        record = self.transition(
            session_id,
            "failed",
            changed_at=changed_at,
        )
        record.error_code = error_code
        record.error_message = error_message
        record.failure_code = error_code
        record.failure_detail = error_message
        record.stop_reason = None
        self._db_session.flush()
        return record

    def cancel(
        self,
        session_id: str,
        *,
        changed_at: dt.datetime | None = None,
        stop_reason: str = "worker_cancelled",
    ) -> SessionRecord:
        record = self.transition(
            session_id,
            "cancelled",
            changed_at=changed_at,
        )
        # Cancellation is a terminal state, not a failure category. Keep the
        # error fields empty so API consumers never have to treat an out-of-
        # taxonomy "cancelled" value as an error code.
        record.error_code = None
        record.error_message = None
        record.failure_code = None
        record.failure_detail = None
        # Worker cancellation is an implementation consequence of an earlier
        # user/room stop and must not overwrite the initiating reason.
        if record.stop_reason is None or stop_reason != "worker_cancelled":
            record.stop_reason = stop_reason
        self._db_session.flush()
        return record

    def mark_translation_starting(
        self,
        session_id: str,
        *,
        provider: str,
        model: str,
    ) -> SessionRecord:
        record = self.get_required(session_id)
        if record.target_language is None:
            raise ValueError("Session translation is disabled")
        record.translation_status = "starting"
        record.translation_provider = provider
        record.translation_model = model
        record.translation_error_code = None
        record.translation_error_message = None
        record.translation_ended_at = None
        self._db_session.flush()
        return record

    def mark_translation_active(self, session_id: str) -> SessionRecord:
        record = self.get_required(session_id)
        if record.target_language is None:
            raise ValueError("Session translation is disabled")
        record.translation_status = "running"
        self._db_session.flush()
        return record

    def begin_translation_finalizing(self, session_id: str) -> SessionRecord:
        record = self.get_required(session_id)
        if record.translation_status in {"completed", "failed", "disabled"}:
            return record
        record.translation_status = "running"
        self._db_session.flush()
        return record

    def complete_translation(self, session_id: str) -> SessionRecord:
        record = self.get_required(session_id)
        if record.translation_status == "failed":
            return record
        record.translation_status = "completed"
        record.translation_error_code = None
        record.translation_error_message = None
        record.translation_ended_at = utc_now()
        self._db_session.flush()
        return record

    def fail_translation(
        self,
        session_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> SessionRecord:
        if not error_code:
            raise ValueError("error_code is required")
        if not error_message:
            raise ValueError("error_message is required")
        record = self.get_required(session_id)
        if record.translation_status == "completed":
            return record
        record.translation_status = "failed"
        record.translation_error_code = error_code
        record.translation_error_message = error_message
        record.translation_ended_at = utc_now()
        self._db_session.flush()
        return record

    def mark_source_running(self, session_id: str) -> SessionRecord:
        record = self.get_required(session_id)
        if record.source_status == "stopped":
            return record
        record.source_status = "running"
        record.cleanup_status = "not_started"
        record.cleanup_detail = None
        self._db_session.flush()
        return record

    def mark_source_stopping(self, session_id: str) -> SessionRecord:
        record = self.get_required(session_id)
        if record.source_status == "stopped":
            return record
        record.source_status = "stopping"
        record.cleanup_status = "in_progress"
        self._db_session.flush()
        return record

    def mark_source_stopped(
        self,
        session_id: str,
        *,
        cleanup_detail: str | None = None,
        changed_at: dt.datetime | None = None,
    ) -> SessionRecord:
        record = self.get_required(session_id)
        if (
            record.source_status == "stopped"
            and record.cleanup_status == "completed"
        ):
            return record
        record.source_status = "stopped"
        record.source_ended_at = changed_at or utc_now()
        record.cleanup_status = "completed"
        record.cleanup_detail = cleanup_detail
        self._db_session.flush()
        return record

    def mark_source_cleanup_failed(
        self,
        session_id: str,
        *,
        cleanup_detail: str,
    ) -> SessionRecord:
        if not cleanup_detail:
            raise ValueError("cleanup_detail is required")
        record = self.get_required(session_id)
        if record.source_status != "stopped":
            record.source_status = "failed"
            record.source_ended_at = utc_now()
        record.cleanup_status = "failed"
        record.cleanup_detail = cleanup_detail
        self._db_session.flush()
        return record

    def mark_source_lost(
        self,
        session_id: str,
        *,
        cleanup_detail: str,
        changed_at: dt.datetime | None = None,
    ) -> SessionRecord:
        if not cleanup_detail:
            raise ValueError("cleanup_detail is required")
        record = self.get_required(session_id)
        record.source_status = "lost"
        record.source_ended_at = changed_at or utc_now()
        record.cleanup_status = "failed"
        record.cleanup_detail = cleanup_detail
        self._db_session.flush()
        return record
