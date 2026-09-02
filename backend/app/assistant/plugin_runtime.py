"""Small durable operation dispatcher; SQLite transactions end before awaits."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.assistant.fast_runner import FastTurnRequest
from app.assistant.plugin_operations import MeetingPluginOperations
from app.assistant.plugin_repository import MeetingPluginDenied
from app.assistant.repository import AssistantRepository
from app.assistant.state_machine import is_terminal_execution_status
from app.persistence.models import AssistantExecutionRecord, MeetingPluginOperationRecord
from app.plugins.host_actions import begin_write


class MeetingPluginOperationWorker:
    def __init__(self, service: MeetingPluginOperations, runtime, *, concurrency: int = 2, poll_seconds: float = 0.2):
        if not 1 <= concurrency <= 16 or poll_seconds <= 0:
            raise ValueError("invalid meeting worker limits")
        self.service = service
        self.runtime = runtime
        self.concurrency = concurrency
        self.poll_seconds = poll_seconds
        self._workers: list[asyncio.Task] = []
        self._changed = asyncio.Event()

    async def start(self):
        if self._workers:
            return
        self._recover()
        self._workers = [asyncio.create_task(self._loop(), name=f"meeting-operation-{i}") for i in range(self.concurrency)]

    async def stop(self):
        tasks, self._workers = self._workers, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._changed.set()

    async def flush(self, *, timeout: float = 10):
        async with asyncio.timeout(timeout):
            while True:
                self._changed.clear()
                with self.service.database.session() as db:
                    pending = db.scalar(select(MeetingPluginOperationRecord.id).where(
                        MeetingPluginOperationRecord.status.in_(("accepted", "running")),
                    ).limit(1))
                if pending is None:
                    return
                await self._changed.wait()

    def _recover(self):
        with self.service.database.session() as db:
            begin_write(db)
            rows = list(db.scalars(select(MeetingPluginOperationRecord).where(MeetingPluginOperationRecord.status == "running")))
            for row in rows:
                execution = db.get(AssistantExecutionRecord, row.execution_id)
                if execution is None:
                    row.status, row.error_code = "failed", "execution_missing"
                elif is_terminal_execution_status(execution.profile, execution.status) or execution.status == "needs_input":
                    row.status = "failed" if execution.status == "failed" else "completed"
                elif execution.profile == "fast_turn" and execution.status != "received":
                    row.status, row.error_code = "failed", "model_outcome_unknown"
                    AssistantRepository(db).transition_execution(execution.id, expected_version=execution.state_version,
                        target_status="failed", event_type="meeting.model_outcome_unknown",
                        summary="Model outcome requires user review; automatic replay is disabled",
                        error_code="model_outcome_unknown")
                else:
                    row.status = "accepted"
            db.commit()

    def _claim(self):
        with self.service.database.session() as db:
            begin_write(db)
            self._refresh_running(db)
            row = db.scalar(select(MeetingPluginOperationRecord).where(
                MeetingPluginOperationRecord.status == "accepted",
            ).order_by(MeetingPluginOperationRecord.created_at, MeetingPluginOperationRecord.id).limit(1))
            identifier = None
            if row is not None:
                changed = db.execute(update(MeetingPluginOperationRecord).where(
                    MeetingPluginOperationRecord.id == row.id, MeetingPluginOperationRecord.status == "accepted",
                ).values(status="running").execution_options(synchronize_session=False))
                if changed.rowcount == 1:
                    identifier = row.id
            db.commit()
        self._changed.set()
        return identifier

    def _refresh_running(self, db):
        rows = db.execute(select(MeetingPluginOperationRecord, AssistantExecutionRecord).join(
            AssistantExecutionRecord, AssistantExecutionRecord.id == MeetingPluginOperationRecord.execution_id,
        ).where(MeetingPluginOperationRecord.status == "running")).all()
        for operation, execution in rows:
            if is_terminal_execution_status(execution.profile, execution.status) or execution.status == "needs_input":
                operation.status = "failed" if execution.status == "failed" else "completed"
                operation.error_code = execution.error_code

    async def _loop(self):
        while True:
            try:
                identifier = self._claim()
            except OperationalError:
                logging.getLogger(__name__).warning("Meeting operation claim temporarily unavailable")
                await asyncio.sleep(self.poll_seconds)
                continue
            if identifier is None:
                await asyncio.sleep(self.poll_seconds)
                continue
            try:
                await self._dispatch(identifier)
            except asyncio.CancelledError:
                raise
            except MeetingPluginDenied:
                self._fail(identifier, "authority_revoked", cancelled=True)
            except Exception:
                self._fail(identifier, "operation_failed")
            finally:
                self._changed.set()

    async def _dispatch(self, identifier: str):
        with self.service.database.session() as db:
            operation = db.get(MeetingPluginOperationRecord, identifier)
            scope = self.service.require_authority(db, operation)
            execution = db.get(AssistantExecutionRecord, operation.execution_id)
            arguments = dict(operation.request_payload_json)
            action = operation.action
            execution_id = execution.id
            request = FastTurnRequest(session_id=scope.legacy_session_id, goal=execution.goal,
                actor_id=scope.actor_id, client_request_id=execution.client_request_id,
                mark_ids=tuple(arguments.get("mark_ids", ())), allow_handoff=True)
        # No caller/Broker database Session or write lock exists beyond here.
        if action == "meeting.ask":
            await self.runtime.fast_runner.run(request)
        elif action in {"meeting.execute", "meeting.input"}:
            scheduler = self.runtime.action_runtime.scheduler
            if action == "meeting.input":
                await scheduler.resume(execution_id)
            else:
                await scheduler.enqueue(execution_id)
        with self.service.database.session() as db:
            self._refresh_running(db)
            db.commit()

    def _fail(self, identifier: str, error_code: str, *, cancelled=False):
        with self.service.database.session() as db:
            begin_write(db)
            row = db.get(MeetingPluginOperationRecord, identifier)
            row.status, row.error_code = ("cancelled" if cancelled else "failed"), error_code
            execution = db.get(AssistantExecutionRecord, row.execution_id)
            if execution is not None and not is_terminal_execution_status(execution.profile, execution.status):
                AssistantRepository(db).transition_execution(execution.id, expected_version=execution.state_version,
                    target_status="cancelled" if cancelled else "failed", event_type="meeting.operation_stopped",
                    summary="Meeting operation stopped", error_code=error_code)
            db.commit()
