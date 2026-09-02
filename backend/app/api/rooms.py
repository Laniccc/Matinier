from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from typing import Literal
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from livekit import api
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.api.livekit_token import TokenResponse
from app.api.sessions import SessionResponse
from app.hls.manager import HLSStartRequest
from app.hls.url_policy import HLSURLValidationError, validate_hls_url
from app.languages import validate_translation_pair
from app.persistence.models import RoomRecord, SessionRecord
from app.persistence.rooms import RoomRepository
from app.persistence.sessions import SessionRepository
from app.sessions.state import TERMINAL_SESSION_STATES


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/rooms", tags=["rooms"])


def _log_session_created(record: SessionRecord) -> None:
    logger.info(
        "caption session created",
        extra={
            "process_name": "api",
            "session_id": record.id,
            "room_name": record.room_name,
            "source_type": record.source_type,
            "event": "session_created",
            "status": record.status,
        },
    )


async def _ensure_caption_agent(request: Request, room_name: str) -> None:
    settings = request.app.state.settings
    admin = request.app.state.room_admin_factory(settings)
    try:
        await admin.ensure_agent_dispatch(
            room_name,
            settings.livekit_agent_name,
        )
    except asyncio.CancelledError:
        raise
    except BaseException as error:
        logger.error(
            "caption agent dispatch failed",
            extra={
                "process_name": "api",
                "room_name": room_name,
                "event": "caption_agent_dispatch_failed",
                "error_type": type(error).__name__,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Caption Worker is unavailable",
        ) from error
    finally:
        await admin.aclose()


class RoomCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name must not be blank")
        return normalized


class RoomUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name must not be blank")
        return normalized


class RoomResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    room_name: str
    display_name: str
    status: Literal["ready", "closed"]
    created_at: dt.datetime
    updated_at: dt.datetime
    closed_at: dt.datetime | None


class CaptionRunCreate(BaseModel):
    source_type: Literal["microphone", "screen", "file"]
    source_name: str = Field(min_length=1, max_length=255)
    language: str = Field(default="zh-CN", min_length=2, max_length=32)
    target_language: str | None = Field(default=None, min_length=2, max_length=32)

    @field_validator("source_name", "language")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("target_language")
    @classmethod
    def normalize_target_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_languages(self) -> CaptionRunCreate:
        validate_translation_pair(self.language, self.target_language)
        return self


class HLSInputCreate(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    language: str = Field(default="zh-CN", min_length=2, max_length=32)
    target_language: str | None = Field(default=None, min_length=2, max_length=32)

    @field_validator("url", "language")
    @classmethod
    def normalize_hls_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("target_language")
    @classmethod
    def normalize_target_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_languages(self) -> HLSInputCreate:
        validate_translation_pair(self.language, self.target_language)
        return self


class RoomTokenRequest(BaseModel):
    participant_identity: str | None = Field(default=None, min_length=1, max_length=64)


def _get_room_or_404(db_session: Session, room_id: str) -> RoomRecord:
    record = RoomRepository(db_session).get(room_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )
    return record


def _require_ready_room(db_session: Session, room_id: str) -> RoomRecord:
    record = _get_room_or_404(db_session, room_id)
    if record.status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Room is closed",
        )
    return record


@router.post("", response_model=RoomResponse, status_code=status.HTTP_201_CREATED)
def create_room(
    payload: RoomCreate,
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> RoomRecord:
    room_id = str(uuid.uuid4())
    suffix = room_id.replace("-", "")[:12]
    room_name = f"{request.app.state.settings.livekit_room_name}-room-{suffix}"
    record = RoomRepository(db_session).create(
        room_id=room_id,
        room_name=room_name,
        display_name=payload.display_name.strip(),
    )
    db_session.commit()
    db_session.refresh(record)
    return record


@router.get("", response_model=list[RoomResponse])
def list_rooms(
    db_session: Session = Depends(get_db_session),
) -> list[RoomRecord]:
    return RoomRepository(db_session).list_newest_first()


@router.get("/{room_id}", response_model=RoomResponse)
def get_room(
    room_id: str,
    db_session: Session = Depends(get_db_session),
) -> RoomRecord:
    return _get_room_or_404(db_session, room_id)


@router.patch("/{room_id}", response_model=RoomResponse)
def update_room(
    room_id: str,
    payload: RoomUpdate,
    db_session: Session = Depends(get_db_session),
) -> RoomRecord:
    _require_ready_room(db_session, room_id)
    record = RoomRepository(db_session).rename(
        room_id,
        payload.display_name.strip(),
    )
    db_session.commit()
    db_session.refresh(record)
    return record


@router.get("/{room_id}/caption-runs", response_model=list[SessionResponse])
def list_caption_runs(
    room_id: str,
    db_session: Session = Depends(get_db_session),
) -> list[SessionRecord]:
    _get_room_or_404(db_session, room_id)
    return SessionRepository(db_session).list_for_room(room_id)


@router.post(
    "/{room_id}/caption-runs",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_caption_run(
    room_id: str,
    payload: CaptionRunCreate,
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> SessionRecord:
    room = _require_ready_room(db_session, room_id)
    sessions = SessionRepository(db_session)
    if sessions.get_active_for_room(room_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Room already has an active caption run",
        )
    await _ensure_caption_agent(request, room.room_name)
    record = sessions.create(
        room_id=room.id,
        room_name=room.room_name,
        source_type=payload.source_type,
        source_name=payload.source_name.strip(),
        language=payload.language.strip(),
        target_language=payload.target_language,
    )
    db_session.commit()
    db_session.refresh(record)
    _log_session_created(record)
    return record


@router.post(
    "/{room_id}/hls-inputs",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_hls_input(
    room_id: str,
    payload: HLSInputCreate,
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> SessionRecord:
    room = _require_ready_room(db_session, room_id)
    sessions = SessionRepository(db_session)
    if sessions.get_active_for_room(room_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Room already has an active caption run",
        )
    settings = request.app.state.settings
    try:
        source_url = await validate_hls_url(
            payload.url,
            max_redirects=settings.remote_media_max_redirects,
            connect_timeout_seconds=(
                settings.remote_media_connect_timeout_seconds
            ),
            read_timeout_seconds=settings.remote_media_read_timeout_seconds,
        )
    except HLSURLValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    logger.info(
        "remote media URL allowed",
        extra={
            "process_name": "api",
            "room_name": room.room_name,
            "source_type": "hls",
            "event": "remote_media_url_allowed",
            "resolved_host": source_url.resolved_host,
            "resolved_ips": source_url.resolved_ips,
            "redirect_count": source_url.redirect_count,
        },
    )

    record = sessions.create(
        room_id=room.id,
        room_name=room.room_name,
        source_type="hls",
        source_name=source_url.display_url,
        language=payload.language,
        target_language=payload.target_language,
    )
    db_session.commit()
    db_session.refresh(record)
    _log_session_created(record)
    try:
        await request.app.state.hls_input_manager.start(
            HLSStartRequest(
                session_id=record.id,
                room_id=room.id,
                room_name=room.room_name,
                fetch_url=source_url.fetch_url,
            )
        )
    except BaseException as error:
        if isinstance(error, asyncio.CancelledError):
            raise
        sessions.fail(
            record.id,
            error_code="hls_stream_error",
            error_message="The HLS input could not be started.",
        )
        db_session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="HLS input could not be started",
        ) from error
    return record


@router.post(
    "/{room_id}/hls-inputs/{session_id}/stop",
    response_model=SessionResponse,
)
async def stop_hls_input(
    room_id: str,
    session_id: str,
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> SessionRecord:
    _get_room_or_404(db_session, room_id)
    sessions = SessionRepository(db_session)
    record = sessions.get(session_id)
    if (
        record is None
        or record.room_id != room_id
        or record.source_type != "hls"
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="HLS input not found",
        )
    # Persist the initiating intent before asking the HLS manager to stop.
    # The Worker may complete concurrently while the manager drains FFmpeg.
    if record.status != "failed" and record.stop_reason is None:
        record.stop_reason = "user_requested"
        db_session.commit()
    cleanup = await request.app.state.hls_input_manager.stop(
        session_id,
        graceful=True,
        force=True,
    )
    if (
        not cleanup.released
        and record.status not in TERMINAL_SESSION_STATES
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="HLS input is not running",
        )
    db_session.expire_all()
    record = sessions.get_required(session_id)
    return record


@router.post(
    "/{room_id}/caption-runs/{session_id}/cancel",
    response_model=SessionResponse,
)
def cancel_caption_run(
    room_id: str,
    session_id: str,
    db_session: Session = Depends(get_db_session),
) -> SessionRecord:
    _get_room_or_404(db_session, room_id)
    sessions = SessionRepository(db_session)
    record = sessions.get(session_id)
    if record is None or record.room_id != room_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Caption run not found",
        )
    if record.status not in TERMINAL_SESSION_STATES:
        record = sessions.cancel(session_id, stop_reason="user_requested")
        db_session.commit()
        db_session.refresh(record)
    return record


@router.post("/{room_id}/token", response_model=TokenResponse)
def create_room_token(
    room_id: str,
    payload: RoomTokenRequest,
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> TokenResponse:
    room = _require_ready_room(db_session, room_id)
    settings = request.app.state.settings
    participant_identity = (
        payload.participant_identity or f"operator-{uuid.uuid4().hex[:10]}"
    )
    grants = api.VideoGrants(
        room_join=True,
        room=room.room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )
    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(participant_identity)
        .with_name(participant_identity)
        .with_ttl(dt.timedelta(hours=4))
        .with_grants(grants)
        .to_jwt()
    )
    return TokenResponse(
        token=token,
        url=settings.livekit_url,
        room_name=room.room_name,
        participant_identity=participant_identity,
    )


@router.post("/{room_id}/close", response_model=RoomResponse)
async def close_room(
    room_id: str,
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> RoomRecord:
    room = _get_room_or_404(db_session, room_id)
    if room.status == "closed":
        return room

    await request.app.state.hls_input_manager.stop_for_room(
        room_id,
        graceful=False,
    )
    admin = request.app.state.room_admin_factory(request.app.state.settings)
    try:
        await admin.delete_room(room.room_name)
    finally:
        await admin.aclose()

    sessions = SessionRepository(db_session)
    active = sessions.get_active_for_room(room_id)
    if active is not None:
        sessions.cancel(active.id, stop_reason="room_closed")
    room = RoomRepository(db_session).close(room_id)
    db_session.commit()
    db_session.refresh(room)
    return room


@router.post(
    "/{room_id}/participants/{participant_identity}/remove",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_participant(
    room_id: str,
    participant_identity: str,
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> None:
    room = _require_ready_room(db_session, room_id)
    identity = unquote(participant_identity)
    if identity.startswith("agent-"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Worker participants cannot be removed from the console",
        )
    admin = request.app.state.room_admin_factory(request.app.state.settings)
    try:
        await admin.remove_participant(room.room_name, identity)
    finally:
        await admin.aclose()
