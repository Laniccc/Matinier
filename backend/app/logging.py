from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any


STABLE_CONTEXT_FIELDS = (
    "trace_id",
    "session_id",
    "execution_id",
    "root_execution_id",
    "snapshot_id",
    "handoff_id",
    "target_execution_id",
    "tool_call_id",
    "tool_name",
    "capability",
    "step_sequence",
    "step_kind",
    "state_version",
    "room_name",
    "participant_identity",
    "track_sid",
    "source_type",
    "provider",
    "provider_request_id",
    "event",
    "phase",
    "status",
    "error_code",
    "failure_code",
    "duration_ms",
    "cleanup_status",
    "elapsed_ms",
    "retry_count",
    "error_type",
)

ASSISTANT_LIFECYCLE_FIELDS = (
    "profile",
    "source_event",
    "attempt_count",
)

_URL_QUERY_PATTERN = re.compile(
    r"(?P<base>\b(?:https?|wss?)://[^\s?'\"<>]+)\?[^\s'\"<>]*",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|authorization|token|secret|password)"
    r"(\s*[:=]\s*)([^\s,;&]+)",
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)

METRIC_FIELDS = (
    "frame_count",
    "audio_bytes",
    "sample_rate",
    "channels",
    "samples_per_channel",
    "audio_duration_seconds",
    "playback_elapsed_seconds",
    "received_started_at",
    "received_ended_at",
    "final_result_count",
    "first_partial_latency_ms",
    "average_final_latency_ms",
    "provider_error_count",
    "sent_audio_chunk_count",
    "sent_audio_bytes",
    "audio_duration_ms",
    "receive_wall_time_ms",
    "realtime_ratio",
    "queue_size",
    "max_queue_size",
    "dropped_frames",
    "process_memory_mb",
    "active_task_count",
)

ASR_EVENT_FIELDS = (
    "provider_event_id",
    "segment_id",
    "text",
    "is_final",
    "begin_time_ms",
    "end_time_ms",
    "confidence",
    "raw_payload",
)

TRANSLATION_FIELDS = (
    "translation_provider",
    "translation_model",
    "source_language",
    "target_language",
    "translation_error_code",
    "translation_provider_revision_count",
    "translation_published_draft_count",
    "translation_published_final_count",
    "text_length",
    "revision",
)

INTERNAL_DIAGNOSTIC_FIELDS = (
    "internal_error_type",
    "stop_reason",
    "cleanup_step",
    "request_id",
    "database_path",
    "journal_mode",
    "synchronous",
    "foreign_keys",
    "busy_timeout_ms",
    "worker_runtime_mode",
    "resolved_host",
    "resolved_ips",
    "redirect_count",
)


def _sanitize_text(value: str) -> str:
    value = _URL_QUERY_PATTERN.sub(r"\g<base>?[redacted]", value)
    value = _SECRET_ASSIGNMENT_PATTERN.sub(
        r"\1\2[redacted]",
        value,
    )
    return _BEARER_PATTERN.sub("Bearer [redacted]", value)


def _sanitize_value(value: Any, *, field_name: str | None = None) -> Any:
    normalized_name = (field_name or "").lower().replace("-", "_")
    if normalized_name and any(
        marker in normalized_name for marker in _SECRET_KEY_MARKERS
    ):
        return "[redacted]"
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        return {
            str(key): _sanitize_value(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    """One-line JSON logs with a stable set of correlation fields."""

    def __init__(self, process_name: str | None = None) -> None:
        super().__init__()
        self.process_name = process_name

    def format(self, record: logging.LogRecord) -> str:
        error_type = getattr(record, "error_type", None)
        if error_type is None and record.exc_info and record.exc_info[0]:
            error_type = record.exc_info[0].__name__

        payload: dict[str, Any] = {
            "timestamp": dt.datetime.now(dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _sanitize_text(record.getMessage()),
            "process": getattr(record, "process_name", self.process_name),
        }
        for field in STABLE_CONTEXT_FIELDS:
            if field == "error_type":
                value = error_type
            elif field == "trace_id":
                value = getattr(
                    record,
                    field,
                    getattr(record, "root_execution_id", None),
                )
            elif field == "duration_ms":
                value = getattr(
                    record,
                    field,
                    getattr(record, "elapsed_ms", None),
                )
            elif field == "error_code":
                value = getattr(
                    record,
                    field,
                    getattr(record, "failure_code", None),
                )
            elif field == "provider_request_id":
                value = getattr(
                    record,
                    field,
                    getattr(record, "provider_event_id", None),
                )
            elif field == "provider":
                value = getattr(
                    record,
                    field,
                    getattr(record, "translation_provider", None),
                )
            else:
                value = getattr(record, field, None)
            payload[field] = _sanitize_value(value, field_name=field)
        if record.exc_info:
            payload["exception"] = _sanitize_text(
                self.formatException(record.exc_info)
            )
        for field in (
            *METRIC_FIELDS,
            *ASR_EVENT_FIELDS,
            *TRANSLATION_FIELDS,
            *INTERNAL_DIAGNOSTIC_FIELDS,
            *ASSISTANT_LIFECYCLE_FIELDS,
        ):
            if hasattr(record, field):
                payload[field] = _sanitize_value(
                    getattr(record, field),
                    field_name=field,
                )
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(process_name: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(process_name))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    logging.getLogger(__name__).info(
        "logging configured",
        extra={"process_name": process_name, "event": "logging_configured"},
    )


def log_assistant_lifecycle(
    event: str,
    *,
    session_id: str,
    execution_id: str,
    root_execution_id: str,
    status: str,
    elapsed_ms: int,
    **fields: object,
) -> None:
    """Emit content-free, correlation-safe Assistant lifecycle telemetry."""

    logging.getLogger("app.assistant.lifecycle").info(
        "Assistant lifecycle event",
        extra={
            "event": event,
            "trace_id": root_execution_id,
            "session_id": session_id,
            "execution_id": execution_id,
            "root_execution_id": root_execution_id,
            "phase": fields.pop("phase", status),
            "status": status,
            "duration_ms": max(0, elapsed_ms),
            "elapsed_ms": max(0, elapsed_ms),
            **fields,
        },
    )


__all__ = [
    "JsonFormatter",
    "configure_logging",
    "log_assistant_lifecycle",
]
