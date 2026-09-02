from __future__ import annotations

from collections.abc import Generator
import hmac

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.persistence.database import Database
from app.processing.runner import ProcessingJobRunner
from app.settings import Settings
from app.assistant.context import ContextBuilder
from app.assistant.bootstrap import AssistantRuntime
from app.meeting_state.projector import MeetingStateProjector
from app.plugins.bootstrap import PluginHostRuntimeLike


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_db_session(request: Request) -> Generator[Session, None, None]:
    yield from get_database(request).sessions()


def get_meeting_plugin_history(db_session: Session = Depends(get_db_session)):
    from app.assistant.plugin_history import MeetingPluginHistory
    return MeetingPluginHistory(db_session)


def get_processing_job_runner(request: Request) -> ProcessingJobRunner:
    return request.app.state.processing_job_runner


def get_meeting_state_projector(
    request: Request,
) -> MeetingStateProjector | None:
    return getattr(request.app.state, "meeting_state_projector", None)


def get_assistant_runtime(request: Request) -> AssistantRuntime:
    runtime = getattr(request.app.state, "assistant_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Private meeting Assistant is disabled",
        )
    return runtime


def get_plugin_host_runtime(request: Request) -> PluginHostRuntimeLike:
    runtime = getattr(request.app.state, "plugin_host_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Plugin framework is unavailable",
        )
    return runtime


def require_plugin_admin(
    request: Request,
    x_plugin_admin_token: str | None = Header(
        default=None,
        alias="X-Plugin-Admin-Token",
    ),
) -> None:
    configured = request.app.state.settings.plugin_admin_token
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Plugin administration is disabled",
        )
    if x_plugin_admin_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Plugin administrator token required",
        )
    expected = configured.get_secret_value()
    if not hmac.compare_digest(
        x_plugin_admin_token.encode("utf-8"),
        expected.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Plugin administrator token rejected",
        )


def get_context_builder(
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> ContextBuilder:
    settings = get_app_settings(request)
    return ContextBuilder(
        db_session,
        projector=get_meeting_state_projector(request),
        stale_after_seconds=settings.meeting_state_stale_after_seconds,
    )
