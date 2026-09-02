"""Real Host/Broker/controller/engine integration; only containers and providers fake."""
import asyncio
import datetime as dt
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select

from app.persistence.models import (AssistantExecutionRecord, MeetingPluginOperationRecord,
    MeetingProjectionOffsetRecord, SegmentRecord, SessionRecord)
from app.plugins.container_runtime import PluginIdentity
from app.plugins.ui_schema import parse_plugin_view
from meeting_plugin_client_fakes import load_plugin
from meeting_plugin_e2e_support import Harness, PLUGIN, VERSION, ADMIN, meeting_package, wait_for
from test_course_organizer_e2e import (CoursePeer, CourseProcess, FakeCourseContainerRuntime,
    CourseCapabilityClient, DeterministicCourseProvider, _course_package, _add_caption_pair)


class MeetingPeer(CoursePeer):
    def __init__(self):
        super().__init__()
        self.capabilities.create_task = lambda coroutine, **kwargs: asyncio.create_task(coroutine, **kwargs)
        self.plugin = load_plugin()(self.capabilities)
        self.sessions = self.plugin.sessions
        self.lose_reply = False

    async def request(self, method, params, *, timeout):
        handler = {"plugin.initialize": self.plugin.initialize, "session.open": self.plugin.open_session,
            "event.batch": self.plugin.event_batch, "command.execute": self.plugin.command,
            "plugin.heartbeat": self.plugin.heartbeat}.get(method)
        if handler is None:
            return await super().request(method, params, timeout=timeout)
        result = await handler(params)
        if method == "command.execute" and params.get("command") == "apply_action" and self.lose_reply:
            self.lose_reply = False
            raise TimeoutError("Injected reply loss after durable admission")
        return result

    async def notify(self, method, params):
        if method == "session.close":
            await self.plugin.close_session(params)
        elif method == "plugin.shutdown":
            await self.aclose()

    async def aclose(self):
        await self.plugin.shutdown({})
        self.closed = True


class Containers(FakeCourseContainerRuntime):
    async def start(self, spec):
        if spec.plugin_id != PLUGIN:
            return await super().start(spec)
        identity = (spec.plugin_id, spec.version)
        process = CourseProcess(f"meeting-{len(self.processes[identity]) + 1}", MeetingPeer())
        self.processes[identity].append(process)
        return process


def add_course_captions(database, first=1):
    for index, text in [(first, "Gradient is defined as a vector of partial derivatives."),
                        (first + 1, "For example, gradient descent updates parameters.")]:
        _add_caption_pair(database, session_id="meeting-a", index=index, start_ms=index * 61000,
            source=text, translation="梯度定义为偏导数组成的向量。例如，梯度下降更新参数。")


def test_installed_course_and_meeting_activation_ask_marks_confirm_and_history(tmp_path):
    containers, course = Containers(), DeterministicCourseProvider()
    h = Harness(tmp_path, containers=containers, course_provider=course)
    add_course_captions(h.database)
    key = Ed25519PrivateKey.generate()
    with h:
        h.install(_course_package(tmp_path, key), "com.matinier.course-organizer")
        h.install(meeting_package(tmp_path, key))
        assert h.bind() == "media-a"
        h.bind("meeting-b")
        wait_for(lambda: "知识坐标" in json.dumps(h.view(plugin="com.matinier.course-organizer"), ensure_ascii=False), "course notes")
        assert h.extractor.calls == [] and h.provider.calls == []
        assert h.history()["view"]["processing"]["status"] == "inactive"
        assert h.action("meeting.analysis.activate")["status"] == "applied"
        wait_for(lambda: h.history()["view"]["processing"]["pending_finals"] == 0, "backlog")
        assert all(s[0] == "meeting-a" for call in h.extractor.calls for s in call)
        assert h.history("media-b")["view"]["processing"]["status"] == "inactive"

        # A slow ask must finish and refresh the view with no new media event.
        h.provider.ask_release.clear()
        admitted = h.action("meeting.ask", {"message": "Explain release notes"})
        h.client.portal.call(asyncio.wait_for, h.provider.ask_entered.wait(), 3)
        course_before = h.view(plugin="com.matinier.course-organizer")
        assert course_before is not None
        h.client.portal.call(h.provider.ask_release.set)
        assert h.settled(admitted)["execution_status"] == "completed"
        wait_for(lambda: "Acceptance answer" in json.dumps(h.view()), "answer view without media")
        assert h.view(plugin="com.matinier.course-organizer") == course_before

        mark = h.action("meeting.mark.create", {"operation": "create", "title": "Important point",
            "evidence": [{"segment_id": "final-1", "revision": 2}]})
        assert h.settled(mark)["status"] == "completed"
        assert any(m["title"] == "Important point" for m in h.history()["view"]["marks"])

        arguments = {"message": "Create release notes", "candidate_ids": ["meeting-candidate"]}
        preview = h.prepare("meeting.execute", arguments)
        assert preview["confirmation_required"] and preview["max_side_effects"] == 1
        assert h.confirm(preview, False) == {"status": "cancelled"}
        assert h.linear.create_calls == 0
        admitted = h.action("meeting.execute", arguments)
        outcome = h.settled(admitted)
        assert outcome["execution_status"] == "completed", h.history(execution_id=outcome["execution_id"])
        assert h.linear.create_calls == 1 and h.linear.task_count == 1

        # Current Final revisions, not an ACK, determine projection completion.
        with h.database.session() as db:
            row = db.scalar(select(SegmentRecord).where(SegmentRecord.session_id == "meeting-a",
                SegmentRecord.segment_id == "course-segment-1"))
            row.revision += 1
            row.updated_at = dt.datetime.now(dt.UTC)
            db.commit()
        wait_for(lambda: any(("meeting-a", "course-segment-1", 2) in c for c in h.extractor.calls), "revised Final")
        with h.database.session() as db:
            row = db.get(SessionRecord, "meeting-a")
            row.status, row.stop_reason, row.ended_at = "completed", "source_ended", dt.datetime.now(dt.UTC)
            db.commit()
        wait_for(lambda: h.history()["view"]["processing"]["status"] == "completed", "natural end")
        documents = wait_for(lambda: h.client.get("/api/media-sessions/media-a/plugin-documents").json(), "course final document")
        assert all(d["plugin_id"] == "com.matinier.course-organizer" for d in documents)
        before = h.history()["view"]["executions"]
        assert h.client.post(f"/api/plugins/{PLUGIN}/disable", headers=ADMIN).status_code == 200
        assert h.history()["view"]["executions"] == before
        assert h.linear.create_calls == 1


def test_rpc_loss_crash_and_host_restart_do_not_repeat_accepted_ask(tmp_path):
    containers = Containers()
    h = Harness(tmp_path, containers=containers)
    with h:
        h.install(meeting_package(tmp_path)); h.bind()
        peer = containers.processes[(PLUGIN, VERSION)][-1].peer
        peer.lose_reply = True
        preview = h.prepare("meeting.ask", {"message": "Explain"})
        assert h.confirm(preview)["status"] == "unknown"
        admitted = h.confirm(preview)
        assert h.settled(admitted)["execution_status"] == "completed"
        old_scope = h.runtime.supervisor.binding(PluginIdentity(PLUGIN, VERSION), "media-a").scope
        process = containers.processes[(PLUGIN, VERSION)][-1]
        h.client.portal.call(process.crash)
        wait_for(lambda: len(containers.processes[(PLUGIN, VERSION)]) == 2 and
            h.runtime.supervisor.status(PluginIdentity(PLUGIN, VERSION)) == "ready", "plugin crash recovery")
        assert h.confirm(preview) == admitted
        # Old-generation callbacks cannot publish into the new session.
        async def stale_call():
            return await peer.capabilities.capability(name="meeting.state.query", session_scope=old_scope, input_value={})
        with pytest.raises(Exception):
            h.client.portal.call(stale_call)
        assert h.provider.calls == ["fast_turn"]
    restarted = Harness(tmp_path, containers=Containers(), seed=False)
    with restarted:
        restarted.bind()
        assert restarted.operation(admitted["operation_id"])["execution_status"] == "completed"
        assert restarted.provider.calls == []
        with restarted.database.session() as db:
            assert db.scalar(select(func.count(MeetingPluginOperationRecord.id))) == 1


def test_needs_input_then_cancel_and_queued_disable(tmp_path):
    h = Harness(tmp_path, containers=Containers())
    with h:
        h.install(meeting_package(tmp_path)); h.bind()
        h.action("meeting.analysis.activate")
        wait_for(lambda: h.history()["view"]["state_version"] > 0, "state")
        result = h.settled(h.action("meeting.execute", {"message": "Need input before create",
            "candidate_ids": ["meeting-candidate"]}))
        assert result["execution_status"] == "needs_input"
        result = h.settled(h.action("meeting.input", {"execution_id": result["execution_id"],
            "expected_state_version": result["state_version"], "input": "Please clarify again"}))
        assert result["execution_status"] == "needs_input"
        result = h.settled(h.action("meeting.cancel", {"execution_id": result["execution_id"],
            "expected_state_version": result["state_version"]}))
        assert result["execution_status"] == "cancelled"
        h.client.portal.call(h.assistant.operation_worker.stop)
        queued = h.action("meeting.ask", {"message": "Do not run after disable"})
        calls = list(h.provider.calls)
        assert h.client.post(f"/api/plugins/{PLUGIN}/disable", headers=ADMIN).status_code == 200
        h.client.portal.call(h.assistant.operation_worker.start)
        assert h.settled(queued)["status"] == "cancelled"
        assert h.provider.calls == calls and h.linear.create_calls == 0


def test_remote_create_response_lost_reconciles_without_duplicate(tmp_path):
    h = Harness(tmp_path, containers=Containers(), lose_response=True)
    with h:
        h.install(meeting_package(tmp_path)); h.bind()
        h.action("meeting.analysis.activate")
        wait_for(lambda: h.history()["view"]["state_version"] > 0, "state")
        result = h.settled(h.action("meeting.execute", {"message": "Create release notes",
            "candidate_ids": ["meeting-candidate"]}))
        assert result["execution_status"] == "completed"
        assert h.linear.create_calls == 1 and h.linear.reconcile_calls >= 1
        assert h.linear.task_count == 1
