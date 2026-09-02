from __future__ import annotations

import logging

import httpx

from app.settings import Settings
from app.task_system.contracts import TaskSystemAdapter
from app.task_system.fake import FakeTaskSystemAdapter
from app.task_system.linear import LinearTaskSystemAdapter
from app.task_system.models import (
    ExternalMember,
    ExternalTask,
    TaskCreateResult,
    TaskDraft,
    TaskReconciliationResult,
    TaskSearchQuery,
    TaskSearchResult,
    TaskSystemConnection,
)


logger = logging.getLogger(__name__)


class DisabledTaskSystemAdapter:
    """Read-safe sentinel used when external task integration is disabled."""

    provider_name = "disabled"

    def __init__(self) -> None:
        self._connection = TaskSystemConnection(
            provider=self.provider_name,
            team_id="disabled",
        )

    @property
    def connection(self) -> TaskSystemConnection:
        return self._connection

    async def search(self, query: TaskSearchQuery) -> TaskSearchResult:
        del query
        return TaskSearchResult()

    async def get(self, task_ref: str) -> ExternalTask:
        del task_ref
        raise LookupError("External task integration is disabled")

    async def create(
        self,
        draft: TaskDraft,
        *,
        action_key: str,
    ) -> TaskCreateResult:
        del draft, action_key
        raise RuntimeError("External task integration is disabled")

    async def reconcile_create(
        self,
        *,
        action_key: str,
    ) -> TaskReconciliationResult:
        del action_key
        return TaskReconciliationResult(status="none")

    async def search_members(self, query: str) -> tuple[ExternalMember, ...]:
        del query
        return ()

    async def get_member(self, external_user_id: str) -> ExternalMember:
        del external_user_id
        raise LookupError("External task integration is disabled")


def build_task_system_adapter(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> TaskSystemAdapter:
    provider = settings.task_system_provider
    logger.info(
        "task-system Adapter configured",
        extra={
            "event": "task_system_adapter_configured",
            "task_system_provider": provider,
            "linear_api_key_configured": settings.linear_api_key is not None,
            "linear_team_id_configured": settings.linear_team_id is not None,
            "linear_project_configured": (
                settings.linear_default_project_id is not None
            ),
        },
    )
    if provider == "disabled":
        return DisabledTaskSystemAdapter()
    if provider == "fake":
        return FakeTaskSystemAdapter(
            team_id=settings.linear_team_id or "local-demo-team",
            default_project_id=settings.linear_default_project_id,
        )
    api_key = settings.linear_api_key
    team_id = settings.linear_team_id
    if api_key is None or team_id is None:
        raise RuntimeError("Linear task-system settings were not validated")
    return LinearTaskSystemAdapter(
        api_url=str(settings.linear_api_url),
        api_key=api_key.get_secret_value(),
        team_id=team_id,
        default_project_id=settings.linear_default_project_id,
        request_timeout_seconds=settings.linear_request_timeout_seconds,
        max_search_results=settings.linear_max_search_results,
        transport=transport,
    )


async def validate_task_system_adapter(adapter: TaskSystemAdapter) -> None:
    validator = getattr(adapter, "validate_connection", None)
    if validator is not None:
        await validator()


__all__ = [
    "DisabledTaskSystemAdapter",
    "build_task_system_adapter",
    "validate_task_system_adapter",
]
