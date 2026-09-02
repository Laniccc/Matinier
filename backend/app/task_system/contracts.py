from __future__ import annotations

from typing import Protocol, runtime_checkable

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


@runtime_checkable
class TaskSystemAdapter(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def connection(self) -> TaskSystemConnection: ...

    async def search(self, query: TaskSearchQuery) -> TaskSearchResult: ...

    async def get(self, task_ref: str) -> ExternalTask: ...

    async def create(
        self,
        draft: TaskDraft,
        *,
        action_key: str,
    ) -> TaskCreateResult: ...

    async def reconcile_create(
        self,
        *,
        action_key: str,
    ) -> TaskReconciliationResult: ...

    async def search_members(self, query: str) -> tuple[ExternalMember, ...]: ...

    async def get_member(self, external_user_id: str) -> ExternalMember: ...


__all__ = ["TaskSystemAdapter"]
