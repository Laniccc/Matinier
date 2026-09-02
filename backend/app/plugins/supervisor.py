from __future__ import annotations

import asyncio
import inspect
import secrets
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Callable, Protocol

from app.contract_versions import HOST_API_VERSION
from app.plugins.container_runtime import (
    ContainerRuntime,
    PluginContainerProcess,
    PluginContainerSpec,
    PluginIdentity,
)
from app.plugins.manifest import host_api_compatible
from app.plugins.rpc import JsonRpcPeer


class StaleSessionScopeError(PermissionError):
    pass


class PluginPeer(Protocol):
    async def start(self) -> None: ...

    async def request(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout: float,
    ) -> object: ...

    async def notify(self, method: str, params: dict[str, object]) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    max_restarts: int = 5
    window_seconds: float = 60.0
    base_delay: float = 0.5
    max_delay: float = 10.0

    def __post_init__(self) -> None:
        if self.max_restarts < 0 or self.window_seconds <= 0:
            raise ValueError("invalid restart policy")
        if self.base_delay < 0 or self.max_delay < self.base_delay:
            raise ValueError("invalid restart backoff")


@dataclass(frozen=True, slots=True)
class SessionBinding:
    identity: PluginIdentity
    media_session_id: str
    scope: str
    generation: int
    last_acknowledged_sequence: int = 0


@dataclass(frozen=True, slots=True)
class RestoredSession:
    media_session_id: str
    last_acknowledged_sequence: int = 0


@dataclass(slots=True)
class _ManagedPlugin:
    spec: PluginContainerSpec
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    process: object | None = None
    peer: PluginPeer | None = None
    status: str = "disabled"
    generation: int = 0
    desired_enabled: bool = True
    sessions: dict[str, SessionBinding] = field(default_factory=dict)
    crash_times: deque[float] = field(default_factory=deque)
    monitor_task: asyncio.Task[None] | None = None
    heartbeat_task: asyncio.Task[None] | None = None


StatusSink = Callable[[PluginIdentity, str, dict[str, object]], object]
PeerFactory = Callable[[object], PluginPeer]
PeerConfigurator = Callable[[PluginIdentity, PluginPeer, int], object]
Sleep = Callable[[float], object]
BindingLoader = Callable[[PluginIdentity], Iterable[RestoredSession] | object]
BindingSink = Callable[[SessionBinding], object]
ScopeSink = Callable[[SessionBinding, bool], object]


class PluginSupervisor:
    """Owns plugin processes and invalidates all authority on process restart."""

    def __init__(
        self,
        runtime: ContainerRuntime,
        *,
        peer_factory: PeerFactory | None = None,
        peer_configurator: PeerConfigurator | None = None,
        status_sink: StatusSink | None = None,
        binding_loader: BindingLoader | None = None,
        binding_sink: BindingSink | None = None,
        scope_sink: ScopeSink | None = None,
        restart_policy: RestartPolicy = RestartPolicy(),
        heartbeat_interval: float = 10.0,
        rpc_timeout: float = 5.0,
        shutdown_timeout: float = 10.0,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if heartbeat_interval <= 0 or rpc_timeout <= 0 or shutdown_timeout <= 0:
            raise ValueError("supervisor timeouts must be positive")
        self._runtime = runtime
        self._peer_factory = peer_factory or self._default_peer_factory
        self._peer_configurator = peer_configurator
        self._status_sink = status_sink
        self._binding_loader = binding_loader
        self._binding_sink = binding_sink
        self._scope_sink = scope_sink
        self._restart_policy = restart_policy
        self._heartbeat_interval = heartbeat_interval
        self._rpc_timeout = rpc_timeout
        self._shutdown_timeout = shutdown_timeout
        self._sleep = sleep
        self._clock = clock
        self._plugins: dict[PluginIdentity, _ManagedPlugin] = {}
        self._scope_index: dict[str, SessionBinding] = {}
        self._registry_lock = asyncio.Lock()
        self._shutting_down = False

    async def enable(self, spec: PluginContainerSpec) -> None:
        identity = spec.identity
        async with self._registry_lock:
            managed = self._plugins.get(identity)
            if managed is None:
                managed = _ManagedPlugin(spec=spec)
                self._plugins[identity] = managed
            elif managed.spec != spec:
                raise ValueError("enabled plugin version has conflicting launch settings")
        async with managed.lock:
            managed.desired_enabled = True
            if managed.process is None and managed.status != "quarantined":
                if managed.generation == 0 and not managed.sessions:
                    await self._load_persisted_sessions(managed)
                await self._launch(managed)

    async def disable(self, identity: PluginIdentity) -> None:
        managed = self._require(identity)
        async with managed.lock:
            managed.desired_enabled = False
            await self._drain_and_stop(managed)
            await self._transition(managed, "disabled")

    async def open_session(
        self,
        identity: PluginIdentity,
        media_session_id: str,
        *,
        last_acknowledged_sequence: int = 0,
    ) -> SessionBinding:
        managed = self._require(identity)
        async with managed.lock:
            if managed.status not in {"ready", "degraded"} or managed.peer is None:
                raise RuntimeError("plugin is not ready")
            existing = managed.sessions.get(media_session_id)
            if existing is not None and existing.generation == managed.generation:
                return existing
            binding = self._new_binding(
                managed,
                media_session_id,
                last_acknowledged_sequence=last_acknowledged_sequence,
            )
            await self._set_scope(binding, active=True)
            try:
                await managed.peer.request(
                    "session.open",
                    {
                        "media_session_id": media_session_id,
                        "session_scope": binding.scope,
                        "after_sequence": last_acknowledged_sequence,
                    },
                    timeout=self._rpc_timeout,
                )
            except BaseException:
                await self._set_scope(binding, active=False)
                raise
            managed.sessions[media_session_id] = binding
            await self._persist_binding(binding)
            return binding

    def binding(
        self,
        identity: PluginIdentity,
        media_session_id: str,
    ) -> SessionBinding:
        binding = self._require(identity).sessions.get(media_session_id)
        if binding is None:
            raise LookupError("plugin session binding does not exist")
        return binding

    def validate_scope(
        self,
        scope: str,
        identity: PluginIdentity,
        media_session_id: str,
    ) -> SessionBinding:
        binding = self._scope_index.get(scope)
        managed = self._plugins.get(identity)
        if (
            binding is None
            or managed is None
            or binding.identity != identity
            or binding.media_session_id != media_session_id
            or binding.generation != managed.generation
            or managed.status not in {"ready", "degraded"}
        ):
            raise StaleSessionScopeError("session scope is stale or belongs to another plugin")
        return binding

    async def heartbeat_once(self, identity: PluginIdentity) -> None:
        managed = self._require(identity)
        async with managed.lock:
            peer = managed.peer
            generation = managed.generation
        if peer is None:
            return
        try:
            await peer.request("plugin.heartbeat", {}, timeout=self._rpc_timeout)
        except Exception:
            async with managed.lock:
                if managed.generation == generation and managed.peer is peer:
                    await self._transition(managed, "degraded")
            return
        async with managed.lock:
            if managed.generation == generation and managed.peer is peer:
                await self._transition(managed, "ready")

    def status(self, identity: PluginIdentity) -> str:
        return self._require(identity).status

    def is_supervised(self, identity: PluginIdentity) -> bool:
        return identity in self._plugins

    def statuses(self) -> dict[PluginIdentity, str]:
        return {identity: managed.status for identity, managed in self._plugins.items()}

    def container_id(self, identity: PluginIdentity) -> str | None:
        process = self._require(identity).process
        value = getattr(process, "container_id", None)
        return value if isinstance(value, str) else None

    async def deliver_events(
        self,
        identity: PluginIdentity,
        media_session_id: str,
        events: list[dict[str, object]],
    ) -> int:
        managed = self._require(identity)
        async with managed.lock:
            if managed.peer is None or managed.status not in {"ready", "degraded"}:
                raise RuntimeError("plugin is not ready")
            binding = managed.sessions.get(media_session_id)
            if binding is None:
                raise LookupError("plugin session binding does not exist")
            if not events:
                return binding.last_acknowledged_sequence
            sequences = [item.get("sequence") for item in events]
            if any(isinstance(item, bool) or not isinstance(item, int) for item in sequences):
                raise ValueError("event batch contains an invalid sequence")
            maximum = max(sequences)
            result = await managed.peer.request(
                "event.batch",
                {
                    "media_session_id": media_session_id,
                    "session_scope": binding.scope,
                    "events": events,
                },
                timeout=self._rpc_timeout,
            )
            acknowledged = (
                result.get("acknowledged_sequence")
                if isinstance(result, dict)
                else None
            )
            if (
                isinstance(acknowledged, bool)
                or not isinstance(acknowledged, int)
                or acknowledged < binding.last_acknowledged_sequence
                or acknowledged > maximum
            ):
                raise ValueError("plugin returned an invalid event acknowledgement")
            updated = replace(
                binding,
                last_acknowledged_sequence=acknowledged,
            )
            managed.sessions[media_session_id] = updated
            self._scope_index[updated.scope] = updated
            return acknowledged

    async def migrate_state(
        self,
        identity: PluginIdentity,
        *,
        from_schema_version: int,
        to_schema_version: int,
        items: list[dict[str, object]],
    ) -> dict[str, object]:
        managed = self._require(identity)
        async with managed.lock:
            if managed.peer is None or managed.status not in {"ready", "degraded"}:
                raise RuntimeError("plugin is not ready")
            result = await managed.peer.request(
                "plugin.migrate_state",
                {
                    "from_schema_version": from_schema_version,
                    "to_schema_version": to_schema_version,
                    "items": items,
                },
                timeout=self._rpc_timeout,
            )
            if not isinstance(result, dict):
                raise ValueError("plugin returned an invalid state migration result")
            return result

    async def acknowledge_through(
        self,
        identity: PluginIdentity,
        media_session_id: str,
        sequence: int,
    ) -> int:
        managed = self._require(identity)
        async with managed.lock:
            binding = managed.sessions.get(media_session_id)
            if binding is None:
                raise LookupError("plugin session binding does not exist")
            if sequence < binding.last_acknowledged_sequence:
                raise ValueError("event acknowledgement cannot move backwards")
            updated = replace(
                binding,
                last_acknowledged_sequence=sequence,
            )
            managed.sessions[media_session_id] = updated
            self._scope_index[updated.scope] = updated
            return sequence

    @property
    def rpc_pending_count(self) -> int:
        total = 0
        for managed in self._plugins.values():
            pending = getattr(managed.peer, "pending_request_count", 0)
            if isinstance(pending, int):
                total += pending
        return total

    async def invoke_command(
        self,
        identity: PluginIdentity,
        media_session_id: str,
        session_scope: str,
        *,
        command_id: str,
        command: str,
        values: dict[str, object],
        expected_view_version: int,
    ) -> object:
        self.validate_scope(session_scope, identity, media_session_id)
        managed = self._require(identity)
        async with managed.lock:
            if managed.peer is None or managed.status not in {"ready", "degraded"}:
                raise RuntimeError("plugin is not ready")
            return await managed.peer.request(
                "command.execute",
                {
                    "command_id": command_id,
                    "media_session_id": media_session_id,
                    "session_scope": session_scope,
                    "command": command,
                    "values": values,
                    "expected_view_version": expected_view_version,
                },
                timeout=self._rpc_timeout,
            )

    async def wait_for_generation(
        self,
        identity: PluginIdentity,
        generation: int,
        *,
        timeout: float,
    ) -> None:
        async def wait() -> None:
            while self._require(identity).generation < generation:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait(), timeout=timeout)

    async def wait_for_status(
        self,
        identity: PluginIdentity,
        status: str,
        *,
        timeout: float,
    ) -> None:
        async def wait() -> None:
            while self._require(identity).status != status:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait(), timeout=timeout)

    async def shutdown(self) -> None:
        self._shutting_down = True
        managed_plugins = list(self._plugins.values())
        await asyncio.gather(
            *(self._shutdown_managed(managed) for managed in managed_plugins),
            return_exceptions=True,
        )

    async def _shutdown_managed(self, managed: _ManagedPlugin) -> None:
        async with managed.lock:
            managed.desired_enabled = False
            await self._drain_and_stop(managed)
            await self._transition(managed, "disabled")

    async def _launch(self, managed: _ManagedPlugin) -> None:
        if not host_api_compatible(managed.spec.host_api_requirement):
            await self._transition(managed, "incompatible")
            raise RuntimeError("plugin requires an incompatible Host API")
        await self._transition(managed, "starting")
        process = await self._runtime.start(managed.spec)
        peer = self._peer_factory(process)
        next_generation = managed.generation + 1
        if self._peer_configurator is not None:
            configured = self._peer_configurator(managed.spec.identity, peer, next_generation)
            if inspect.isawaitable(configured):
                await configured
        await peer.start()
        result = await peer.request(
            "plugin.initialize",
            {
                "plugin_id": managed.spec.plugin_id,
                "version": managed.spec.version,
                "protocol_version": "1.0",
                "host_api": HOST_API_VERSION,
                "host_api_requirement": managed.spec.host_api_requirement,
            },
            timeout=self._rpc_timeout,
        )
        if not isinstance(result, dict) or (
            result.get("plugin_id") != managed.spec.plugin_id
            or result.get("version") != managed.spec.version
            or result.get("protocol_version") != "1.0"
            or result.get("host_api") != HOST_API_VERSION
        ):
            await peer.aclose()
            await self._runtime.stop(process.container_id, timeout=self._shutdown_timeout)
            await self._transition(managed, "incompatible")
            raise RuntimeError("plugin identity or protocol negotiation failed")
        managed.process = process
        managed.peer = peer
        managed.generation = next_generation
        await self._restore_logical_sessions(managed)
        await self._transition(managed, "ready")
        managed.monitor_task = asyncio.create_task(
            self._monitor_exit(managed, process, next_generation),
            name=f"plugin-monitor-{managed.spec.plugin_id}-{managed.spec.version}",
        )
        managed.heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(managed.spec.identity, next_generation),
            name=f"plugin-heartbeat-{managed.spec.plugin_id}-{managed.spec.version}",
        )

    async def _restore_logical_sessions(self, managed: _ManagedPlugin) -> None:
        if managed.peer is None:
            return
        previous = list(managed.sessions.values())
        managed.sessions.clear()
        for binding in previous:
            restored = self._new_binding(
                managed,
                binding.media_session_id,
                last_acknowledged_sequence=binding.last_acknowledged_sequence,
            )
            await self._set_scope(restored, active=True)
            try:
                await managed.peer.request(
                    "session.open",
                    {
                        "media_session_id": restored.media_session_id,
                        "session_scope": restored.scope,
                        "after_sequence": restored.last_acknowledged_sequence,
                    },
                    timeout=self._rpc_timeout,
                )
            except BaseException:
                await self._set_scope(restored, active=False)
                raise
            managed.sessions[restored.media_session_id] = restored
            await self._persist_binding(restored)

    async def _load_persisted_sessions(self, managed: _ManagedPlugin) -> None:
        if self._binding_loader is None:
            return
        loaded = self._binding_loader(managed.spec.identity)
        if inspect.isawaitable(loaded):
            loaded = await loaded
        if not isinstance(loaded, Iterable):
            raise TypeError("binding loader must return RestoredSession values")
        for item in loaded:
            if not isinstance(item, RestoredSession):
                raise TypeError("binding loader returned an invalid session")
            if item.media_session_id in managed.sessions:
                continue
            managed.sessions[item.media_session_id] = SessionBinding(
                identity=managed.spec.identity,
                media_session_id=item.media_session_id,
                scope="pending-restore",
                generation=0,
                last_acknowledged_sequence=item.last_acknowledged_sequence,
            )

    async def _persist_binding(self, binding: SessionBinding) -> None:
        if self._binding_sink is None:
            return
        persisted = self._binding_sink(binding)
        if inspect.isawaitable(persisted):
            await persisted

    async def _set_scope(self, binding: SessionBinding, *, active: bool) -> None:
        if active:
            self._scope_index[binding.scope] = binding
        else:
            self._scope_index.pop(binding.scope, None)
        if self._scope_sink is None:
            return
        result = self._scope_sink(binding, active)
        if inspect.isawaitable(result):
            await result

    def _new_binding(
        self,
        managed: _ManagedPlugin,
        media_session_id: str,
        *,
        last_acknowledged_sequence: int,
    ) -> SessionBinding:
        return SessionBinding(
            identity=managed.spec.identity,
            media_session_id=media_session_id,
            scope=secrets.token_urlsafe(32),
            generation=managed.generation,
            last_acknowledged_sequence=last_acknowledged_sequence,
        )

    async def _monitor_exit(
        self,
        managed: _ManagedPlugin,
        process: object,
        generation: int,
    ) -> None:
        exit_code = await process.wait()
        if self._shutting_down or not managed.desired_enabled:
            return
        async with managed.lock:
            if managed.process is not process or managed.generation != generation:
                return
            await self._handle_crash(managed, int(exit_code))

    async def _handle_crash(self, managed: _ManagedPlugin, exit_code: int) -> None:
        heartbeat = managed.heartbeat_task
        if heartbeat is not None and heartbeat is not asyncio.current_task():
            heartbeat.cancel()
        await self._transition(managed, "crashed", exit_code=exit_code)
        await self._invalidate_scopes(managed)
        peer = managed.peer
        managed.peer = None
        managed.process = None
        if peer is not None:
            try:
                await asyncio.wait_for(
                    peer.aclose(),
                    timeout=self._shutdown_timeout,
                )
            except Exception:
                # The process has already exited, so transport cleanup is
                # best-effort and must not prevent a bounded restart.
                pass
        now = self._clock()
        cutoff = now - self._restart_policy.window_seconds
        while managed.crash_times and managed.crash_times[0] < cutoff:
            managed.crash_times.popleft()
        managed.crash_times.append(now)
        if len(managed.crash_times) > self._restart_policy.max_restarts:
            await self._transition(
                managed,
                "quarantined",
                reason="crash-loop threshold exceeded",
            )
            return
        exponent = len(managed.crash_times) - 1
        delay = min(
            self._restart_policy.max_delay,
            self._restart_policy.base_delay * (2**exponent),
        )
        slept = self._sleep(delay)
        if inspect.isawaitable(slept):
            await slept
        if managed.desired_enabled and not self._shutting_down:
            await self._launch(managed)

    async def _heartbeat_loop(self, identity: PluginIdentity, generation: int) -> None:
        try:
            while not self._shutting_down:
                await asyncio.sleep(self._heartbeat_interval)
                managed = self._plugins.get(identity)
                if managed is None or managed.generation != generation:
                    return
                await self.heartbeat_once(identity)
        except asyncio.CancelledError:
            return

    async def _drain_and_stop(self, managed: _ManagedPlugin) -> None:
        monitor = managed.monitor_task
        heartbeat = managed.heartbeat_task
        if heartbeat is not None:
            heartbeat.cancel()
        peer = managed.peer
        process = managed.process
        if peer is not None:
            async def drain() -> None:
                for binding in list(managed.sessions.values()):
                    await peer.notify(
                        "session.close",
                        {
                            "media_session_id": binding.media_session_id,
                            "session_scope": binding.scope,
                        },
                    )
                await peer.notify("plugin.shutdown", {})

            try:
                await asyncio.wait_for(drain(), timeout=self._shutdown_timeout)
            except Exception:
                pass
            try:
                await asyncio.wait_for(peer.aclose(), timeout=self._shutdown_timeout)
            except Exception:
                pass
        if process is not None:
            try:
                await asyncio.wait_for(
                    self._runtime.stop(
                        process.container_id,
                        timeout=self._shutdown_timeout,
                    ),
                    timeout=self._shutdown_timeout + 2,
                )
            except Exception:
                pass
        if monitor is not None and monitor is not asyncio.current_task():
            try:
                await asyncio.wait_for(monitor, timeout=self._shutdown_timeout)
            except Exception:
                monitor.cancel()
        await self._invalidate_scopes(managed)
        managed.process = None
        managed.peer = None
        managed.monitor_task = None
        managed.heartbeat_task = None

    async def _invalidate_scopes(self, managed: _ManagedPlugin) -> None:
        for binding in managed.sessions.values():
            await self._set_scope(binding, active=False)

    async def _transition(
        self,
        managed: _ManagedPlugin,
        status: str,
        **detail: object,
    ) -> None:
        managed.status = status
        if self._status_sink is not None:
            result = self._status_sink(managed.spec.identity, status, detail)
            if inspect.isawaitable(result):
                await result

    def _require(self, identity: PluginIdentity) -> _ManagedPlugin:
        managed = self._plugins.get(identity)
        if managed is None:
            raise LookupError(f"plugin is not supervised: {identity.plugin_id}@{identity.version}")
        return managed

    @staticmethod
    def _default_peer_factory(process: object) -> PluginPeer:
        if not isinstance(process, PluginContainerProcess):
            raise TypeError("default JSON-RPC peer requires PluginContainerProcess")
        return JsonRpcPeer(process.stdout, process.stdin)


__all__ = [
    "PluginIdentity",
    "PluginSupervisor",
    "RestoredSession",
    "RestartPolicy",
    "SessionBinding",
    "StaleSessionScopeError",
]
