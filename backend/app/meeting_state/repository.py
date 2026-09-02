from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.meeting_state.models import (
    ActionCandidateContent,
    ActionReadiness,
    CaptionEvidenceMessage,
    CandidateChangeKind,
    CandidateContentStatus,
    CandidateExecutionStatus,
    IdentityBindingStatus,
    EvidenceMessageSnapshot,
    MarkKind,
    MarkOrigin,
    MarkStatus,
    MeetingStateStatus,
    UserInputEvidenceMessage,
)
from app.persistence.models import (
    ActionCandidateRecord,
    ActionCandidateRevisionRecord,
    IdentityBindingRecord,
    MeetingMarkRecord,
    MeetingProjectionOffsetRecord,
    MeetingStateHeadRecord,
    utc_now,
)


class MeetingStateConflictError(RuntimeError):
    """Raised when an optimistic meeting-state write uses an old version."""


def normalize_identity_mention(mention: str) -> str:
    normalized = " ".join(mention.casefold().split())
    if not normalized:
        raise ValueError("identity mention is required")
    return normalized


def _json_payload(value: BaseModel | Mapping[str, object]) -> dict[str, object]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value)


class MeetingStateRepository:
    def __init__(self, db_session: Session) -> None:
        self._db_session = db_session

    def advance_offset(
        self,
        *,
        session_id: str,
        segment_id: str,
        processed_revision: int,
        processed_at: dt.datetime | None = None,
    ) -> MeetingProjectionOffsetRecord:
        if processed_revision < 1:
            raise ValueError("processed_revision must be positive")
        existing = self._db_session.scalar(
            select(MeetingProjectionOffsetRecord).where(
                MeetingProjectionOffsetRecord.session_id == session_id,
                MeetingProjectionOffsetRecord.segment_id == segment_id,
            )
        )
        if existing is not None and processed_revision <= existing.processed_revision:
            return existing

        timestamp = processed_at or utc_now()
        if existing is None:
            existing = MeetingProjectionOffsetRecord(
                id=str(uuid.uuid4()),
                session_id=session_id,
                segment_id=segment_id,
                processed_revision=processed_revision,
                processed_at=timestamp,
            )
            self._db_session.add(existing)
        else:
            existing.processed_revision = processed_revision
            existing.processed_at = timestamp
        self._db_session.flush()
        return existing

    def get_head(self, session_id: str) -> MeetingStateHeadRecord | None:
        return self._db_session.get(MeetingStateHeadRecord, session_id)

    def record_projection_error(
        self,
        *,
        session_id: str,
        error_code: str,
        initial_state: BaseModel | Mapping[str, object],
        initial_state_hash: str,
        latest_final_updated_at: dt.datetime | None,
        lag_ms: int,
        pending_segment_count: int,
        failed_at: dt.datetime | None = None,
    ) -> MeetingStateHeadRecord:
        if not error_code or len(error_code) > 128:
            raise ValueError("projection error code must contain at most 128 characters")
        if len(initial_state_hash) != 64:
            raise ValueError("initial_state_hash must be a 64-character digest")
        if lag_ms < 0 or pending_segment_count < 0:
            raise ValueError("freshness counters must be non-negative")
        timestamp = failed_at or utc_now()
        existing = self.get_head(session_id)
        if existing is None:
            existing = MeetingStateHeadRecord(
                session_id=session_id,
                version=0,
                status="stale",
                state_json=_json_payload(initial_state),
                state_hash=initial_state_hash,
                source_frontier_json={},
                latest_final_updated_at=latest_final_updated_at,
                projected_through=None,
                lag_ms=lag_ms,
                pending_segment_count=pending_segment_count,
                last_success_at=None,
                last_error_at=timestamp,
                last_error_code=error_code,
                updated_at=timestamp,
            )
            self._db_session.add(existing)
        else:
            # Freshness/error metadata may advance without mutating the last good
            # semantic state, its hash, version, or successfully projected frontier.
            existing.status = "stale"
            existing.latest_final_updated_at = latest_final_updated_at
            existing.lag_ms = lag_ms
            existing.pending_segment_count = pending_segment_count
            existing.last_error_at = timestamp
            existing.last_error_code = error_code
            existing.updated_at = timestamp
        self._db_session.flush()
        return existing

    def replace_head(
        self,
        *,
        session_id: str,
        expected_version: int,
        status: MeetingStateStatus,
        state: BaseModel | Mapping[str, object],
        state_hash: str,
        source_frontier: Mapping[str, object],
        latest_final_updated_at: dt.datetime | None,
        projected_through: dt.datetime | None,
        lag_ms: int,
        pending_segment_count: int,
        last_success_at: dt.datetime | None,
        last_error_at: dt.datetime | None = None,
        last_error_code: str | None = None,
        updated_at: dt.datetime | None = None,
    ) -> MeetingStateHeadRecord:
        if expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        if lag_ms < 0 or pending_segment_count < 0:
            raise ValueError("freshness counters must be non-negative")
        if len(state_hash) != 64:
            raise ValueError("state_hash must be a 64-character digest")

        timestamp = updated_at or utc_now()
        values = {
            "status": status,
            "state_json": _json_payload(state),
            "state_hash": state_hash,
            "source_frontier_json": dict(source_frontier),
            "latest_final_updated_at": latest_final_updated_at,
            "projected_through": projected_through,
            "lag_ms": lag_ms,
            "pending_segment_count": pending_segment_count,
            "last_success_at": last_success_at,
            "last_error_at": last_error_at,
            "last_error_code": last_error_code,
            "updated_at": timestamp,
        }
        existing = self.get_head(session_id)
        if existing is None:
            if expected_version != 0:
                raise MeetingStateConflictError(
                    f"Meeting State head {session_id} expected version "
                    f"{expected_version}, but no head exists"
                )
            record = MeetingStateHeadRecord(
                session_id=session_id,
                version=1,
                **values,
            )
            self._db_session.add(record)
            self._db_session.flush()
            return record

        result = self._db_session.execute(
            update(MeetingStateHeadRecord)
            .where(
                MeetingStateHeadRecord.session_id == session_id,
                MeetingStateHeadRecord.version == expected_version,
            )
            .values(version=expected_version + 1, **values)
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount != 1:
            raise MeetingStateConflictError(
                f"Meeting State head {session_id} no longer has version "
                f"{expected_version}"
            )
        self._db_session.flush()
        record = self.get_head(session_id)
        if record is None:
            raise LookupError(f"Meeting State head not found: {session_id}")
        return record

    def create_mark(
        self,
        *,
        session_id: str,
        origin: MarkOrigin,
        kind: MarkKind,
        title: str,
        source_segment_ids: Sequence[str],
        source_segment_revisions: Mapping[str, int],
        evidence_messages: Sequence[EvidenceMessageSnapshot],
        source_state_version: int,
        note: str | None = None,
        confidence: float | None = None,
        audio_start_ms: int | None = None,
        audio_end_ms: int | None = None,
        status: MarkStatus | None = None,
        mark_id: str | None = None,
        created_at: dt.datetime | None = None,
    ) -> MeetingMarkRecord:
        if not title.strip():
            raise ValueError("mark title is required")
        if not source_segment_ids:
            raise ValueError("mark evidence is required")
        unique_segment_ids = tuple(dict.fromkeys(source_segment_ids))
        if set(source_segment_revisions) != set(unique_segment_ids):
            raise ValueError("mark evidence revisions must match source Segment IDs")
        if any(value < 1 for value in source_segment_revisions.values()):
            raise ValueError("mark evidence revisions must be positive")
        if not evidence_messages:
            raise ValueError("mark evidence messages are required")
        if any(message.session_id != session_id for message in evidence_messages):
            raise ValueError("mark evidence messages belong to another Session")
        caption_messages = {
            message.segment_id: message
            for message in evidence_messages
            if isinstance(message, CaptionEvidenceMessage)
        }
        if set(caption_messages) != set(unique_segment_ids) or any(
            caption_messages[segment_id].segment_revision
            != source_segment_revisions[segment_id]
            for segment_id in unique_segment_ids
        ):
            raise ValueError("mark caption evidence does not match source revisions")
        if origin == "manual" and not any(
            isinstance(message, UserInputEvidenceMessage)
            for message in evidence_messages
        ):
            raise ValueError("manual marks require user-input evidence")
        if source_state_version < 0:
            raise ValueError("source_state_version must be non-negative")
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("confidence must be between zero and one")
        if (
            audio_start_ms is not None
            and audio_end_ms is not None
            and audio_end_ms < audio_start_ms
        ):
            raise ValueError("audio end must not precede audio start")

        timestamp = created_at or utc_now()
        record = MeetingMarkRecord(
            id=mark_id or str(uuid.uuid4()),
            session_id=session_id,
            origin=origin,
            kind=kind,
            status=status or ("accepted" if origin == "manual" else "candidate"),
            title=title.strip(),
            note=note,
            confidence=confidence,
            source_segment_ids_json=list(unique_segment_ids),
            source_segment_revisions_json=dict(source_segment_revisions),
            evidence_messages_json=[
                message.model_dump(mode="json") for message in evidence_messages
            ],
            audio_start_ms=audio_start_ms,
            audio_end_ms=audio_end_ms,
            source_state_version=source_state_version,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._db_session.add(record)
        self._db_session.flush()
        return record

    def update_mark_status(
        self,
        mark_id: str,
        status: MarkStatus,
        *,
        updated_at: dt.datetime | None = None,
    ) -> MeetingMarkRecord:
        record = self._db_session.get(MeetingMarkRecord, mark_id)
        if record is None:
            raise LookupError(f"Meeting mark not found: {mark_id}")
        record.status = status
        record.updated_at = updated_at or utc_now()
        self._db_session.flush()
        return record

    def list_marks(
        self, session_id: str, *, limit: int | None = None, offset: int = 0,
    ) -> list[MeetingMarkRecord]:
        statement = (
            select(MeetingMarkRecord)
            .where(MeetingMarkRecord.session_id == session_id)
            .order_by(MeetingMarkRecord.created_at, MeetingMarkRecord.id)
            .offset(max(0, offset))
        )
        if limit is not None:
            statement = statement.limit(max(1, min(limit, 101)))
        return list(
            self._db_session.scalars(statement)
        )

    def create_candidate(
        self,
        *,
        session_id: str,
        content: ActionCandidateContent | Mapping[str, object],
        readiness: ActionReadiness,
        candidate_id: str | None = None,
        lineage_root_id: str | None = None,
        content_status: CandidateContentStatus = "active",
        execution_status: CandidateExecutionStatus = "not_requested",
        derived_from_candidate_id: str | None = None,
        derived_from_revision: int | None = None,
        change_summary: str | None = None,
        created_at: dt.datetime | None = None,
    ) -> ActionCandidateRecord:
        identifier = candidate_id or str(uuid.uuid4())
        timestamp = created_at or utc_now()
        record = ActionCandidateRecord(
            id=identifier,
            session_id=session_id,
            lineage_root_id=lineage_root_id or identifier,
            current_revision=1,
            content_status=content_status,
            execution_status=execution_status,
            superseded_by_candidate_id=None,
            derived_from_candidate_id=derived_from_candidate_id,
            derived_from_revision=derived_from_revision,
            created_at=timestamp,
            updated_at=timestamp,
        )
        revision = ActionCandidateRevisionRecord(
            id=str(uuid.uuid4()),
            candidate_id=identifier,
            revision=1,
            readiness=readiness,
            content_json=_json_payload(content),
            change_kind="create",
            parent_revision=None,
            change_summary=change_summary,
            created_at=timestamp,
        )
        self._db_session.add_all((record, revision))
        self._db_session.flush()
        return record

    def append_candidate_revision(
        self,
        *,
        candidate_id: str,
        expected_revision: int,
        content: ActionCandidateContent | Mapping[str, object],
        readiness: ActionReadiness,
        change_kind: CandidateChangeKind,
        change_summary: str | None = None,
        created_at: dt.datetime | None = None,
    ) -> ActionCandidateRevisionRecord:
        if expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        timestamp = created_at or utc_now()
        result = self._db_session.execute(
            update(ActionCandidateRecord)
            .where(
                ActionCandidateRecord.id == candidate_id,
                ActionCandidateRecord.current_revision == expected_revision,
            )
            .values(
                current_revision=expected_revision + 1,
                updated_at=timestamp,
            )
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount != 1:
            raise MeetingStateConflictError(
                f"Action Candidate {candidate_id} no longer has revision "
                f"{expected_revision}"
            )
        revision = ActionCandidateRevisionRecord(
            id=str(uuid.uuid4()),
            candidate_id=candidate_id,
            revision=expected_revision + 1,
            readiness=readiness,
            content_json=_json_payload(content),
            change_kind=change_kind,
            parent_revision=expected_revision,
            change_summary=change_summary,
            created_at=timestamp,
        )
        self._db_session.add(revision)
        self._db_session.flush()
        return revision

    def supersede_candidate(
        self,
        candidate_id: str,
        *,
        superseded_by_candidate_id: str,
        updated_at: dt.datetime | None = None,
    ) -> ActionCandidateRecord:
        if candidate_id == superseded_by_candidate_id:
            raise ValueError("a candidate cannot supersede itself")
        record = self._db_session.get(ActionCandidateRecord, candidate_id)
        survivor = self._db_session.get(
            ActionCandidateRecord,
            superseded_by_candidate_id,
        )
        if record is None:
            raise LookupError(f"Action Candidate not found: {candidate_id}")
        if survivor is None:
            raise LookupError(
                f"Action Candidate not found: {superseded_by_candidate_id}"
            )
        if record.session_id != survivor.session_id:
            raise ValueError("candidates from different Sessions cannot be merged")
        record.content_status = "superseded"
        record.superseded_by_candidate_id = superseded_by_candidate_id
        record.updated_at = updated_at or utc_now()
        self._db_session.flush()
        return record

    def transition_candidate_execution(
        self,
        candidate_id: str,
        target: CandidateExecutionStatus,
        *,
        updated_at: dt.datetime | None = None,
    ) -> ActionCandidateRecord:
        record = self._db_session.get(ActionCandidateRecord, candidate_id)
        if record is None:
            raise LookupError(f"Action Candidate not found: {candidate_id}")
        current = record.execution_status
        if current == target:
            return record
        allowed: dict[str, frozenset[str]] = {
            "not_requested": frozenset({"handed_off", "executing"}),
            "handed_off": frozenset({"executing", "failed"}),
            "executing": frozenset({"executed", "failed"}),
            "executed": frozenset(),
            "failed": frozenset(),
        }
        if target not in allowed[current]:
            raise ValueError(
                f"invalid candidate execution transition: {current} -> {target}"
            )
        record.execution_status = target
        record.updated_at = updated_at or utc_now()
        self._db_session.flush()
        return record

    def confirm_identity_binding(
        self,
        *,
        actor_id: str,
        linear_team_id: str,
        mention: str,
        linear_user_id: str,
        confirmed_by_actor_id: str,
        verified_at: dt.datetime | None = None,
    ) -> IdentityBindingRecord:
        normalized_mention = normalize_identity_mention(mention)
        if not actor_id or not linear_team_id or not linear_user_id:
            raise ValueError("actor, Linear Team, and Linear user are required")
        if not confirmed_by_actor_id:
            raise ValueError("confirmed_by_actor_id is required")
        timestamp = verified_at or utc_now()
        record = self._db_session.scalar(
            select(IdentityBindingRecord).where(
                IdentityBindingRecord.actor_id == actor_id,
                IdentityBindingRecord.linear_team_id == linear_team_id,
                IdentityBindingRecord.normalized_mention == normalized_mention,
                IdentityBindingRecord.status == "confirmed",
            )
        )
        if record is None:
            record = self._db_session.scalar(
                select(IdentityBindingRecord)
                .where(
                    IdentityBindingRecord.actor_id == actor_id,
                    IdentityBindingRecord.linear_team_id == linear_team_id,
                    IdentityBindingRecord.normalized_mention
                    == normalized_mention,
                    IdentityBindingRecord.status == "revoked",
                )
                .order_by(IdentityBindingRecord.updated_at.desc())
                .limit(1)
            )
        if record is None:
            record = IdentityBindingRecord(
                id=str(uuid.uuid4()),
                actor_id=actor_id,
                linear_team_id=linear_team_id,
                normalized_mention=normalized_mention,
                linear_user_id=linear_user_id,
                status="confirmed",
                confirmed_by_actor_id=confirmed_by_actor_id,
                last_verified_at=timestamp,
                created_at=timestamp,
                updated_at=timestamp,
                revoked_at=None,
            )
            self._db_session.add(record)
        else:
            record.linear_user_id = linear_user_id
            record.status = "confirmed"
            record.confirmed_by_actor_id = confirmed_by_actor_id
            record.last_verified_at = timestamp
            record.updated_at = timestamp
            record.revoked_at = None
        self._db_session.flush()
        return record

    def revoke_identity_binding(
        self,
        binding_id: str,
        *,
        revoked_at: dt.datetime | None = None,
    ) -> IdentityBindingRecord:
        record = self._db_session.get(IdentityBindingRecord, binding_id)
        if record is None:
            raise LookupError(f"Identity binding not found: {binding_id}")
        if record.status == "revoked":
            return record
        timestamp = revoked_at or utc_now()
        record.status = "revoked"
        record.revoked_at = timestamp
        record.updated_at = timestamp
        self._db_session.flush()
        return record

    def find_identity_binding(
        self,
        *,
        actor_id: str,
        linear_team_id: str,
        mention: str,
        status: IdentityBindingStatus = "confirmed",
    ) -> IdentityBindingRecord | None:
        return self._db_session.scalar(
            select(IdentityBindingRecord).where(
                IdentityBindingRecord.actor_id == actor_id,
                IdentityBindingRecord.linear_team_id == linear_team_id,
                IdentityBindingRecord.normalized_mention
                == normalize_identity_mention(mention),
                IdentityBindingRecord.status == status,
            )
        )
