from __future__ import annotations

import asyncio
import datetime as dt

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


class FakeTaskSystemAdapter:
    """Recordable in-memory adapter used by the one positive Agent test."""

    provider_name = "fake-task-system"

    def __init__(
        self,
        *,
        team_id: str = "fake-team",
        workspace_id: str = "fake-workspace",
        default_project_id: str | None = "fake-project",
        members: tuple[ExternalMember, ...] = (),
    ) -> None:
        self._connection = TaskSystemConnection(
            provider=self.provider_name,
            team_id=team_id,
            workspace_id=workspace_id,
            default_project_id=default_project_id,
        )
        self._tasks: dict[str, ExternalTask] = {}
        self._members = {
            member.external_user_id: member for member in members
        }
        self._lock = asyncio.Lock()
        self._next_number = 1

    @property
    def connection(self) -> TaskSystemConnection:
        return self._connection

    @property
    def tasks(self) -> tuple[ExternalTask, ...]:
        return tuple(
            sorted(self._tasks.values(), key=lambda task: task.identifier)
        )

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    async def search(self, query: TaskSearchQuery) -> TaskSearchResult:
        normalized_title = (
            " ".join(query.title.casefold().split())
            if query.title is not None
            else None
        )
        matches = []
        for task in self.tasks:
            if query.action_key is not None and task.action_key != query.action_key:
                continue
            if (
                normalized_title is not None
                and normalized_title
                not in " ".join(task.title.casefold().split())
            ):
                continue
            if (
                query.assignee_ref is not None
                and task.assignee_ref != query.assignee_ref
            ):
                continue
            if query.due_at is not None and task.due_at != query.due_at:
                continue
            matches.append(task)
        return TaskSearchResult(tasks=tuple(matches[: query.limit]))

    async def get(self, task_ref: str) -> ExternalTask:
        if task_ref in self._tasks:
            return self._tasks[task_ref]
        for task in self._tasks.values():
            if task.identifier == task_ref:
                return task
        raise LookupError(f"Fake task not found: {task_ref}")

    async def create(
        self,
        draft: TaskDraft,
        *,
        action_key: str,
    ) -> TaskCreateResult:
        async with self._lock:
            existing = [
                task
                for task in self._tasks.values()
                if task.action_key == action_key
            ]
            if existing:
                return TaskCreateResult(task=existing[0], created=False)
            number = self._next_number
            self._next_number += 1
            now = dt.datetime.now(dt.UTC)
            external_id = f"fake-task-{number}"
            task = ExternalTask(
                external_id=external_id,
                identifier=f"FAKE-{number}",
                url=f"https://fake.tasks.local/{external_id}",
                title=draft.title,
                description=draft.description,
                assignee_ref=draft.assignee_ref,
                due_at=draft.due_at,
                priority=draft.priority,
                status="active",
                team_id=self.connection.team_id,
                project_id=self.connection.default_project_id,
                action_key=action_key,
                created_at=now,
                updated_at=now,
            )
            self._tasks[external_id] = task
            return TaskCreateResult(task=task, created=True)

    async def reconcile_create(
        self,
        *,
        action_key: str,
    ) -> TaskReconciliationResult:
        matches = tuple(
            task for task in self.tasks if task.action_key == action_key
        )
        status = "none" if not matches else (
            "single" if len(matches) == 1 else "multiple"
        )
        return TaskReconciliationResult(status=status, matches=matches)

    async def search_members(self, query: str) -> tuple[ExternalMember, ...]:
        normalized = " ".join(query.casefold().split())
        return tuple(
            member
            for member in self._members.values()
            if normalized in " ".join(member.display_name.casefold().split())
            or (
                member.email is not None
                and normalized in " ".join(member.email.casefold().split())
            )
        )

    async def get_member(self, external_user_id: str) -> ExternalMember:
        try:
            return self._members[external_user_id]
        except KeyError as error:
            raise LookupError(
                f"Fake task-system member not found: {external_user_id}"
            ) from error


__all__ = ["FakeTaskSystemAdapter"]
