from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

import pytest

from app.transcription.errors import ASRBackpressureError, ASRProviderError
from app.transcription.models import ASREvent, ASREventType
from app.transcription.provider import SpeechRecognitionProvider
from app.transcription.session import TranscriptionSession


_EVENTS_DONE = object()


class FakeProvider(SpeechRecognitionProvider):
    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        send_error: Exception | None = None,
        send_gate: asyncio.Event | None = None,
    ) -> None:
        self.start_error = start_error
        self.send_error = send_error
        self.send_gate = send_gate
        self.audio: list[bytes] = []
        self.started = False
        self.finished = False
        self.closed = False
        self.events_queue: asyncio.Queue[ASREvent | object] = asyncio.Queue()
        self._result_emitted = False

    async def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True
        await self.events_queue.put(
            ASREvent(
                event_type=ASREventType.STREAM_STARTED,
                provider_event_id="started",
            )
        )

    async def send_audio(self, pcm: bytes) -> None:
        if self.send_gate is not None:
            await self.send_gate.wait()
        if self.send_error is not None:
            raise self.send_error
        self.audio.append(pcm)
        if not self._result_emitted:
            self._result_emitted = True
            await self.events_queue.put(
                ASREvent(
                    event_type=ASREventType.PARTIAL_RESULT,
                    provider_event_id="partial",
                    segment_id="1",
                    text="测试",
                )
            )
            await self.events_queue.put(
                ASREvent(
                    event_type=ASREventType.FINAL_RESULT,
                    provider_event_id="final",
                    segment_id="1",
                    text="测试完成",
                )
            )

    async def finish(self) -> None:
        self.finished = True
        await self.events_queue.put(
            ASREvent(
                event_type=ASREventType.STREAM_COMPLETED,
                provider_event_id="completed",
            )
        )
        await self.events_queue.put(_EVENTS_DONE)

    async def events(self) -> AsyncIterator[ASREvent]:
        while True:
            event = await self.events_queue.get()
            if event is _EVENTS_DONE:
                return
            assert isinstance(event, ASREvent)
            yield event

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.events_queue.put(_EVENTS_DONE)


def frame(value: int = 1) -> bytes:
    return bytes((value, 0)) * 320


def provider_factory(
    providers: list[FakeProvider],
) -> Callable[[], SpeechRecognitionProvider]:
    pending = iter(providers)

    def create() -> SpeechRecognitionProvider:
        return next(pending)

    return create


def test_normal_flow_aggregates_frames_emits_metrics_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        session = TranscriptionSession(
            provider_factory=provider_factory([provider]),
            log_context={"session_id": "session-1", "room_name": "room-1"},
        )

        await session.start()
        for _ in range(10):
            session.send_frame(frame())
        metrics = await session.finish()
        await session.aclose()

        assert provider.audio == [frame() * 5, frame() * 5]
        assert provider.finished is True
        assert provider.closed is True
        assert metrics.final_result_count == 1
        assert metrics.first_partial_latency_ms is not None
        assert metrics.average_final_latency_ms is not None
        assert metrics.provider_error_count == 0
        assert metrics.sent_audio_chunk_count == 2
        assert metrics.sent_audio_bytes == 6_400

    with caplog.at_level(logging.INFO, logger="app.transcription.session"):
        asyncio.run(scenario())

    messages = [record.getMessage() for record in caplog.records]
    assert "[partial] 测试" in messages
    assert "[final] 测试完成" in messages
    assert messages.count("asr session summary") == 1


def test_metrics_start_with_audio_and_ignore_blank_results() -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        provider._result_emitted = True
        clock_values = iter((5_000.0, 5_180.0, 5_900.0, 6_050.0))
        session = TranscriptionSession(
            provider_factory=provider_factory([provider]),
            clock_ms=lambda: next(clock_values),
        )
        await session.start()
        session.send_frame(frame() * 5)
        await asyncio.sleep(0)
        await provider.events_queue.put(
            ASREvent(
                event_type=ASREventType.FINAL_RESULT,
                provider_event_id="blank-final",
                segment_id="1",
                text=" ",
                end_time_ms=600,
            )
        )
        await provider.events_queue.put(
            ASREvent(
                event_type=ASREventType.PARTIAL_RESULT,
                provider_event_id="meaningful-partial",
                segment_id="2",
                text="hello",
            )
        )
        await provider.events_queue.put(
            ASREvent(
                event_type=ASREventType.FINAL_RESULT,
                provider_event_id="meaningful-final",
                segment_id="2",
                text="hello world",
                end_time_ms=900,
            )
        )
        await session.finish()
        await session.aclose()

        assert session.metrics.final_result_count == 1
        assert session.metrics.first_partial_latency_ms == 900.0
        assert session.metrics.average_final_latency_ms == 150.0

    asyncio.run(scenario())


def test_provider_payload_logging_is_opt_in(
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = ASREvent(
        event_type=ASREventType.STREAM_STARTED,
        provider_event_id="started",
        raw_payload={"transcript": "private"},
    )
    session = TranscriptionSession(
        provider_factory=lambda: FakeProvider(),
    )
    enabled = TranscriptionSession(
        provider_factory=lambda: FakeProvider(),
        log_provider_payloads=True,
    )

    with caplog.at_level(logging.DEBUG, logger="app.transcription.session"):
        session._log_event(event)
        assert not any(
            record.getMessage() == "asr provider payload"
            for record in caplog.records
        )
        enabled._log_event(event)

    payload_records = [
        record
        for record in caplog.records
        if record.getMessage() == "asr provider payload"
    ]
    assert len(payload_records) == 1


def test_finish_flushes_short_aligned_tail() -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        session = TranscriptionSession(
            provider_factory=provider_factory([provider]),
        )
        await session.start()
        session.send_frame(frame(2))

        await session.finish()
        await session.aclose()

        assert provider.audio == [frame(2)]
        assert session.metrics.sent_audio_chunk_count == 1

    asyncio.run(scenario())


def test_queue_full_raises_without_silent_drop() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        provider = FakeProvider(send_gate=gate)
        session = TranscriptionSession(
            provider_factory=provider_factory([provider]),
            queue_max_chunks=1,
        )
        await session.start()

        for _ in range(5):
            session.send_frame(frame(1))
        await asyncio.sleep(0)
        for _ in range(5):
            session.send_frame(frame(2))

        with pytest.raises(ASRBackpressureError, match="queue"):
            for _ in range(5):
                session.send_frame(frame(3))

        gate.set()
        await session.aclose()
        assert provider.closed is True

    asyncio.run(scenario())


def test_start_retries_once_with_a_new_provider() -> None:
    async def scenario() -> None:
        first = FakeProvider(start_error=ASRProviderError("temporary connect failure"))
        second = FakeProvider()
        session = TranscriptionSession(
            provider_factory=provider_factory([first, second]),
            startup_retries=1,
        )

        await session.start()
        session.send_frame(frame())
        await session.finish()
        await session.aclose()

        assert first.closed is True
        assert second.started is True
        assert second.closed is True
        assert session.metrics.provider_error_count == 1

    asyncio.run(scenario())


def test_send_failure_is_not_retried_after_audio_delivery_begins() -> None:
    async def scenario() -> None:
        created = 0
        provider = FakeProvider(send_error=ASRProviderError("network failed"))

        def create() -> SpeechRecognitionProvider:
            nonlocal created
            created += 1
            return provider

        session = TranscriptionSession(provider_factory=create, startup_retries=1)
        await session.start()
        for _ in range(5):
            session.send_frame(frame())

        with pytest.raises(ASRProviderError, match="network failed"):
            await session.finish()
        await session.aclose()

        assert created == 1
        assert session.metrics.provider_error_count == 1
        assert provider.closed is True

    asyncio.run(scenario())


def test_finish_does_not_hang_when_sender_failed_with_a_full_queue() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        provider = FakeProvider(
            send_error=ASRProviderError("send failed"),
            send_gate=gate,
        )
        session = TranscriptionSession(
            provider_factory=provider_factory([provider]),
            queue_max_chunks=1,
        )
        await session.start()
        for _ in range(5):
            session.send_frame(frame(1))
        await asyncio.sleep(0)
        for _ in range(5):
            session.send_frame(frame(2))
        gate.set()
        await asyncio.sleep(0)

        with pytest.raises(ASRProviderError, match="send failed"):
            await asyncio.wait_for(session.finish(), timeout=0.1)
        await session.aclose()

    asyncio.run(scenario())


def test_aclose_cancels_background_tasks_without_leaks() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        provider = FakeProvider(send_gate=gate)
        session = TranscriptionSession(
            provider_factory=provider_factory([provider]),
        )
        await session.start()
        for _ in range(5):
            session.send_frame(frame())
        await asyncio.sleep(0)

        await session.aclose()
        await session.aclose()

        active_names = {
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        }
        assert not any(name.startswith("transcription-") for name in active_names)
        assert provider.closed is True

    asyncio.run(scenario())


def test_event_handler_receives_every_normalized_event_after_metrics_update() -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        handled: list[tuple[ASREventType, int]] = []

        async def handle_event(event: ASREvent) -> None:
            handled.append((event.event_type, session.metrics.final_result_count))

        session = TranscriptionSession(
            provider_factory=provider_factory([provider]),
            event_handler=handle_event,
        )
        await session.start()
        for _ in range(5):
            session.send_frame(frame())

        await session.finish()
        await session.aclose()

        assert handled == [
            (ASREventType.STREAM_STARTED, 0),
            (ASREventType.PARTIAL_RESULT, 0),
            (ASREventType.FINAL_RESULT, 1),
            (ASREventType.STREAM_COMPLETED, 1),
        ]

    asyncio.run(scenario())


def test_event_handler_failure_is_explicit_and_not_counted_as_provider_error() -> None:
    async def scenario() -> None:
        provider = FakeProvider()

        async def handle_event(event: ASREvent) -> None:
            if event.event_type is ASREventType.PARTIAL_RESULT:
                raise RuntimeError("caption handler failed")

        session = TranscriptionSession(
            provider_factory=provider_factory([provider]),
            event_handler=handle_event,
        )
        await session.start()
        for _ in range(5):
            session.send_frame(frame())

        with pytest.raises(RuntimeError, match="caption handler failed"):
            await session.finish()
        await session.aclose()

        assert session.metrics.first_partial_latency_ms is not None
        assert session.metrics.provider_error_count == 0
        assert provider.closed is True

    asyncio.run(scenario())


def test_aclose_cancels_a_blocked_event_handler() -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        handler_started = asyncio.Event()
        handler_cancelled = asyncio.Event()

        async def handle_event(_event: ASREvent) -> None:
            handler_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                handler_cancelled.set()
                raise

        session = TranscriptionSession(
            provider_factory=provider_factory([provider]),
            event_handler=handle_event,
        )
        await session.start()
        await handler_started.wait()
        await session.aclose()

        assert handler_cancelled.is_set()
        assert provider.closed is True

    asyncio.run(scenario())
