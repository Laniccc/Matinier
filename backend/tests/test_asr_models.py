from __future__ import annotations

import asyncio

from app.transcription.errors import (
    ASRAuthenticationError,
    ASRBackpressureError,
    ASRConfigurationError,
    ASRProtocolError,
    ASRProviderError,
    ASRStateError,
    ASRTimeoutError,
)
from app.transcription.models import ASREvent, ASREventType, TranscriptionMetrics
from app.transcription.provider import SpeechRecognitionProvider


def test_final_event_normalizes_is_final_and_preserves_taskbook_fields() -> None:
    event = ASREvent(
        event_type=ASREventType.FINAL_RESULT,
        provider_event_id="event-1",
        segment_id="7",
        text="你好，世界",
        is_final=False,
        begin_time_ms=120,
        end_time_ms=820,
        confidence=0.93,
        raw_payload={"sentence_end": True},
        received_at_ms=1_234_567,
    )

    assert event.is_final is True
    assert event.provider_event_id == "event-1"
    assert event.segment_id == "7"
    assert event.text == "你好，世界"
    assert event.begin_time_ms == 120
    assert event.end_time_ms == 820
    assert event.confidence == 0.93
    assert event.raw_payload == {"sentence_end": True}
    assert event.received_at_ms == 1_234_567


def test_partial_event_cannot_be_marked_final() -> None:
    event = ASREvent(
        event_type=ASREventType.PARTIAL_RESULT,
        provider_event_id="event-2",
        is_final=True,
    )

    assert event.is_final is False


def test_metrics_calculate_latency_counts_and_audio_totals() -> None:
    metrics = TranscriptionMetrics(started_at_ms=1_000.0)
    metrics.observe(
        ASREvent(
            event_type=ASREventType.PARTIAL_RESULT,
            provider_event_id="partial",
            text="draft",
        ),
        observed_at_ms=1_125.0,
    )
    metrics.observe(
        ASREvent(
            event_type=ASREventType.FINAL_RESULT,
            provider_event_id="final-1",
            text="one",
        ),
        observed_at_ms=1_300.0,
    )
    metrics.observe(
        ASREvent(
            event_type=ASREventType.FINAL_RESULT,
            provider_event_id="final-2",
            text="two",
        ),
        observed_at_ms=1_500.0,
    )
    metrics.observe(
        ASREvent(
            event_type=ASREventType.STREAM_ERROR,
            provider_event_id="error",
        ),
        observed_at_ms=1_600.0,
    )
    metrics.mark_audio_sent(3_200)
    metrics.mark_audio_sent(640)

    assert metrics.final_result_count == 2
    assert metrics.first_partial_latency_ms == 125.0
    assert metrics.average_final_latency_ms == 400.0
    assert metrics.provider_error_count == 1
    assert metrics.sent_audio_chunk_count == 2
    assert metrics.sent_audio_bytes == 3_840
    assert metrics.log_fields() == {
        "final_result_count": 2,
        "first_partial_latency_ms": 125.0,
        "average_final_latency_ms": 400.0,
        "provider_error_count": 1,
        "sent_audio_chunk_count": 2,
        "sent_audio_bytes": 3_840,
    }


def test_asr_errors_have_stable_codes() -> None:
    expected = {
        ASRConfigurationError: "configuration_error",
        ASRAuthenticationError: "authentication_error",
        ASRTimeoutError: "timeout_error",
        ASRProtocolError: "protocol_error",
        ASRProviderError: "provider_error",
        ASRBackpressureError: "backpressure_error",
        ASRStateError: "state_error",
    }

    for error_type, code in expected.items():
        assert error_type("message").code == code


def test_provider_contract_is_abstract() -> None:
    try:
        SpeechRecognitionProvider()
    except TypeError:
        pass
    else:
        raise AssertionError("abstract provider must not be instantiable")

    assert asyncio.iscoroutinefunction(SpeechRecognitionProvider.start)
    assert asyncio.iscoroutinefunction(SpeechRecognitionProvider.send_audio)
    assert asyncio.iscoroutinefunction(SpeechRecognitionProvider.finish)
    assert asyncio.iscoroutinefunction(SpeechRecognitionProvider.aclose)
