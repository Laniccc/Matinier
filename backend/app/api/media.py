from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import get_plugin_host_runtime
from app.plugins.bootstrap import PluginHostRuntimeLike


logger = logging.getLogger(__name__)
router = APIRouter(tags=["media-plugins"])


class PluginCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(min_length=5, max_length=128)
    plugin_version: str = Field(min_length=1, max_length=64)
    session_scope: str = Field(min_length=1, max_length=256)
    surface: str = Field(min_length=1, max_length=32)
    view_id: str = Field(min_length=1, max_length=128)
    expected_view_version: int = Field(ge=1)
    action_id: str = Field(min_length=1, max_length=128)
    values: dict[str, object] = Field(default_factory=dict)


@router.get("/api/sessions/{session_id}/media-session")
async def resolve_media_session(
    session_id: str,
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
):
    try:
        return await runtime.resolve_media_session(session_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Session not found") from None
    except Exception:
        logger.exception(
            "MediaSession bridge failed",
            extra={
                "session_id": session_id,
                "event": "media_session_bridge_failed",
            },
        )
        raise HTTPException(status_code=500, detail="Media bridge failed") from None


@router.get("/api/media-sessions/{media_session_id}/events")
def list_media_events(
    media_session_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1_000),
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
):
    try:
        return runtime.list_media_events(
            media_session_id,
            after_sequence=after,
            limit=limit,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="MediaSession not found") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Media event query failed") from None


@router.get("/api/media-sessions/{media_session_id}/plugin-views")
def list_plugin_views(
    media_session_id: str,
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
):
    try:
        return runtime.list_plugin_views(media_session_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="MediaSession not found") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Plugin view query failed") from None


@router.post("/api/media-sessions/{media_session_id}/plugin-commands")
async def execute_plugin_command(
    media_session_id: str,
    payload: PluginCommandRequest,
    runtime: PluginHostRuntimeLike = Depends(get_plugin_host_runtime),
):
    # Even a compromised view may not expose the Host-only authorization command.
    if payload.plugin_id == "com.matinier.meeting-assistant" and (
        payload.action_id == "apply_action" or "intent_token" in payload.values or "command" in payload.values
    ):
        raise HTTPException(403, "Use the Host action channel")
    try:
        return await runtime.execute_plugin_command(
            media_session_id,
            **payload.model_dump(),
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Plugin command scope rejected") from None
    except LookupError:
        raise HTTPException(status_code=404, detail="Plugin command target not found") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="Plugin command is stale or invalid") from None
    except Exception:
        raise HTTPException(status_code=500, detail="Plugin command failed") from None
