from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from livekit import api
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.persistence.models import SessionRecord
from app.persistence.sessions import SessionRepository


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/livekit", tags=["livekit"])


class TokenRequest(BaseModel):
    session_id: str = Field(min_length=36, max_length=36)
    participant_identity: str | None = Field(default=None, min_length=1, max_length=64)
    participant_type: Literal["browser", "replay"] = "browser"


class TokenResponse(BaseModel):
    token: str
    url: str
    room_name: str
    participant_identity: str


@router.post("/token", response_model=TokenResponse)
def create_livekit_token(
    payload: TokenRequest,
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> TokenResponse:
    session_repository = SessionRepository(db_session)
    session_record = session_repository.get(payload.session_id)
    if session_record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    settings = request.app.state.settings
    is_replay = payload.participant_type == "replay"
    participant_identity = (
        f"replay-{session_record.id}"
        if is_replay
        else payload.participant_identity or f"browser-{uuid.uuid4().hex[:10]}"
    )
    grants = api.VideoGrants(
        room_join=True,
        room=session_record.room_name,
        can_publish=is_replay,
        can_subscribe=not is_replay,
        can_publish_data=not is_replay,
        can_publish_sources=["microphone"] if is_replay else None,
    )
    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(participant_identity)
        .with_name(participant_identity)
        .with_ttl(dt.timedelta(hours=1))
        .with_grants(grants)
        .to_jwt()
    )
    session_repository.mark_room_ready(session_record.id)
    db_session.commit()
    logger.info(
        "LiveKit token issued",
        extra={
            "process_name": "api",
            "session_id": session_record.id,
            "room_name": session_record.room_name,
            "participant_identity": participant_identity,
            "event": "room_ready",
            "source_type": session_record.source_type,
            "status": "starting",
        },
    )
    return TokenResponse(
        token=token,
        url=settings.livekit_url,
        room_name=session_record.room_name,
        participant_identity=participant_identity,
    )
