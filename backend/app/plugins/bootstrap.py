from __future__ import annotations

import asyncio
import datetime as dt
import logging
import shutil
import socket
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol

import httpx
from sqlalchemy import select

from app.media.bootstrap import build_media_event_projector
from app.media.contracts import MediaEvent
from app.media.projector import MediaEventProjector
from app.media.repository import MediaRepository
from app.persistence.database import Database
from app.persistence.models import (
    MediaSessionRecord,
    PluginRuntimeHealthRecord,
)
from app.plugins.broker import (
    BrokerConnection,
    CapabilityBroker,
    CapabilityExecutionContext,
    NetworkResponse,
)
from app.plugins.builtins import BuiltinPluginRegistry
from app.plugins.builtin_packages import (
    BuiltinPackageBuildDisabledError,
    BuiltinPluginPackageService,
)
from app.plugins.capabilities import (
    CapabilityRegistry,
    CapabilitySpec,
    DeliveryPrepareInput,
    DeliveryPrepareOutput,
    DeliveryQueryInput,
    DeliveryQueryOutput,
    MediaQueryInput,
    MediaQueryOutput,
    ModelInvokeInput,
    ModelInvokeOutput,
    NetworkFetchInput,
    NetworkFetchOutput,
    StateGetInput,
    StateGetOutput,
    StatePutInput,
    StatePutOutput,
    UIViewPublishInput,
    UIViewPublishOutput,
)
from app.plugins.container_runtime import (
    ContainerRuntime,
    DockerContainerRuntime,
    PluginContainerSpec,
    PluginIdentity,
)
from app.plugins.contracts import PluginManifest
from app.plugins.delivery import DeliveryCapabilityAdapter
from app.plugins.document_contracts import (
    PluginDocumentPublishInput,
    PluginDocumentPublishOutput,
)
from app.plugins.documents import PluginDocumentCapabilityAdapter
from app.plugins.model_adapter import PluginModelCapabilityAdapter
from app.plugins.package_store import PackageStore, PackageStoreConfig
from app.plugins.permissions import PermissionEvaluator
from app.plugins.repository import PluginRepository
from app.plugins.supervisor import (
    PluginSupervisor,
    RestartPolicy,
    RestoredSession,
    SessionBinding,
)
from app.settings import Settings
from app.text_processing.deepseek_provider import DeepSeekCompletionProvider
from app.text_processing.provider import StructuredTextProvider


logger = logging.getLogger(__name__)


class BuiltinPluginBuildDisabledError(RuntimeError):
    """Raised when a known built-in cannot be packaged in this environment."""


class BuiltinPackageServiceLike(Protocol):
    async def prepare(self, plugin_id: str) -> Path: ...


class PluginHostRuntimeLike(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def health_snapshot(self) -> dict[str, object]: ...
    def inspect_package(self, package_path: Path) -> object: ...
    async def confirm_install(
        self,
        *,
        ticket_id: str,
        accepted_permissions: tuple[str, ...],
        trust_publisher: bool,
        approved_publisher_fingerprint: str | None,
    ) -> dict[str, object]: ...
    def list_plugins(self) -> list[dict[str, object]]: ...
    def detail(self, plugin_id: str) -> dict[str, object]: ...
    async def enable_plugin(self, plugin_id: str) -> dict[str, object]: ...
    async def disable_plugin(self, plugin_id: str) -> dict[str, object]: ...
    def create_grant(
        self,
        plugin_id: str,
        *,
        media_session_id: str | None,
        capability: str,
        effect: str,
        scope: dict[str, object],
        ttl_seconds: int,
    ) -> dict[str, object]: ...
    async def uninstall_plugin(self, plugin_id: str) -> None: ...
    def list_builtin_plugins(self) -> list[dict[str, object]]: ...
    async def inspect_builtin_plugin(self, plugin_id: str) -> object: ...
    async def resolve_media_session(
        self,
        legacy_session_id: str,
    ) -> dict[str, object]: ...
    def list_media_events(
        self,
        media_session_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> list[dict[str, object]]: ...
    def list_plugin_views(self, media_session_id: str) -> list[dict[str, object]]: ...
    async def execute_plugin_command(
        self,
        media_session_id: str,
        *,
        plugin_id: str,
        plugin_version: str,
        session_scope: str,
        surface: str,
        view_id: str,
        expected_view_version: int,
        action_id: str,
        values: dict[str, object],
    ) -> dict[str, object]: ...


class PluginHostRuntime:
    """Composition root for package, projection, sandbox, broker, and UI services."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        container_runtime: ContainerRuntime | None = None,
        media_projector: MediaEventProjector | None = None,
        peer_factory: Callable[[object], object] | None = None,
        structured_provider: StructuredTextProvider | None = None,
        builtin_package_service: BuiltinPackageServiceLike | None = None,
        meeting_operations: MeetingPluginOperations | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.container_runtime = container_runtime or DockerContainerRuntime()
        self.builtin_registry = BuiltinPluginRegistry(settings)
        self.builtin_package_service = (
            builtin_package_service
            if builtin_package_service is not None
            else BuiltinPluginPackageService(
                settings=settings,
                registry=self.builtin_registry,
            )
        )
        self.media_projector = (
            media_projector
            if media_projector is not None
            else build_media_event_projector(settings, database)
        )
        self.structured_provider = structured_provider or DeepSeekCompletionProvider(
            api_key=settings.deepseek_api_key,
            base_url=str(settings.deepseek_base_url),
            model=settings.deepseek_model,
            timeout_seconds=settings.deepseek_request_timeout_seconds,
            temperature=settings.deepseek_temperature,
            max_output_tokens=settings.plugin_model_max_output_tokens,
        )
        self._model_adapter = PluginModelCapabilityAdapter(
            self.structured_provider,
            max_input_chars=settings.plugin_model_max_input_chars,
            max_output_tokens=settings.plugin_model_max_output_tokens,
        )
        self._model_slots = asyncio.Semaphore(settings.plugin_model_max_concurrency)
        self.package_store = PackageStore(
            database,
            image_importer=self.container_runtime,
            config=PackageStoreConfig(
                packages_dir=settings.plugin_packages_dir,
                staging_dir=settings.plugin_staging_dir,
                allow_unsigned=settings.plugin_allow_unsigned,
                max_entries=settings.plugin_package_max_entries,
                max_compressed_bytes=settings.plugin_package_max_compressed_bytes,
                max_uncompressed_bytes=settings.plugin_package_max_uncompressed_bytes,
                ticket_ttl_seconds=300,
            ),
        )
        self.capability_registry = self._build_capability_registry()
        from app.plugins.host_actions import HostActions
        from app.assistant.plugin_operations import MeetingPluginOperations
        self.meeting_operations = meeting_operations or MeetingPluginOperations(HostActions(database, settings))
        self._scope_maps: dict[PluginIdentity, dict[str, str]] = {}
        self.supervisor = PluginSupervisor(
            self.container_runtime,
            peer_factory=peer_factory,
            peer_configurator=self._configure_peer,
            status_sink=self._record_status,
            binding_loader=self._load_bindings,
            binding_sink=self._persist_binding,
            scope_sink=self._set_binding_scope,
            restart_policy=RestartPolicy(
                max_restarts=settings.plugin_crash_loop_max_restarts,
                window_seconds=settings.plugin_crash_loop_window_seconds,
            ),
            rpc_timeout=settings.plugin_rpc_timeout_seconds,
            shutdown_timeout=settings.plugin_shutdown_timeout_seconds,
        )
        self._started = False
        self._container_available = False
        # Plugin callbacks are concurrent by design. SQLite only permits one
        # writer, so serialize Host-owned plugin writes (including supervisor
        # status/binding persistence) instead of letting startup restore and
        # capability callbacks deadlock each other behind busy_timeout.
        self._database_write_lock = asyncio.Lock()
        self._event_dispatch_task: asyncio.Task[None] | None = None
        self._event_delivery_tasks: dict[tuple[PluginIdentity, str], asyncio.Task[None]] = {}
        self._event_delivery_locks: dict[tuple[PluginIdentity, str], asyncio.Lock] = {}

    async def start(self) -> None:
        if self._started or not self.settings.plugin_framework_enabled:
            self._started = self.settings.plugin_framework_enabled
            return
        self._cleanup_expired_authority()
        with self.database.session() as db_session:
            repository = PluginRepository(db_session)
            enabled = []
            for item in repository.list_installations():
                if item.status != "enabled":
                    continue
                packages = {
                    repository.preferred_package(item.plugin_id).version:
                    repository.preferred_package(item.plugin_id)
                }
                for binding in repository.list_open_bindings_for_plugin(item.plugin_id):
                    package = repository.get_package(
                        item.plugin_id,
                        binding.plugin_version,
                    )
                    assert package is not None
                    packages[package.version] = package
                enabled.extend(packages.values())
        if enabled:
            if self.media_projector is not None:
                await self.media_projector.start()
            self._container_available = await self.container_runtime.available()
            if self._container_available:
                for package in enabled:
                    await self.supervisor.enable(self._container_spec(package))
            else:
                with self.database.session() as db_session:
                    repository = PluginRepository(db_session)
                    for package in enabled:
                        repository.record_runtime_status(
                            plugin_id=package.plugin_id,
                            version=package.version,
                            status="degraded",
                            quarantine_reason="container runtime unavailable",
                        )
                    db_session.commit()
        self._started = True
        self._event_dispatch_task = asyncio.create_task(
            self._dispatch_events_loop(), name="plugin-event-dispatcher",
        )

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._event_dispatch_task is not None:
            self._event_dispatch_task.cancel()
            await asyncio.gather(self._event_dispatch_task, return_exceptions=True)
            self._event_dispatch_task = None
        deliveries = list(self._event_delivery_tasks.values())
        for task in deliveries:
            task.cancel()
        await asyncio.gather(*deliveries, return_exceptions=True)
        self._event_delivery_tasks.clear()
        self._event_delivery_locks.clear()
        await self.supervisor.shutdown()
        if self.media_projector is not None:
            await self.media_projector.stop()

    async def _dispatch_events_loop(self) -> None:
        while self._started:
            try:
                statuses = self.supervisor.statuses()
                # Only dispatch scopes that are already open in this runtime.
                # Projecting history must never implicitly open plugin bindings.
                targets = {
                    (identity, media_id)
                    for identity, scopes in self._scope_maps.items()
                    if statuses.get(identity) in {"ready", "degraded"}
                    for media_id in scopes.values()
                }
                for target, task in list(self._event_delivery_tasks.items()):
                    if task.done():
                        del self._event_delivery_tasks[target]
                    elif target not in targets:
                        task.cancel()
                for identity, media_id in targets:
                    target = (identity, media_id)
                    if target not in self._event_delivery_tasks:
                        self._event_delivery_tasks[target] = asyncio.create_task(
                            self._deliver_background_events(identity, media_id),
                            name=f"plugin-events-{identity.plugin_id}-{media_id}",
                        )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Plugin event dispatch scan failed",
                    extra={"event": "plugin_event_dispatch_scan_failed",
                           "error_type": type(error).__name__},
                )
            await asyncio.sleep(self.settings.media_event_projector_poll_interval_ms / 1_000)

    async def _deliver_background_events(self, identity: PluginIdentity, media_id: str) -> None:
        try:
            # One bounded batch per pass prevents a busy history or a slow plugin
            # from monopolizing delivery for other active sessions/plugins.
            await self._deliver_pending_events(identity, media_id, max_batches=1)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Leave the durable ACK unchanged; the next pass retries from it.
            # Never log transcript/model payloads or private exception details.
            logger.warning(
                "Plugin event delivery failed; retrying from durable cursor",
                extra={"event": "plugin_event_delivery_failed",
                       "plugin_id": identity.plugin_id, "plugin_version": identity.version,
                       "session_id": media_id, "error_type": type(error).__name__},
            )

    def inspect_package(self, package_path: Path):
        self._require_enabled()
        return self.package_store.inspect(package_path)

    def list_builtin_plugins(self) -> list[dict[str, object]]:
        return [
            {
                "id": descriptor.plugin_id,
                "name": descriptor.display_name,
                "description": descriptor.description,
                "version": descriptor.version,
                "dynamic_build_available": descriptor.dynamic_build_available,
            }
            for descriptor in self.builtin_registry.list()
        ]

    async def inspect_builtin_plugin(self, plugin_id: str):
        # Resolve the server-owned ID before consulting environment state so an
        # unknown/path-shaped identifier is always a stable 404 at the API edge.
        descriptor = self.builtin_registry.require(plugin_id)
        if not descriptor.dynamic_build_available:
            raise BuiltinPluginBuildDisabledError(
                "dynamic built-in package preparation is disabled"
            )

        # Expired inspection trees are removed before creating another ticket;
        # both cleanup and ZIP verification are blocking filesystem/DB work.
        await asyncio.to_thread(self._cleanup_expired_authority)
        try:
            package_path = await self.builtin_package_service.prepare(plugin_id)
        except BuiltinPackageBuildDisabledError:
            raise BuiltinPluginBuildDisabledError(
                "dynamic built-in package preparation is disabled"
            ) from None
        return await asyncio.to_thread(self.package_store.inspect, package_path)

    async def confirm_install(
        self,
        *,
        ticket_id: str,
        accepted_permissions: tuple[str, ...],
        trust_publisher: bool,
        approved_publisher_fingerprint: str | None,
    ) -> dict[str, object]:
        self._require_enabled()
        for permission in accepted_permissions:
            try:
                self.capability_registry.require(permission)
            except LookupError:
                raise ValueError(
                    f"unsupported capability permission: {permission}"
                ) from None
        package = await self.package_store.confirm_install(
            ticket_id,
            accepted_permissions=accepted_permissions,
            trust_publisher=trust_publisher,
            approved_publisher_fingerprint=approved_publisher_fingerprint,
        )
        await self._activate_installed_package(package)
        return self.detail(package.plugin_id)

    def list_plugins(self) -> list[dict[str, object]]:
        with self.database.session() as db_session:
            repository = PluginRepository(db_session)
            plugin_ids = [item.plugin_id for item in repository.list_installations()]
        return [self.detail(plugin_id) for plugin_id in plugin_ids]

    def detail(self, plugin_id: str) -> dict[str, object]:
        with self.database.session() as db_session:
            repository = PluginRepository(db_session)
            installation = repository.get_installation(plugin_id)
            if installation is None:
                raise LookupError("plugin installation not found")
            package = repository.preferred_package(plugin_id)
            health = repository.get_runtime_health(
                plugin_id=plugin_id,
                version=package.version,
            )
            manifest = package.manifest_json
            permissions = sorted(
                repository.list_base_permissions(
                    plugin_id=plugin_id,
                    version=package.version,
                )
            )
            versions = [item.version for item in repository.list_packages(plugin_id)]
            return {
                "plugin_id": plugin_id,
                "name": str(manifest.get("name") or plugin_id),
                "status": installation.status,
                "preferred_version": package.version,
                "versions": versions,
                "permissions": permissions,
                "runtime_status": health.status if health is not None else "installed",
                "quarantine_reason": health.quarantine_reason if health is not None else None,
            }

    async def enable_plugin(self, plugin_id: str) -> dict[str, object]:
        self._require_enabled()
        with self.database.session() as db_session:
            repository = PluginRepository(db_session)
            package = repository.preferred_package(plugin_id)
            repository.set_installation_status(plugin_id, "enabled")
            db_session.commit()
        self._container_available = await self.container_runtime.available()
        if not self._container_available:
            with self.database.session() as db_session:
                PluginRepository(db_session).record_runtime_status(
                    plugin_id=package.plugin_id,
                    version=package.version,
                    status="degraded",
                    quarantine_reason="container runtime unavailable",
                )
                db_session.commit()
            return self.detail(plugin_id)
        if self.media_projector is not None:
            await self.media_projector.start()
        await self.supervisor.enable(self._container_spec(package))
        return self.detail(plugin_id)

    async def disable_plugin(self, plugin_id: str) -> dict[str, object]:
        with self.database.session() as db_session:
            from app.plugins.host_actions import begin_write
            from app.assistant.plugin_repository import MEETING_PLUGIN_ID, MeetingPluginRepository
            from app.persistence.models import MeetingPluginSessionRecord, PluginCapabilityGrantRecord, utc_now
            begin_write(db_session)
            repository = PluginRepository(db_session)
            package = repository.preferred_package(plugin_id)
            repository.set_installation_status(plugin_id, "disabled")
            if plugin_id == MEETING_PLUGIN_ID:
                for session_id in db_session.scalars(select(MeetingPluginSessionRecord.legacy_session_id).where(
                        MeetingPluginSessionRecord.plugin_id == plugin_id)):
                    MeetingPluginRepository(db_session).revoke_authority(session_id)
                for grant in db_session.scalars(select(PluginCapabilityGrantRecord).where(
                        PluginCapabilityGrantRecord.plugin_id == plugin_id, PluginCapabilityGrantRecord.status == "active")):
                    grant.status, grant.revoked_at = "revoked", utc_now()
            db_session.commit()
        identities = [
            identity
            for identity in self.supervisor.statuses()
            if identity.plugin_id == plugin_id
        ]
        for identity in identities:
            await self.supervisor.disable(identity)
        with self.database.session() as db_session:
            repository = PluginRepository(db_session)
            repository.set_installation_status(plugin_id, "disabled")
            repository.record_runtime_status(
                plugin_id=plugin_id,
                version=package.version,
                status="disabled",
            )
            db_session.commit()
        return self.detail(plugin_id)

    def create_grant(
        self,
        plugin_id: str,
        *,
        media_session_id: str | None,
        capability: str,
        effect: str,
        scope: dict[str, object],
        ttl_seconds: int,
    ) -> dict[str, object]:
        with self.database.session() as db_session:
            repository = PluginRepository(db_session)
            package = repository.preferred_package(plugin_id)
            if capability not in repository.list_base_permissions(
                plugin_id=plugin_id,
                version=package.version,
            ):
                raise ValueError("capability was not accepted at installation")
            binding = self.capability_registry.require(capability)
            if effect != binding.spec.effect:
                raise ValueError("capability effect does not match the host registry")
            grant = repository.create_capability_grant(
                plugin_id=plugin_id,
                version=package.version,
                media_session_id=media_session_id,
                capability=capability,
                effect=effect,
                scope=scope,
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl_seconds),
            )
            db_session.commit()
            return {"grant_id": grant.id, "status": grant.status}

    async def uninstall_plugin(self, plugin_id: str) -> None:
        try:
            await self.disable_plugin(plugin_id)
        except LookupError:
            raise
        with self.database.session() as db_session:
            paths = PluginRepository(db_session).delete_plugin(plugin_id)
            db_session.commit()
        package_root = self.settings.plugin_packages_dir.resolve()
        for raw_path in paths:
            path = Path(raw_path).resolve()
            if path != package_root and package_root in path.parents:
                shutil.rmtree(path, ignore_errors=True)

    async def resolve_media_session(self, legacy_session_id: str) -> dict[str, object]:
        if self.media_projector is not None:
            result = await self.media_projector.request_catch_up(legacy_session_id)
            media_session_id = result.media_session_id
        else:
            with self.database.session() as db_session:
                media = MediaRepository(db_session).ensure_legacy_session_bridge(
                    legacy_session_id
                )
                db_session.commit()
                media_session_id = media.id
        await self._open_ready_bindings(media_session_id)
        with self.database.session() as db_session:
            media = db_session.get(MediaSessionRecord, media_session_id)
            if media is None:
                raise LookupError("MediaSession not found")
            return {
                "media_session_id": media.id,
                "legacy_session_id": media.legacy_session_id,
                "mode": media.mode,
                "source_kind": media.source_kind,
                "status": media.status,
            }

    def list_media_events(
        self,
        media_session_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> list[dict[str, object]]:
        with self.database.session() as db_session:
            records = MediaRepository(db_session).list_events_after(
                media_session_id,
                after_sequence=after_sequence,
                limit=limit,
            )
            return [
                MediaEvent(
                    event_id=item.id,
                    session_id=item.media_session_id,
                    sequence=item.sequence,
                    schema_version=item.schema_version,
                    event_type=item.event_type,
                    media_time_ms=item.media_time_ms,
                    duration_ms=item.duration_ms,
                    logical_id=item.logical_id,
                    revision=item.revision,
                    finality=item.finality,
                    source=item.source,
                    payload=item.payload_json,
                    created_at=(
                        item.created_at.replace(tzinfo=dt.UTC)
                        if item.created_at.tzinfo is None
                        else item.created_at
                    ),
                ).model_dump(mode="json")
                for item in records
            ]

    def list_plugin_views(self, media_session_id: str) -> list[dict[str, object]]:
        with self.database.session() as db_session:
            repository = PluginRepository(db_session)
            output: list[dict[str, object]] = []
            for view in repository.list_views_for_session(media_session_id):
                binding = repository.get_binding_for_identity(
                    plugin_id=view.plugin_id,
                    version=view.plugin_version,
                    media_session_id=media_session_id,
                )
                if binding is None:
                    continue
                package = repository.get_package(
                    view.plugin_id,
                    view.plugin_version,
                )
                if package is None:
                    continue
                commands = package.manifest_json.get("commands", [])
                output.append(
                    {
                        "plugin_id": view.plugin_id,
                        "plugin_version": view.plugin_version,
                        "session_scope": binding.session_scope,
                        "surface": view.surface,
                        "view_id": view.view_id,
                        "view_version": view.view_version,
                        "allowed_commands": [
                            str(item)
                            for item in commands
                            if isinstance(item, str)
                        ],
                        "view": view.view_json,
                    }
                )
            return output

    async def execute_plugin_command(
        self,
        media_session_id: str,
        *,
        plugin_id: str,
        plugin_version: str,
        session_scope: str,
        surface: str,
        view_id: str,
        expected_view_version: int,
        action_id: str,
        values: dict[str, object],
    ) -> dict[str, object]:
        with self.database.session() as db_session:
            repository = PluginRepository(db_session)
            binding = repository.get_binding_for_identity(
                plugin_id=plugin_id,
                version=plugin_version,
                media_session_id=media_session_id,
            )
            if binding is None or binding.session_scope != session_scope:
                raise PermissionError("plugin session scope mismatch")
            view = repository.get_view(
                plugin_id=plugin_id,
                version=plugin_version,
                media_session_id=media_session_id,
                surface=surface,
                view_id=view_id,
            )
            if view is None or view.view_version != expected_view_version:
                raise ValueError("plugin view version is stale")
            actions = view.view_json.get("actions", [])
            selected = next(
                (
                    item
                    for item in actions
                    if isinstance(item, dict) and item.get("id") == action_id
                ),
                None,
            )
            if selected is None or not isinstance(selected.get("command"), str):
                raise ValueError("plugin action does not exist")
            command = selected["command"]
            if plugin_id == "com.matinier.meeting-assistant" and command == "apply_action":
                raise PermissionError("Host-only action command")
        command_id = str(uuid.uuid4())
        await self.supervisor.invoke_command(
            PluginIdentity(plugin_id, plugin_version),
            media_session_id,
            session_scope,
            command_id=command_id,
            command=command,
            values=values,
            expected_view_version=expected_view_version,
        )
        return {"accepted": True, "command_id": command_id}

    def health_snapshot(self) -> dict[str, object]:
        with self.database.session() as db_session:
            repository = PluginRepository(db_session)
            installations = repository.list_installations()
            health_rows = list(db_session.scalars(select(PluginRuntimeHealthRecord)))
        return {
            "framework_enabled": self.settings.plugin_framework_enabled,
            "container_runtime_available": self._container_available,
            "installed_count": len(installations),
            "enabled_count": sum(item.status == "enabled" for item in installations),
            "ready_count": sum(item.status == "ready" for item in health_rows),
            "quarantined_count": sum(item.status == "quarantined" for item in health_rows),
            "media_projector_status": (
                "disabled"
                if self.media_projector is None
                else "running" if self.media_projector.started else "stopped"
            ),
            "media_projector_lag": 0,
            "rpc_pending_count": self.supervisor.rpc_pending_count,
        }

    def _container_spec(self, package) -> PluginContainerSpec:
        manifest = PluginManifest.model_validate(package.manifest_json)
        return PluginContainerSpec(
            plugin_id=package.plugin_id,
            version=package.version,
            image_ref=package.runtime_image_ref or f"{package.plugin_id}:{package.version}",
            resources=manifest.resources,
            host_api_requirement=manifest.host_api,
        )

    async def _open_ready_bindings(self, media_session_id: str) -> None:
        with self.database.session() as db_session:
            repository = PluginRepository(db_session)
            identities: list[PluginIdentity] = []
            for installation in repository.list_installations():
                if installation.status != "enabled":
                    continue
                existing = repository.get_open_binding_for_plugin_session(
                    plugin_id=installation.plugin_id,
                    media_session_id=media_session_id,
                )
                version = (
                    existing.plugin_version
                    if existing is not None
                    else repository.preferred_package(installation.plugin_id).version
                )
                identities.append(PluginIdentity(installation.plugin_id, version))
        statuses = self.supervisor.statuses()
        for identity in identities:
            status = statuses.get(identity)
            if status not in {"ready", "degraded"}:
                continue
            try:
                await self.supervisor.open_session(identity, media_session_id)
                await self._deliver_pending_events(identity, media_session_id)
            except RuntimeError:
                continue

    async def _deliver_pending_events(
        self,
        identity: PluginIdentity,
        media_session_id: str,
        *,
        max_batches: int = 100,
    ) -> None:
        target = (identity, media_session_id)
        lock = self._event_delivery_locks.setdefault(target, asyncio.Lock())
        async with lock:
            # The scope may have closed while a foreground/background caller
            # waited for this cursor. Recheck before reading or advancing it.
            if media_session_id not in self._scope_maps.get(identity, {}).values():
                return
            await self._deliver_event_batches(identity, media_session_id, max_batches=max_batches)

    async def _deliver_event_batches(
        self,
        identity: PluginIdentity,
        media_session_id: str,
        *,
        max_batches: int,
    ) -> None:
        with self.database.session() as db_session:
            repository = PluginRepository(db_session)
            package = repository.get_package(identity.plugin_id, identity.version)
            binding = repository.get_binding_for_identity(
                plugin_id=identity.plugin_id,
                version=identity.version,
                media_session_id=media_session_id,
            )
            assert package is not None and binding is not None
            subscriptions = frozenset(
                PluginManifest.model_validate(package.manifest_json).subscriptions
            )
            cursor = binding.last_acknowledged_sequence
            binding_id = binding.id

        batch_size = self.settings.media_event_projector_batch_size
        for _ in range(max_batches):
            raw_events = self.list_media_events(
                media_session_id,
                after_sequence=cursor,
                limit=batch_size,
            )
            if not raw_events:
                return
            events = [
                event for event in raw_events if event["event_type"] in subscriptions
            ]
            delivered_through = int(
                (events[-1] if events else raw_events[-1])["sequence"]
            )
            await self._advance_binding_delivery(
                binding_id,
                sequence=delivered_through,
            )
            if events:
                acknowledged = await self.supervisor.deliver_events(
                    identity,
                    media_session_id,
                    events,
                )
            else:
                acknowledged = await self.supervisor.acknowledge_through(
                    identity,
                    media_session_id,
                    delivered_through,
                )
            await self._acknowledge_binding(
                binding_id,
                sequence=acknowledged,
            )
            if acknowledged <= cursor:
                return
            cursor = acknowledged
            raw_through = int(raw_events[-1]["sequence"])
            if cursor < raw_through:
                continue
            if len(raw_events) < batch_size:
                return

    async def _activate_installed_package(self, package) -> None:
        with self.database.session() as db_session:
            repository = PluginRepository(db_session)
            installation = repository.get_installation(package.plugin_id)
            assert installation is not None
            previous = repository.preferred_package(package.plugin_id)
            installation_status = installation.status
            if previous.version == package.version:
                return
            source_items = repository.export_state(
                plugin_id=previous.plugin_id,
                version=previous.version,
            )
            previous_manifest = PluginManifest.model_validate(previous.manifest_json)
        candidate_manifest = PluginManifest.model_validate(package.manifest_json)
        identity = PluginIdentity(package.plugin_id, package.version)
        old_prefix = f"plugin:{package.plugin_id}:{previous.version}:"
        new_prefix = f"plugin:{package.plugin_id}:{package.version}:"
        migrated_input = [
            {
                **item,
                "namespace": new_prefix + str(item["namespace"])[len(old_prefix):],
            }
            for item in source_items
            if str(item["namespace"]).startswith(old_prefix)
        ]
        try:
            self._container_available = await self.container_runtime.available()
            if not self._container_available:
                raise RuntimeError("container runtime unavailable")
            await self.supervisor.enable(self._container_spec(package))
            result = await self.supervisor.migrate_state(
                identity,
                from_schema_version=previous_manifest.state_schema_version,
                to_schema_version=candidate_manifest.state_schema_version,
                items=migrated_input,
            )
            migrated = self._validate_migrated_state(
                package.plugin_id,
                package.version,
                result.get("items"),
            )
            with self.database.session() as db_session:
                repository = PluginRepository(db_session)
                repository.replace_state_snapshot(
                    plugin_id=package.plugin_id,
                    version=package.version,
                    items=migrated,
                    quota_bytes=2 * 1024 * 1024,
                )
                repository.set_preferred_version(
                    plugin_id=package.plugin_id,
                    version=package.version,
                )
                db_session.commit()
            if installation_status != "enabled":
                await self.supervisor.disable(identity)
        except Exception as error:
            if self.supervisor.is_supervised(identity):
                await self.supervisor.disable(identity)
            raise ValueError("plugin update migration failed") from error

    @staticmethod
    def _validate_migrated_state(
        plugin_id: str,
        version: str,
        raw_items: object,
    ) -> list[dict[str, object]]:
        if not isinstance(raw_items, list) or len(raw_items) > 1_000:
            raise ValueError("plugin returned an invalid state snapshot")
        prefix = f"plugin:{plugin_id}:{version}:"
        validated: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for item in raw_items:
            if not isinstance(item, dict) or set(item) != {
                "namespace",
                "key",
                "value",
                "version",
            }:
                raise ValueError("plugin returned an invalid state item")
            namespace = item["namespace"]
            key = item["key"]
            value = item["value"]
            item_version = item["version"]
            if (
                not isinstance(namespace, str)
                or not namespace.startswith(prefix)
                or len(namespace) > 192
                or not isinstance(key, str)
                or not key
                or len(key) > 192
                or not isinstance(value, dict)
                or isinstance(item_version, bool)
                or not isinstance(item_version, int)
                or item_version < 1
            ):
                raise ValueError("plugin returned an invalid state item")
            identity = (namespace, key)
            if identity in seen:
                raise ValueError("plugin returned duplicate state items")
            seen.add(identity)
            validated.append(dict(item))
        return validated

    async def _record_status(
        self,
        identity: PluginIdentity,
        status: str,
        detail: dict[str, object],
    ) -> None:
        async with self._database_write_lock:
            with self.database.session() as db_session:
                PluginRepository(db_session).record_runtime_status(
                    plugin_id=identity.plugin_id,
                    version=identity.version,
                    status=status,
                    crashed=status == "crashed",
                    exit_code=(
                        detail.get("exit_code")
                        if isinstance(detail.get("exit_code"), int)
                        else None
                    ),
                    quarantine_reason=(
                        str(detail.get("reason")) if detail.get("reason") else None
                    ),
                )
                db_session.commit()

    async def _advance_binding_delivery(self, binding_id: str, *, sequence: int) -> None:
        async with self._database_write_lock:
            with self.database.session() as db_session:
                PluginRepository(db_session).advance_binding_delivery(
                    binding_id,
                    sequence=sequence,
                )
                db_session.commit()

    async def _acknowledge_binding(self, binding_id: str, *, sequence: int) -> None:
        async with self._database_write_lock:
            with self.database.session() as db_session:
                PluginRepository(db_session).acknowledge_binding(
                    binding_id,
                    sequence=sequence,
                )
                db_session.commit()

    def _load_bindings(self, identity: PluginIdentity) -> list[RestoredSession]:
        with self.database.session() as db_session:
            records = PluginRepository(db_session).list_open_bindings(
                plugin_id=identity.plugin_id,
                version=identity.version,
            )
            return [
                RestoredSession(
                    item.media_session_id,
                    last_acknowledged_sequence=item.last_acknowledged_sequence,
                )
                for item in records
            ]

    async def _persist_binding(self, binding: SessionBinding) -> None:
        async with self._database_write_lock:
            with self.database.session() as db_session:
                PluginRepository(db_session).bind_session(
                    plugin_id=binding.identity.plugin_id,
                    version=binding.identity.version,
                    media_session_id=binding.media_session_id,
                    session_scope=binding.scope,
                )
                db_session.commit()

    def _set_binding_scope(self, binding: SessionBinding, active: bool) -> None:
        scopes = self._scope_maps.setdefault(binding.identity, {})
        if active:
            scopes[binding.scope] = binding.media_session_id
        else:
            scopes.pop(binding.scope, None)

    def _configure_peer(self, identity: PluginIdentity, peer, generation: int) -> None:
        scopes: dict[str, str] = {}
        self._scope_maps[identity] = scopes

        async def invoke(params: dict[str, object]) -> object:
            allowed = {"capability", "input", "session_scope", "idempotency_key"}
            if set(params) - allowed:
                raise ValueError("capability request contains unknown fields")
            capability = params.get("capability")
            raw_input = params.get("input", {})
            if not isinstance(capability, str) or not isinstance(raw_input, dict):
                raise ValueError("invalid capability request")
            session_scope = params.get("session_scope")
            idempotency_key = params.get("idempotency_key")
            if session_scope is not None and not isinstance(session_scope, str):
                raise ValueError("invalid session scope")
            if idempotency_key is not None and not isinstance(idempotency_key, str):
                raise ValueError("invalid idempotency key")
            await self._database_write_lock.acquire()
            lock_held = True
            try:
                with self.database.session() as db_session:

                    @asynccontextmanager
                    async def model_call_scope():
                        nonlocal lock_held
                        # Keep the pending audit durable, but never hold SQLite's
                        # writer or the plugin gate during a remote model wait.
                        db_session.commit()
                        self._database_write_lock.release()
                        lock_held = False
                        try:
                            yield
                        finally:
                            await self._database_write_lock.acquire()
                            lock_held = True

                    broker = CapabilityBroker(
                        connection=BrokerConnection(
                            plugin_id=identity.plugin_id,
                            plugin_version=identity.version,
                            generation=generation,
                            session_scopes=scopes,
                        ),
                        repository=PluginRepository(db_session),
                        registry=self.capability_registry,
                        permission_evaluator=PermissionEvaluator(),
                        network_resolver=self._resolve_network,
                        meeting_adapter=self._meeting_adapter(db_session, identity, scopes),
                        delivery_adapter=DeliveryCapabilityAdapter(
                            db_session,
                            max_page_items=self.settings.plugin_delivery_max_page_items,
                        ),
                        document_adapter=PluginDocumentCapabilityAdapter(
                            db_session,
                            max_document_bytes=self.settings.plugin_document_max_bytes,
                        ),
                        model_semaphore=self._model_slots,
                        model_call_scope=model_call_scope,
                    )
                    try:
                        result = await broker.invoke(
                            capability,
                            raw_input,
                            session_scope=session_scope,
                            idempotency_key=idempotency_key,
                        )
                    except Exception:
                        # Model requests have already committed their pending
                        # record. Other capabilities must retain atomic rollback.
                        if capability == "model.invoke" and db_session.is_active:
                            db_session.commit()
                        raise
                    db_session.commit()
                    return result
            finally:
                if lock_held:
                    self._database_write_lock.release()

        peer.register_handler("capability.invoke", invoke, write_effect=True)

    def _meeting_adapter(self, db, identity, scopes):
        from app.plugins.meeting_adapter import MeetingCapabilityAdapter
        return MeetingCapabilityAdapter(db, self.meeting_operations,
            is_current=lambda: self._scope_maps.get(identity) is scopes)

    def _build_capability_registry(self) -> CapabilityRegistry:
        registry = CapabilityRegistry()
        from app.plugins.meeting_adapter import register_meeting_capabilities
        register_meeting_capabilities(registry, timeout_seconds=self.settings.plugin_rpc_timeout_seconds)

        def add(
            name: str,
            effect: str,
            input_model,
            output_model,
            handler,
            *,
            supports_idempotency: bool = False,
        ) -> None:
            registry.register_host(
                CapabilitySpec(
                    name=name,
                    version="1.0",
                    effect=effect,
                    input_model=input_model,
                    output_model=output_model,
                    timeout_seconds=self.settings.plugin_rpc_timeout_seconds,
                    requires_action_grant=False,
                    supports_idempotency=supports_idempotency,
                    supports_reconciliation=False,
                ),
                handler,
            )

        add("media.query", "read", MediaQueryInput, MediaQueryOutput, self._media_query)
        add(
            "model.invoke",
            "read",
            ModelInvokeInput,
            ModelInvokeOutput,
            self._model_adapter,
        )
        add(
            "delivery.prepare",
            "local_write",
            DeliveryPrepareInput,
            DeliveryPrepareOutput,
            None,
            supports_idempotency=True,
        )
        add(
            "delivery.query",
            "read",
            DeliveryQueryInput,
            DeliveryQueryOutput,
            None,
        )
        add(
            "document.publish",
            "local_write",
            PluginDocumentPublishInput,
            PluginDocumentPublishOutput,
            None,
            supports_idempotency=True,
        )
        add("state.get", "read", StateGetInput, StateGetOutput, None)
        add("state.put", "local_write", StatePutInput, StatePutOutput, None)
        add("ui.publish", "local_write", UIViewPublishInput, UIViewPublishOutput, None)
        add(
            "network.fetch",
            "network",
            NetworkFetchInput,
            NetworkFetchOutput,
            self._network_fetch,
        )
        return registry

    async def _media_query(
        self,
        context: CapabilityExecutionContext,
        value: MediaQueryInput,
    ) -> MediaQueryOutput:
        if context.media_session_id is None:
            raise ValueError("media.query requires a MediaSession")
        events = self.list_media_events(
            context.media_session_id,
            after_sequence=value.after_sequence,
            limit=value.limit,
        )
        allowed = set(value.event_types)
        return MediaQueryOutput(
            events=[item for item in events if item["event_type"] in allowed]
        )

    async def _network_fetch(
        self,
        _context: CapabilityExecutionContext,
        value: NetworkFetchInput,
    ) -> NetworkResponse:
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=self.settings.plugin_rpc_timeout_seconds,
        ) as client:
            async with client.stream(
                value.method,
                str(value.url),
                content=value.body.encode("utf-8") if value.body is not None else None,
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > value.max_response_bytes:
                        break
                return NetworkResponse(
                    status=response.status_code,
                    mime_type=response.headers.get("content-type", "application/octet-stream"),
                    body=bytes(body),
                    headers={
                        key: response.headers[key]
                        for key in ("location",)
                        if key in response.headers
                    },
                )

    @staticmethod
    async def _resolve_network(hostname: str) -> list[str]:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(
            hostname,
            443,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        return sorted({item[4][0] for item in records})

    def _cleanup_expired_authority(self) -> None:
        with self.database.session() as db_session:
            paths = PluginRepository(db_session).cleanup_expired_authority()
            db_session.commit()
        staging_root = self.settings.plugin_staging_dir.resolve()
        for raw_path in paths:
            path = Path(raw_path).resolve()
            if path != staging_root and staging_root in path.parents:
                shutil.rmtree(path, ignore_errors=True)

    def _require_enabled(self) -> None:
        if not self.settings.plugin_framework_enabled:
            raise RuntimeError("plugin framework is disabled")


def build_plugin_host_runtime(
    settings: Settings,
    database: Database,
    *, meeting_operations: MeetingPluginOperations | None = None,
) -> PluginHostRuntime:
    return PluginHostRuntime(settings, database, meeting_operations=meeting_operations)


__all__ = [
    "BuiltinPluginBuildDisabledError",
    "PluginHostRuntime",
    "PluginHostRuntimeLike",
    "build_plugin_host_runtime",
]
