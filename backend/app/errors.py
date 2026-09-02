from __future__ import annotations

from typing import Literal, TypeAlias


ErrorCategory: TypeAlias = Literal[
    "configuration_error",
    "media_decode_error",
    "livekit_error",
    "asr_auth_error",
    "asr_stream_error",
    "persistence_error",
    "deepseek_error",
    "export_error",
]

ERROR_CATEGORIES: set[ErrorCategory] = {
    "configuration_error",
    "media_decode_error",
    "livekit_error",
    "asr_auth_error",
    "asr_stream_error",
    "persistence_error",
    "deepseek_error",
    "export_error",
}

PUBLIC_ERROR_MESSAGES: dict[ErrorCategory, str] = {
    "configuration_error": "The application configuration is incomplete.",
    "media_decode_error": "The audio file could not be decoded.",
    "livekit_error": "The realtime media connection failed.",
    "asr_auth_error": "Speech recognition authentication failed.",
    "asr_stream_error": "Speech recognition stopped before completion.",
    "persistence_error": "Session data could not be saved.",
    "deepseek_error": "Processed script generation failed.",
    "export_error": "Transcript export failed.",
}


class CategorizedError(Exception):
    category: ErrorCategory


class ConfigurationError(CategorizedError):
    category: ErrorCategory = "configuration_error"


class MediaDecodeError(CategorizedError):
    category: ErrorCategory = "media_decode_error"


class LiveKitOperationError(CategorizedError):
    category: ErrorCategory = "livekit_error"


def error_category(
    error: BaseException,
    *,
    default: ErrorCategory,
) -> ErrorCategory:
    if isinstance(error, CategorizedError):
        return error.category

    from sqlalchemy.exc import SQLAlchemyError

    from app.transcription.errors import ASRAuthenticationError, ASRError

    if isinstance(error, ASRAuthenticationError):
        return "asr_auth_error"
    if isinstance(error, ASRError):
        return "asr_stream_error"
    if isinstance(error, SQLAlchemyError):
        return "persistence_error"
    return default


def public_error_for(
    error: BaseException,
    *,
    default: ErrorCategory = "configuration_error",
) -> tuple[ErrorCategory, str]:
    category = error_category(error, default=default)
    return category, PUBLIC_ERROR_MESSAGES[category]
