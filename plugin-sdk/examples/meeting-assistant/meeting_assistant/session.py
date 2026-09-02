from __future__ import annotations

import asyncio
import json

from .contracts import (ACTION_COMMANDS, COMMANDS, QUIET_STATUSES, authorized_command,
    identifier, ids, integer, object_value, operation_response, state_response)
from .view import build_meeting_view, selection_name


class MeetingSession:
    """One serial Host projection, with coalesced media hints and bounded polling."""
    def __init__(self, client, *, create_task, sleep=asyncio.sleep):
        self.client = client
        self.create_task = create_task
        self.sleep = sleep
        self.closed = False
        self.scope = None
        self.media_id = None
        self.snapshot = None
        self.live_executions = []
        self.detail = None
        self.error_code = None
        self.notice = None
        self.pending_operations = []
        self.selected_candidates = []
        self.selected_marks = []
        self.selected_execution = None
        self.offset = 0
        self.event_after = 0
        self.page_cursors = []
        self.detail_offset = 0
        self.detail_after = 0
        self.detail_cursors = []
        self.sequence = 0
        self.view_version = 0
        self.state_version = 0
        self._lock = asyncio.Lock()
        self._worker = None
        self._dirty = False
        self._failures = 0
        self._published = None
        self._refresh_count = 0
        self._refreshed = asyncio.Condition()
        self._commands = set()
        self._saved = None

    async def _call(self, name, value, key=None):
        return await self.client.capability(name=name, session_scope=self.scope,
            input_value=value, idempotency_key=key)

    def _require_scope(self, params):
        if (self.closed or params.get("session_scope") != self.scope
                or params.get("media_session_id", self.media_id) != self.media_id):
            raise ValueError("invalid session scope")

    async def open(self, params):
        self.scope = identifier(params.get("session_scope"))
        self.media_id = identifier(params.get("media_session_id"))
        self.sequence = integer(params.get("after_sequence", 0))
        restored = object_value(await self._call("state.get", {"key": "meeting-ui"}))
        self.state_version = integer(restored.get("version", 0))
        value = restored.get("value")
        if value is not None:
            value = object_value(value)
            self.view_version = integer(value.get("view_version", 0))
            self.selected_candidates = ids(value.get("selected_candidates", []), 20)
            self.selected_marks = ids(value.get("selected_marks", []))
            self.pending_operations = ids(value.get("pending_operations", []), 20)
            self.selected_execution = value.get("selected_execution")
            if self.selected_execution is not None:
                identifier(self.selected_execution)
            self.offset = integer(value.get("offset", 0), maximum=1_000_000)
            self.event_after = integer(value.get("event_after", 0))
            self.detail_offset = integer(value.get("detail_offset", 0), maximum=1_000_000)
            self.detail_after = integer(value.get("detail_after", 0))
            for key in ("page_cursors", "detail_cursors"):
                cursors = value.get(key, [])
                if not isinstance(cursors, list) or len(cursors) > 1000:
                    raise ValueError("invalid saved pagination")
                setattr(self, key, [integer(cursor) for cursor in cursors])
        await self.refresh()
        self._schedule(dirty=False)
        return {"opened": True}

    async def event_batch(self, params):
        self._require_scope(params)
        events = params.get("events")
        if not isinstance(events, list) or len(events) > 1000:
            raise ValueError("invalid event batch")
        # ACK never waits behind a Host read or operation response; no media text is retained.
        sequences = [integer(object_value(event).get("sequence"), 1) for event in events]
        for event in events:
            if event.get("media_session_id", self.media_id) != self.media_id:
                raise ValueError("wrong event scope")
        latest = max([self.sequence, *sequences])
        if latest > self.sequence:
            self.sequence = latest
            self._schedule()
        return {"acknowledged_sequence": self.sequence}

    def _poll_needed(self):
        if self.closed or self._failures >= 3:
            return False
        if self.pending_operations:
            return True
        if self.snapshot is None:
            return False
        if self.snapshot["processing"]["status"] in {"active", "draining"}:
            return True
        executions = self.live_executions + self.snapshot["executions"] + ([self.detail["execution"]] if self.detail and self.detail.get("execution") else [])
        return any(row["status"] not in QUIET_STATUSES for row in executions)

    def _schedule(self, *, dirty=True):
        if self.closed:
            return
        self._dirty |= dirty
        if self._worker is None and (self._dirty or self._poll_needed()):
            self._worker = self.create_task(self._run(), name="meeting-refresh")

    async def _run(self):
        try:
            while not self.closed:
                if not self._dirty:
                    if not self._poll_needed():
                        break
                    await self.sleep(2)
                self._dirty = False
                await self.refresh()
        finally:
            self._worker = None

    async def _read_snapshot(self):
        snapshot = state_response(await self._call("meeting.state.query", {"offset": self.offset, "after": self.event_after, "limit": 10}), self.media_id)
        # Browsing older pages must not hide a running handoff on the newest page.
        latest = state_response(await self._call("meeting.state.query", {"offset": 0, "after": 0, "limit": 10}), self.media_id) if self.offset else snapshot
        return snapshot, latest["executions"]

    async def refresh(self):
        async with self._lock:
            if self.closed:
                return
            read_succeeded = False
            try:
                snapshot, live_executions = await self._read_snapshot()
                detail = self.detail
                pending = []
                for operation_id in self.pending_operations:
                    value = operation_response(await self._call("meeting.operation.query", {"operation_id": operation_id, "limit": 10}),
                        snapshot["legacy_session_id"], operation_id=operation_id)
                    if value["operation"]["status"] in {"accepted", "queued", "running"}:
                        pending.append(operation_id)
                    if value.get("execution") or value["operation"].get("error_code"):
                        detail = value
                if len(pending) != len(self.pending_operations):
                    # Admission may complete between the state and operation read.
                    # Re-read after that completion before stopping an idle poller.
                    snapshot, live_executions = await self._read_snapshot()
                if self.selected_execution:
                    detail = operation_response(await self._call("meeting.operation.query", {"execution_id": self.selected_execution, "offset": self.detail_offset, "after": self.detail_after, "limit": 10}),
                        snapshot["legacy_session_id"], execution_id=self.selected_execution)
                # Only replace last success after every response has passed scope/shape checks.
                self.snapshot, self.detail, self.pending_operations = snapshot, detail, pending
                self.live_executions = live_executions
                self.error_code = None
                read_succeeded = True
            except Exception as error:
                self._failures += 1
                self.error_code = "scope_unavailable" if getattr(error, "code", None) in {
                    "plugin.scope.invalid", "plugin.permission.denied"} else "refresh_failed"
                if self.error_code == "scope_unavailable":
                    self._failures = 3
            try:
                await self._publish()
                if read_succeeded:
                    self._failures = 0
            except Exception:
                # A UI transport failure must not kill the owned refresh task or
                # erase the last published view. Stop after three failed rounds.
                self._failures += 1
                self.error_code = "view_unavailable"
        async with self._refreshed:
            self._refresh_count += 1
            self._refreshed.notify_all()

    async def command(self, params):
        task = asyncio.current_task()
        self._commands.add(task)
        try:
            return await self._command(params)
        finally:
            self._commands.discard(task)

    async def _command(self, params):
        self._require_scope(params)
        command = params.get("command")
        if command not in COMMANDS:
            raise ValueError("unknown command")
        if "payload" in params and "values" in params:
            raise ValueError("ambiguous command envelope")
        # Supervisor sends values; payload is accepted for direct SDK callers.
        payload = object_value(params.get("values", params.get("payload", {})))
        values = object_value(payload.get("values", payload))
        if command in ACTION_COMMANDS:
            async with self._lock:
                self.notice = "confirmation_required"
                await self._publish()
            return {"status": "confirmation_required", "action": ACTION_COMMANDS[command]}
        if command == "apply_action":
            name, value, key = authorized_command(payload)
            async with self._lock:
                if len(self.pending_operations) >= 20:
                    raise ValueError("too many pending operations")
                try:
                    accepted = object_value(await self._call(name, value, key))
                    operation_id = identifier(accepted.get("operation_id"), 36)
                    if set(accepted) != {"status", "operation_id"} or accepted["status"] != "accepted":
                        raise ValueError("invalid operation admission")
                except Exception:
                    self.notice = "operation_unknown"
                    await self._publish()
                    # Never retry automatically or regenerate the original request ID.
                    return {"status": "unknown"}
                if operation_id not in self.pending_operations:
                    self.pending_operations.append(operation_id)
                self.selected_execution = None
                self.detail_offset, self.detail_after, self.detail_cursors = 0, 0, []
                self.notice = None
                try:
                    await self._save()
                except Exception:
                    # Host operation is already durable. A UI-state failure does
                    # not undo admission or justify a second execution.
                    self.error_code = "view_unavailable"
            self._schedule()
            return {"status": "accepted", "operation_id": operation_id}
        async with self._lock:
            if command in {"select_candidates", "select_marks"}:
                field, id_field, maximum = ("candidates", "candidate_id", 20) if command == "select_candidates" else ("marks", "mark_id", 50)
                available = (self.snapshot or {}).get(field, [])
                existing = getattr(self, "selected_" + field)
                selected = ids(payload.get("ids", [row[id_field] for row in available if values.get(selection_name(id_field, row[id_field]), row[id_field] in existing) is True]), maximum)
                if not set(selected) <= {row[id_field] for row in available}:
                    raise ValueError("selection is not in the current page")
                setattr(self, "selected_" + field, selected)
            elif command == "select_execution":
                selected = identifier(payload.get("execution_id") or values.get("execution_id"))
                if selected not in {row["execution_id"] for row in (self.snapshot or {}).get("executions", [])}:
                    raise ValueError("execution is not in the current page")
                self.selected_execution = selected
                self.detail_offset, self.detail_after, self.detail_cursors = 0, 0, []
            elif command == "load_more" and self.snapshot and (self.snapshot["has_more"] or self.snapshot["has_more_events"]):
                if len(self.page_cursors) >= 1000:
                    raise ValueError("page navigation limit reached")
                self.page_cursors.append(self.event_after)
                self.event_after = self.snapshot["next_cursor"]
                self.offset = integer(self.snapshot["next_offset"], maximum=1_000_000)
            elif command == "previous_page":
                self.offset = max(0, self.offset - 10)
                self.event_after = self.page_cursors.pop() if self.page_cursors else 0
            elif command == "detail_next":
                if not self.detail or not self.detail.get("execution") or len(self.detail_cursors) >= 1000:
                    raise ValueError("execution page unavailable")
                self.selected_execution = self.detail["execution"]["execution_id"]
                self.detail_cursors.append(self.detail_after)
                self.detail_after = self.detail.get("next_cursor", self.detail_after)
                self.detail_offset = integer(self.detail_offset + 10, maximum=1_000_000)
            elif command == "detail_previous":
                self.detail_offset = max(0, self.detail_offset - 10)
                self.detail_after = self.detail_cursors.pop() if self.detail_cursors else 0
            self.notice = None
            self._failures = 0
            await self._save()
        await self.refresh()
        self._schedule(dirty=False)
        return {"refreshed": True}

    async def _save(self):
        # Pointers/cursors only: no transcript, reply body, grant, or intent secret.
        value = {"view_version": self.view_version, "offset": self.offset,
            "event_after": self.event_after, "page_cursors": self.page_cursors,
            "detail_offset": self.detail_offset, "detail_after": self.detail_after, "detail_cursors": self.detail_cursors,
            "selected_candidates": self.selected_candidates, "selected_marks": self.selected_marks,
            "selected_execution": self.selected_execution, "pending_operations": self.pending_operations}
        encoded = json.dumps(value, sort_keys=True)
        if encoded == self._saved:
            return
        result = await self._call("state.put", {"key": "meeting-ui", "expected_version": self.state_version, "value": value})
        self.state_version = integer(result.get("version"), self.state_version + 1)
        self._saved = encoded

    def _view(self):
        return build_meeting_view(snapshot=self.snapshot, detail=self.detail, error_code=self.error_code,
            notice=self.notice, selected_candidates=self.selected_candidates, selected_marks=self.selected_marks,
            pending_operations=self.pending_operations, detail_offset=self.detail_offset,
            live_executions=self.live_executions)

    async def _publish(self):
        view = self._view()
        fingerprint = json.dumps(view, sort_keys=True, ensure_ascii=False)
        if fingerprint == self._published:
            await self._save()
            return
        self.view_version += 1
        # Reserve version before publication: response loss/reopen cannot reuse an old version.
        await self._save()
        result = await self._call("ui.publish", {"surface": "panel", "view_id": view["view_id"],
            "view_version": self.view_version, "view": view["root"], "actions": view["actions"]})
        if result.get("accepted") is not True or result.get("view_version") != self.view_version:
            raise ValueError("view publication was not accepted")
        self._published = fingerprint

    async def wait_idle(self):
        if self._worker:
            await asyncio.wait_for(asyncio.shield(self._worker), 3)

    async def wait_refresh(self):
        target = self._refresh_count
        async with self._refreshed:
            await asyncio.wait_for(self._refreshed.wait_for(lambda: self._refresh_count > target), 3)

    async def close(self):
        self.closed = True
        current = asyncio.current_task()
        tasks = {task for task in [self._worker, *self._commands] if task and task is not current}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._worker = None
