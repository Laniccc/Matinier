from __future__ import annotations

import os
import sys
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.worker.modes import WorkerRuntimeMode


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
PROJECT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_DATA_DIR = BACKEND_ROOT / "data"


def _default_plugin_builtin_work_dir() -> Path:
    """Return a platform-local work root that is not part of the repository."""

    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (base.expanduser() / "Matinier" / "plugin-builtins").resolve()


class Settings(BaseSettings):
    """Application configuration shared by the API and worker processes."""

    model_config = SettingsConfigDict(
        env_file=(str(PROJECT_ENV_FILE),),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_env: Literal["development", "test", "production"] = Field(
        default="development",
        validation_alias="APP_ENV",
    )
    public_api_base_url: AnyHttpUrl = Field(
        default="http://127.0.0.1:8000",
        validation_alias="PUBLIC_API_BASE_URL",
    )
    livekit_url: str = Field(validation_alias="LIVEKIT_URL")
    livekit_api_key: str = Field(validation_alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(validation_alias="LIVEKIT_API_SECRET")
    livekit_room_name: str = Field(validation_alias="LIVEKIT_ROOM_NAME")
    livekit_agent_name: str = Field(
        default="live-caption-agent",
        validation_alias="LIVEKIT_AGENT_NAME",
    )
    livekit_connect_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        validation_alias="LIVEKIT_CONNECT_TIMEOUT_SECONDS",
    )
    ffmpeg_start_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        validation_alias="FFMPEG_START_TIMEOUT_SECONDS",
    )
    hls_first_frame_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        validation_alias="HLS_FIRST_FRAME_TIMEOUT_SECONDS",
    )
    hls_stop_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        validation_alias="HLS_STOP_TIMEOUT_SECONDS",
    )
    remote_media_connect_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        validation_alias="REMOTE_MEDIA_CONNECT_TIMEOUT_SECONDS",
    )
    remote_media_read_timeout_seconds: float = Field(
        default=20.0,
        gt=0,
        validation_alias="REMOTE_MEDIA_READ_TIMEOUT_SECONDS",
    )
    remote_media_max_redirects: int = Field(
        default=3,
        ge=0,
        le=10,
        validation_alias="REMOTE_MEDIA_MAX_REDIRECTS",
    )
    max_remote_stream_duration_seconds: float = Field(
        default=14_400.0,
        gt=0,
        validation_alias="MAX_REMOTE_STREAM_DURATION_SECONDS",
    )
    ffmpeg_bin: str = Field(default="ffmpeg", validation_alias="FFMPEG_BIN")
    cors_origins: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000",
        validation_alias="CORS_ORIGINS",
    )
    internal_api_base_url: AnyHttpUrl = Field(
        default="http://127.0.0.1:8000",
        validation_alias="INTERNAL_API_BASE_URL",
    )
    internal_control_token: str = Field(
        default="dev-internal-control-token",
        min_length=16,
        validation_alias="INTERNAL_CONTROL_TOKEN",
    )
    internal_control_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        validation_alias="INTERNAL_CONTROL_TIMEOUT_SECONDS",
    )

    dashscope_api_key: str | None = Field(default=None, validation_alias="DASHSCOPE_API_KEY")
    dashscope_workspace_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "DASHSCOPE_WORKSPACE_ID",
            "BAILIAN_WORKSPACE_ID",
        ),
    )
    dashscope_region: str = Field(default="beijing", validation_alias="DASHSCOPE_REGION")
    dashscope_websocket_url: str | None = Field(
        default=None,
        validation_alias="DASHSCOPE_WEBSOCKET_URL",
    )
    bailian_asr_model: str = Field(
        default="fun-asr-realtime",
        validation_alias="BAILIAN_ASR_MODEL",
    )
    bailian_translation_model: str = Field(
        default="qwen3.5-livetranslate-flash-realtime",
        validation_alias="BAILIAN_TRANSLATION_MODEL",
    )
    dashscope_translation_websocket_url: str | None = Field(
        default=None,
        validation_alias="DASHSCOPE_TRANSLATION_WEBSOCKET_URL",
    )
    dashscope_websocket_proxy_url: str | None = Field(
        default=None,
        validation_alias="DASHSCOPE_WEBSOCKET_PROXY_URL",
    )
    asr_chunk_duration_ms: int = Field(
        default=100,
        gt=0,
        validation_alias="ASR_CHUNK_DURATION_MS",
    )
    asr_queue_max_chunks: int = Field(
        default=20,
        gt=0,
        validation_alias="ASR_QUEUE_MAX_CHUNKS",
    )
    asr_start_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        validation_alias="ASR_START_TIMEOUT_SECONDS",
    )
    asr_finish_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        validation_alias="ASR_FINISH_TIMEOUT_SECONDS",
    )
    asr_startup_retries: int = Field(
        default=1,
        ge=0,
        validation_alias="ASR_STARTUP_RETRIES",
    )
    translation_queue_max_chunks: int = Field(
        default=20,
        gt=0,
        validation_alias="TRANSLATION_QUEUE_MAX_CHUNKS",
    )
    translation_start_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        validation_alias="TRANSLATION_START_TIMEOUT_SECONDS",
    )
    translation_finish_timeout_seconds: float = Field(
        default=20.0,
        gt=0,
        validation_alias="TRANSLATION_FINISH_TIMEOUT_SECONDS",
    )

    worker_runtime_mode: WorkerRuntimeMode = Field(
        default=WorkerRuntimeMode.PRODUCTION,
        validation_alias="WORKER_RUNTIME_MODE",
    )
    transport_queue_max_frames: int = Field(
        default=100,
        gt=0,
        validation_alias="TRANSPORT_QUEUE_MAX_FRAMES",
    )
    transport_consumer_delay_ms: float = Field(
        default=0.0,
        ge=0,
        validation_alias="TRANSPORT_CONSUMER_DELAY_MS",
    )
    fake_provider_chunks_per_segment: int = Field(
        default=3,
        ge=3,
        validation_alias="FAKE_PROVIDER_CHUNKS_PER_SEGMENT",
    )
    fake_provider_send_delay_ms: float = Field(
        default=0.0,
        ge=0,
        validation_alias="FAKE_PROVIDER_SEND_DELAY_MS",
    )
    fake_provider_duplicate_final: bool = Field(
        default=False,
        validation_alias="FAKE_PROVIDER_DUPLICATE_FINAL",
    )
    fake_provider_stale_partial: bool = Field(
        default=False,
        validation_alias="FAKE_PROVIDER_STALE_PARTIAL",
    )
    fake_asr_fail_after_chunks: int | None = Field(
        default=None,
        gt=0,
        validation_alias="FAKE_ASR_FAIL_AFTER_CHUNKS",
    )
    fake_translation_fail_after_chunks: int | None = Field(
        default=None,
        gt=0,
        validation_alias="FAKE_TRANSLATION_FAIL_AFTER_CHUNKS",
    )
    fake_asr_timeout_on_finish: bool = Field(
        default=False,
        validation_alias="FAKE_ASR_TIMEOUT_ON_FINISH",
    )
    fake_translation_timeout_on_finish: bool = Field(
        default=False,
        validation_alias="FAKE_TRANSLATION_TIMEOUT_ON_FINISH",
    )

    deepseek_api_key: str | None = Field(default=None, validation_alias="DEEPSEEK_API_KEY")
    deepseek_model: Literal["deepseek-v4-flash", "deepseek-v4-pro"] = Field(
        default="deepseek-v4-flash",
        validation_alias="DEEPSEEK_MODEL",
    )
    deepseek_base_url: AnyHttpUrl = Field(
        default="https://api.deepseek.com",
        validation_alias="DEEPSEEK_BASE_URL",
    )
    deepseek_request_timeout_seconds: float = Field(
        default=45.0,
        gt=0,
        validation_alias=AliasChoices(
            "DEEPSEEK_REQUEST_TIMEOUT_SECONDS",
            "LLM_REQUEST_TIMEOUT",
        ),
    )
    deepseek_max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        validation_alias=AliasChoices(
            "DEEPSEEK_MAX_RETRIES",
            "LLM_MAX_RETRIES",
        ),
    )
    deepseek_temperature: float = Field(
        default=0.2,
        ge=0,
        le=2,
        validation_alias=AliasChoices(
            "DEEPSEEK_TEMPERATURE",
            "LLM_TEMPERATURE",
        ),
    )
    deepseek_max_input_chars: int = Field(
        default=12_000,
        gt=0,
        validation_alias="DEEPSEEK_MAX_INPUT_CHARS",
    )
    deepseek_max_segments_per_chunk: int = Field(
        default=40,
        gt=0,
        validation_alias="DEEPSEEK_MAX_SEGMENTS_PER_CHUNK",
    )
    deepseek_max_output_tokens: int = Field(
        default=4_096,
        gt=0,
        validation_alias="DEEPSEEK_MAX_OUTPUT_TOKENS",
    )

    task_system_provider: Literal["disabled", "fake", "linear"] = Field(
        default="disabled",
        validation_alias="TASK_SYSTEM_PROVIDER",
    )
    linear_api_url: AnyHttpUrl = Field(
        default="https://api.linear.app/graphql",
        validation_alias="LINEAR_API_URL",
    )
    linear_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="LINEAR_API_KEY",
    )
    linear_team_id: str | None = Field(
        default=None,
        validation_alias="LINEAR_TEAM_ID",
    )
    linear_default_project_id: str | None = Field(
        default=None,
        validation_alias="LINEAR_DEFAULT_PROJECT_ID",
    )
    linear_request_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        validation_alias="LINEAR_REQUEST_TIMEOUT_SECONDS",
    )
    linear_max_search_results: int = Field(
        default=20,
        ge=1,
        le=20,
        validation_alias="LINEAR_MAX_SEARCH_RESULTS",
    )

    assistant_enabled: bool = Field(
        default=False,
        validation_alias="ASSISTANT_ENABLED",
    )
    assistant_host_token: SecretStr | None = Field(
        default=None, validation_alias="ASSISTANT_HOST_TOKEN",
    )
    assistant_ui_nonce_ttl_seconds: int = Field(
        default=900, ge=30, le=900, validation_alias="ASSISTANT_UI_NONCE_TTL_SECONDS",
    )
    assistant_ui_context_limit: int = Field(
        default=256, ge=1, le=4096, validation_alias="ASSISTANT_UI_CONTEXT_LIMIT",
    )
    assistant_fast_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        validation_alias="ASSISTANT_FAST_TIMEOUT_SECONDS",
    )
    assistant_fast_max_model_rounds: int = Field(
        default=2,
        ge=1,
        le=2,
        validation_alias="ASSISTANT_FAST_MAX_MODEL_ROUNDS",
    )
    assistant_fast_max_parallel_read_tools: int = Field(
        default=2,
        ge=1,
        le=2,
        validation_alias="ASSISTANT_FAST_MAX_PARALLEL_READ_TOOLS",
    )
    assistant_grant_max_ttl_seconds: int = Field(
        default=900,
        gt=0,
        le=86_400,
        validation_alias="ASSISTANT_GRANT_MAX_TTL_SECONDS",
    )
    assistant_action_max_steps: int = Field(
        default=20,
        gt=0,
        validation_alias="ASSISTANT_ACTION_MAX_STEPS",
    )
    assistant_action_max_model_calls: int = Field(
        default=16,
        gt=0,
        validation_alias="ASSISTANT_ACTION_MAX_MODEL_CALLS",
    )
    assistant_action_max_planning_rounds: int = Field(
        default=4,
        gt=0,
        validation_alias="ASSISTANT_ACTION_MAX_PLANNING_ROUNDS",
    )
    assistant_action_concurrency: int = Field(
        default=3,
        gt=0,
        validation_alias="ASSISTANT_ACTION_CONCURRENCY",
    )
    assistant_subagents_per_run: int = Field(
        default=3,
        ge=0,
        le=3,
        validation_alias="ASSISTANT_SUBAGENTS_PER_RUN",
    )
    assistant_subagent_global_concurrency: int = Field(
        default=6,
        gt=0,
        validation_alias="ASSISTANT_SUBAGENT_GLOBAL_CONCURRENCY",
    )
    assistant_tool_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        validation_alias="ASSISTANT_TOOL_TIMEOUT_SECONDS",
    )
    assistant_reconciliation_delays_seconds: str = Field(
        default="1,3,10",
        validation_alias="ASSISTANT_RECONCILIATION_DELAYS_SECONDS",
    )
    meeting_state_poll_interval_ms: int = Field(
        default=400,
        gt=0,
        validation_alias="MEETING_STATE_POLL_INTERVAL_MS",
    )
    meeting_state_batch_segments: int = Field(
        default=10,
        gt=0,
        validation_alias="MEETING_STATE_BATCH_SEGMENTS",
    )
    meeting_state_batch_trigger_chars: int = Field(
        default=2_000,
        gt=0,
        validation_alias="MEETING_STATE_BATCH_TRIGGER_CHARS",
    )
    meeting_state_max_input_chars: int = Field(
        default=12_000,
        gt=0,
        validation_alias="MEETING_STATE_MAX_INPUT_CHARS",
    )
    meeting_state_max_batch_wait_seconds: float = Field(
        default=4.0,
        gt=0,
        validation_alias="MEETING_STATE_MAX_BATCH_WAIT_SECONDS",
    )
    meeting_state_scan_overlap_seconds: float = Field(
        default=2.0,
        ge=0,
        validation_alias="MEETING_STATE_SCAN_OVERLAP_SECONDS",
    )
    meeting_state_stale_after_seconds: float = Field(
        default=10.0,
        gt=0,
        validation_alias="MEETING_STATE_STALE_AFTER_SECONDS",
    )
    meeting_state_concurrency: int = Field(
        default=2,
        gt=0,
        validation_alias="MEETING_STATE_CONCURRENCY",
    )
    meeting_state_priority_burst: int = Field(
        default=2,
        gt=0,
        validation_alias="MEETING_STATE_PRIORITY_BURST",
    )
    meeting_state_retry_delays_seconds: str = Field(
        default="1,2,5,10,30",
        validation_alias="MEETING_STATE_RETRY_DELAYS_SECONDS",
    )

    plugin_framework_enabled: bool = Field(
        default=True,
        validation_alias="PLUGIN_FRAMEWORK_ENABLED",
    )
    plugin_allow_unsigned: bool = Field(
        default=False,
        validation_alias="PLUGIN_ALLOW_UNSIGNED",
    )
    plugin_container_runtime: Literal["docker"] = Field(
        default="docker",
        validation_alias="PLUGIN_CONTAINER_RUNTIME",
    )
    plugin_admin_token: SecretStr | None = Field(
        default=None,
        validation_alias="PLUGIN_ADMIN_TOKEN",
    )
    plugin_builtin_build_enabled: bool = Field(
        default=False,
        validation_alias="PLUGIN_BUILTIN_BUILD_ENABLED",
    )
    plugin_builtin_work_dir: Path = Field(
        default_factory=_default_plugin_builtin_work_dir,
        validation_alias="PLUGIN_BUILTIN_WORK_DIR",
    )
    plugin_package_max_compressed_bytes: int = Field(
        default=512 * 1024 * 1024,
        gt=0,
        validation_alias="PLUGIN_PACKAGE_MAX_COMPRESSED_BYTES",
    )
    plugin_package_max_uncompressed_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,
        gt=0,
        validation_alias="PLUGIN_PACKAGE_MAX_UNCOMPRESSED_BYTES",
    )
    plugin_package_max_entries: int = Field(
        default=2_000,
        ge=1,
        le=20_000,
        validation_alias="PLUGIN_PACKAGE_MAX_ENTRIES",
    )
    plugin_rpc_max_line_bytes: int = Field(
        default=256 * 1024,
        ge=1_024,
        validation_alias="PLUGIN_RPC_MAX_LINE_BYTES",
    )
    plugin_rpc_max_message_bytes: int = Field(
        default=256 * 1024,
        ge=1_024,
        validation_alias="PLUGIN_RPC_MAX_MESSAGE_BYTES",
    )
    plugin_rpc_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        validation_alias="PLUGIN_RPC_TIMEOUT_SECONDS",
    )
    plugin_model_max_concurrency: int = Field(
        default=2,
        ge=1,
        le=16,
        validation_alias="PLUGIN_MODEL_MAX_CONCURRENCY",
    )
    plugin_model_max_input_chars: int = Field(
        default=32_000,
        ge=1_024,
        le=128_000,
        validation_alias="PLUGIN_MODEL_MAX_INPUT_CHARS",
    )
    plugin_model_max_output_tokens: int = Field(
        default=4_096,
        ge=1,
        le=32_768,
        validation_alias="PLUGIN_MODEL_MAX_OUTPUT_TOKENS",
    )
    plugin_document_max_bytes: int = Field(
        default=192 * 1_024,
        ge=1_024,
        le=2 * 1_024 * 1_024,
        validation_alias="PLUGIN_DOCUMENT_MAX_BYTES",
    )
    plugin_delivery_max_page_items: int = Field(
        default=100,
        ge=1,
        le=1_000,
        validation_alias="PLUGIN_DELIVERY_MAX_PAGE_ITEMS",
    )
    plugin_container_memory_mb: int = Field(
        default=256,
        ge=64,
        le=4_096,
        validation_alias="PLUGIN_CONTAINER_MEMORY_MB",
    )
    plugin_container_cpu_count: float = Field(
        default=0.5,
        gt=0,
        le=4,
        validation_alias="PLUGIN_CONTAINER_CPU_COUNT",
    )
    plugin_container_pid_limit: int = Field(
        default=64,
        ge=16,
        le=512,
        validation_alias="PLUGIN_CONTAINER_PID_LIMIT",
    )
    plugin_container_tmpfs_mb: int = Field(
        default=64,
        ge=16,
        le=1_024,
        validation_alias="PLUGIN_CONTAINER_TMPFS_MB",
    )
    plugin_crash_loop_max_restarts: int = Field(
        default=5,
        ge=1,
        le=100,
        validation_alias="PLUGIN_CRASH_LOOP_MAX_RESTARTS",
    )
    plugin_crash_loop_window_seconds: float = Field(
        default=60.0,
        gt=0,
        validation_alias="PLUGIN_CRASH_LOOP_WINDOW_SECONDS",
    )
    plugin_shutdown_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        validation_alias="PLUGIN_SHUTDOWN_TIMEOUT_SECONDS",
    )
    media_event_projector_poll_interval_ms: int = Field(
        default=500,
        gt=0,
        validation_alias="MEDIA_EVENT_PROJECTOR_POLL_INTERVAL_MS",
    )
    media_event_projector_batch_size: int = Field(
        default=100,
        ge=1,
        le=1_000,
        validation_alias="MEDIA_EVENT_PROJECTOR_BATCH_SIZE",
    )
    media_event_projector_scan_session_limit: int = Field(
        default=100,
        ge=1,
        le=10_000,
        validation_alias="MEDIA_EVENT_PROJECTOR_SCAN_SESSION_LIMIT",
    )

    data_dir: Path = Field(default=DEFAULT_DATA_DIR, validation_alias="DATA_DIR")
    database_url: str = Field(default="", validation_alias="DATABASE_URL")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        validation_alias="LOG_LEVEL",
    )
    log_provider_payloads: bool = Field(
        default=False,
        validation_alias="LOG_PROVIDER_PAYLOADS",
    )

    @property
    def livekit_is_local(self) -> bool:
        hostname = urlsplit(self.livekit_url).hostname
        return hostname in {"localhost", "127.0.0.1", "::1"}

    @property
    def database_path(self) -> Path | None:
        """Return the normalized SQLite file path without exposing other URLs."""

        database_url = make_url(self.database_url)
        if not database_url.drivername.startswith("sqlite"):
            return None
        if database_url.database in {None, "", ":memory:"}:
            return None
        return Path(database_url.database)

    @property
    def database_location(self) -> str:
        """A startup-safe database location that never includes credentials."""

        database_path = self.database_path
        if database_path is not None:
            return str(database_path)
        driver_name = make_url(self.database_url).drivername
        return f"<{driver_name} database>"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def worker_health_dir(self) -> Path:
        return self.data_dir / "worker-health"

    @property
    def plugins_dir(self) -> Path:
        return self.data_dir / "plugins"

    @property
    def plugin_packages_dir(self) -> Path:
        return self.plugins_dir / "packages"

    @property
    def plugin_images_dir(self) -> Path:
        return self.plugins_dir / "images"

    @property
    def plugin_state_dir(self) -> Path:
        return self.plugins_dir / "state"

    @property
    def plugin_staging_dir(self) -> Path:
        return self.plugins_dir / "staging"

    @property
    def plugin_audit_dir(self) -> Path:
        return self.plugins_dir / "audit"

    @property
    def persistent_directories(self) -> tuple[Path, ...]:
        return (
            self.data_dir,
            self.exports_dir,
            self.logs_dir,
            self.reports_dir,
            self.worker_health_dir,
            self.plugins_dir,
            self.plugin_packages_dir,
            self.plugin_images_dir,
            self.plugin_state_dir,
            self.plugin_staging_dir,
            self.plugin_audit_dir,
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            origin.strip().rstrip("/")
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]

    @property
    def meeting_state_retry_delay_list(self) -> tuple[float, ...]:
        return tuple(
            float(value)
            for value in self.meeting_state_retry_delays_seconds.split(",")
        )

    @property
    def assistant_reconciliation_delay_list(self) -> tuple[float, ...]:
        return tuple(
            float(value)
            for value in self.assistant_reconciliation_delays_seconds.split(",")
        )

    @property
    def task_system_configured(self) -> bool:
        """Configuration-only availability signal; never performs provider I/O."""

        if self.task_system_provider == "disabled":
            return False
        if self.task_system_provider == "fake":
            return True
        return self.linear_api_key is not None and self.linear_team_id is not None

    @model_validator(mode="after")
    def validate_and_normalize_settings(self) -> "Settings":
        data_dir = self.data_dir.expanduser()
        if not data_dir.is_absolute():
            data_dir = PROJECT_ROOT / data_dir
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        builtin_work_dir = self.plugin_builtin_work_dir
        if not builtin_work_dir.is_absolute():
            raise ValueError(
                "PLUGIN_BUILTIN_WORK_DIR must be an absolute path outside PROJECT_ROOT"
            )
        builtin_work_dir = builtin_work_dir.resolve()
        if builtin_work_dir.is_relative_to(PROJECT_ROOT.resolve()):
            raise ValueError(
                "PLUGIN_BUILTIN_WORK_DIR must be an absolute path outside PROJECT_ROOT"
            )
        self.plugin_builtin_work_dir = builtin_work_dir

        if not self.ffmpeg_bin.strip():
            raise ValueError("FFMPEG_BIN must not be blank")
        self.ffmpeg_bin = self.ffmpeg_bin.strip()
        if not self.livekit_agent_name.strip():
            raise ValueError("LIVEKIT_AGENT_NAME must not be blank")
        self.livekit_agent_name = self.livekit_agent_name.strip()

        cors_origins = self.cors_origin_list
        if not cors_origins:
            raise ValueError("CORS_ORIGINS must contain at least one origin")
        for origin in cors_origins:
            parsed_origin = urlsplit(origin)
            if (
                parsed_origin.scheme not in {"http", "https"}
                or parsed_origin.hostname is None
                or parsed_origin.path not in {"", "/"}
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                raise ValueError(
                    "CORS_ORIGINS entries must be HTTP(S) origins without paths"
                )
        self.cors_origins = ",".join(cors_origins)

        retry_delays: list[str] = []
        for raw_delay in self.meeting_state_retry_delays_seconds.split(","):
            normalized = raw_delay.strip()
            if not normalized:
                raise ValueError(
                    "MEETING_STATE_RETRY_DELAYS_SECONDS must contain only "
                    "positive numbers"
                )
            try:
                delay = float(normalized)
            except ValueError as error:
                raise ValueError(
                    "MEETING_STATE_RETRY_DELAYS_SECONDS must contain only "
                    "positive numbers"
                ) from error
            if delay <= 0:
                raise ValueError(
                    "MEETING_STATE_RETRY_DELAYS_SECONDS must contain only "
                    "positive numbers"
                )
            retry_delays.append(format(delay, "g"))
        if not retry_delays:
            raise ValueError(
                "MEETING_STATE_RETRY_DELAYS_SECONDS must not be empty"
            )
        self.meeting_state_retry_delays_seconds = ",".join(retry_delays)

        reconciliation_delays: list[str] = []
        for raw_delay in self.assistant_reconciliation_delays_seconds.split(","):
            normalized = raw_delay.strip()
            if not normalized:
                raise ValueError(
                    "ASSISTANT_RECONCILIATION_DELAYS_SECONDS must contain "
                    "only positive numbers"
                )
            try:
                delay = float(normalized)
            except ValueError as error:
                raise ValueError(
                    "ASSISTANT_RECONCILIATION_DELAYS_SECONDS must contain "
                    "only positive numbers"
                ) from error
            if delay <= 0:
                raise ValueError(
                    "ASSISTANT_RECONCILIATION_DELAYS_SECONDS must contain "
                    "only positive numbers"
                )
            reconciliation_delays.append(format(delay, "g"))
        self.assistant_reconciliation_delays_seconds = ",".join(
            reconciliation_delays
        )

        if self.dashscope_websocket_proxy_url is not None:
            proxy_url = self.dashscope_websocket_proxy_url.strip()
            self.dashscope_websocket_proxy_url = proxy_url or None

        if self.plugin_admin_token is not None:
            admin_token = self.plugin_admin_token.get_secret_value().strip()
            self.plugin_admin_token = SecretStr(admin_token) if admin_token else None
        if (
            self.plugin_package_max_uncompressed_bytes
            < self.plugin_package_max_compressed_bytes
        ):
            raise ValueError(
                "PLUGIN_PACKAGE_MAX_UNCOMPRESSED_BYTES must be greater than or equal "
                "to PLUGIN_PACKAGE_MAX_COMPRESSED_BYTES"
            )
        if self.plugin_rpc_max_message_bytes > self.plugin_rpc_max_line_bytes:
            raise ValueError(
                "PLUGIN_RPC_MAX_MESSAGE_BYTES must not exceed "
                "PLUGIN_RPC_MAX_LINE_BYTES"
            )
        if self.plugin_document_max_bytes > self.plugin_rpc_max_message_bytes:
            raise ValueError(
                "PLUGIN_DOCUMENT_MAX_BYTES must not exceed "
                "PLUGIN_RPC_MAX_MESSAGE_BYTES"
            )
        if self.app_env == "production" and self.plugin_allow_unsigned:
            raise ValueError("PLUGIN_ALLOW_UNSIGNED is not allowed in production")
        if self.app_env == "production" and self.plugin_builtin_build_enabled:
            raise ValueError(
                "PLUGIN_BUILTIN_BUILD_ENABLED is not allowed in production"
            )

        if self.linear_api_key is not None:
            api_key = self.linear_api_key.get_secret_value().strip()
            self.linear_api_key = SecretStr(api_key) if api_key else None
        for field_name in ("linear_team_id", "linear_default_project_id"):
            value = getattr(self, field_name)
            setattr(
                self,
                field_name,
                value.strip() if value is not None and value.strip() else None,
            )
        if self.task_system_provider == "linear" and (
            self.linear_api_key is None or self.linear_team_id is None
        ):
            raise ValueError(
                "LINEAR_API_KEY and LINEAR_TEAM_ID are required when "
                "TASK_SYSTEM_PROVIDER=linear"
            )
        if self.app_env == "production" and self.task_system_provider == "fake":
            raise ValueError("TASK_SYSTEM_PROVIDER=fake is not allowed in production")
        if (
            self.app_env == "production"
            and urlsplit(str(self.linear_api_url)).scheme != "https"
        ):
            raise ValueError("LINEAR_API_URL must use HTTPS in production")

        raw_database_url = self.database_url.strip()
        if not raw_database_url:
            database_path = self.data_dir / "live_caption.db"
            database_url = make_url("sqlite://").set(
                database=database_path.as_posix()
            )
            self.database_url = database_url.render_as_string(
                hide_password=False
            )
        else:
            database_url = make_url(raw_database_url)
            database_name = database_url.database
            if (
                database_url.drivername.startswith("sqlite")
                and database_name not in {None, "", ":memory:"}
            ):
                database_path = Path(database_name).expanduser()
                if not database_path.is_absolute():
                    database_path = BACKEND_ROOT / database_path
                database_path = database_path.resolve()
                database_path.parent.mkdir(parents=True, exist_ok=True)
                database_url = database_url.set(
                    database=database_path.as_posix()
                )
                self.database_url = database_url.render_as_string(
                    hide_password=False
                )

        for directory in self.persistent_directories:
            directory.mkdir(parents=True, exist_ok=True)

        if self.app_env == "production":
            public_host = urlsplit(str(self.public_api_base_url)).hostname
            if public_host in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError(
                    "PUBLIC_API_BASE_URL must not use localhost in production"
                )
            if self.livekit_is_local:
                raise ValueError(
                    "LIVEKIT_URL must not use localhost in production"
                )
            if any(
                urlsplit(origin).hostname
                in {"localhost", "127.0.0.1", "::1"}
                for origin in self.cors_origin_list
            ):
                raise ValueError(
                    "CORS_ORIGINS must not use localhost in production"
                )

        if (
            self.internal_control_timeout_seconds
            <= self.hls_stop_timeout_seconds
        ):
            raise ValueError(
                "INTERNAL_CONTROL_TIMEOUT_SECONDS must exceed "
                "HLS_STOP_TIMEOUT_SECONDS"
            )
        if len(self.livekit_api_secret.encode("utf-8")) >= 32:
            return self
        if self.livekit_is_local:
            return self
        raise ValueError(
            "LIVEKIT_API_SECRET must be at least 32 bytes outside local development"
        )



@lru_cache
def get_settings() -> Settings:
    return Settings()


def configure_livekit_jwt_warnings(settings: Settings) -> None:
    """Hide PyJWT's known weak-key warning only for local LiveKit dev mode."""
    if not (
        settings.livekit_is_local
        and settings.livekit_api_key == "devkey"
        and settings.livekit_api_secret == "secret"
    ):
        return
    try:
        from jwt.warnings import InsecureKeyLengthWarning
    except ImportError:
        return
    warnings.filterwarnings("ignore", category=InsecureKeyLengthWarning)
