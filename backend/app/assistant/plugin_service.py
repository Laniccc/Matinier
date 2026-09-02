"""Read the original meeting records; never start analysis or an engine runtime."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.assistant.context import read_meeting_state
from app.assistant.plugin_contracts import MeetingOperationQueryInput, MeetingStateQueryInput
from app.assistant.plugin_policy import MeetingPluginPolicy
from app.assistant.plugin_repository import MEETING_PLUGIN_ID, MeetingPluginDenied, canonical_hash
from app.assistant.repository import AssistantRepository
from app.meeting_state.candidates import load_action_candidates
from app.meeting_state.repository import MeetingStateRepository
from app.persistence.models import (
    ActionCandidateRecord, AssistantExecutionRecord, MediaSessionRecord,
    MeetingMarkRecord, MeetingPluginOperationRecord, MeetingPluginSessionRecord,
    PluginSessionBindingRecord, SegmentRecord, SessionRecord,
)


def _unresolved_task_outcome(record) -> dict[str, object] | None:
    """Project only typed identity warning fields from a task result."""
    if record.tool_name != "task.create" or not isinstance(record.result_json, dict):
        return None
    output = record.result_json.get("output")
    outcome = output.get("outcome") if isinstance(output, dict) else None
    identity = outcome.get("unresolved_identity") if isinstance(outcome, dict) else None
    if (not isinstance(outcome, dict) or outcome.get("partial") is not True
            or not isinstance(identity, dict)
            or identity.get("is_placeholder") is not True
            or identity.get("resolution") not in {"missing", "ambiguous"}
            or not isinstance(identity.get("spoken_text"), str)):
        return None
    return {
        "partial": True,
        "unresolved_identity": {
            "spoken_text": identity["spoken_text"],
            "resolution": identity["resolution"],
            "is_placeholder": True,
        },
    }


def begin_read_snapshot(db: Session) -> None:
    # sqlite3 legacy transaction mode does not BEGIN for SELECT. Explicitly
    # anchor this caller-owned read transaction so state and event cursor agree.
    connection = db.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")


class MeetingPluginReadService:
    def __init__(self, db: Session):
        self.db = db

    def resolve(self, media_session_id: str) -> str:
        begin_read_snapshot(self.db)
        media = self.db.get(MediaSessionRecord, media_session_id)
        # Read-only alias for pre-plugin history. Never accepted as an execution
        # owner; an explicit Host cancellation first establishes a real bridge.
        if media is None and media_session_id.startswith("legacy:"):
            legacy_id = media_session_id.removeprefix("legacy:")
            if self.db.get(SessionRecord, legacy_id) is not None:
                return legacy_id
        if media is None or not media.legacy_session_id:
            raise MeetingPluginDenied("meeting Session is unavailable")
        if self.db.get(SessionRecord, media.legacy_session_id) is None:
            raise MeetingPluginDenied("meeting Session is unavailable")
        return media.legacy_session_id

    def require_scope(self, media_session_id: str, plugin_id: str, plugin_version: str) -> str:
        session_id = self.resolve(media_session_id)
        if plugin_id != MEETING_PLUGIN_ID:
            raise MeetingPluginDenied("meeting capability owner is invalid")
        binding = self.db.scalar(MeetingPluginPolicy._bindings().where(
            PluginSessionBindingRecord.media_session_id == media_session_id,
            PluginSessionBindingRecord.plugin_version == plugin_version,
        ))
        if binding is None:
            raise MeetingPluginDenied("meeting binding is unavailable")
        return session_id

    def require_mark(self, session_id: str, mark_id: str) -> MeetingMarkRecord:
        record = self.db.scalar(select(MeetingMarkRecord).where(
            MeetingMarkRecord.id == mark_id, MeetingMarkRecord.session_id == session_id,
        ))
        if record is None:
            raise MeetingPluginDenied("meeting resource is unavailable")
        return record

    def require_candidate(self, session_id: str, candidate_id: str):
        values = load_action_candidates(
            self.db, session_id=session_id, current_segment_revisions=self._revisions(session_id),
            candidate_ids=(candidate_id,), limit=1,
        )
        if not values:
            raise MeetingPluginDenied("meeting resource is unavailable")
        return values[0]

    def require_execution(self, session_id: str, execution_id: str) -> AssistantExecutionRecord:
        record = self.db.scalar(select(AssistantExecutionRecord).where(
            AssistantExecutionRecord.id == execution_id,
            AssistantExecutionRecord.session_id == session_id,
        ))
        if record is None:
            raise MeetingPluginDenied("meeting resource is unavailable")
        return record

    def _revisions(self, session_id: str) -> dict[str, int]:
        return dict(self.db.execute(select(SegmentRecord.segment_id, SegmentRecord.revision).where(
            SegmentRecord.session_id == session_id, SegmentRecord.status == "final",
        )).all())

    def query(self, *, media_session_id: str, plugin_id: str, plugin_version: str,
              query: MeetingStateQueryInput) -> dict[str, object]:
        self.require_scope(media_session_id, plugin_id, plugin_version)
        return self.read_session(media_session_id, query)

    def read_session(self, media_session_id: str, query: MeetingStateQueryInput) -> dict[str, object]:
        # Import public serializers lazily; they share the original API's
        # filtering without pulling runtime bootstrap into module import cycles.
        from app.api.assistant import _event_response, _execution_summary, _public_json
        from app.api.meeting_state import _mark_response

        session_id = self.resolve(media_session_id)
        repo = AssistantRepository(self.db)
        view = read_meeting_state(self.db, session_id)
        revisions = self._revisions(session_id)
        control = self.db.get(MeetingPluginSessionRecord, session_id)
        state = view.state.model_dump(mode="json")
        state_has_more = any(isinstance(value, list) and len(value) > query.offset + query.limit
            for value in state.values())
        for key, value in state.items():
            if isinstance(value, list):
                state[key] = value[query.offset:query.offset + query.limit]
        marks = MeetingStateRepository(self.db).list_marks(session_id, limit=query.limit + 1, offset=query.offset)
        candidates = load_action_candidates(self.db, session_id=session_id,
            current_segment_revisions=revisions, limit=query.limit + 1, offset=query.offset)
        executions = repo.list_executions(session_id, limit=query.limit + 1, offset=query.offset)
        events = repo.list_session_events(session_id, after=query.after, limit=query.limit)
        event_cursor = repo.current_event_cursor(session_id)
        next_cursor = events[-1].id if events else query.after
        freshness = view.freshness.model_dump(mode="json")
        pending = view.freshness.pending_segment_count
        processing = {
            "status": control.analysis_state if control is not None else "inactive",
            "analysis_epoch": control.analysis_epoch if control is not None else 0,
            "authority_epoch": control.authority_epoch if control is not None else 0,
            "current_finals": len(revisions), "processed_finals": len(revisions) - pending,
            "pending_finals": pending, "updated_at": freshness["state_updated_at"],
            "last_error_code": freshness["last_error_code"],
        }
        result = {
            "legacy_session_id": session_id, "media_session_id": media_session_id,
            "state": _public_json(state), "state_version": view.state.version,
            "state_hash": view.state_hash, "processing": processing, "freshness": freshness,
            "marks": [_mark_response(row).model_dump(mode="json") for row in marks[:query.limit]],
            "candidates": [row.model_dump(mode="json") for row in candidates[:query.limit]],
            "executions": [_execution_summary(repo, row).model_dump(mode="json") for row in executions[:query.limit]],
            "events": [_event_response(row).model_dump(mode="json") for row in events],
            "event_cursor": event_cursor, "next_cursor": next_cursor,
            "has_more_events": next_cursor < event_cursor,
            "offset": query.offset, "next_offset": query.offset + query.limit,
            "has_more": state_has_more or any(len(rows) > query.limit for rows in (marks, candidates, executions)),
        }
        # Wall-clock lag changes on every read; it is not a source revision.
        result["snapshot_key"] = canonical_hash({
            key: value for key, value in result.items() if key != "freshness"
        })
        return result

    def operation(self, *, media_session_id: str, plugin_id: str, plugin_version: str,
                  query: MeetingOperationQueryInput) -> dict[str, object]:
        session_id = self.require_scope(media_session_id, plugin_id, plugin_version)
        operation = None
        execution_id = query.execution_id
        if query.operation_id:
            operation = self.db.scalar(select(MeetingPluginOperationRecord).where(
                MeetingPluginOperationRecord.id == query.operation_id,
                MeetingPluginOperationRecord.media_session_id == media_session_id,
                MeetingPluginOperationRecord.legacy_session_id == session_id,
                MeetingPluginOperationRecord.plugin_id == plugin_id,
                MeetingPluginOperationRecord.plugin_version == plugin_version,
            ))
            if operation is None:
                raise MeetingPluginDenied("meeting resource is unavailable")
            execution_id = operation.execution_id
        if execution_id:
            execution = self.require_execution(session_id, execution_id)
            owners = list(self.db.scalars(select(MeetingPluginOperationRecord).where(
                MeetingPluginOperationRecord.execution_id.in_((execution.id, execution.root_execution_id)),
            )))
            if owners and not any((o.plugin_id, o.plugin_version, o.media_session_id) ==
                    (plugin_id, plugin_version, media_session_id) for o in owners):
                raise MeetingPluginDenied("meeting resource is unavailable")
        result = self.read_execution(session_id, execution_id, query) if execution_id else {}
        result["operation"] = None if operation is None else {
            "operation_id": operation.id, "status": operation.status,
            "execution_id": operation.execution_id, "error_code": operation.error_code,
        }
        return result

    def read_execution(self, session_id: str, execution_id: str,
                       query: MeetingOperationQueryInput) -> dict[str, object]:
        from app.api.assistant import _event_response, _execution_summary, _step_summary, _tool_call_summary
        record = self.require_execution(session_id, execution_id)
        repo = AssistantRepository(self.db)
        events = repo.list_session_events(session_id, after=query.after, limit=query.limit)
        tool_calls = []
        for row in repo.list_tool_calls(execution_id, limit=query.limit, offset=query.offset):
            summary = _tool_call_summary(row).model_dump(mode="json")
            outcome = _unresolved_task_outcome(row)
            if outcome is not None:
                summary["outcome"] = outcome
            tool_calls.append(summary)
        return {
            "execution": _execution_summary(repo, record).model_dump(mode="json"),
            "steps": [_step_summary(row).model_dump(mode="json") for row in repo.list_steps(execution_id, limit=query.limit, offset=query.offset)],
            "tool_calls": tool_calls,
            "events": [_event_response(row).model_dump(mode="json") for row in events if row.root_execution_id == record.root_execution_id],
            "next_cursor": events[-1].id if events else query.after,
            "event_cursor": repo.current_event_cursor(session_id),
        }
