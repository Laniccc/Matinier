from __future__ import annotations

import logging
import secrets
import uuid
import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.persistence.sessions import SessionRepository


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


class SourceAbortRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal[
        "asr_auth_error",
        "asr_stream_error",
        "caption_runtime_error",
        "worker_cancelled",
    ]
    detail: str = Field(min_length=1, max_length=1000)
    requested_by: Literal["caption_worker"]
    request_id: uuid.UUID


class SourceAbortResponse(BaseModel):
    request_id: uuid.UUID
    outcome: Literal[
        "stopped",
        "already_stopped",
        "cleanup_pending",
    ]
    source_status: str
    cleanup_status: str


class WorkerRuntimeSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_connected: bool
    track_subscribed: bool
    asr_connected: bool
    translation_connected: bool
    audio_bytes: int = Field(ge=0)
    audio_frames: int = Field(ge=0)
    audio_queue_current: int = Field(ge=0)
    audio_queue_max: int = Field(ge=0)
    last_event_at: dt.datetime | None = None


class WorkerRuntimeSnapshotResponse(BaseModel):
    accepted: bool
    reported_at: dt.datetime


class WorkerHealthResponse(BaseModel):
    worker_alive: bool
    livekit_connected: bool
    active_jobs: int
    last_job_event_at: dt.datetime | None


def require_internal_control_token(
    request: Request,
    supplied_token: str | None = Header(
        default=None,
        alias="X-Internal-Control-Token",
    ),
) -> None:
    expected_token = request.app.state.settings.internal_control_token
    if supplied_token is None or not secrets.compare_digest(
        supplied_token,
        expected_token,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Internal control authentication failed",
        )


@router.get(
    "/worker/health",
    response_model=WorkerHealthResponse,
    dependencies=[Depends(require_internal_control_token)],
)
def read_worker_health(request: Request) -> WorkerHealthResponse:
    snapshot = request.app.state.worker_health_store.snapshot()
    return WorkerHealthResponse(
        worker_alive=snapshot.worker_alive,
        livekit_connected=snapshot.livekit_connected,
        active_jobs=snapshot.active_jobs,
        last_job_event_at=snapshot.last_job_event_at,
    )


@router.post(
    "/sessions/{session_id}/runtime",
    response_model=WorkerRuntimeSnapshotResponse,
    dependencies=[Depends(require_internal_control_token)],
)
def report_worker_runtime(
    session_id: str,
    payload: WorkerRuntimeSnapshotRequest,
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> WorkerRuntimeSnapshotResponse:
    if SessionRepository(db_session).get(session_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    snapshot = request.app.state.runtime_registry.update(
        session_id,
        payload.model_dump(mode="python"),
    )
    return WorkerRuntimeSnapshotResponse(
        accepted=True,
        reported_at=snapshot.reported_at,
    )


@router.post(
    "/sessions/{session_id}/source/abort",
    response_model=SourceAbortResponse,
    dependencies=[Depends(require_internal_control_token)],
)
async def abort_session_source(
    session_id: str,
    payload: SourceAbortRequest,
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> SourceAbortResponse:
    repository = SessionRepository(db_session)
    record = repository.get(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )

    room_name = record.room_name
    logger.warning(
        "internal source abort received",
        extra={
            "process_name": "api",
            "session_id": session_id,
            "room_name": room_name,
            "event": "source_abort_received",
            "failure_code": payload.reason,
            "request_id": str(payload.request_id),
        },
    )
    # Release the read transaction before Manager lifecycle callbacks use a
    # separate SQLAlchemy Session to persist cleanup progress.
    db_session.rollback()
    cleanup = await request.app.state.hls_input_manager.stop(
        session_id,
        graceful=False,
        force=True,
    )

    repository = SessionRepository(db_session)
    record = repository.get_required(session_id)
    if cleanup.released:
        repository.mark_source_stopped(
            session_id,
            cleanup_detail=cleanup.detail,
        )
        outcome = cleanup.outcome
    elif record.source_status == "stopped":
        outcome = "already_stopped"
    else:
        repository.mark_source_cleanup_failed(
            session_id,
            cleanup_detail=cleanup.detail,
        )
        outcome = "cleanup_pending"
    db_session.commit()
    db_session.refresh(record)

    logger.info(
        "internal source abort completed",
        extra={
            "process_name": "api",
            "session_id": session_id,
            "room_name": room_name,
            "event": "source_abort_completed",
            "failure_code": payload.reason,
            "cleanup_status": record.cleanup_status,
            "request_id": str(payload.request_id),
        },
    )
    return SourceAbortResponse(
        request_id=payload.request_id,
        outcome=outcome,
        source_status=record.source_status,
        cleanup_status=record.cleanup_status,
    )
