from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.assistant import router as assistant_router
from app.api.assistant_actions import router as assistant_actions_router
from app.assistant.plugin_policy import MeetingPluginPolicy
from app.assistant.plugin_operations import MeetingPluginOperations
from app.plugins.host_actions import HostActions
from app.plugins.host_action_descriptors import meeting_target
from app.api.artifacts import router as artifacts_router
from app.api.health import router as health_router
from app.api.internal import router as internal_router
from app.api.exports import router as exports_router
from app.api.livekit_token import router as livekit_token_router
from app.api.media import router as media_router
from app.api.meeting_state import router as meeting_state_router
from app.api.packages import router as packages_router
from app.api.processing_jobs import router as processing_jobs_router
from app.api.plugins import router as plugins_router
from app.api.plugin_documents import router as plugin_documents_router
from app.api.rooms import router as rooms_router
from app.api.revisions import router as revisions_router
from app.api.scripts import router as scripts_router
from app.api.sessions import router as sessions_router
from app.logging import configure_logging
from app.assistant.bootstrap import (
    AssistantRuntime,
    build_assistant_runtime,
)
from app.assistant.tools.processing_job import (
    PROCESSING_JOB_TOOL_NAME,
    register_processing_job_tool,
)
from app.diagnostics.runtime import RuntimeSnapshotRegistry
from app.hls.manager import (
    HLSInputManager,
    HLSInputManagerFactory,
    build_hls_input_manager,
)
from app.persistence.database import Database
from app.plugins.bootstrap import (
    PluginHostRuntimeLike,
    build_plugin_host_runtime,
)
from app.processing.bootstrap import build_processing_job_runner
from app.processing.runner import ProcessingJobRunner
from app.settings import (
    Settings,
    configure_livekit_jwt_warnings,
    get_settings,
)
from app.rooms.livekit_admin import (
    RoomAdminFactory,
    build_room_admin,
)
from app.worker.health import WorkerHealthStore


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    room_admin_factory: RoomAdminFactory | None = None,
    hls_input_manager: HLSInputManager | None = None,
    hls_input_manager_factory: HLSInputManagerFactory | None = None,
    runtime_registry: RuntimeSnapshotRegistry | None = None,
    worker_health_store: WorkerHealthStore | None = None,
    processing_job_runner: ProcessingJobRunner | None = None,
    assistant_runtime: AssistantRuntime | None = None,
    assistant_runtime_factory: Callable[
        [Settings, Database], AssistantRuntime | None
    ]
    | None = None,
    plugin_host_runtime: PluginHostRuntimeLike | None = None,
    plugin_host_runtime_factory: Callable[
        [Settings, Database], PluginHostRuntimeLike
    ]
    | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    app_database = database or Database(app_settings.database_url)
    configure_livekit_jwt_warnings(app_settings)
    configure_logging("api", app_settings.log_level)
    logger = logging.getLogger(__name__)
    host_actions = HostActions(app_database, app_settings)
    host_actions.accepting = False
    host_actions.runtime_check = meeting_target
    meeting_policy = MeetingPluginPolicy(app_database, enabled=False, require_runtime_ready=True)
    meeting_operations = MeetingPluginOperations(host_actions)
    app_hls_input_manager = hls_input_manager or (
        hls_input_manager_factory or build_hls_input_manager
    )(app_settings, app_database)
    app_processing_job_runner = (
        processing_job_runner
        or build_processing_job_runner(app_settings, app_database)
    )
    app_assistant_runtime = assistant_runtime
    if app_assistant_runtime is None:
        app_assistant_runtime = (assistant_runtime_factory(app_settings, app_database)
            if assistant_runtime_factory else build_assistant_runtime(app_settings, app_database,
                meeting_operations=meeting_operations, meeting_policy=meeting_policy))
    if (
        app_assistant_runtime is not None
        and PROCESSING_JOB_TOOL_NAME not in app_assistant_runtime.registry
    ):
        register_processing_job_tool(
            app_assistant_runtime.registry,
            app_database,
            app_processing_job_runner,
            timeout_seconds=app_settings.assistant_tool_timeout_seconds,
        )
    app_plugin_host_runtime = plugin_host_runtime or (
        plugin_host_runtime_factory(app_settings, app_database) if plugin_host_runtime_factory
        else build_plugin_host_runtime(app_settings, app_database, meeting_operations=meeting_operations))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database_settings = app_database.check_connection()
        plugin_started = False
        try:
            try:
                await app_plugin_host_runtime.start()
                plugin_started = True
            except Exception:
                logger.exception(
                    "Plugin framework startup failed",
                    extra={"event": "plugin_framework_start_failed"},
                )
            recovered_processing_jobs = await app_processing_job_runner.start()
            # Bindings and container generations have been restored; HTTP is not
            # served until lifespan yields. Recovery now uses the same authority.
            host_actions.accepting = True
            assistant_recovery = (
                await app_assistant_runtime.start()
                if app_assistant_runtime is not None
                else None
            )
            host_actions.accepting = True
            meeting_policy.enabled = bool(plugin_started and app_assistant_runtime is not None and app_settings.plugin_framework_enabled)
            recovered_hls_inputs = (
                await app_hls_input_manager.reconcile_orphans()
            )
            logger.info(
                "API started",
                extra={
                    "process_name": "api",
                    "event": "api_started",
                    "database_path": app_settings.database_location,
                    "assistant_enabled": app_assistant_runtime is not None,
                    "assistant_recovered_executions": (
                        len(assistant_recovery.enqueued_execution_ids)
                        if assistant_recovery is not None
                        else 0
                    ),
                    "recovered_hls_inputs": recovered_hls_inputs,
                    "recovered_processing_jobs": recovered_processing_jobs,
                    "plugin_framework_enabled": (
                        app_settings.plugin_framework_enabled
                    ),
                    **database_settings,
                },
            )
            yield
        finally:
            host_actions.accepting = False
            meeting_policy.enabled = False
            try:
                if app_assistant_runtime is not None:
                    await app_assistant_runtime.stop()
            finally:
                try:
                    await app_processing_job_runner.stop()
                finally:
                    try:
                        await app_hls_input_manager.aclose()
                    finally:
                        try:
                            await app_plugin_host_runtime.stop()
                        finally:
                            app_database.dispose()
                            logger.info(
                                "API stopped",
                                extra={
                                    "process_name": "api",
                                    "event": "api_stopped",
                                },
                            )

    application = FastAPI(
        title="LiveCaption Studio API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = app_settings
    application.state.database = app_database
    application.state.room_admin_factory = room_admin_factory or build_room_admin
    application.state.hls_input_manager = app_hls_input_manager
    application.state.processing_job_runner = app_processing_job_runner
    application.state.assistant_runtime = app_assistant_runtime
    application.state.host_actions = host_actions
    application.state.meeting_plugin_policy = meeting_policy
    application.state.plugin_host_runtime = app_plugin_host_runtime
    application.state.meeting_state_projector = (
        app_assistant_runtime.projector
        if app_assistant_runtime is not None
        else None
    )
    application.state.runtime_registry = (
        runtime_registry or RuntimeSnapshotRegistry()
    )
    application.state.worker_health_store = (
        worker_health_store
        or WorkerHealthStore(app_settings.worker_health_dir)
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Plugin-Admin-Token", "X-Assistant-UI", "X-Assistant-UI-Nonce"],
    )
    application.include_router(health_router)
    application.include_router(internal_router)
    application.include_router(livekit_token_router)
    application.include_router(plugins_router)
    application.include_router(plugin_documents_router)
    application.include_router(media_router)
    application.include_router(rooms_router)
    application.include_router(sessions_router)
    application.include_router(meeting_state_router)
    application.include_router(assistant_router)
    application.include_router(assistant_actions_router)
    application.include_router(exports_router)
    application.include_router(scripts_router)
    application.include_router(packages_router)
    application.include_router(revisions_router)
    application.include_router(processing_jobs_router)
    application.include_router(artifacts_router)
    return application


app = create_app()
