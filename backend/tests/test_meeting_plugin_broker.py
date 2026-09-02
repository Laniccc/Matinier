import asyncio

import pytest
from sqlalchemy import select

from app.assistant.plugin_operations import MeetingPluginOperations
from app.assistant.plugin_repository import MEETING_PLUGIN_ID, MeetingPluginDenied
from app.persistence.models import PluginCapabilityInvocationRecord
from app.plugins.broker import BrokerConnection, CapabilityBroker
from app.plugins.capabilities import CapabilityRegistry
from app.plugins.meeting_adapter import MeetingCapabilityAdapter, register_meeting_capabilities
from app.plugins.permissions import PermissionDeniedError, PermissionEvaluator
from app.plugins.repository import PluginRepository
from test_host_action_intents import make_actions, prepare, confirm_input, MEETING_PERMISSIONS


def broker(db, actions, f, *, current=True, plugin_id=MEETING_PLUGIN_ID, media=None):
    registry = CapabilityRegistry()
    register_meeting_capabilities(registry)
    return CapabilityBroker(connection=BrokerConnection(plugin_id, "1.0.0", 1, {"scope": media or f.media_id}),
        repository=PluginRepository(db), registry=registry, permission_evaluator=PermissionEvaluator(),
        network_resolver=None, meeting_adapter=MeetingCapabilityAdapter(db, MeetingPluginOperations(actions),
            is_current=lambda: current))


@pytest.mark.parametrize("capability", MEETING_PERMISSIONS)
def test_all_seven_capabilities_admit_scoped_commands(meeting_history, capability):
    async def scenario():
        f = meeting_history
        actions, context = make_actions(f)
        args = {"meeting.turn.submit": ("meeting.ask", {"message": "Explain"}),
            "meeting.mark.write": ("meeting.mark.create", {"operation": "create", "title": "Note",
                "evidence": [{"segment_id": "final-1", "revision": 2}]}),
            "meeting.execution.submit": ("meeting.execute", {"message": "Prepare release notes", "candidate_ids": [f.candidate_id]}),
            "meeting.execution.input": ("meeting.input", {"execution_id": f.child_id, "expected_state_version": 3, "input": "This release"}),
            "meeting.execution.cancel": ("meeting.cancel", {"execution_id": f.child_id, "expected_state_version": 3})}
        if capability in args:
            action, arguments = args[capability]
            preview = prepare(actions, context, f, action=action, arguments=arguments)
            command = actions.confirm(context, f.media_id, confirm_input(preview))["command"]
        else:
            command = {"execution_id": f.child_id} if capability == "meeting.operation.query" else {}
        with f.database.session() as db:
            value = await broker(db, actions, f).invoke(capability, command, session_scope="scope", idempotency_key="call-1")
            db.commit()
            if capability in args:
                assert value["operation_id"]
                again = await broker(db, actions, f).invoke(capability, command, session_scope="scope", idempotency_key="call-1")
                assert again == value
                row = db.scalar(select(PluginCapabilityInvocationRecord))
                assert command["intent_token"] not in str(row.request_json)
            else:
                assert value["data"]
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["generation", "owner", "media", "permission", "intent"])
def test_broker_rejects_untrusted_authority(meeting_history, change):
    async def scenario():
        f = meeting_history
        actions, context = make_actions(f)
        preview = prepare(actions, context, f)
        command = actions.confirm(context, f.media_id, confirm_input(preview))["command"]
        with f.database.session() as db:
            if change == "permission":
                PluginRepository(db).replace_base_permissions(plugin_id=MEETING_PLUGIN_ID, version="1.0.0", permissions=("meeting.state.query",))
            if change == "intent":
                command["intent_token"] = "z" * 48
            boundary = broker(db, actions, f, current=change != "generation",
                plugin_id="com.example.other" if change == "owner" else MEETING_PLUGIN_ID,
                media=f.other_media_id if change == "media" else None)
            with pytest.raises((MeetingPluginDenied, PermissionDeniedError)):
                await boundary.invoke("meeting.execution.submit", command, session_scope="scope", idempotency_key="call-1")
    asyncio.run(scenario())


def test_unknown_invocation_only_finds_previously_accepted_operation(meeting_history):
    async def scenario():
        f = meeting_history
        actions, context = make_actions(f)
        preview = prepare(actions, context, f, action="meeting.ask")
        command = actions.confirm(context, f.media_id, confirm_input(preview))["command"]
        with f.database.session() as db:
            boundary = broker(db, actions, f)
            accepted = await boundary.invoke("meeting.turn.submit", command, session_scope="scope", idempotency_key="call-1")
            row = db.scalar(select(PluginCapabilityInvocationRecord))
            row.status, row.outcome_unknown, row.result_json = "failed", True, None
            db.commit()
            assert await boundary.invoke("meeting.turn.submit", command, session_scope="scope", idempotency_key="call-1") == accepted
            from app.persistence.models import MeetingPluginOperationRecord
            assert len(list(db.scalars(select(MeetingPluginOperationRecord)))) == 1
    asyncio.run(scenario())


def test_runtime_meeting_model_wait_allows_other_plugin_state_ui_and_captions(meeting_history, tmp_path):
    async def scenario():
        from types import SimpleNamespace
        from app.plugins.bootstrap import PluginHostRuntime
        from app.plugins.container_runtime import PluginIdentity
        from app.assistant.plugin_runtime import MeetingPluginOperationWorker
        from test_meeting_plugin_operations import ControlledFastRunner
        from test_plugin_broker import install
        from meeting_plugin_fakes import final_segment
        f = meeting_history
        actions, context = make_actions(f, data_dir=tmp_path)
        preview = prepare(actions, context, f, action="meeting.ask")
        command = actions.confirm(context, f.media_id, confirm_input(preview))["command"]
        with f.database.session() as db:
            install(PluginRepository(db))
            db.commit()
        runtime = PluginHostRuntime(actions.settings, f.database)
        class Peer:
            def register_handler(self, _method, handler, **_kwargs):
                self.invoke = handler
        meeting, other = Peer(), Peer()
        for identity, peer in ((PluginIdentity(MEETING_PLUGIN_ID, "1.0.0"), meeting),
                               (PluginIdentity("com.example.viewer", "1.0.0"), other)):
            runtime._configure_peer(identity, peer, 1)
            runtime._scope_maps[identity]["scope"] = f.media_id
        await meeting.invoke({"capability": "meeting.turn.submit", "input": command,
            "session_scope": "scope", "idempotency_key": "call-1"})
        runner = ControlledFastRunner(f.database, blocked=True)
        worker = MeetingPluginOperationWorker(runtime.meeting_operations, SimpleNamespace(fast_runner=runner))
        await worker.start()
        try:
            await asyncio.wait_for(runner.entered.wait(), 2)
            assert not runtime._database_write_lock.locked()
            assert (await asyncio.wait_for(other.invoke({"capability": "state.get", "input": {"key": "notes"}, "session_scope": "scope"}), 2))["version"] == 0
            view = {"type": "text", "id": "ready", "text": "Course continues"}
            assert (await asyncio.wait_for(other.invoke({"capability": "ui.publish", "session_scope": "scope",
                "input": {"surface": "panel", "view_id": "main", "view_version": 1, "view": view}}), 2))["accepted"]
            with f.database.session() as db:
                db.add(final_segment(segment_id="while-meeting-model-waits"))
                db.commit()
            # A replaced process cannot reuse even its previously valid scope.
            identity = PluginIdentity(MEETING_PLUGIN_ID, "1.0.0")
            runtime._configure_peer(identity, Peer(), 2)
            with pytest.raises(MeetingPluginDenied):
                await meeting.invoke({"capability": "meeting.state.query", "input": {}, "session_scope": "scope"})
        finally:
            runner.release.set()
            await worker.flush(timeout=3)
            await worker.stop()
    asyncio.run(scenario())
