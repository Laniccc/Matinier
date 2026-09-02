from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.persistence.models import (
    SegmentRecord,
    SessionRecord,
    TranslationSegmentRecord,
)
from app.persistence.segments import SegmentRepository
from app.persistence.sessions import SessionRepository
from app.persistence.translations import TranslationSegmentRepository
from app.errors import PUBLIC_ERROR_MESSAGES
from app.languages import validate_translation_pair
from app.sessions.state import InvalidSessionTransition
from app.sessions.state import SessionStatus


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    source_type: Literal["empty", "file"] = "empty"
    source_name: str = Field(default="empty-room-demo", min_length=1, max_length=255)
    language: str = Field(default="zh-CN", min_length=2, max_length=32)
    target_language: str | None = Field(default=None, min_length=2, max_length=32)

    @field_validator("target_language")
    @classmethod
    def normalize_target_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_languages(self) -> SessionCreate:
        validate_translation_pair(self.language, self.target_language)
        return self


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    room_id: str | None
    room_name: str
    status: SessionStatus
    source_type: str
    source_name: str
    language: str
    target_language: str | None
    asr_provider: str | None
    asr_model: str | None
    translation_status: str
    translation_provider: str | None
    translation_model: str | None
    translation_error_code: str | None
    translation_error_message: str | None
    translation_ended_at: dt.datetime | None
    source_status: str
    source_ended_at: dt.datetime | None
    cleanup_status: str
    cleanup_detail: str | None
    final_result_count: int | None
    first_partial_latency_ms: float | None
    average_final_latency_ms: float | None
    provider_error_count: int | None
    sent_audio_chunk_count: int | None
    sent_audio_bytes: int | None
    error_code: str | None
    error_message: str | None
    stop_reason: str | None
    failure_code: str | None
    failure_detail: str | None
    started_at: dt.datetime | None
    ended_at: dt.datetime | None
    created_at: dt.datetime


class SessionRuntimeResponse(BaseModel):
    session_status: SessionStatus
    source_status: str
    translation_status: str
    cleanup_status: str
    room_connected: bool | None
    publisher_connected: bool | None
    track_published: bool | None
    track_subscribed: bool | None
    asr_connected: bool | None
    translation_connected: bool | None
    ffmpeg_running: bool | None
    audio_bytes: int | None
    audio_frames: int | None
    audio_queue_current: int | None
    audio_queue_max: int | None
    source_final_count: int
    translation_final_count: int
    last_event_at: dt.datetime | None
    started_at: dt.datetime | None
    ended_at: dt.datetime | None
    source_ended_at: dt.datetime | None
    translation_ended_at: dt.datetime | None
    failure_code: str | None
    stop_reason: str | None
    unavailable: list[str]


class SegmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    segment_id: str
    track_id: str
    revision: int
    language: str
    raw_text: str
    display_text: str
    audio_start_ms: int | None
    audio_end_ms: int | None
    confidence: float | None
    status: str
    received_at_ms: int
    finalized_at: dt.datetime
    created_at: dt.datetime
    updated_at: dt.datetime


class TranslationSegmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    segment_id: str
    revision: int
    source_language: str
    target_language: str
    text: str
    audio_start_ms: int | None
    audio_end_ms: int | None
    source_segment_ids: list[str]
    status: Literal["final"]
    received_at_ms: int
    finalized_at: dt.datetime
    created_at: dt.datetime
    updated_at: dt.datetime


class ReplayTerminalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["failed", "cancelled"]
    error_code: Literal["media_decode_error", "livekit_error"] | None = None

    @model_validator(mode="after")
    def validate_error_code(self) -> ReplayTerminalReport:
        if self.status == "failed" and self.error_code is None:
            raise ValueError("failed replay requires error_code")
        if self.status == "cancelled" and self.error_code is not None:
            raise ValueError("cancelled replay must not include error_code")
        return self


def _get_session_or_404(db_session: Session, session_id: str) -> SessionRecord:
    record = SessionRepository(db_session).get(session_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return record


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(
    payload: SessionCreate,
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> SessionRecord:
    session_id = str(uuid.uuid4())
    room_suffix = session_id.replace("-", "")[:12]
    room_name = f"{request.app.state.settings.livekit_room_name}-{room_suffix}"
    record = SessionRepository(db_session).create(
        session_id=session_id,
        room_name=room_name,
        source_type=payload.source_type,
        source_name=payload.source_name,
        language=payload.language,
        target_language=payload.target_language,
    )
    db_session.commit()
    db_session.refresh(record)
    logger.info(
        "session created",
        extra={
            "process_name": "api",
            "session_id": record.id,
            "room_name": record.room_name,
            "event": "session_created",
            "source_type": record.source_type,
            "status": record.status,
        },
    )
    return record


@router.get("", response_model=list[SessionResponse])
def list_sessions(
    db_session: Session = Depends(get_db_session),
) -> list[SessionRecord]:
    return SessionRepository(db_session).list_newest_first()


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: str,
    db_session: Session = Depends(get_db_session),
) -> SessionRecord:
    return _get_session_or_404(db_session, session_id)


@router.get(
    "/{session_id}/runtime",
    response_model=SessionRuntimeResponse,
)
def get_session_runtime(
    session_id: str,
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> SessionRuntimeResponse:
    record = _get_session_or_404(db_session, session_id)
    worker_snapshot = request.app.state.runtime_registry.get(session_id)
    source_snapshot = request.app.state.hls_input_manager.runtime_snapshot(
        session_id
    )
    worker = worker_snapshot.values if worker_snapshot is not None else {}
    source = source_snapshot or {}

    def first_value(name: str) -> object | None:
        if name in worker:
            return worker[name]
        return source.get(name)

    last_events = [
        value
        for value in (
            worker.get("last_event_at"),
            source.get("last_event_at"),
        )
        if isinstance(value, dt.datetime)
    ]
    live_fields = {
        "room_connected": first_value("room_connected"),
        "publisher_connected": source.get("publisher_connected"),
        "track_published": source.get("track_published"),
        "track_subscribed": worker.get("track_subscribed"),
        "asr_connected": worker.get("asr_connected"),
        "translation_connected": worker.get("translation_connected"),
        "ffmpeg_running": source.get("ffmpeg_running"),
        "audio_bytes": first_value("audio_bytes"),
        "audio_frames": first_value("audio_frames"),
        "audio_queue_current": worker.get("audio_queue_current"),
        "audio_queue_max": worker.get("audio_queue_max"),
    }
    unavailable = [
        name for name, value in live_fields.items() if value is None
    ]
    return SessionRuntimeResponse(
        session_status=record.status,
        source_status=record.source_status,
        translation_status=record.translation_status,
        cleanup_status=record.cleanup_status,
        room_connected=live_fields["room_connected"],
        publisher_connected=live_fields["publisher_connected"],
        track_published=live_fields["track_published"],
        track_subscribed=live_fields["track_subscribed"],
        asr_connected=live_fields["asr_connected"],
        translation_connected=live_fields["translation_connected"],
        ffmpeg_running=live_fields["ffmpeg_running"],
        audio_bytes=live_fields["audio_bytes"],
        audio_frames=live_fields["audio_frames"],
        audio_queue_current=live_fields["audio_queue_current"],
        audio_queue_max=live_fields["audio_queue_max"],
        source_final_count=len(SegmentRepository(db_session).list_final(session_id)),
        translation_final_count=len(
            TranslationSegmentRepository(db_session).list_final(session_id)
        ),
        last_event_at=max(last_events) if last_events else None,
        started_at=record.started_at,
        ended_at=record.ended_at,
        source_ended_at=record.source_ended_at,
        translation_ended_at=record.translation_ended_at,
        failure_code=record.failure_code or record.error_code,
        stop_reason=record.stop_reason,
        unavailable=unavailable,
    )


@router.get("/{session_id}/segments", response_model=list[SegmentResponse])
def get_session_segments(
    session_id: str,
    db_session: Session = Depends(get_db_session),
) -> list[SegmentRecord]:
    _get_session_or_404(db_session, session_id)
    return SegmentRepository(db_session).list_final(session_id)


@router.get(
    "/{session_id}/translations",
    response_model=list[TranslationSegmentResponse],
)
def get_session_translations(
    session_id: str,
    db_session: Session = Depends(get_db_session),
) -> list[TranslationSegmentRecord]:
    _get_session_or_404(db_session, session_id)
    return TranslationSegmentRepository(db_session).list_final(session_id)


@router.post("/{session_id}/replay-status", response_model=SessionResponse)
def report_replay_terminal_status(
    session_id: str,
    payload: ReplayTerminalReport,
    db_session: Session = Depends(get_db_session),
) -> SessionRecord:
    repository = SessionRepository(db_session)
    if repository.get(session_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    try:
        if payload.status == "cancelled":
            record = repository.cancel(
                session_id,
                stop_reason="user_requested",
            )
        else:
            assert payload.error_code is not None
            record = repository.fail(
                session_id,
                error_code=payload.error_code,
                error_message=PUBLIC_ERROR_MESSAGES[payload.error_code],
            )
        db_session.commit()
        db_session.refresh(record)
        return record
    except InvalidSessionTransition as error:
        db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session is already terminal",
        ) from error
    except Exception:
        db_session.rollback()
        raise
