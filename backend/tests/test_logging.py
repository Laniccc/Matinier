from __future__ import annotations

import json
import logging

from app.logging import JsonFormatter
from app.errors import (
    ERROR_CATEGORIES,
    MediaDecodeError,
    public_error_for,
)


def test_json_logs_always_include_required_context() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="ready",
        args=(),
        exc_info=None,
    )
    record.process_name = "api"
    record.event = "startup_completed"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["process"] == "api"
    assert payload["event"] == "startup_completed"
    assert payload["session_id"] is None
    assert payload["room_name"] is None
    assert payload["participant_identity"] is None
    assert payload["track_sid"] is None
    assert payload["source_type"] is None
    assert payload["provider"] is None
    assert payload["provider_request_id"] is None
    assert payload["status"] is None
    assert payload["failure_code"] is None
    assert payload["cleanup_status"] is None
    assert payload["elapsed_ms"] is None
    assert payload["error_type"] is None


def test_json_logs_redact_secrets_and_url_queries_recursively() -> None:
    record = logging.LogRecord(
        name="app.hls.manager",
        level=logging.ERROR,
        pathname=__file__,
        lineno=40,
        msg=(
            "failed https://media.example/live.m3u8?signature=private "
            "token=top-secret"
        ),
        args=(),
        exc_info=None,
    )
    record.raw_payload = {
        "url": "wss://provider.example/ws?api_key=private",
        "authorization": "Bearer private",
    }

    encoded = JsonFormatter("worker").format(record)
    payload = json.loads(encoded)

    assert "signature=private" not in encoded
    assert "top-secret" not in encoded
    assert "api_key=private" not in encoded
    assert "Bearer private" not in encoded
    assert payload["message"].endswith("token=[redacted]")
    assert payload["raw_payload"]["authorization"] == "[redacted]"


def test_json_logs_preserve_internal_error_type_for_server_diagnostics() -> None:
    record = logging.LogRecord(
        name="app.hls.manager",
        level=logging.ERROR,
        pathname=__file__,
        lineno=30,
        msg="HLS input task failed",
        args=(),
        exc_info=None,
    )
    record.internal_error_type = "HLSDecodeError"

    payload = json.loads(JsonFormatter("api").format(record))

    assert payload["internal_error_type"] == "HLSDecodeError"


def test_json_logs_use_formatter_process_for_dependency_records() -> None:
    record = logging.LogRecord(
        name="dependency",
        level=logging.INFO,
        pathname=__file__,
        lineno=30,
        msg="connected",
        args=(),
        exc_info=None,
    )

    payload = json.loads(JsonFormatter("worker").format(record))

    assert payload["process"] == "worker"


def test_json_logs_preserve_audio_metric_fields() -> None:
    record = logging.LogRecord(
        name="worker",
        level=logging.INFO,
        pathname=__file__,
        lineno=50,
        msg="progress",
        args=(),
        exc_info=None,
    )
    record.frame_count = 25
    record.sample_rate = 16_000
    record.channels = 1
    record.samples_per_channel = 320
    record.audio_duration_seconds = 0.5

    payload = json.loads(JsonFormatter("worker").format(record))

    assert payload["frame_count"] == 25
    assert payload["sample_rate"] == 16_000
    assert payload["channels"] == 1
    assert payload["samples_per_channel"] == 320
    assert payload["audio_duration_seconds"] == 0.5


def test_json_logs_preserve_translation_summary_without_caption_text() -> None:
    record = logging.LogRecord(
        name="translation",
        level=logging.INFO,
        pathname=__file__,
        lineno=70,
        msg="translation session summary",
        args=(),
        exc_info=None,
    )
    record.translation_provider = "bailian"
    record.target_language = "en-US"
    record.translation_provider_revision_count = 224
    record.translation_published_draft_count = 12
    record.translation_published_final_count = 1

    payload = json.loads(JsonFormatter("worker").format(record))

    assert payload["translation_provider"] == "bailian"
    assert payload["target_language"] == "en-US"
    assert payload["translation_provider_revision_count"] == 224
    assert payload["translation_published_draft_count"] == 12
    assert payload["translation_published_final_count"] == 1
    assert "text" not in payload


def test_stage6_error_taxonomy_is_stable_and_sanitized() -> None:
    assert ERROR_CATEGORIES == {
        "configuration_error",
        "media_decode_error",
        "livekit_error",
        "asr_auth_error",
        "asr_stream_error",
        "persistence_error",
        "deepseek_error",
        "export_error",
    }
    code, message = public_error_for(
        MediaDecodeError("private path E:/secret/input.wav")
    )
    assert code == "media_decode_error"
    assert message == "The audio file could not be decoded."
    assert "secret" not in message
