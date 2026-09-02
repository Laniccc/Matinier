from __future__ import annotations
from app.api.legacy_meeting import reject_legacy_meeting_write

import datetime as dt
import hashlib
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_app_settings, get_db_session
from app.assistant.context import MeetingStateFreshness, read_meeting_state
from app.meeting_state.candidates import caption_evidence_message
from app.meeting_state.contracts import ProjectionSegment
from app.meeting_state.models import (
    EvidenceMessageSnapshot,
    MarkKind,
    MarkOrigin,
    MarkStatus,
    MeetingState,
    UserInputEvidenceMessage,
)
from app.meeting_state.repository import MeetingStateRepository
from app.persistence.models import MeetingMarkRecord, SegmentRecord, SessionRecord
from app.settings import Settings


router = APIRouter(prefix="/api/sessions", tags=["meeting-state"])
_EVIDENCE_MESSAGES = TypeAdapter(tuple[EvidenceMessageSnapshot, ...])


class MeetingStateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: MeetingState
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_frontier: dict[str, object]
    freshness: MeetingStateFreshness


class MarkEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_id: str = Field(min_length=1, max_length=255)
    revision: int = Field(ge=1)


class MeetingMarkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: MarkKind
    title: str = Field(min_length=1, max_length=500)
    note: str | None = Field(default=None, max_length=2_000)
    actor_id: str = Field(default="local-user", min_length=1, max_length=255)
    evidence: tuple[MarkEvidenceRef, ...] = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("mark title must not be blank")
        return normalized

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def unique_evidence(self) -> MeetingMarkCreate:
        identifiers = [value.segment_id for value in self.evidence]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("mark evidence Segment IDs must be unique")
        return self


class MeetingMarkPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted", "dismissed"]
    evidence: tuple[MarkEvidenceRef, ...] | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> MeetingMarkPatch:
        if self.status == "accepted" and not self.evidence:
            raise ValueError("accepting a mark requires its evidence revisions")
        if self.evidence:
            identifiers = [value.segment_id for value in self.evidence]
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("mark evidence Segment IDs must be unique")
        return self


class MeetingMarkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mark_id: str
    session_id: str
    origin: MarkOrigin
    kind: MarkKind
    status: MarkStatus
    title: str
    note: str | None
    confidence: float | None
    evidence: tuple[MarkEvidenceRef, ...]
    evidence_messages: tuple[EvidenceMessageSnapshot, ...]
    audio_start_ms: int | None
    audio_end_ms: int | None
    source_state_version: int
    created_at: dt.datetime
    updated_at: dt.datetime


def _require_session(db_session: Session, session_id: str) -> SessionRecord:
    record = db_session.get(SessionRecord, session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    return record


def _evidence_records(
    db_session: Session,
    *,
    session_id: str,
    evidence: tuple[MarkEvidenceRef, ...],
) -> list[SegmentRecord]:
    segment_ids = tuple(value.segment_id for value in evidence)
    records = list(
        db_session.scalars(
            select(SegmentRecord).where(
                SegmentRecord.session_id == session_id,
                SegmentRecord.segment_id.in_(segment_ids),
                SegmentRecord.status == "final",
            )
        )
    )
    by_id = {record.segment_id: record for record in records}
    missing = set(segment_ids) - set(by_id)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Mark evidence does not belong to this Session",
        )
    stale = [
        value.segment_id
        for value in evidence
        if by_id[value.segment_id].revision != value.revision
    ]
    if stale:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Mark evidence revision is stale",
        )
    return [by_id[value.segment_id] for value in evidence]


def _projection_segment(record: SegmentRecord) -> ProjectionSegment:
    return ProjectionSegment(
        session_id=record.session_id,
        segment_id=record.segment_id,
        revision=record.revision,
        track_id=record.track_id,
        language=record.language,
        raw_text=record.raw_text,
        display_text=record.display_text,
        audio_start_ms=record.audio_start_ms,
        audio_end_ms=record.audio_end_ms,
        confidence=record.confidence,
        received_at_ms=record.received_at_ms,
        finalized_at=record.finalized_at,
        updated_at=record.updated_at,
    )


def _manual_mark_message(
    *,
    mark_id: str,
    session_id: str,
    actor_id: str,
    title: str,
    note: str | None,
    created_at: dt.datetime,
) -> UserInputEvidenceMessage:
    raw_text = title if note is None else f"{title}\n{note}"
    display_text = " ".join(raw_text.split())
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    identity = hashlib.sha256(
        f"{session_id}:{mark_id}:{actor_id}:{content_hash}".encode("utf-8")
    ).hexdigest()
    return UserInputEvidenceMessage(
        message_id=f"mark_{identity[:48]}",
        session_id=session_id,
        actor_id=actor_id,
        raw_text=raw_text,
        display_text=display_text,
        created_at=created_at,
        content_hash=content_hash,
    )


def _mark_response(
    record: MeetingMarkRecord,
) -> MeetingMarkResponse:
    segment_ids = tuple(record.source_segment_ids_json)
    stored_revisions = dict(record.source_segment_revisions_json)
    evidence_messages = _EVIDENCE_MESSAGES.validate_python(
        record.evidence_messages_json
    )
    return MeetingMarkResponse(
        mark_id=record.id,
        session_id=record.session_id,
        origin=record.origin,
        kind=record.kind,
        status=record.status,
        title=record.title,
        note=record.note,
        confidence=record.confidence,
        evidence=tuple(
            MarkEvidenceRef(
                segment_id=segment_id,
                revision=stored_revisions[segment_id],
            )
            for segment_id in segment_ids
            if segment_id in stored_revisions
        ),
        evidence_messages=evidence_messages,
        audio_start_ms=record.audio_start_ms,
        audio_end_ms=record.audio_end_ms,
        source_state_version=record.source_state_version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.get(
    "/{session_id}/meeting-state",
    response_model=MeetingStateResponse,
)
def get_meeting_state(
    session_id: str,
    settings: Settings = Depends(get_app_settings),
    db_session: Session = Depends(get_db_session),
) -> MeetingStateResponse:
    _require_session(db_session, session_id)
    view = read_meeting_state(
        db_session,
        session_id,
        stale_after_seconds=settings.meeting_state_stale_after_seconds,
    )
    return MeetingStateResponse(
        state=view.state,
        state_hash=view.state_hash,
        source_frontier=view.source_frontier,
        freshness=view.freshness,
    )


@router.get(
    "/{session_id}/marks",
    response_model=list[MeetingMarkResponse],
)
def list_meeting_marks(
    session_id: str,
    db_session: Session = Depends(get_db_session),
) -> list[MeetingMarkResponse]:
    _require_session(db_session, session_id)
    records = MeetingStateRepository(db_session).list_marks(session_id)
    return [_mark_response(record) for record in records]


@router.post(
    "/{session_id}/marks",
    dependencies=[Depends(reject_legacy_meeting_write)],
    response_model=MeetingMarkResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_meeting_mark(
    session_id: str,
    payload: MeetingMarkCreate,
    db_session: Session = Depends(get_db_session),
) -> MeetingMarkResponse:
    _require_session(db_session, session_id)
    evidence_records = _evidence_records(
        db_session,
        session_id=session_id,
        evidence=payload.evidence,
    )
    head = MeetingStateRepository(db_session).get_head(session_id)
    audio_starts = [
        record.audio_start_ms
        for record in evidence_records
        if record.audio_start_ms is not None
    ]
    audio_ends = [
        record.audio_end_ms
        for record in evidence_records
        if record.audio_end_ms is not None
    ]
    mark_id = str(uuid.uuid4())
    created_at = dt.datetime.now(dt.UTC)
    caption_messages = tuple(
        caption_evidence_message(_projection_segment(record))
        for record in evidence_records
    )
    user_message = _manual_mark_message(
        mark_id=mark_id,
        session_id=session_id,
        actor_id=payload.actor_id,
        title=payload.title,
        note=payload.note,
        created_at=created_at,
    )
    record = MeetingStateRepository(db_session).create_mark(
        session_id=session_id,
        origin="manual",
        kind=payload.kind,
        title=payload.title,
        note=payload.note,
        source_segment_ids=[value.segment_id for value in payload.evidence],
        source_segment_revisions={
            value.segment_id: value.revision for value in payload.evidence
        },
        evidence_messages=(*caption_messages, user_message),
        source_state_version=head.version if head is not None else 0,
        audio_start_ms=min(audio_starts) if audio_starts else None,
        audio_end_ms=max(audio_ends) if audio_ends else None,
        status="accepted",
        mark_id=mark_id,
        created_at=created_at,
    )
    db_session.commit()
    db_session.refresh(record)
    return _mark_response(record)


@router.patch(
    "/{session_id}/marks/{mark_id}",
    dependencies=[Depends(reject_legacy_meeting_write)],
    response_model=MeetingMarkResponse,
)
def update_meeting_mark(
    session_id: str,
    mark_id: str,
    payload: MeetingMarkPatch,
    db_session: Session = Depends(get_db_session),
) -> MeetingMarkResponse:
    _require_session(db_session, session_id)
    existing = db_session.get(MeetingMarkRecord, mark_id)
    if existing is None or existing.session_id != session_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Meeting mark not found",
        )
    if payload.status == "accepted":
        if payload.evidence is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Accepting a mark requires evidence revisions",
            )
        stored_revisions = dict(existing.source_segment_revisions_json)
        requested_revisions = {
            value.segment_id: value.revision for value in payload.evidence
        }
        if not stored_revisions or requested_revisions != stored_revisions:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Mark evidence does not match the proposed revisions",
            )
        _evidence_records(
            db_session,
            session_id=session_id,
            evidence=payload.evidence,
        )
    updated = MeetingStateRepository(db_session).update_mark_status(
        mark_id,
        payload.status,
    )
    db_session.commit()
    db_session.refresh(updated)
    return _mark_response(updated)


__all__ = ["router"]
