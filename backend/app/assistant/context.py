from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.assistant.models import ContextSnapshot
from app.assistant.repository import AssistantRepository
from app.meeting_state.candidates import caption_evidence_message
from app.meeting_state.contracts import ProjectionSegment
from app.meeting_state.merge import canonical_state_hash
from app.meeting_state.models import (
    ActionCandidate,
    CaptionEvidenceMessage,
    EvidenceMessageSnapshot,
    MeetingState,
    MeetingStateItem,
    MeetingStateStatus,
    UserInputEvidenceMessage,
)
from app.meeting_state.projector import MeetingStateProjector
from app.persistence.models import (
    MeetingMarkRecord,
    MeetingProjectionOffsetRecord,
    MeetingStateHeadRecord,
    SegmentRecord,
    SessionRecord,
    utc_now,
)


logger = logging.getLogger(__name__)
_EVIDENCE_MESSAGES = TypeAdapter(tuple[EvidenceMessageSnapshot, ...])


class ContextSizeBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_chars: int = Field(default=8_000, ge=1_000, le=48_000)
    max_state_items: int = Field(default=24, ge=1, le=100)
    max_tail_segments: int = Field(default=24, ge=1, le=50)
    max_evidence_messages: int = Field(default=64, ge=1, le=200)


class MeetingStateFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MeetingStateStatus
    state_updated_at: dt.datetime | None
    latest_final_updated_at: dt.datetime | None
    projected_through: dt.datetime | None
    lag_ms: int = Field(ge=0)
    pending_segment_count: int = Field(ge=0)
    has_unprojected_tail: bool
    last_success_at: dt.datetime | None
    last_error_at: dt.datetime | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class MeetingStateRead:
    state: MeetingState
    state_hash: str
    source_frontier: dict[str, Any]
    freshness: MeetingStateFreshness


@dataclass(frozen=True, slots=True)
class _RankedItem:
    category: str
    key: str
    text: str
    payload: dict[str, Any]
    source_segment_ids: tuple[str, ...]
    source_segment_revisions: Mapping[str, int] = field(default_factory=dict)
    evidence_messages: tuple[EvidenceMessageSnapshot, ...] = ()
    candidate: ActionCandidate | None = None
    forced: bool = False


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _segment_snapshot(record: SegmentRecord) -> ProjectionSegment:
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


def _caption_message(record: SegmentRecord) -> CaptionEvidenceMessage:
    return caption_evidence_message(_segment_snapshot(record))


def _offset_match():
    return (
        (MeetingProjectionOffsetRecord.session_id == SegmentRecord.session_id)
        & (MeetingProjectionOffsetRecord.segment_id == SegmentRecord.segment_id)
    )


def read_meeting_state(
    db_session: Session,
    session_id: str,
    *,
    stale_after_seconds: float = 10.0,
) -> MeetingStateRead:
    if db_session.get(SessionRecord, session_id) is None:
        raise LookupError(f"Session not found: {session_id}")
    head = db_session.get(MeetingStateHeadRecord, session_id)
    state = (
        MeetingState.model_validate(head.state_json)
        if head is not None
        else MeetingState(session_id=session_id, version=0)
    )
    pending_statement = (
        select(
            func.count(SegmentRecord.id),
            func.min(SegmentRecord.updated_at),
            func.max(SegmentRecord.updated_at),
        )
        .outerjoin(MeetingProjectionOffsetRecord, _offset_match())
        .where(
            SegmentRecord.session_id == session_id,
            SegmentRecord.status == "final",
            or_(
                MeetingProjectionOffsetRecord.processed_revision.is_(None),
                MeetingProjectionOffsetRecord.processed_revision
                < SegmentRecord.revision,
            ),
        )
    )
    pending_count, oldest_pending, latest_pending = db_session.execute(
        pending_statement
    ).one()
    pending_count = int(pending_count or 0)

    if head is None:
        status: MeetingStateStatus = "ready" if pending_count == 0 else "lagging"
        lag_ms = 0
        if oldest_pending is not None:
            lag_ms = max(
                0,
                int((utc_now() - _as_utc(oldest_pending)).total_seconds() * 1_000),
            )
            if lag_ms >= stale_after_seconds * 1_000:
                status = "stale"
        source_frontier: dict[str, Any] = {}
        state_hash = canonical_state_hash(state)
        freshness = MeetingStateFreshness(
            status=status,
            state_updated_at=None,
            latest_final_updated_at=latest_pending,
            projected_through=None,
            lag_ms=lag_ms,
            pending_segment_count=pending_count,
            has_unprojected_tail=pending_count > 0,
            last_success_at=None,
            last_error_at=None,
            last_error_code=None,
        )
    else:
        status = head.status
        lag_ms = head.lag_ms
        if pending_count > 0 and oldest_pending is not None:
            current_lag = max(
                0,
                int((utc_now() - _as_utc(oldest_pending)).total_seconds() * 1_000),
            )
            lag_ms = max(lag_ms, current_lag)
            status = (
                "stale"
                if lag_ms >= stale_after_seconds * 1_000
                else "lagging"
            )
        elif pending_count == 0 and status in {"lagging", "stale"}:
            status = "ready"
        source_frontier = dict(head.source_frontier_json)
        state_hash = head.state_hash
        freshness = MeetingStateFreshness(
            status=status,
            state_updated_at=head.updated_at,
            latest_final_updated_at=(
                latest_pending or head.latest_final_updated_at
            ),
            projected_through=head.projected_through,
            lag_ms=lag_ms,
            pending_segment_count=pending_count,
            has_unprojected_tail=pending_count > 0,
            last_success_at=head.last_success_at,
            last_error_at=head.last_error_at,
            last_error_code=head.last_error_code,
        )
    return MeetingStateRead(
        state=state,
        state_hash=state_hash,
        source_frontier=source_frontier,
        freshness=freshness,
    )


def _tokens(value: str) -> frozenset[str]:
    normalized = value.casefold()
    latin = re.findall(r"[a-z0-9_]+", normalized)
    cjk = re.findall(r"[\u4e00-\u9fff]", normalized)
    return frozenset((*latin, *cjk))


def _score(goal_tokens: frozenset[str], item: _RankedItem) -> tuple[int, str, str]:
    overlap = len(goal_tokens & _tokens(item.text))
    return (overlap + (10_000 if item.forced else 0), item.category, item.key)


def _candidate_text(candidate: ActionCandidate) -> str:
    content = candidate.content
    values = [content.title.value, content.deliverable.value]
    if content.assignee.value is not None:
        values.append(content.assignee.value.spoken_text)
    if content.due_at.value is not None:
        values.append(content.due_at.value.isoformat())
    if content.priority.value is not None:
        values.append(content.priority.value)
    return " ".join(str(value) for value in values if value is not None)


def _candidate_payload(candidate: ActionCandidate) -> dict[str, Any]:
    payload = candidate.model_dump(mode="json")
    payload["content"].pop("evidence_messages", None)
    return payload


def _state_items(state: MeetingState) -> list[_RankedItem]:
    values: list[_RankedItem] = []
    for category in (
        "topics",
        "entities",
        "decisions",
        "highlights",
        "conflicts",
        "user_concerns",
    ):
        for item in getattr(state, category):
            values.append(
                _RankedItem(
                    category=category,
                    key=item.item_id,
                    text=item.text,
                    payload=item.model_dump(mode="json"),
                    source_segment_ids=item.source_segment_ids,
                    source_segment_revisions=item.source_segment_revisions,
                )
            )
    for candidate in state.action_candidates:
        values.append(
            _RankedItem(
                category="action_candidates",
                key=candidate.candidate_id,
                text=_candidate_text(candidate),
                payload=_candidate_payload(candidate),
                source_segment_ids=candidate.source_segment_ids,
                candidate=candidate,
            )
        )
    return values


def _mark_item(mark: MeetingMarkRecord) -> _RankedItem:
    evidence_messages = _EVIDENCE_MESSAGES.validate_python(
        mark.evidence_messages_json
    )
    if not evidence_messages or not mark.source_segment_revisions_json:
        raise ValueError(
            f"Meeting mark {mark.id} does not contain frozen evidence"
        )
    return _RankedItem(
        category="marks",
        key=mark.id,
        text=" ".join(value for value in (mark.title, mark.note) if value),
        payload={
            "mark_id": mark.id,
            "origin": mark.origin,
            "kind": mark.kind,
            "status": mark.status,
            "title": mark.title,
            "note": mark.note,
            "confidence": mark.confidence,
            "source_segment_ids": list(mark.source_segment_ids_json),
            "source_segment_revisions": dict(
                mark.source_segment_revisions_json
            ),
            "audio_start_ms": mark.audio_start_ms,
            "audio_end_ms": mark.audio_end_ms,
            "source_state_version": mark.source_state_version,
        },
        source_segment_ids=tuple(mark.source_segment_ids_json),
        source_segment_revisions=dict(mark.source_segment_revisions_json),
        evidence_messages=evidence_messages,
        forced=True,
    )


def _candidate_field_refs(
    candidate: ActionCandidate,
) -> dict[str, tuple[str, ...]]:
    prefix = f"action_candidates.{candidate.candidate_id}"
    return {
        f"{prefix}.{field_name}": getattr(
            candidate.content,
            field_name,
        ).evidence_message_ids
        for field_name in ("title", "deliverable", "assignee", "due_at", "priority")
    }


def _user_goal_message(
    *,
    session_id: str,
    actor_id: str,
    goal: str,
    created_at: dt.datetime,
) -> UserInputEvidenceMessage:
    content_hash = hashlib.sha256(goal.encode("utf-8")).hexdigest()
    identity = hashlib.sha256(
        f"{session_id}:{actor_id}:{goal}".encode("utf-8")
    ).hexdigest()
    return UserInputEvidenceMessage(
        message_id=f"user_{identity[:48]}",
        session_id=session_id,
        actor_id=actor_id,
        raw_text=goal,
        display_text=goal,
        created_at=created_at,
        content_hash=content_hash,
    )


def _canonical_hash(
    *,
    meeting_state_slice: Mapping[str, object],
    marks: Sequence[Mapping[str, object]],
    field_evidence_refs: Mapping[str, Sequence[str]],
    messages: Sequence[EvidenceMessageSnapshot],
) -> str:
    payload = {
        "meeting_state": meeting_state_slice,
        "marks": list(marks),
        "field_evidence_refs": {
            key: list(value) for key, value in sorted(field_evidence_refs.items())
        },
        "evidence": sorted(
            (
                {
                    "message_id": message.message_id,
                    "content_hash": message.content_hash,
                    "segment_revision": getattr(message, "segment_revision", None),
                }
                for message in messages
            ),
            key=lambda value: value["message_id"],
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ContextBuilder:
    def __init__(
        self,
        db_session: Session,
        *,
        projector: MeetingStateProjector | None = None,
        stale_after_seconds: float = 10.0,
    ) -> None:
        self._db_session = db_session
        self._projector = projector
        self._stale_after_seconds = stale_after_seconds

    async def build(
        self,
        *,
        session_id: str,
        goal: str,
        mark_ids: Sequence[str] = (),
        candidate_ids: Sequence[str] = (),
        size_budget: ContextSizeBudget | None = None,
        actor_id: str = "local-user",
        fast_ask: bool = False,
        persist: bool = True,
    ) -> ContextSnapshot:
        return self.build_sync(
            session_id=session_id, goal=goal, mark_ids=mark_ids,
            candidate_ids=candidate_ids, size_budget=size_budget,
            actor_id=actor_id, fast_ask=fast_ask, persist=persist,
        )

    def build_sync(
        self,
        *,
        session_id: str,
        goal: str,
        mark_ids: Sequence[str] = (),
        candidate_ids: Sequence[str] = (),
        size_budget: ContextSizeBudget | None = None,
        actor_id: str = "local-user",
        fast_ask: bool = False,
        persist: bool = True,
    ) -> ContextSnapshot:
        normalized_goal = " ".join(goal.split())
        if not normalized_goal:
            raise ValueError("context goal is required")
        budget = size_budget or ContextSizeBudget()
        meeting_read = read_meeting_state(
            self._db_session,
            session_id,
            stale_after_seconds=self._stale_after_seconds,
        )
        marks = self._selected_marks(session_id, mark_ids)
        tail_records = self._tail_segments(
            session_id,
            min(budget.max_tail_segments, budget.max_evidence_messages - 1),
        )
        tail_char_limit = max(0, int(budget.max_chars * 0.6) - len(normalized_goal))
        bounded_tail: list[SegmentRecord] = []
        tail_chars = 0
        for record in reversed(tail_records):
            # Tail text is stored both in state_slice and evidence_messages.
            record_chars = 2 * (len(record.raw_text) + len(record.display_text))
            if tail_chars + record_chars > tail_char_limit:
                continue
            bounded_tail.append(record)
            tail_chars += record_chars
        if not bounded_tail and tail_records:
            latest = tail_records[-1]
            latest_chars = 2 * (len(latest.raw_text) + len(latest.display_text))
            if latest_chars + len(normalized_goal) > budget.max_chars:
                raise ValueError("context budget is too small for the latest Final tail")
            bounded_tail.append(latest)
        tail_records = list(reversed(bounded_tail))
        now = utc_now()
        goal_message = _user_goal_message(
            session_id=session_id,
            actor_id=actor_id,
            goal=normalized_goal,
            created_at=now,
        )

        selected_candidate_ids = frozenset(candidate_ids)
        ranked = [_mark_item(mark) for mark in marks]
        ranked.extend(_state_items(meeting_read.state))
        available_candidate_ids = {
            item.key
            for item in ranked
            if item.category == "action_candidates"
        }
        missing_candidate_ids = selected_candidate_ids - available_candidate_ids
        if missing_candidate_ids:
            raise LookupError(
                "Action Candidate not found in the current Meeting State: "
                + ", ".join(sorted(missing_candidate_ids))
            )
        ranked = [
            replace(item, forced=True)
            if item.category == "action_candidates"
            and item.key in selected_candidate_ids
            else item
            for item in ranked
        ]
        goal_tokens = _tokens(normalized_goal)
        ranked.sort(key=lambda item: _score(goal_tokens, item), reverse=True)
        ranked = ranked[: budget.max_state_items + len(marks)]

        segment_ids = {
            segment_id
            for item in ranked
            for segment_id in item.source_segment_ids
        }
        segment_records = self._segments_by_id(session_id, segment_ids)
        segment_messages = {
            segment_id: _caption_message(record)
            for segment_id, record in segment_records.items()
        }
        tail_messages = tuple(_caption_message(record) for record in tail_records)

        selected: dict[str, list[dict[str, Any]]] = {
            "topics": [],
            "entities": [],
            "decisions": [],
            "action_candidates": [],
            "highlights": [],
            "conflicts": [],
            "user_concerns": [],
        }
        selected_marks: list[dict[str, Any]] = []
        field_refs: dict[str, tuple[str, ...]] = {
            "user_goal": (goal_message.message_id,)
        }
        messages_by_id: dict[str, EvidenceMessageSnapshot] = {
            goal_message.message_id: goal_message
        }
        for message in tail_messages:
            messages_by_id[message.message_id] = message

        used_chars = len(normalized_goal) + sum(
            2 * (len(message.raw_text) + len(message.display_text))
            for message in tail_messages
        )
        for item in ranked:
            item_messages: list[EvidenceMessageSnapshot]
            item_refs: dict[str, tuple[str, ...]]
            if item.category in {
                "topics",
                "entities",
                "decisions",
                "highlights",
                "conflicts",
            } and not item.source_segment_revisions:
                continue
            if (
                item.category != "marks"
                and item.source_segment_revisions
                and any(
                    segment_id not in segment_messages
                    or segment_messages[segment_id].segment_revision != revision
                    for segment_id, revision in item.source_segment_revisions.items()
                )
            ):
                continue
            if item.evidence_messages:
                item_messages = list(item.evidence_messages)
                item_refs = {
                    f"{item.category}.{item.key}": tuple(
                        message.message_id for message in item_messages
                    )
                }
            elif item.candidate is not None:
                item_messages = list(item.candidate.content.evidence_messages)
                item_refs = _candidate_field_refs(item.candidate)
            else:
                item_messages = [
                    segment_messages[segment_id]
                    for segment_id in item.source_segment_ids
                    if segment_id in segment_messages
                ]
                item_refs = {
                    f"{item.category}.{item.key}": tuple(
                        message.message_id for message in item_messages
                    )
                }
            new_messages = [
                message
                for message in item_messages
                if message.message_id not in messages_by_id
            ]
            item_cost = len(
                json.dumps(item.payload, ensure_ascii=False, separators=(",", ":"))
            ) + sum(
                len(message.raw_text) + len(message.display_text)
                for message in new_messages
            )
            fits = (
                used_chars + item_cost <= budget.max_chars
                and len(messages_by_id) + len(new_messages)
                <= budget.max_evidence_messages
            )
            if not fits and not item.forced:
                continue
            if not fits and item.forced:
                raise ValueError("context budget is too small for selected marks")
            used_chars += item_cost
            for message in new_messages:
                messages_by_id[message.message_id] = message
            field_refs.update(item_refs)
            if item.category == "marks":
                selected_marks.append(item.payload)
            else:
                selected[item.category].append(item.payload)

        tail_payload = [message.model_dump(mode="json") for message in tail_messages]
        state_slice: dict[str, Any] = {
            "user_goal_message_id": goal_message.message_id,
            "meeting_state": selected,
            "marks": selected_marks,
            "freshness": meeting_read.freshness.model_dump(mode="json"),
            "field_evidence_refs": {
                key: list(value) for key, value in sorted(field_refs.items())
            },
            "tail_segments": tail_payload,
        }
        messages = tuple(messages_by_id.values())
        evidence_refs = tuple(message.message_id for message in messages)
        context_hash = _canonical_hash(
            meeting_state_slice=selected,
            marks=selected_marks,
            field_evidence_refs=field_refs,
            messages=messages,
        )
        snapshot_id = str(uuid.uuid4())
        if persist:
            record = AssistantRepository(self._db_session).create_context_snapshot(
                snapshot_id=snapshot_id,
                session_id=session_id,
                meeting_state_version=meeting_read.state.version,
                state_slice=state_slice,
                source_frontier=meeting_read.source_frontier,
                evidence_refs=evidence_refs,
                evidence_messages=messages,
                relevant_context_hash=context_hash,
                created_at=now,
            )
            snapshot_id = record.id

        if fast_ask and self._projector is not None and self._projector.can_request_catch_up(session_id):
            task = asyncio.create_task(
                self._projector.request_catch_up(session_id),
                name=f"meeting-state-catch-up-{session_id}",
            )
            task.add_done_callback(self._consume_catch_up_result)

        return ContextSnapshot(
            snapshot_id=snapshot_id,
            session_id=session_id,
            meeting_state_version=meeting_read.state.version,
            state_slice=state_slice,
            source_frontier=meeting_read.source_frontier,
            evidence_refs=evidence_refs,
            evidence_messages=messages,
            relevant_context_hash=context_hash,
            created_at=now,
        )

    def _selected_marks(
        self,
        session_id: str,
        mark_ids: Sequence[str],
    ) -> list[MeetingMarkRecord]:
        unique_ids = tuple(dict.fromkeys(mark_ids))
        if not unique_ids:
            return []
        marks = list(
            self._db_session.scalars(
                select(MeetingMarkRecord)
                .where(MeetingMarkRecord.id.in_(unique_ids))
                .order_by(MeetingMarkRecord.created_at, MeetingMarkRecord.id)
            )
        )
        found = {mark.id for mark in marks}
        missing = set(unique_ids) - found
        if missing:
            raise LookupError("Meeting mark not found: " + ", ".join(sorted(missing)))
        if any(mark.session_id != session_id for mark in marks):
            raise ValueError("selected Meeting mark belongs to another Session")
        return marks

    def _segments_by_id(
        self,
        session_id: str,
        segment_ids: set[str],
    ) -> dict[str, SegmentRecord]:
        if not segment_ids:
            return {}
        records = list(
            self._db_session.scalars(
                select(SegmentRecord).where(
                    SegmentRecord.session_id == session_id,
                    SegmentRecord.segment_id.in_(tuple(segment_ids)),
                    SegmentRecord.status == "final",
                )
            )
        )
        return {record.segment_id: record for record in records}

    def _tail_segments(
        self,
        session_id: str,
        limit: int,
    ) -> list[SegmentRecord]:
        records = list(
            self._db_session.scalars(
                select(SegmentRecord)
                .where(
                    SegmentRecord.session_id == session_id,
                    SegmentRecord.status == "final",
                    func.length(func.trim(SegmentRecord.raw_text)) > 0,
                    func.length(func.trim(SegmentRecord.display_text)) > 0,
                )
                .order_by(
                    SegmentRecord.finalized_at.desc(),
                    SegmentRecord.id.desc(),
                )
                .limit(limit)
            )
        )
        records.reverse()
        return records

    @staticmethod
    def _consume_catch_up_result(task: asyncio.Task[object]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as error:
            logger.warning(
                "Fast Ask catch-up request failed",
                extra={
                    "event": "fast_ask_catch_up_request_failed",
                    "internal_error_type": type(error).__name__,
                },
            )


__all__ = [
    "ContextBuilder",
    "ContextSizeBudget",
    "MeetingStateFreshness",
    "MeetingStateRead",
    "read_meeting_state",
]
