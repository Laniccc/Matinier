from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.settings import PROJECT_ROOT, Settings


def test_example_environment_separates_backend_secrets_from_browser() -> None:
    backend_example = (PROJECT_ROOT / ".env.example").read_text(
        encoding="utf-8"
    )
    frontend_example = (
        PROJECT_ROOT / "frontend" / ".env.local.example"
    ).read_text(encoding="utf-8")
    required_backend_keys = (
        "APP_ENV",
        "PUBLIC_API_BASE_URL",
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "LIVEKIT_AGENT_NAME",
        "DATABASE_URL",
        "DATA_DIR",
        "FFMPEG_BIN",
        "INTERNAL_API_BASE_URL",
        "INTERNAL_CONTROL_TOKEN",
        "CORS_ORIGINS",
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_WORKSPACE_ID",
        "DASHSCOPE_WEBSOCKET_PROXY_URL",
        "BAILIAN_ASR_MODEL",
        "BAILIAN_TRANSLATION_MODEL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
        "MAX_REMOTE_STREAM_DURATION_SECONDS",
        "REMOTE_MEDIA_CONNECT_TIMEOUT_SECONDS",
        "REMOTE_MEDIA_READ_TIMEOUT_SECONDS",
        "LOG_LEVEL",
        "PLUGIN_BUILTIN_BUILD_ENABLED",
    )
    active_backend_keys = {
        line.split("=", 1)[0].strip()
        for line in backend_example.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    for key in required_backend_keys:
        assert key in active_backend_keys
    assert (
        "# PLUGIN_BUILTIN_WORK_DIR=C:/Users/your-name/AppData/Local/"
        "Matinier/plugin-builtins"
    ) in backend_example
    assert "PLUGIN_BUILTIN_WORK_DIR" not in active_backend_keys
    assert "NEXT_PUBLIC_API_BASE_URL=" in frontend_example
    for secret in (
        "LIVEKIT_API_SECRET",
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
        "INTERNAL_CONTROL_TOKEN",
    ):
        assert secret not in frontend_example


def test_missing_required_livekit_settings_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    required = (
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "LIVEKIT_ROOM_NAME",
    )
    for name in required:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    message = str(exc_info.value)
    for name in required:
        assert name.lower() in message.lower()


def test_future_provider_settings_have_safe_stage_zero_defaults() -> None:
    settings = Settings(
        _env_file=None,
        livekit_url="ws://localhost:7880",
        livekit_api_key="key",
        livekit_api_secret="secret",
        livekit_room_name="room",
    )

    assert settings.bailian_asr_model == "fun-asr-realtime"
    assert settings.deepseek_model == "deepseek-v4-flash"
    assert settings.dashscope_api_key is None
    assert settings.deepseek_api_key is None
    assert str(settings.deepseek_base_url).rstrip("/") == "https://api.deepseek.com"
    assert settings.deepseek_request_timeout_seconds == 45.0
    assert settings.deepseek_max_retries == 2
    assert settings.deepseek_temperature == 0.2
    assert settings.deepseek_max_input_chars == 12_000
    assert settings.deepseek_max_segments_per_chunk == 40
    assert settings.deepseek_max_output_tokens == 4_096
    assert settings.dashscope_websocket_url is None
    assert settings.dashscope_websocket_proxy_url is None
    assert settings.asr_chunk_duration_ms == 100
    assert settings.asr_queue_max_chunks == 20
    assert settings.asr_start_timeout_seconds == 10.0
    assert settings.asr_finish_timeout_seconds == 15.0
    assert settings.asr_startup_retries == 1
    assert settings.livekit_connect_timeout_seconds == 15.0
    assert settings.livekit_agent_name == "live-caption-agent"
    assert settings.ffmpeg_start_timeout_seconds == 10.0
    assert settings.log_provider_payloads is False
    assert settings.worker_runtime_mode.value == "production"
    assert settings.app_env == "development"
    assert str(settings.public_api_base_url).rstrip("/") == (
        "http://127.0.0.1:8000"
    )
    assert settings.ffmpeg_bin == "ffmpeg"
    assert settings.cors_origin_list == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    assert settings.plugin_builtin_build_enabled is False
    assert settings.plugin_builtin_work_dir.is_absolute()
    assert not settings.plugin_builtin_work_dir.is_relative_to(
        PROJECT_ROOT.resolve()
    )


@pytest.mark.parametrize(
    "work_dir",
    (
        "relative/plugin-builtins",
        PROJECT_ROOT / "backend" / "data" / "plugin-builtins",
    ),
)
def test_builtin_work_dir_must_be_absolute_and_outside_project(work_dir) -> None:
    with pytest.raises(
        ValidationError,
        match="PLUGIN_BUILTIN_WORK_DIR must be an absolute path outside PROJECT_ROOT",
    ):
        Settings(
            _env_file=None,
            livekit_url="ws://localhost:7880",
            livekit_api_key="key",
            livekit_api_secret="secret",
            livekit_room_name="room",
            plugin_builtin_work_dir=work_dir,
        )


def test_production_rejects_dynamic_builtin_signing(tmp_path) -> None:
    with pytest.raises(
        ValidationError,
        match="PLUGIN_BUILTIN_BUILD_ENABLED is not allowed in production",
    ):
        Settings(
            _env_file=None,
            app_env="production",
            public_api_base_url="https://api.example.com",
            cors_origins="https://app.example.com",
            livekit_url="wss://livekit.example.com",
            livekit_api_key="key",
            livekit_api_secret="a-production-secret-that-is-long-enough",
            livekit_room_name="room",
            data_dir=tmp_path / "data",
            plugin_builtin_build_enabled=True,
            plugin_builtin_work_dir=tmp_path / "plugin-builtins",
        )


def test_real_provider_smoke_is_only_available_through_bounded_cli() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            livekit_url="ws://localhost:7880",
            livekit_api_key="key",
            livekit_api_secret="secret",
            livekit_room_name="room",
            worker_runtime_mode="real-provider-smoke",
        )


def test_relative_data_dir_resolves_independently_of_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    values = {
        "livekit_url": "ws://localhost:7880",
        "livekit_api_key": "key",
        "livekit_api_secret": "secret",
        "livekit_room_name": "room",
        "data_dir": "backend/data",
        "database_url": "",
    }

    first = Settings(_env_file=None, **values)
    elsewhere = tmp_path / "unrelated-working-directory"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    second = Settings(_env_file=None, **values)

    expected = (PROJECT_ROOT / "backend" / "data" / "live_caption.db").resolve()
    assert first.data_dir == second.data_dir == expected.parent
    assert first.database_path == second.database_path == expected
    assert first.database_url == second.database_url
    assert first.database_path is not None
    assert first.database_path.is_absolute()
    assert first.persistent_directories == (
        first.data_dir,
        first.data_dir / "exports",
        first.data_dir / "logs",
        first.data_dir / "reports",
        first.data_dir / "worker-health",
        first.data_dir / "plugins",
        first.data_dir / "plugins" / "packages",
        first.data_dir / "plugins" / "images",
        first.data_dir / "plugins" / "state",
        first.data_dir / "plugins" / "staging",
        first.data_dir / "plugins" / "audit",
    )
    assert all(path.is_dir() for path in first.persistent_directories)


def test_production_rejects_localhost_deployment_defaults(tmp_path) -> None:
    with pytest.raises(
        ValidationError,
        match="PUBLIC_API_BASE_URL must not use localhost",
    ):
        Settings(
            _env_file=None,
            app_env="production",
            livekit_url="wss://livekit.example.com",
            livekit_api_key="key",
            livekit_api_secret="a-production-secret-that-is-long-enough",
            livekit_room_name="room",
            data_dir=tmp_path,
        )


def test_legacy_relative_sqlite_url_is_anchored_to_backend() -> None:
    settings = Settings(
        _env_file=None,
        livekit_url="ws://localhost:7880",
        livekit_api_key="key",
        livekit_api_secret="secret",
        livekit_room_name="room",
        database_url="sqlite:///./legacy.db",
    )

    assert settings.database_path == (
        PROJECT_ROOT / "backend" / "legacy.db"
    ).resolve()


def test_short_livekit_secret_is_only_allowed_for_local_development() -> None:
    local = Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="devkey",
        livekit_api_secret="secret",
        livekit_room_name="room",
    )
    assert local.livekit_is_local is True

    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(
            _env_file=None,
            livekit_url="wss://livekit.example.com",
            livekit_api_key="key",
            livekit_api_secret="secret",
            livekit_room_name="room",
        )


def test_internal_control_timeout_exceeds_hls_cleanup_budget() -> None:
    with pytest.raises(
        ValidationError,
        match="INTERNAL_CONTROL_TIMEOUT_SECONDS must exceed",
    ):
        Settings(
            _env_file=None,
            livekit_url="ws://127.0.0.1:7880",
            livekit_api_key="devkey",
            livekit_api_secret="secret",
            livekit_room_name="room",
            hls_stop_timeout_seconds=5,
            internal_control_timeout_seconds=5,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("asr_chunk_duration_ms", 0),
        ("asr_queue_max_chunks", 0),
        ("asr_start_timeout_seconds", 0),
        ("asr_finish_timeout_seconds", -1),
        ("asr_startup_retries", -1),
        ("livekit_connect_timeout_seconds", 0),
        ("ffmpeg_start_timeout_seconds", -1),
    ],
)
def test_invalid_asr_limits_are_rejected(field: str, value: int) -> None:
    values = {
        "livekit_url": "ws://localhost:7880",
        "livekit_api_key": "key",
        "livekit_api_secret": "secret",
        "livekit_room_name": "room",
        field: value,
    }

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_bailian_workspace_alias_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAILIAN_WORKSPACE_ID", "workspace-from-sibling-project")
    monkeypatch.delenv("DASHSCOPE_WORKSPACE_ID", raising=False)

    settings = Settings(
        _env_file=None,
        livekit_url="ws://localhost:7880",
        livekit_api_key="key",
        livekit_api_secret="secret",
        livekit_room_name="room",
    )

    assert settings.dashscope_workspace_id == "workspace-from-sibling-project"


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-v4-pro"])
def test_current_deepseek_models_are_supported(model: str) -> None:
    settings = Settings(
        _env_file=None,
        livekit_url="ws://localhost:7880",
        livekit_api_key="key",
        livekit_api_secret="secret",
        livekit_room_name="room",
        deepseek_model=model,
    )

    assert settings.deepseek_model == model


@pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-reasoner", "unknown"])
def test_deprecated_or_unknown_deepseek_models_are_rejected(model: str) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            livekit_url="ws://localhost:7880",
            livekit_api_key="key",
            livekit_api_secret="secret",
            livekit_room_name="room",
            deepseek_model=model,
        )


def test_copied_llm_setting_aliases_are_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "31.5")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.35")

    settings = Settings(
        _env_file=None,
        livekit_url="ws://localhost:7880",
        livekit_api_key="key",
        livekit_api_secret="secret",
        livekit_room_name="room",
    )

    assert settings.deepseek_request_timeout_seconds == 31.5
    assert settings.deepseek_max_retries == 1
    assert settings.deepseek_temperature == 0.35


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("deepseek_request_timeout_seconds", 0),
        ("deepseek_max_retries", -1),
        ("deepseek_temperature", -0.1),
        ("deepseek_temperature", 2.1),
        ("deepseek_max_input_chars", 0),
        ("deepseek_max_segments_per_chunk", 0),
        ("deepseek_max_output_tokens", 0),
    ],
)
def test_invalid_deepseek_limits_are_rejected(field: str, value: float) -> None:
    values = {
        "livekit_url": "ws://localhost:7880",
        "livekit_api_key": "key",
        "livekit_api_secret": "secret",
        "livekit_room_name": "room",
        field: value,
    }

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
