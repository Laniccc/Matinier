"""Host-owned UI authority; this router is not available inside plugin RPC."""
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, ValidationError

from app.api.dependencies import get_meeting_plugin_history
from app.assistant.plugin_contracts import MeetingStateQueryInput
from app.assistant.plugin_repository import MeetingPluginConflict, MeetingPluginDenied
from app.plugins.host_actions import HostActions
from app.plugins.host_action_contracts import HostActionConfirmInput, HostActionPrepareInput
from app.plugins.host_action_descriptors import describe_actions, meeting_target
from app.assistant.plugin_repository import MEETING_PLUGIN_ID
from app.plugins.supervisor import PluginIdentity
from app.persistence.models import MeetingPluginOperationRecord
from sqlalchemy import select
from app.media.repository import MediaRepository
from app.persistence.models import MediaSessionRecord

router = APIRouter(prefix="/api", tags=["assistant-host-actions"])


class EmptyUIRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def get_host_actions(request: Request) -> HostActions:
    actions = getattr(request.app.state, "host_actions", None)
    if actions is None:
        raise HTTPException(503, "Host actions are unavailable")
    return actions


def require_ui_request(request: Request, actions: HostActions = Depends(get_host_actions)):
    origin = request.headers.get("origin")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(403, "JSON UI request required")
    if request.headers.get("x-assistant-ui") != "1":
        raise HTTPException(403, "Host UI header required")
    if origin is not None:
        if origin not in actions.settings.cors_origin_list:
            raise HTTPException(403, "Untrusted UI origin")
    else:
        configured = actions.settings.assistant_host_token
        supplied = request.headers.get("x-assistant-host-token", "")
        if configured is None or not supplied or not hmac.compare_digest(
            supplied.encode(), configured.get_secret_value().encode(),
        ):
            raise HTTPException(403, "Explicit Host authorization required without Origin")
    return origin


def trusted_context(request: Request, origin=Depends(require_ui_request),
                    actions: HostActions = Depends(get_host_actions)):
    try:
        return actions.require_ui_context(request.headers.get("x-assistant-ui-nonce", ""), origin)
    except MeetingPluginDenied as error:
        raise HTTPException(403, str(error)) from None


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except MeetingPluginDenied as error:
        raise HTTPException(403, str(error)) from None
    except MeetingPluginConflict as error:
        raise HTTPException(409, str(error)) from None
    except ValidationError:
        raise HTTPException(422, "Invalid Host action arguments") from None

def history_media_id(actions, media_id, *, create=False):
    if not media_id.startswith("legacy:"):
        return media_id
    with actions.database.session() as db:
        session_id = media_id.removeprefix("legacy:")
        if create:
            try:
                media = MediaRepository(db).ensure_legacy_session_bridge(session_id)
            except LookupError:
                raise HTTPException(404, "Session not found") from None
            db.commit()
        else:
            media = db.scalar(select(MediaSessionRecord).where(MediaSessionRecord.legacy_session_id == session_id))
        if media is None:
            raise HTTPException(404, "History mapping unavailable")
        return media.id


@router.post("/assistant-actions/ui-context")
def create_ui_context(payload: EmptyUIRequest, origin=Depends(require_ui_request),
                      actions: HostActions = Depends(get_host_actions)):
    return {"ui_nonce": _call(actions.issue_ui_nonce, origin)}


@router.post("/media-sessions/{media_id}/assistant-actions/prepare")
def prepare_action(media_id: str, payload: HostActionPrepareInput,
                   context=Depends(trusted_context), actions: HostActions = Depends(get_host_actions)):
    if payload.plugin_version is None or payload.view_version is None:
        raise HTTPException(422, "Plugin and view versions are required")
    with actions.database.session() as db:
        _call(meeting_target, db, media_id, payload.action, payload.plugin_version, payload.view_version)
    return _call(actions.prepare, context, media_id, payload)


@router.post("/media-sessions/{media_id}/assistant-actions/confirm")
async def confirm_action(media_id: str, payload: HostActionConfirmInput, request: Request,
                   context=Depends(trusted_context), actions: HostActions = Depends(get_host_actions)):
    media_id = history_media_id(actions, media_id)
    result = _call(actions.confirm, context, media_id, payload)
    if result["status"] != "authorized":
        if result["status"] == "applied":
            scope, _ = _call(actions.dispatch_metadata, payload.preview_id)
            projector = getattr(request.app.state, "meeting_state_projector", None)
            if projector is not None and result["action"] == "meeting.analysis.activate":
                await projector.request_catch_up(scope.legacy_session_id)
            runtime = getattr(request.app.state, "plugin_host_runtime", None)
            if runtime is not None:
                try:
                    with actions.database.session() as db:
                        binding, view = meeting_target(db, media_id, version=scope.plugin_version)
                        target_scope, target_version = binding.session_scope, view.view_version
                    await runtime.supervisor.invoke_command(PluginIdentity(scope.plugin_id, scope.plugin_version), media_id, target_scope,
                        command_id="refresh-" + payload.preview_id, command="refresh", values={}, expected_view_version=target_version)
                except Exception:
                    return {**result, "view_refresh_pending": True}
        return result
    scope, view_version = _call(actions.dispatch_metadata, payload.preview_id)
    command = result["command"]
    with actions.database.session() as db:
        existing = db.scalar(select(MeetingPluginOperationRecord).where(MeetingPluginOperationRecord.plugin_id == scope.plugin_id,
            MeetingPluginOperationRecord.plugin_version == scope.plugin_version, MeetingPluginOperationRecord.media_session_id == media_id,
            MeetingPluginOperationRecord.client_request_id == command["request_id"]))
        if existing is not None:
            admitted = {"status": "accepted", "operation_id": existing.id}
            actions.remember_admission(payload.preview_id, admitted)
            return admitted
        binding, _ = _call(meeting_target, db, media_id, scope.action, scope.plugin_version, view_version)
        session_scope = binding.session_scope
    runtime = getattr(request.app.state, "plugin_host_runtime", None)
    if runtime is None:
        raise HTTPException(503, "Meeting plugin runtime unavailable")
    try:
        reply = await runtime.supervisor.invoke_command(PluginIdentity(scope.plugin_id, scope.plugin_version), media_id, session_scope,
            command_id=command["request_id"], command="apply_action", values=result, expected_view_version=view_version)
    except Exception:
        # Never serialize the capsule or uncertain RPC exception to the browser.
        return {"status": "unknown"}
    if isinstance(reply, dict) and reply.get("status") == "accepted" and isinstance(reply.get("operation_id"), str):
        with actions.database.session() as db:
            durable = db.get(MeetingPluginOperationRecord, reply["operation_id"])
            if durable is None or (durable.plugin_id, durable.plugin_version, durable.media_session_id, durable.client_request_id, durable.action) != (
                    scope.plugin_id, scope.plugin_version, media_id, command["request_id"], scope.action):
                return {"status": "unknown"}
        admitted = {"status": "accepted", "operation_id": reply["operation_id"]}
        actions.remember_admission(payload.preview_id, admitted)
        return admitted
    return {"status": "unknown"}


@router.post("/media-sessions/{media_id}/assistant-history/prepare-cancel")
def prepare_history_cancel(media_id: str, payload: HostActionPrepareInput,
                           context=Depends(trusted_context), actions: HostActions = Depends(get_host_actions)):
    if payload.action != "meeting.cancel":
        raise HTTPException(403, "History only permits cancellation")
    media_id = history_media_id(actions, media_id, create=True)
    return _call(actions.prepare, context, media_id, payload, history_cancel=True)


@router.get("/media-sessions/{media_id}/assistant-actions")
def action_descriptors(media_id: str, actions: HostActions = Depends(get_host_actions)):
    return _call(describe_actions, actions, media_id)

@router.get("/sessions/{session_id}/assistant-history-sources")
def history_sources(session_id: str, history=Depends(get_meeting_plugin_history)):
    return _call(history.sources, session_id)

@router.get("/media-sessions/{media_id}/assistant-history")
def read_history(media_id: str, offset: int = Query(0, ge=0, le=1_000_000), after: int = Query(0, ge=0),
                 limit: int = Query(10, ge=1, le=50), plugin_id: str = MEETING_PLUGIN_ID, execution_id: str | None = None,
                 history=Depends(get_meeting_plugin_history)):
    if plugin_id != MEETING_PLUGIN_ID:
        raise HTTPException(404, "Unknown history source")
    return _call(history.page, media_id, MeetingStateQueryInput(offset=offset, after=after, limit=limit), execution_id)
