from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import pytest

from app.plugins.container_runtime import PluginContainerSpec
from app.plugins.supervisor import (
    PluginIdentity,
    PluginSupervisor,
    RestoredSession,
    RestartPolicy,
    StaleSessionScopeError,
)


class FakePeer:
    def __init__(self, plugin_id: str, version: str) -> None:
        self.plugin_id = plugin_id
        self.version = version
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.notifications: list[tuple[str, dict[str, object]]] = []
        self.heartbeat_error = False
        self.close_hangs = False
        self.closed = False

    async def start(self) -> None:
        return None

    async def request(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout: float,
    ) -> object:
        del timeout
        self.calls.append((method, params))
        if method == "plugin.initialize":
            return {
                "plugin_id": self.plugin_id,
                "version": self.version,
                "protocol_version": "1.0",
                "host_api": "1.0.0",
            }
        if method == "plugin.heartbeat" and self.heartbeat_error:
            raise TimeoutError("heartbeat missed")
        return {"ok": True}

    async def notify(self, method: str, params: dict[str, object]) -> None:
        self.notifications.append((method, params))

    async def aclose(self) -> None:
        if self.close_hangs:
            await asyncio.Event().wait()
        self.closed = True


@dataclass
class FakeProcess:
    container_id: str
    peer: FakePeer

    def __post_init__(self) -> None:
        self._exit: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def wait(self) -> int:
        return await self._exit

    def crash(self, exit_code: int = 1) -> None:
        if not self._exit.done():
            self._exit.set_result(exit_code)


class FakeContainerRuntime:
    def __init__(self) -> None:
        self.starts: list[PluginContainerSpec] = []
        self.processes: dict[tuple[str, str], list[FakeProcess]] = defaultdict(list)
        self.stops: list[str] = []

    async def available(self) -> bool:
        return True

    async def start(self, spec: PluginContainerSpec) -> FakeProcess:
        self.starts.append(spec)
        peer = FakePeer(spec.plugin_id, spec.version)
        process = FakeProcess(
            container_id=f"{spec.plugin_id}-{spec.version}-{len(self.starts)}",
            peer=peer,
        )
        self.processes[(spec.plugin_id, spec.version)].append(process)
        return process

    async def stop(self, container_id: str, *, timeout: float) -> None:
        del timeout
        self.stops.append(container_id)
        for processes in self.processes.values():
            for process in processes:
                if process.container_id == container_id:
                    process.crash(0)


def spec(plugin_id: str, version: str = "1.0.0") -> PluginContainerSpec:
    return PluginContainerSpec(
        plugin_id=plugin_id,
        version=version,
        image_ref=f"{plugin_id}:{version}",
        command=("python", "-m", "plugin"),
    )


def test_one_process_per_version_and_concurrent_logical_sessions() -> None:
    async def scenario() -> None:
        runtime = FakeContainerRuntime()
        supervisor = PluginSupervisor(
            runtime,
            peer_factory=lambda process: process.peer,
            heartbeat_interval=60,
        )
        plugin = spec("com.example.viewer")
        await asyncio.gather(supervisor.enable(plugin), supervisor.enable(plugin))
        bindings = await asyncio.gather(
            supervisor.open_session(plugin.identity, "media-one"),
            supervisor.open_session(plugin.identity, "media-two"),
        )

        assert len(runtime.starts) == 1
        assert {item.media_session_id for item in bindings} == {
            "media-one",
            "media-two",
        }
        assert len({item.scope for item in bindings}) == 2
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_heartbeat_state_and_stale_scope_after_restart() -> None:
    async def scenario() -> None:
        runtime = FakeContainerRuntime()
        transitions: list[str] = []
        supervisor = PluginSupervisor(
            runtime,
            peer_factory=lambda process: process.peer,
            status_sink=lambda _identity, status, _detail: transitions.append(status),
            restart_policy=RestartPolicy(max_restarts=3, base_delay=0, max_delay=0),
            heartbeat_interval=60,
        )
        plugin = spec("com.example.viewer")
        await supervisor.enable(plugin)
        binding = await supervisor.open_session(plugin.identity, "media-one")
        assert supervisor.validate_scope(
            binding.scope, plugin.identity, "media-one"
        ) is binding

        first = runtime.processes[(plugin.plugin_id, plugin.version)][0]
        first.peer.heartbeat_error = True
        await supervisor.heartbeat_once(plugin.identity)
        assert transitions[-1] == "degraded"

        first.crash(17)
        await supervisor.wait_for_generation(plugin.identity, 2, timeout=1)
        with pytest.raises(StaleSessionScopeError):
            supervisor.validate_scope(binding.scope, plugin.identity, "media-one")
        current = supervisor.binding(plugin.identity, "media-one")
        assert current.scope != binding.scope
        assert transitions[:2] == ["starting", "ready"]
        assert "crashed" in transitions
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_crash_restart_bounds_stuck_peer_cleanup() -> None:
    async def scenario() -> None:
        runtime = FakeContainerRuntime()
        supervisor = PluginSupervisor(
            runtime,
            peer_factory=lambda process: process.peer,
            restart_policy=RestartPolicy(max_restarts=3, base_delay=0, max_delay=0),
            heartbeat_interval=60,
            shutdown_timeout=0.01,
        )
        plugin = spec("com.example.stuck-cleanup")
        await supervisor.enable(plugin)
        first = runtime.processes[(plugin.plugin_id, plugin.version)][0]
        first.peer.close_hangs = True

        first.crash(137)
        await supervisor.wait_for_generation(plugin.identity, 2, timeout=1)

        assert supervisor.status(plugin.identity) == "ready"
        assert len(runtime.processes[(plugin.plugin_id, plugin.version)]) == 2
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_bounded_restart_enters_quarantine_without_stopping_other_plugin() -> None:
    async def scenario() -> None:
        runtime = FakeContainerRuntime()
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        supervisor = PluginSupervisor(
            runtime,
            peer_factory=lambda process: process.peer,
            restart_policy=RestartPolicy(
                max_restarts=2,
                window_seconds=60,
                base_delay=0.25,
                max_delay=0.5,
            ),
            sleep=fake_sleep,
            heartbeat_interval=60,
        )
        unstable = spec("com.example.unstable")
        healthy = spec("com.example.healthy")
        await supervisor.enable(unstable)
        await supervisor.enable(healthy)

        for generation in (1, 2, 3):
            process = runtime.processes[(unstable.plugin_id, unstable.version)][-1]
            process.crash(9)
            if generation < 3:
                await supervisor.wait_for_generation(
                    unstable.identity, generation + 1, timeout=1
                )
            else:
                await supervisor.wait_for_status(
                    unstable.identity, "quarantined", timeout=1
                )

        assert delays == [0.25, 0.5]
        assert supervisor.status(unstable.identity) == "quarantined"
        assert supervisor.status(healthy.identity) == "ready"
        assert len(runtime.processes[(healthy.plugin_id, healthy.version)]) == 1
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_shutdown_drains_sessions_then_terminates_with_timeout() -> None:
    async def scenario() -> None:
        runtime = FakeContainerRuntime()
        supervisor = PluginSupervisor(
            runtime,
            peer_factory=lambda process: process.peer,
            shutdown_timeout=0.5,
            heartbeat_interval=60,
        )
        plugin = spec("com.example.viewer")
        await supervisor.enable(plugin)
        await supervisor.open_session(plugin.identity, "media-one")
        process = runtime.processes[(plugin.plugin_id, plugin.version)][0]
        await supervisor.shutdown()

        methods = [method for method, _ in process.peer.notifications]
        assert methods == ["session.close", "plugin.shutdown"]
        assert runtime.stops == [process.container_id]
        assert process.peer.closed is True

    asyncio.run(scenario())


def test_cold_start_restores_persisted_bindings_and_acknowledged_cursors() -> None:
    async def scenario() -> None:
        runtime = FakeContainerRuntime()
        persisted = []
        supervisor = PluginSupervisor(
            runtime,
            peer_factory=lambda process: process.peer,
            binding_loader=lambda _identity: [
                RestoredSession("media-restored", last_acknowledged_sequence=12)
            ],
            binding_sink=persisted.append,
            heartbeat_interval=60,
        )
        plugin = spec("com.example.viewer")
        await supervisor.enable(plugin)

        binding = supervisor.binding(plugin.identity, "media-restored")
        peer = runtime.processes[(plugin.plugin_id, plugin.version)][0].peer
        session_open = [call for call in peer.calls if call[0] == "session.open"]
        assert binding.last_acknowledged_sequence == 12
        assert session_open[0][1]["after_sequence"] == 12
        assert session_open[0][1]["session_scope"] == binding.scope
        assert persisted == [binding]
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_incompatible_host_api_is_rejected_before_container_start() -> None:
    async def scenario() -> None:
        runtime = FakeContainerRuntime()
        supervisor = PluginSupervisor(
            runtime,
            peer_factory=lambda process: process.peer,
            heartbeat_interval=60,
        )
        plugin = PluginContainerSpec(
            plugin_id="com.example.future",
            version="1.0.0",
            image_ref="com.example.future:1.0.0",
            command=("python", "-m", "plugin"),
            host_api_requirement=">=2.0 <3.0",
        )
        with pytest.raises(RuntimeError, match="Host API"):
            await supervisor.enable(plugin)
        assert runtime.starts == []
        assert supervisor.status(plugin.identity) == "incompatible"
        await supervisor.shutdown()

    asyncio.run(scenario())


def test_command_id_is_sent_before_rpc_and_correlates_the_response() -> None:
    async def scenario() -> None:
        runtime = FakeContainerRuntime()
        supervisor = PluginSupervisor(
            runtime,
            peer_factory=lambda process: process.peer,
            heartbeat_interval=60,
        )
        plugin = spec("com.example.viewer")
        await supervisor.enable(plugin)
        binding = await supervisor.open_session(plugin.identity, "media-one")
        await supervisor.invoke_command(
            plugin.identity,
            "media-one",
            binding.scope,
            command_id="command-one",
            command="generate_final",
            values={},
            expected_view_version=1,
        )
        await supervisor.invoke_command(
            plugin.identity,
            "media-one",
            binding.scope,
            command_id="command-two",
            command="generate_final",
            values={},
            expected_view_version=1,
        )
        peer = runtime.processes[(plugin.plugin_id, plugin.version)][0].peer
        commands = [params for method, params in peer.calls if method == "command.execute"]
        assert [item["command_id"] for item in commands] == [
            "command-one",
            "command-two",
        ]
        await supervisor.shutdown()

    asyncio.run(scenario())
