"""Vendor-neutral real-time speech recognition primitives."""

from app.transcription.errors import (
    ASRAuthenticationError,
    ASRBackpressureError,
    ASRConfigurationError,
    ASRError,
    ASRProtocolError,
    ASRProviderError,
    ASRStateError,
    ASRTimeoutError,
)
from app.transcription.models import ASREvent, ASREventType, TranscriptionMetrics
from app.transcription.provider import SpeechRecognitionProvider

__all__ = [
    "ASRAuthenticationError",
    "ASRBackpressureError",
    "ASRConfigurationError",
    "ASRError",
    "ASREvent",
    "ASREventType",
    "ASRProtocolError",
    "ASRProviderError",
    "ASRStateError",
    "ASRTimeoutError",
    "SpeechRecognitionProvider",
    "TranscriptionMetrics",
]
