from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.persistence.database import Database
from app.plugins import bootstrap
from app.plugins.bootstrap import PluginHostRuntime
from app.plugins.container_runtime import PluginIdentity
from app.settings import Settings


class Supervisor:
    def __init__(self):
        self.states = {}
        self.stopped = False

    def statuses(self):
        return self.states.copy()

    async def shutdown(self):
        self.stopped = True


class Projector:
    async def stop(self):
        pass


def runtime_for_test(tmp_path, *, enabled=True):
    settings = Settings(
        _env_file=None, data_dir=tmp_path,
        livekit_url="ws://127.0.0.1:7880", livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="delivery-test", plugin_framework_enabled=enabled,
        media_event_projector_poll_interval_ms=10,
    )
    database = Database("sqlite://")
    database.create_schema()
    runtime = PluginHostRuntime(settings, database, media_projector=Projector())
    runtime.supervisor = Supervisor()
    return runtime, database


async def wait_for(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), timeout=1)


def test_dispatcher_starts_empty_picks_up_new_scopes_and_stops(tmp_path, monkeypatch):
    async def scenario():
        runtime, database = runtime_for_test(tmp_path)
        identity = PluginIdentity("com.example.course", "1.0.0")
        calls = []

        async def deliver(key, media_id, *, max_batches):
            calls.append((key, media_id, max_batches))

        monkeypatch.setattr(runtime, "_deliver_pending_events", deliver)
        try:
            await runtime.start()
            dispatcher = runtime._event_dispatch_task
            await runtime.start()
            assert runtime._event_dispatch_task is dispatcher
            assert not calls
            runtime.supervisor.states[identity] = "ready"
            runtime._scope_maps[identity] = {"scope-1": "media-1"}
            await wait_for(lambda: bool(calls))
            assert set(calls) == {(identity, "media-1", 1)}
            runtime._scope_maps[identity].clear()
            await asyncio.sleep(0.03)
            count = len(calls)
            await asyncio.sleep(0.03)
            assert len(calls) == count
            await runtime.stop()
            assert dispatcher.done()
            assert runtime._event_dispatch_task is None
            assert not runtime._event_delivery_tasks
            assert runtime.supervisor.stopped
        finally:
            await runtime.stop()
            database.dispose()
    asyncio.run(scenario())


def test_slow_or_failing_plugin_does_not_block_other_scopes(tmp_path, monkeypatch, caplog):
    async def scenario():
        runtime, database = runtime_for_test(tmp_path)
        identities = [PluginIdentity(f"com.example.{name}", "1.0.0")
                      for name in ("slow", "failed", "healthy", "offline")]
        slow, failed, healthy, offline = identities
        calls = {identity: 0 for identity in identities}
        cancelled = asyncio.Event()
        for identity in identities:
            runtime.supervisor.states[identity] = "ready" if identity != offline else "crashed"
            runtime._scope_maps[identity] = {"scope": identity.plugin_id}

        async def deliver(identity, _media_id, *, max_batches):
            calls[identity] += 1
            assert max_batches == 1
            if identity == slow:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            if identity == failed:
                raise RuntimeError("private payload must not appear in logs")

        monkeypatch.setattr(runtime, "_deliver_pending_events", deliver)
        try:
            await runtime.start()
            await wait_for(lambda: calls[healthy] >= 3 and calls[failed] >= 2)
            assert calls[slow] == 1
            assert calls[offline] == 0
            # A revoked scope cancels its pending delivery, not just future polls.
            runtime._scope_maps[slow].clear()
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            assert "private payload" not in caplog.text
        finally:
            await runtime.stop()
            database.dispose()
    asyncio.run(scenario())


def test_shutdown_cancels_delivery_before_supervisor_shutdown(tmp_path, monkeypatch):
    async def scenario():
        runtime, database = runtime_for_test(tmp_path)
        identity = PluginIdentity("com.example.course", "1.0.0")
        runtime.supervisor.states[identity] = "ready"
        runtime._scope_maps[identity] = {"scope": "media-1"}
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def deliver(*_args, **_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                assert not runtime.supervisor.stopped
                cancelled.set()

        monkeypatch.setattr(runtime, "_deliver_pending_events", deliver)
        try:
            await runtime.start()
            await asyncio.wait_for(entered.wait(), timeout=1)
            await asyncio.wait_for(runtime.stop(), timeout=1)
            assert cancelled.is_set()
            assert not runtime._event_delivery_tasks
            assert not runtime._event_delivery_locks
        finally:
            await runtime.stop()
            database.dispose()
    asyncio.run(scenario())


def test_background_and_foreground_share_one_cursor_lock(tmp_path, monkeypatch):
    async def scenario():
        runtime, database = runtime_for_test(tmp_path)
        identity = PluginIdentity("com.example.course", "1.0.0")
        runtime._scope_maps[identity] = {"scope": "media-1"}
        entered, release = asyncio.Event(), asyncio.Event()
        cursor, sent = 0, []

        async def batches(*_args, **_kwargs):
            nonlocal cursor
            old_cursor = cursor
            entered.set()
            await release.wait()
            if old_cursor == 0:
                sent.append(1)
                cursor = 1

        monkeypatch.setattr(runtime, "_deliver_event_batches", batches)
        first = asyncio.create_task(runtime._deliver_pending_events(identity, "media-1", max_batches=1))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            second = asyncio.create_task(runtime._deliver_pending_events(identity, "media-1"))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
            assert sent == [1]
            assert not runtime._event_delivery_locks[(identity, "media-1")].locked()
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            database.dispose()
    asyncio.run(scenario())


def test_framework_disabled_does_not_start_dispatcher(tmp_path):
    async def scenario():
        runtime, database = runtime_for_test(tmp_path, enabled=False)
        try:
            await runtime.start()
            assert runtime._event_dispatch_task is None
            assert not runtime._started
        finally:
            await runtime.stop()
            database.dispose()
    asyncio.run(scenario())


def test_failed_delivery_retries_unacknowledged_events(tmp_path, monkeypatch):
    async def scenario():
        runtime, database = runtime_for_test(tmp_path)
        identity = PluginIdentity("com.example.course", "1.0.0")
        runtime._scope_maps[identity] = {"scope": "media-1"}
        binding = SimpleNamespace(id="binding-1", last_acknowledged_sequence=0)
        reads, delivered, acknowledged = [], [], []
        attempts = 0

        class Repository:
            def __init__(self, _session):
                pass

            def get_package(self, *_args):
                return SimpleNamespace(manifest_json={})

            def get_binding_for_identity(self, **_kwargs):
                return binding

            def advance_binding_delivery(self, _id, *, sequence):
                delivered.append(sequence)

            def acknowledge_binding(self, _id, *, sequence):
                acknowledged.append(sequence)
                binding.last_acknowledged_sequence = sequence

        def events(_media_id, *, after_sequence, limit):
            reads.append(after_sequence)
            return [{"sequence": 1, "event_type": "transcript.final"}] if after_sequence == 0 else []

        async def send(*_args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary RPC failure")
            return 1

        monkeypatch.setattr(bootstrap, "PluginRepository", Repository)
        monkeypatch.setattr(bootstrap, "PluginManifest", SimpleNamespace(
            model_validate=lambda _value: SimpleNamespace(subscriptions=["transcript.final"]),
        ))
        monkeypatch.setattr(runtime, "list_media_events", events)
        runtime.supervisor.deliver_events = send
        try:
            await runtime._deliver_background_events(identity, "media-1")
            assert binding.last_acknowledged_sequence == 0
            assert not acknowledged
            await runtime._deliver_background_events(identity, "media-1")
            assert reads == [0, 0]
            assert delivered == [1, 1]
            assert acknowledged == [1]
            await runtime._deliver_background_events(identity, "media-1")
            assert reads[-1] == 1
            assert attempts == 2
        finally:
            database.dispose()
    asyncio.run(scenario())
