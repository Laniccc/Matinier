from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.meeting_state.contracts import (
    CancelCandidateOperation,
    CandidateContentProposal,
    CandidateOperation,
    CreateCandidateOperation,
    GroundedValueProposal,
    MergeCandidatesOperation,
    ProjectionSegment,
    ReviseCandidateOperation,
    SplitCandidateOperation,
)
from app.meeting_state.models import (
    ActionCandidate,
    ActionCandidateContent,
    AssigneeValue,
    CaptionEvidenceMessage,
    GroundedValue,
)
from app.meeting_state.repository import MeetingStateRepository
from app.persistence.models import (
    ActionCandidateRecord,
    ActionCandidateRevisionRecord,
    SegmentRecord,
    utc_now,
)


def _normalized(value: object) -> object:
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    if isinstance(value, Mapping):
        return {
            str(key): _normalized(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalized(item) for item in value]
    return value


def stable_candidate_id(
    *,
    kind: str,
    content: CandidateContentProposal,
    evidence_ids: Sequence[str],
) -> str:
    payload = json.dumps(
        {
            "kind": kind,
            "content": _normalized(content.model_dump(mode="json")),
            "evidence_ids": sorted(set(evidence_ids)),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return str(uuid.UUID(hex=digest[:32]))


def caption_evidence_message(segment: ProjectionSegment) -> CaptionEvidenceMessage:
    content_payload = json.dumps(
        {
            "session_id": segment.session_id,
            "segment_id": segment.segment_id,
            "revision": segment.revision,
            "raw_text": segment.raw_text,
            "display_text": segment.display_text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    content_hash = hashlib.sha256(content_payload.encode("utf-8")).hexdigest()
    message_hash = hashlib.sha256(
        f"{segment.session_id}:{segment.segment_id}:{segment.revision}".encode()
    ).hexdigest()
    return CaptionEvidenceMessage(
        message_id=f"caption_{message_hash[:48]}",
        session_id=segment.session_id,
        track_id=segment.track_id,
        segment_id=segment.segment_id,
        segment_revision=segment.revision,
        language=segment.language,
        raw_text=segment.raw_text,
        display_text=segment.display_text,
        audio_start_ms=segment.audio_start_ms,
        audio_end_ms=segment.audio_end_ms,
        confidence=segment.confidence,
        received_at_ms=segment.received_at_ms,
        finalized_at=segment.finalized_at,
        content_hash=content_hash,
    )


def _grounded(
    proposal: GroundedValueProposal[object],
    messages: Mapping[str, CaptionEvidenceMessage],
    *,
    value: object | None = None,
    resolution: str | None = None,
) -> GroundedValue[object]:
    return GroundedValue[object](
        value=proposal.value if value is None else value,
        origin="meeting_inferred",
        resolution=resolution or proposal.resolution,
        evidence_message_ids=tuple(
            messages[segment_id].message_id
            for segment_id in proposal.source_segment_ids
        ),
        confidence=proposal.confidence,
        explanation=proposal.explanation,
    )


def materialize_candidate_content(
    proposal: CandidateContentProposal,
    evidence_by_segment: Mapping[str, ProjectionSegment],
) -> ActionCandidateContent:
    missing = set(proposal.source_segment_ids) - set(evidence_by_segment)
    if missing:
        raise ValueError(
            "candidate references unavailable Segment evidence: "
            + ", ".join(sorted(missing))
        )
    messages = {
        segment_id: caption_evidence_message(evidence_by_segment[segment_id])
        for segment_id in proposal.source_segment_ids
    }

    assignee_value: AssigneeValue | None = None
    assignee_resolution = proposal.assignee.resolution
    if proposal.assignee.value is not None:
        assignee_value = AssigneeValue(
            spoken_text=proposal.assignee.value,
            linear_user_id=None,
            is_placeholder=True,
        )
        if assignee_resolution == "known":
            assignee_resolution = "ambiguous"

    return ActionCandidateContent(
        title=_grounded(proposal.title, messages),
        deliverable=_grounded(proposal.deliverable, messages),
        assignee=_grounded(
            proposal.assignee,
            messages,
            value=assignee_value,
            resolution=assignee_resolution,
        ),
        due_at=_grounded(proposal.due_at, messages),
        priority=_grounded(proposal.priority, messages),
        evidence_messages=tuple(messages.values()),
        blocking_conflict_message_ids=tuple(
            messages[segment_id].message_id
            for segment_id in proposal.blocking_conflict_segment_ids
        ),
    )


def _candidate_source_ids(content: ActionCandidateContent) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            message.segment_id
            for message in content.evidence_messages
            if isinstance(message, CaptionEvidenceMessage)
        )
    )


def _readiness(content: ActionCandidateContent) -> str:
    if (
        content.title.resolution == "known"
        and content.title.value is not None
        and content.deliverable.resolution == "known"
        and content.deliverable.value is not None
    ):
        return "recordable"
    return "detected"


def load_action_candidates(
    db_session: Session,
    *,
    session_id: str,
    current_segment_revisions: Mapping[str, int],
    candidate_ids: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[ActionCandidate, ...]:
    statement = (
        select(ActionCandidateRecord)
        .where(ActionCandidateRecord.session_id == session_id)
        .order_by(ActionCandidateRecord.created_at, ActionCandidateRecord.id)
        .offset(max(0, offset))
    )
    if candidate_ids is not None:
        statement = statement.where(ActionCandidateRecord.id.in_(tuple(candidate_ids)))
    if limit is not None:
        statement = statement.limit(max(1, min(limit, 101)))
    records = list(
        db_session.scalars(statement)
    )
    candidates: list[ActionCandidate] = []
    for record in records:
        revision = db_session.scalar(
            select(ActionCandidateRevisionRecord).where(
                ActionCandidateRevisionRecord.candidate_id == record.id,
                ActionCandidateRevisionRecord.revision == record.current_revision,
            )
        )
        if revision is None:
            raise LookupError(
                f"current Action Candidate revision not found: {record.id}"
            )
        content = ActionCandidateContent.model_validate(revision.content_json)
        revisions_current = all(
            not isinstance(message, CaptionEvidenceMessage)
            or current_segment_revisions.get(message.segment_id)
            == message.segment_revision
            for message in content.evidence_messages
        )
        candidates.append(
            ActionCandidate(
                candidate_id=record.id,
                lineage_root_id=record.lineage_root_id,
                session_id=record.session_id,
                current_revision=record.current_revision,
                readiness=revision.readiness,
                content_status=record.content_status,
                execution_status=record.execution_status,
                content=content,
                source_segment_ids=_candidate_source_ids(content),
                evidence_revisions_current=revisions_current,
                derived_from_candidate_id=record.derived_from_candidate_id,
                derived_from_revision=record.derived_from_revision,
                superseded_by_candidate_id=record.superseded_by_candidate_id,
            )
        )
    return tuple(candidates)


def _logical_key(content: ActionCandidateContent) -> tuple[str, str]:
    return (
        " ".join(str(content.title.value or "").casefold().split()),
        " ".join(str(content.deliverable.value or "").casefold().split()),
    )


def _utc_instant(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _assignees_compatible(
    left: AssigneeValue | None,
    right: AssigneeValue | None,
) -> bool:
    if left is None or right is None:
        return True
    if left.linear_user_id is not None and right.linear_user_id is not None:
        return left.linear_user_id == right.linear_user_id
    return _normalized(left.spoken_text) == _normalized(right.spoken_text)


def _safe_merge(contents: Sequence[ActionCandidateContent]) -> bool:
    if len(contents) != 2:
        return False
    left, right = contents
    logical_key = _logical_key(left)
    if not all(logical_key) or logical_key != _logical_key(right):
        return False
    grounded_values = (
        left.title,
        left.deliverable,
        left.assignee,
        left.due_at,
        left.priority,
        right.title,
        right.deliverable,
        right.assignee,
        right.due_at,
        right.priority,
    )
    if any(value.resolution == "conflicting" for value in grounded_values):
        return False
    if left.blocking_conflict_message_ids or right.blocking_conflict_message_ids:
        return False
    if not _assignees_compatible(left.assignee.value, right.assignee.value):
        return False
    if (
        left.due_at.value is not None
        and right.due_at.value is not None
        and _utc_instant(left.due_at.value) != _utc_instant(right.due_at.value)
    ):
        return False
    if (
        left.priority.value is not None
        and right.priority.value is not None
        and left.priority.value != right.priority.value
    ):
        return False
    return True


def _merge_grounded(left: GroundedValue, right: GroundedValue) -> GroundedValue:
    rank = {"missing": 0, "ambiguous": 1, "known": 2, "conflicting": -1}
    chosen, other = (
        (right, left)
        if rank[right.resolution] > rank[left.resolution]
        else (left, right)
    )
    confidences = [
        value
        for value in (left.confidence, right.confidence)
        if value is not None
    ]
    return chosen.model_copy(
        update={
            "evidence_message_ids": tuple(
                dict.fromkeys(
                    (*left.evidence_message_ids, *right.evidence_message_ids)
                )
            ),
            "confidence": max(confidences) if confidences else None,
            "explanation": chosen.explanation or other.explanation,
        }
    )


def _merge_content(
    survivor: ActionCandidateContent,
    duplicate: ActionCandidateContent,
) -> ActionCandidateContent:
    messages = {
        message.message_id: message
        for message in (*survivor.evidence_messages, *duplicate.evidence_messages)
    }
    return ActionCandidateContent(
        title=_merge_grounded(survivor.title, duplicate.title),
        deliverable=_merge_grounded(survivor.deliverable, duplicate.deliverable),
        assignee=_merge_grounded(survivor.assignee, duplicate.assignee),
        due_at=_merge_grounded(survivor.due_at, duplicate.due_at),
        priority=_merge_grounded(survivor.priority, duplicate.priority),
        evidence_messages=tuple(messages.values()),
        blocking_conflict_message_ids=tuple(
            dict.fromkeys(
                (
                    *survivor.blocking_conflict_message_ids,
                    *duplicate.blocking_conflict_message_ids,
                )
            )
        ),
    )


class CandidateMergePolicy:
    """Turns model proposals into revision-checked deterministic mutations."""

    def __init__(self, db_session: Session) -> None:
        self._db_session = db_session
        self._repository = MeetingStateRepository(db_session)

    def apply(
        self,
        *,
        session_id: str,
        operations: Sequence[CandidateOperation],
        evidence_by_segment: Mapping[str, ProjectionSegment],
    ) -> tuple[ActionCandidate, ...]:
        self._validate_current_evidence(session_id, evidence_by_segment)
        for operation in operations:
            self._apply_operation(
                session_id=session_id,
                operation=operation,
                evidence_by_segment=evidence_by_segment,
            )
        current_revisions = dict(
            self._db_session.execute(
                select(SegmentRecord.segment_id, SegmentRecord.revision).where(
                    SegmentRecord.session_id == session_id,
                    SegmentRecord.status == "final",
                )
            ).all()
        )
        return load_action_candidates(
            self._db_session,
            session_id=session_id,
            current_segment_revisions=current_revisions,
        )

    def _validate_current_evidence(
        self,
        session_id: str,
        evidence_by_segment: Mapping[str, ProjectionSegment],
    ) -> None:
        if any(
            segment.session_id != session_id
            for segment in evidence_by_segment.values()
        ):
            raise ValueError("candidate evidence belongs to another Session")
        current = dict(
            self._db_session.execute(
                select(SegmentRecord.segment_id, SegmentRecord.revision).where(
                    SegmentRecord.session_id == session_id,
                    SegmentRecord.segment_id.in_(tuple(evidence_by_segment)),
                    SegmentRecord.status == "final",
                )
            ).all()
        )
        stale = [
            segment_id
            for segment_id, segment in evidence_by_segment.items()
            if current.get(segment_id) != segment.revision
        ]
        if stale:
            raise ValueError(
                "candidate evidence is not the current Final revision: "
                + ", ".join(sorted(stale))
            )

    def _apply_operation(
        self,
        *,
        session_id: str,
        operation: CandidateOperation,
        evidence_by_segment: Mapping[str, ProjectionSegment],
    ) -> None:
        if isinstance(operation, CreateCandidateOperation):
            self._create(session_id, operation, evidence_by_segment)
        elif isinstance(operation, ReviseCandidateOperation):
            self._revise(session_id, operation, evidence_by_segment)
        elif isinstance(operation, MergeCandidatesOperation):
            self._merge(session_id, operation, evidence_by_segment)
        elif isinstance(operation, SplitCandidateOperation):
            self._split(session_id, operation, evidence_by_segment)
        elif isinstance(operation, CancelCandidateOperation):
            self._cancel(session_id, operation, evidence_by_segment)
        else:
            raise TypeError(f"unsupported candidate operation: {type(operation)!r}")

    def _get_record(self, session_id: str, candidate_id: str) -> ActionCandidateRecord:
        record = self._db_session.get(ActionCandidateRecord, candidate_id)
        if record is None:
            raise LookupError(f"Action Candidate not found: {candidate_id}")
        if record.session_id != session_id:
            raise ValueError("Action Candidate belongs to another Session")
        return record

    def _current_content(self, record: ActionCandidateRecord) -> ActionCandidateContent:
        revision = self._db_session.scalar(
            select(ActionCandidateRevisionRecord).where(
                ActionCandidateRevisionRecord.candidate_id == record.id,
                ActionCandidateRevisionRecord.revision == record.current_revision,
            )
        )
        if revision is None:
            raise LookupError(f"Action Candidate revision not found: {record.id}")
        return ActionCandidateContent.model_validate(revision.content_json)

    def _create(
        self,
        session_id: str,
        operation: CreateCandidateOperation,
        evidence_by_segment: Mapping[str, ProjectionSegment],
        *,
        identity_kind: str = "action_candidate",
        derived_from: ActionCandidateRecord | None = None,
    ) -> ActionCandidateRecord:
        content = materialize_candidate_content(operation.content, evidence_by_segment)
        candidate_id = stable_candidate_id(
            kind=identity_kind,
            content=operation.content,
            evidence_ids=operation.content.source_segment_ids,
        )
        existing = self._db_session.get(ActionCandidateRecord, candidate_id)
        if existing is not None:
            if existing.session_id != session_id:
                raise ValueError("stable Candidate ID belongs to another Session")
            return existing
        return self._repository.create_candidate(
            session_id=session_id,
            candidate_id=candidate_id,
            # A split child is a new independently executable deliverable and
            # therefore starts a new idempotency lineage. Revisions and merges
            # keep the surviving Candidate's existing lineage instead.
            lineage_root_id=candidate_id,
            content=content,
            readiness=_readiness(content),
            derived_from_candidate_id=(derived_from.id if derived_from else None),
            derived_from_revision=(
                derived_from.current_revision if derived_from else None
            ),
            change_summary=operation.change_summary,
        )

    def _revise(
        self,
        session_id: str,
        operation: ReviseCandidateOperation,
        evidence_by_segment: Mapping[str, ProjectionSegment],
    ) -> None:
        record = self._get_record(session_id, operation.candidate_id)
        if record.current_revision != operation.expected_revision:
            raise ValueError("candidate revision changed before revise")
        if record.content_status != "active":
            raise ValueError("only active candidates can be revised")
        if record.execution_status == "executing":
            raise ValueError("an executing candidate cannot be revised")
        content = materialize_candidate_content(operation.content, evidence_by_segment)
        self._repository.append_candidate_revision(
            candidate_id=record.id,
            expected_revision=operation.expected_revision,
            content=content,
            readiness=_readiness(content),
            change_kind="revise",
            change_summary=operation.change_summary,
        )

    def _merge(
        self,
        session_id: str,
        operation: MergeCandidatesOperation,
        evidence_by_segment: Mapping[str, ProjectionSegment],
    ) -> None:
        missing = set(operation.source_segment_ids) - set(evidence_by_segment)
        if missing:
            raise ValueError("merge references unavailable evidence")
        records = [self._get_record(session_id, value) for value in operation.candidate_ids]
        for record in records:
            if record.current_revision != operation.expected_revisions[record.id]:
                raise ValueError("candidate revision changed before merge")
            if record.content_status != "active" or record.execution_status != "not_requested":
                raise ValueError("only active, unrequested candidates can be merged")
        contents = [self._current_content(record) for record in records]
        if not _safe_merge(contents):
            raise ValueError("candidate merge is not a safe logical duplicate")

        ordered = sorted(records, key=lambda value: (value.created_at, value.id))
        survivor, duplicate = ordered
        survivor_content = self._current_content(survivor)
        duplicate_content = self._current_content(duplicate)
        merged_content = _merge_content(survivor_content, duplicate_content)
        self._repository.append_candidate_revision(
            candidate_id=survivor.id,
            expected_revision=survivor.current_revision,
            content=merged_content,
            readiness=_readiness(merged_content),
            change_kind="merge",
            change_summary=operation.change_summary,
        )
        self._repository.supersede_candidate(
            duplicate.id,
            superseded_by_candidate_id=survivor.id,
        )

    def _split(
        self,
        session_id: str,
        operation: SplitCandidateOperation,
        evidence_by_segment: Mapping[str, ProjectionSegment],
    ) -> None:
        if set(operation.source_segment_ids) - set(evidence_by_segment):
            raise ValueError("split references unavailable evidence")
        parent = self._get_record(session_id, operation.candidate_id)
        if parent.current_revision != operation.expected_revision:
            raise ValueError("candidate revision changed before split")
        if parent.content_status != "active" or parent.execution_status != "not_requested":
            raise ValueError("only active, unrequested candidates can be split")

        children: list[ActionCandidateRecord] = []
        for index, proposal in enumerate(operation.parts):
            child_operation = CreateCandidateOperation(
                content=proposal,
                change_summary=operation.change_summary,
            )
            children.append(
                self._create(
                    session_id,
                    child_operation,
                    evidence_by_segment,
                    identity_kind=f"action_candidate_split:{parent.id}:{index}",
                    derived_from=parent,
                )
            )
        self._repository.supersede_candidate(
            parent.id,
            superseded_by_candidate_id=children[0].id,
        )

    def _cancel(
        self,
        session_id: str,
        operation: CancelCandidateOperation,
        evidence_by_segment: Mapping[str, ProjectionSegment],
    ) -> None:
        if set(operation.source_segment_ids) - set(evidence_by_segment):
            raise ValueError("cancel references unavailable evidence")
        record = self._get_record(session_id, operation.candidate_id)
        if record.current_revision != operation.expected_revision:
            raise ValueError("candidate revision changed before cancel")
        if record.content_status != "active":
            raise ValueError("only active candidates can be cancelled")
        content = self._current_content(record)
        self._repository.append_candidate_revision(
            candidate_id=record.id,
            expected_revision=record.current_revision,
            content=content,
            readiness=_readiness(content),
            change_kind="cancel",
            change_summary=operation.reason,
        )
        record.content_status = "cancelled"
        record.updated_at = utc_now()
        self._db_session.flush()
