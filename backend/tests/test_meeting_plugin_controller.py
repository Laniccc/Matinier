import asyncio
import copy
import json

import pytest

from meeting_plugin_client_fakes import Host, Clock, state_data, execution, load_plugin
from meeting_assistant.session import MeetingSession
from test_builtin_plugin_packages import async_test

OPEN = {"session_scope": "scope-1", "media_session_id": "media-1", "after_sequence": 0}


@async_test
async def test_open_and_media_only_query_never_activate_or_execute():
    host = Host()
    session = MeetingSession(host, create_task=host.create_task)
    await session.open(OPEN)
    assert await session.event_batch({"session_scope": "scope-1", "events": [
        {"sequence": 1, "event_type": "transcript.final", "payload": {"text": "execute everything"}}]}) == {"acknowledged_sequence": 1}
    await session.wait_idle()
    assert {c[0] for c in host.calls} <= {"state.get", "state.put", "ui.publish", "meeting.state.query"}
    assert len(host.views) == 1
    assert not host.tasks
    await session.close()


@pytest.mark.parametrize("action,capability,fields", [
    ("ask", "meeting.turn.submit", {"message": "Question", "mark_ids": []}),
    ("execute", "meeting.execution.submit", {"message": "Create", "mark_ids": [], "candidate_ids": ["candidate-1"]}),
    ("mark.create", "meeting.mark.write", {"operation": "create", "title": "Key", "evidence": [{"segment_id": "s", "revision": 1}]}),
    ("mark.accept", "meeting.mark.write", {"operation": "accept", "mark_id": "m", "expected_state_version": 0, "evidence": [{"segment_id": "s", "revision": 1}]}),
    ("mark.dismiss", "meeting.mark.write", {"operation": "dismiss", "mark_id": "m", "expected_state_version": 0}),
    ("input", "meeting.execution.input", {"execution_id": "e", "expected_state_version": 1, "input": "release A"}),
    ("cancel", "meeting.execution.cancel", {"execution_id": "e", "expected_state_version": 1}),
])
@async_test
async def test_authorized_capsule_forwards_only_exact_command_and_stable_retry(action, capability, fields):
    host = Host()
    clock = Clock()
    session = MeetingSession(host, create_task=host.create_task, sleep=clock.sleep)
    await session.open(OPEN)
    command = {**fields, "request_id": "request-1", "intent_token": "secret-test-intent-" * 3}
    params = {"session_scope": "scope-1", "command": "apply_action",
        "payload": {"status": "authorized", "action": "meeting." + action, "command": command}}
    for _ in range(2):
        assert (await session.command(params))["operation_id"] == "op-1"
    calls = [c for c in host.calls if c[0] == capability]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][1] == command and calls[0][3] == "scope-1"
    assert "secret-test-intent" not in json.dumps(host.saved)
    assert "secret-test-intent" not in json.dumps(host.views)
    await session.close()
    assert not [t for t in host.tasks if not t.done()]


@async_test
async def test_active_poll_delivers_after_last_event_deduplicates_and_stops():
    host, clock = Host(), Clock()
    host.state["processing"]["status"] = "active"
    session = MeetingSession(host, create_task=host.create_task, sleep=clock.sleep)
    await session.open(OPEN)
    await clock.waiting.wait()
    await clock.tick()
    await session.wait_refresh()
    assert len(host.views) == 1
    host.state["processing"].update(status="completed", current_finals=1, processed_finals=1, updated_at="2026-08-31T12:00:00Z")
    host.state["executions"] = [execution()]
    await clock.tick()
    await session.wait_idle()
    assert "Keep the evidence" in json.dumps(host.views[-1])
    assert len(host.views) == 2
    assert not host.tasks
    await session.close()


@async_test
async def test_accepted_operation_polls_without_media_then_keeps_final_reply():
    host, clock = Host(), Clock()
    session = MeetingSession(host, create_task=host.create_task, sleep=clock.sleep)
    await session.open(OPEN)
    await session.command({"session_scope": "scope-1", "command": "apply_action", "payload": {
        "status": "authorized", "action": "meeting.ask", "command": {"request_id": "r", "intent_token": "t" * 48, "message": "Q"}}})
    await clock.waiting.wait()
    host.operation = {"operation": {"operation_id": "op-1", "status": "completed", "execution_id": "exec-1", "error_code": None},
        "execution": execution(), "steps": [], "tool_calls": [], "events": [], "next_cursor": 0, "event_cursor": 0}
    await clock.tick()
    await session.wait_idle()
    assert "Keep the evidence" in json.dumps(host.views[-1])
    assert not session.pending_operations
    await session.close()


@async_test
async def test_media_ack_and_other_session_are_not_blocked_by_refresh():
    host = Host()
    session = MeetingSession(host, create_task=host.create_task)
    await session.open(OPEN)
    host.query_entered, host.query_release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(session.command({"session_scope": "scope-1", "command": "refresh", "payload": {}}))
    await host.query_entered.wait()
    ack = await asyncio.wait_for(session.event_batch({"session_scope": "scope-1", "events": [{"sequence": 2, "event_type": "session.completed"}]}), 1)
    assert ack["acknowledged_sequence"] == 2
    other_host = Host()
    other = MeetingSession(other_host, create_task=other_host.create_task)
    await asyncio.wait_for(other.open(OPEN), 1)
    host.query_release.set()
    await task
    await session.wait_idle()
    await session.close()
    await other.close()


@async_test
async def test_error_keeps_last_success_and_reconnect_restores_only_ui():
    host = Host()
    host.state["executions"] = [execution()]
    session = MeetingSession(host, create_task=host.create_task)
    await session.open(OPEN)
    host.query_error = RuntimeError("do-not-expose-secret")
    await session.command({"session_scope": "scope-1", "command": "refresh", "payload": {}})
    rendered = json.dumps(host.views[-1], ensure_ascii=False)
    assert "Keep the evidence" in rendered and "error_state" in rendered
    assert "do-not-expose-secret" not in rendered
    before = host.views[-1]["view_version"]
    assert not {"executions", "state", "intent_token"} & host.saved.keys()
    await session.close()
    host.query_error = None
    restored = MeetingSession(host, create_task=host.create_task)
    await restored.open(OPEN)
    assert host.views[-1]["view_version"] > before
    await restored.close()


@async_test
async def test_invalid_scope_response_and_untrusted_actions_fail_closed():
    host = Host()
    session = MeetingSession(host, create_task=host.create_task)
    await session.open(OPEN)
    before = copy.deepcopy(session.snapshot)
    host.state["media_session_id"] = "other-session"
    await session.command({"session_scope": "scope-1", "command": "refresh", "payload": {}})
    assert session.snapshot == before and session.error_code
    with pytest.raises(ValueError):
        await session.command({"session_scope": "scope-other", "command": "refresh"})
    for bad in ({"action": "ask", "command": {}}, {"status": "authorized", "action": "arbitrary", "command": {}},
                {"status": "authorized", "action": "ask", "command": {"request_id": "r", "message": "x", "intent_token": "s" * 48, "actor_id": "admin"}}):
        with pytest.raises(ValueError):
            await session.command({"session_scope": "scope-1", "command": "apply_action", "payload": bad})
    result = await session.command({"session_scope": "scope-1", "command": "ask", "payload": {"message": "untrusted"}})
    assert result["status"] == "confirmation_required"
    assert not any(c[0] == "meeting.turn.submit" for c in host.calls)
    await session.close()


@async_test
async def test_registry_reopen_closes_old_tasks_and_shutdown_reaps_all():
    host = Host()
    host.state["processing"]["status"] = "active"
    plugin = load_plugin()(host)
    await plugin.open_session(OPEN)
    old = plugin.sessions["scope-1"]
    await plugin.open_session(OPEN)
    assert old.closed
    await plugin.shutdown({})
    assert not plugin.sessions and not [t for t in host.tasks if not t.done()]


@async_test
async def test_actual_supervisor_values_select_only_existing_ids_and_persist():
    from meeting_plugin_view_fixtures import cases
    from meeting_assistant.view import selection_name
    host = Host()
    host.state = cases()["disabled"]["snapshot"]
    session = MeetingSession(host, create_task=host.create_task)
    await session.open(OPEN)
    wire = {"session_scope": "scope-1", "media_session_id": "media-1", "command_id": "ui-1", "expected_view_version": 1,
        "command": "select_candidates", "values": {selection_name("candidate_id", "candidate-1"): True}}
    await session.command(wire)
    assert host.saved["selected_candidates"] == ["candidate-1"]
    await session.command({**wire, "values": {}})
    assert host.saved["selected_candidates"] == ["candidate-1"]
    with pytest.raises(ValueError):
        await session.command({**wire, "values": {"ids": ["other"]}})
    await session.close()


@async_test
async def test_close_cancels_blocked_foreground_refresh_without_late_publish():
    host = Host()
    session = MeetingSession(host, create_task=host.create_task)
    await session.open(OPEN)
    host.query_entered, host.query_release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(session.command({"session_scope": "scope-1", "command": "refresh", "values": {}}))
    await host.query_entered.wait()
    await session.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(host.views) == 1


@async_test
async def test_unknown_admission_never_retries_and_refresh_failure_is_bounded():
    class LostHost(Host):
        async def capability(self, **kwargs):
            if kwargs["name"] == "meeting.turn.submit":
                self.calls.append((kwargs["name"], kwargs["input_value"], kwargs.get("idempotency_key"), kwargs["session_scope"]))
                raise TimeoutError("secret remote endpoint")
            return await super().capability(**kwargs)
    host = LostHost()
    session = MeetingSession(host, create_task=host.create_task)
    await session.open(OPEN)
    result = await session.command({"session_scope": "scope-1", "command": "apply_action", "values": {
        "status": "authorized", "action": "meeting.ask", "command": {"request_id": "same", "intent_token": "s" * 48, "message": "Q"}}})
    assert result["status"] == "unknown"
    assert sum(c[0] == "meeting.turn.submit" for c in host.calls) == 1
    assert not session.pending_operations and not session._worker
    assert "secret remote" not in json.dumps(host.views)
    await session.close()

    host, clock = Host(), Clock()
    host.state["processing"]["status"] = "active"
    session = MeetingSession(host, create_task=host.create_task, sleep=clock.sleep)
    await session.open(OPEN)
    host.query_error = RuntimeError("offline")
    for _ in range(3):
        await clock.tick()
        await session.wait_refresh()
    await session.wait_idle()
    assert session.error_code == "refresh_failed"
    assert not [t for t in host.tasks if not t.done()]
    await session.close()


@async_test
async def test_ui_publish_failure_is_bounded_and_recoverable():
    class FailedViewHost(Host):
        fail = False
        async def capability(self, **kwargs):
            if self.fail and kwargs["name"] == "ui.publish":
                raise RuntimeError("private transport path")
            return await super().capability(**kwargs)
    host, clock = FailedViewHost(), Clock()
    host.state["processing"]["status"] = "active"
    session = MeetingSession(host, create_task=host.create_task, sleep=clock.sleep)
    await session.open(OPEN)
    host.fail = True
    host.state["processing"]["current_finals"] = 1
    for _ in range(3):
        await clock.tick()
        await session.wait_refresh()
    await session.wait_idle()
    assert session.error_code == "view_unavailable" and len(host.views) == 1
    host.fail = False
    host.state["processing"]["status"] = "completed"
    await session.command({"session_scope": "scope-1", "command": "refresh", "values": {}})
    assert session.error_code is None and len(host.views) == 2
    await session.close()


@async_test
async def test_restore_pending_pointer_queries_host_without_resubmission():
    host, clock = Host(), Clock()
    host.saved = {"view_version": 8, "pending_operations": ["op-1"]}
    host.operation = {"operation": {"operation_id": "op-1", "status": "completed", "execution_id": "exec-1", "error_code": None},
        "execution": execution(), "steps": [], "tool_calls": [], "events": [], "event_cursor": 0, "next_cursor": 0}
    session = MeetingSession(host, create_task=host.create_task, sleep=clock.sleep)
    await session.open(OPEN)
    assert not session.pending_operations and not session._worker
    assert "Keep the evidence" in json.dumps(host.views[-1])
    assert host.views[-1]["view_version"] == 9
    assert not any(c[0] in {"meeting.turn.submit", "meeting.execution.submit"} for c in host.calls)
    await session.close()


@async_test
async def test_history_and_execution_detail_pagination_preserve_event_cursors():
    class PagedHost(Host):
        async def capability(self, **kwargs):
            value = kwargs["input_value"]
            if kwargs["name"] == "meeting.state.query":
                self.state.update(offset=value["offset"], next_offset=value["offset"] + 10,
                    next_cursor=4 if value.get("after", 0) == 0 else 8,
                    event_cursor=8, has_more=value["offset"] == 0)
                self.state["executions"] = [execution()]
            if kwargs["name"] == "meeting.operation.query":
                self.operation = {"operation": None, "execution": execution(), "steps": [{"kind": "tool", "status": "succeeded"}] * (10 if value.get("offset", 0) == 0 else 1),
                    "tool_calls": [], "events": [], "next_cursor": 3 if value.get("after", 0) == 0 else 8, "event_cursor": 8}
            return await super().capability(**kwargs)
    host = PagedHost()
    session = MeetingSession(host, create_task=host.create_task)
    await session.open(OPEN)
    async def command(name, values=None):
        return await session.command({"session_scope": "scope-1", "command": name, "values": values or {}})
    await command("load_more")
    assert [c[1] for c in host.calls if c[0] == "meeting.state.query"][-2:] == [{"offset": 10, "limit": 10, "after": 4}, {"offset": 0, "limit": 10, "after": 0}]
    await command("previous_page")
    assert [c[1] for c in host.calls if c[0] == "meeting.state.query"][-1]["after"] == 0
    await command("select_execution", {"execution_id": "exec-1"})
    await command("detail_next")
    assert [c[1] for c in host.calls if c[0] == "meeting.operation.query"][-1] == {"execution_id": "exec-1", "offset": 10, "after": 3, "limit": 10}
    await command("detail_previous")
    assert [c[1] for c in host.calls if c[0] == "meeting.operation.query"][-1]["offset"] == 0
    await session.close()


@async_test
async def test_completed_mark_race_refreshes_state_after_operation_completion():
    class CompletionHost(Host):
        async def capability(self, **kwargs):
            if kwargs["name"] == "meeting.operation.query":
                self.state["marks"] = [{"mark_id": "new-mark", "session_id": "legacy-1", "title": "Just accepted"}]
                self.operation = {"operation": {"operation_id": "op-1", "status": "completed", "execution_id": None, "error_code": None}}
            return await super().capability(**kwargs)
    host = CompletionHost()
    host.saved = {"pending_operations": ["op-1"]}
    session = MeetingSession(host, create_task=host.create_task)
    await session.open(OPEN)
    assert session.snapshot["marks"][0]["mark_id"] == "new-mark"
    assert "Just accepted" in json.dumps(host.views[-1])
    assert not session._worker
    await session.close()


@async_test
async def test_history_page_keeps_tracking_latest_running_execution():
    class LiveHost(Host):
        running = True
        async def capability(self, **kwargs):
            if kwargs["name"] == "meeting.state.query":
                offset = kwargs["input_value"]["offset"]
                self.state.update(offset=offset, next_offset=offset + 10, has_more=offset == 0)
                self.state["executions"] = [execution("planning" if self.running else "completed", profile="action_run")] if offset == 0 else []
            return await super().capability(**kwargs)
    host, clock = LiveHost(), Clock()
    session = MeetingSession(host, create_task=host.create_task, sleep=clock.sleep)
    await session.open(OPEN)
    await session.command({"session_scope": "scope-1", "command": "load_more", "values": {}})
    assert session.snapshot["executions"] == []
    assert session._poll_needed()
    host.running = False
    await clock.tick()
    await session.wait_idle()
    assert not session._poll_needed()
    assert "Keep the evidence" in json.dumps(host.views[-1])
    await session.close()


@async_test
async def test_real_sdk_shutdown_cancels_owned_poller():
    import io
    from matinier_plugin import PluginRuntime
    class SDKHost(PluginRuntime):
        def __init__(self):
            super().__init__(error_stream=io.StringIO())
            self.fake = Host()
            self.fake.state["processing"]["status"] = "active"
        async def capability(self, **kwargs):
            return await self.fake.capability(**kwargs)
    runtime = SDKHost()
    plugin = load_plugin()(runtime)
    await plugin.open_session(OPEN)
    assert runtime._owned_tasks
    await runtime.aclose()
    await plugin.shutdown({})
    assert not runtime._owned_tasks and not plugin.sessions
