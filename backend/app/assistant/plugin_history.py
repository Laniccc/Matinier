"""Trusted Host history source, independent of an installed plugin/runtime."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant.plugin_contracts import MeetingOperationQueryInput, MeetingStateQueryInput
from app.assistant.plugin_repository import MEETING_PLUGIN_ID
from app.assistant.plugin_service import MeetingPluginReadService
from app.assistant.plugin_history_view import history_document
from app.persistence.models import MediaSessionRecord, MeetingMarkRecord, MeetingStateHeadRecord, AssistantExecutionRecord


class MeetingPluginHistory:
    def __init__(self, db: Session):
        self.service = MeetingPluginReadService(db)

    def describe(self, media_session_id: str) -> dict[str, object]:
        session_id = self.service.resolve(media_session_id)
        return {
            "source_kind": "host_history", "plugin_id": MEETING_PLUGIN_ID,
            "name": "会议助手",
            "legacy_session_id": session_id, "media_session_id": media_session_id,
            "read_only": True,
            "view_path": f"/api/media-sessions/{media_session_id}/assistant-history",
            "controls": [{"action": "meeting.cancel", "source": "host_registry"}],
        }

    def sources(self, session_id):
        db = self.service.db
        media = db.scalar(select(MediaSessionRecord).where(MediaSessionRecord.legacy_session_id == session_id))
        exists = db.get(MeetingStateHeadRecord, session_id) is not None or any(
            db.scalar(select(model.id).where(model.session_id == session_id).limit(1)) is not None
            for model in (MeetingMarkRecord, AssistantExecutionRecord))
        return [self.describe(media.id if media else f"legacy:{session_id}")] if exists else []

    def page(self, media_id, query, execution_id=None):
        data = self.query(media_id, query)
        if execution_id:
            detail = self.execution(media_id, execution_id, MeetingOperationQueryInput(execution_id=execution_id,
                offset=query.offset, after=query.after, limit=query.limit))
            display = {"execution": detail["execution"], "steps": detail["steps"], "tool_calls": detail["tool_calls"], "events": detail["events"]}
            data.update(next_cursor=detail["next_cursor"], has_more_events=detail["next_cursor"] < detail["event_cursor"],
                has_more=len(detail["steps"]) == query.limit or len(detail["tool_calls"]) == query.limit)
        else:
            display = {key: data[key] for key in ("processing", "state", "marks", "candidates", "executions", "events")}
        view = history_document(display, data["state_version"])
        return {"source": self.describe(media_id), "view": data, "detail": detail if execution_id else None, "ui_view": view,
            "executions": [{"value": e["execution_id"], "label": e["goal"][:160]} for e in data["executions"]]}

    def query(self, media_session_id: str, query: MeetingStateQueryInput) -> dict[str, object]:
        return self.service.read_session(media_session_id, query)

    def execution(self, media_session_id: str, execution_id: str,
                  query: MeetingOperationQueryInput) -> dict[str, object]:
        return self.service.read_execution(self.service.resolve(media_session_id), execution_id, query)
