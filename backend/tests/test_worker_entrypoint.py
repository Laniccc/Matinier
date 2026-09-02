from __future__ import annotations

import asyncio

import pytest
from livekit.agents import WorkerOptions

from app.worker import entrypoint


def test_worker_exposes_worker_options_and_entrypoint() -> None:
    assert isinstance(entrypoint.worker_options, WorkerOptions)
    assert callable(entrypoint.worker_entrypoint)
    assert entrypoint.worker_options.agent_name == "live-caption-agent"


def test_consume_replay_audio_closes_stream_and_returns_stats() -> None:
    class Frame:
        sample_rate = 16_000
        num_channels = 1
        samples_per_channel = 320
        data = b"\x01\x00" * 320

    class Event:
        frame = Frame()

    class FakeStream:
        def __init__(self, track, **kwargs) -> None:
            assert track == "remote-track"
            assert kwargs == {
                "sample_rate": 16_000,
                "num_channels": 1,
                "frame_size_ms": 20,
            }
            self.events = iter([Event(), Event()])
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.events)
            except StopIteration as error:
                raise StopAsyncIteration from error

        async def aclose(self) -> None:
            self.closed = True

    streams: list[FakeStream] = []
    operations: list[object] = []

    def stream_factory(*args, **kwargs) -> FakeStream:
        stream = FakeStream(*args, **kwargs)
        streams.append(stream)
        return stream

    class FakeTranscriptionSession:
        async def start(self) -> None:
            operations.append("start")

        def send_frame(self, pcm: bytes) -> None:
            operations.append(("send", pcm))

        async def finish(self) -> None:
            operations.append("finish")

        async def aclose(self) -> None:
            operations.append("close")

    def transcription_session_factory(**kwargs) -> FakeTranscriptionSession:
        assert kwargs == {
            "session_id": "session-id",
            "room_name": "room-1",
            "participant_identity": "replay-session-id",
            "event_handler": None,
        }
        return FakeTranscriptionSession()

    stats = asyncio.run(
        entrypoint.consume_replay_audio(
            track="remote-track",
            room_name="room-1",
            participant_identity="replay-session-id",
            stream_factory=stream_factory,
            transcription_session_factory=transcription_session_factory,
        )
    )

    assert stats.frame_count == 2
    assert stats.audio_duration_seconds == 0.04
    assert streams[0].closed is True
    assert operations == [
        "start",
        ("send", Frame.data),
        ("send", Frame.data),
        "finish",
        "close",
    ]


def test_consume_replay_audio_wires_runtime_progress_and_completion() -> None:
    class Frame:
        sample_rate = 16_000
        num_channels = 1
        samples_per_channel = 320
        data = b"\x01\x00" * 320

    class Event:
        frame = Frame()

    class FakeStream:
        def __init__(self) -> None:
            self.events = iter(Event() for _ in range(25))
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.events)
            except StopIteration as error:
                raise StopAsyncIteration from error

        async def aclose(self) -> None:
            self.closed = True

    operations: list[object] = []
    metrics = object()

    class FakeRuntime:
        async def start_replay(self) -> None:
            operations.append("replaying")

        async def handle_asr_event(self, event) -> None:
            operations.append(("event", event))

        async def publish_progress(self, audio_time_ms: int) -> None:
            operations.append(("progress", audio_time_ms))

        async def complete(self, completed_metrics) -> None:
            operations.append(("complete", completed_metrics))

        async def begin_finalizing(self) -> None:
            operations.append("finalizing")

        async def fail(self, error: BaseException) -> None:
            operations.append(("fail", type(error).__name__))

        async def cancel(self) -> None:
            operations.append("cancel")

        async def complete_source_cleanup(self) -> None:
            operations.append("source_cleanup")

        async def fail_source_cleanup(self, error: BaseException) -> None:
            operations.append(("source_cleanup_failed", type(error).__name__))

        async def aclose(self) -> None:
            operations.append("runtime_close")

    runtime = FakeRuntime()

    class FakeSession:
        async def start(self) -> None:
            operations.append("start")

        def send_frame(self, _pcm: bytes) -> None:
            pass

        async def finish(self):
            operations.append("finish")
            return metrics

        async def aclose(self) -> None:
            operations.append("session_close")

    def session_factory(**kwargs) -> FakeSession:
        assert kwargs["event_handler"] == runtime.handle_asr_event
        return FakeSession()

    stream = FakeStream()
    stats = asyncio.run(
        entrypoint.consume_replay_audio(
            track="track",
            room_name="room",
            participant_identity="replay-session",
            stream_factory=lambda *args, **kwargs: stream,
            transcription_session_factory=session_factory,
            caption_runtime=runtime,
        )
    )

    assert stats.frame_count == 25
    assert operations == [
        "replaying",
        "start",
        ("progress", 500),
        "finalizing",
        "finish",
            ("complete", metrics),
            "session_close",
            "source_cleanup",
            "runtime_close",
    ]


def test_consume_replay_audio_closes_both_resources_on_asr_failure() -> None:
    class Frame:
        sample_rate = 16_000
        num_channels = 1
        samples_per_channel = 320
        data = b"\x00\x00" * 320

    class Event:
        frame = Frame()

    class FakeStream:
        def __init__(self) -> None:
            self.yielded = False
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.yielded:
                self.yielded = True
                return Event()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    class FailingSession:
        def __init__(self) -> None:
            self.closed = False
            self.finished = False

        async def start(self) -> None:
            pass

        def send_frame(self, pcm: bytes) -> None:
            raise RuntimeError("asr failed")

        async def finish(self) -> None:
            self.finished = True

        async def aclose(self) -> None:
            self.closed = True

    stream = FakeStream()
    session = FailingSession()
    runtime_operations: list[object] = []
    translation_operations: list[object] = []
    abort_requests: list[tuple[str, str]] = []

    class FakeRuntime:
        source_type = "hls"

        async def start_replay(self) -> None:
            runtime_operations.append("replaying")

        async def handle_asr_event(self, _event) -> None:
            pass

        async def publish_progress(self, _audio_time_ms: int) -> None:
            pass

        async def complete(self, _metrics) -> None:
            runtime_operations.append("complete")

        async def begin_finalizing(self) -> None:
            runtime_operations.append("finalizing")

        async def fail(self, error: BaseException) -> None:
            runtime_operations.append(("fail", type(error).__name__))

        async def cancel(self) -> None:
            runtime_operations.append("cancel")

        async def complete_source_cleanup(self) -> None:
            return None

        async def fail_source_cleanup(self, _error: BaseException) -> None:
            return None

        async def aclose(self) -> None:
            runtime_operations.append("close")

    runtime = FakeRuntime()

    class FakeTranslationRuntime:
        source_language = "en-US"
        target_language = "zh-CN"

        async def start(self) -> None:
            translation_operations.append("runtime_start")

        async def handle_event(self, _event) -> None:
            pass

        async def fail(self, error: BaseException) -> None:
            translation_operations.append(("fail", type(error).__name__))

        async def aclose(self) -> None:
            translation_operations.append("runtime_close")

    class FakeTranslationSession:
        async def start(self) -> None:
            translation_operations.append("session_start")

        def send_frame(self, _pcm: bytes) -> None:
            pass

        async def aclose(self) -> None:
            translation_operations.append("session_close")

    translation_runtime = FakeTranslationRuntime()
    translation_session = FakeTranslationSession()

    async def abort_source(reason: str, detail: str) -> None:
        abort_requests.append((reason, detail))

    with pytest.raises(RuntimeError, match="asr failed"):
        asyncio.run(
            entrypoint.consume_replay_audio(
                track="track",
                room_name="room",
                participant_identity="replay-session",
                stream_factory=lambda *args, **kwargs: stream,
                transcription_session_factory=lambda **kwargs: session,
                caption_runtime=runtime,
                translation_session_factory=(
                    lambda **kwargs: translation_session
                ),
                translation_runtime=translation_runtime,
                source_abort_handler=abort_source,
            )
        )

    assert stream.closed is True
    assert session.closed is True
    assert session.finished is False
    assert runtime_operations == [
        "replaying",
        ("fail", "RuntimeError"),
        "close",
    ]
    assert abort_requests == [("asr_stream_error", "asr failed")]
    assert translation_operations == [
        "runtime_start",
        "session_start",
        ("fail", "RuntimeError"),
        "session_close",
        "runtime_close",
    ]


def test_consume_replay_audio_cancellation_closes_both_resources() -> None:
    async def scenario() -> None:
        iteration_started = asyncio.Event()

        class BlockingStream:
            def __init__(self) -> None:
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                iteration_started.set()
                await asyncio.Future()

            async def aclose(self) -> None:
                self.closed = True

        class FakeSession:
            def __init__(self) -> None:
                self.closed = False

            async def start(self) -> None:
                pass

            def send_frame(self, pcm: bytes) -> None:
                pass

            async def finish(self) -> None:
                pass

            async def aclose(self) -> None:
                self.closed = True

        stream = BlockingStream()
        session = FakeSession()
        runtime_operations: list[str] = []
        abort_requests: list[tuple[str, str]] = []

        class FakeRuntime:
            source_type = "hls"

            async def start_replay(self) -> None:
                runtime_operations.append("replaying")

            async def handle_asr_event(self, _event) -> None:
                pass

            async def publish_progress(self, _audio_time_ms: int) -> None:
                pass

            async def complete(self, _metrics) -> None:
                runtime_operations.append("complete")

            async def begin_finalizing(self) -> None:
                runtime_operations.append("finalizing")

            async def fail(self, _error: BaseException) -> None:
                runtime_operations.append("fail")

            async def cancel(self) -> None:
                runtime_operations.append("cancel")

            async def complete_source_cleanup(self) -> None:
                return None

            async def fail_source_cleanup(self, _error: BaseException) -> None:
                return None

            async def aclose(self) -> None:
                runtime_operations.append("close")

        runtime = FakeRuntime()

        async def abort_source(reason: str, detail: str) -> None:
            abort_requests.append((reason, detail))

        task = asyncio.create_task(
            entrypoint.consume_replay_audio(
                track="track",
                room_name="room",
                participant_identity="replay-session",
                stream_factory=lambda *args, **kwargs: stream,
                transcription_session_factory=lambda **kwargs: session,
                caption_runtime=runtime,
                source_abort_handler=abort_source,
            )
        )
        await iteration_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert stream.closed is True
        assert session.closed is True
        assert runtime_operations == ["replaying", "cancel", "close"]
        assert abort_requests == [("worker_cancelled", "CancelledError")]

    asyncio.run(scenario())


def test_consume_replay_audio_yields_to_bounded_asr_sender_for_buffered_frames() -> None:
    from app.transcription.models import ASREvent, ASREventType
    from app.transcription.session import TranscriptionSession

    events_done = object()

    class Frame:
        sample_rate = 16_000
        num_channels = 1
        samples_per_channel = 320
        data = b"\x01\x00" * 320

    class Event:
        frame = Frame()

    class BufferedStream:
        def __init__(self) -> None:
            self.events = iter(Event() for _ in range(105))
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.events)
            except StopIteration as error:
                raise StopAsyncIteration from error

        async def aclose(self) -> None:
            self.closed = True

    class SlowFakeProvider:
        def __init__(self) -> None:
            self.audio: list[bytes] = []
            self.closed = False
            self.event_queue: asyncio.Queue[object] = asyncio.Queue()

        async def start(self) -> None:
            await self.event_queue.put(
                ASREvent(
                    event_type=ASREventType.STREAM_STARTED,
                    provider_event_id="started",
                )
            )

        async def send_audio(self, pcm: bytes) -> None:
            await asyncio.sleep(0)
            self.audio.append(pcm)

        async def finish(self) -> None:
            await self.event_queue.put(
                ASREvent(
                    event_type=ASREventType.STREAM_COMPLETED,
                    provider_event_id="completed",
                )
            )
            await self.event_queue.put(events_done)

        async def events(self):
            while True:
                event = await self.event_queue.get()
                if event is events_done:
                    return
                yield event

        async def aclose(self) -> None:
            self.closed = True
            await self.event_queue.put(events_done)

    async def scenario() -> None:
        stream = BufferedStream()
        provider = SlowFakeProvider()
        session = TranscriptionSession(
            provider_factory=lambda: provider,
            queue_max_chunks=1,
        )

        stats = await entrypoint.consume_replay_audio(
            track="track",
            room_name="room",
            participant_identity="replay-buffered",
            stream_factory=lambda *args, **kwargs: stream,
            transcription_session_factory=lambda **kwargs: session,
        )

        assert stats.frame_count == 105
        assert len(provider.audio) == 21
        assert provider.closed is True
        assert stream.closed is True

    asyncio.run(scenario())


def test_consume_replay_audio_finishes_after_stop_and_drains_buffered_frames() -> None:
    async def scenario() -> None:
        class Frame:
            sample_rate = 16_000
            num_channels = 1
            samples_per_channel = 320
            data = b"\x01\x00" * 320

        class Event:
            frame = Frame()

        class BufferedThenBlockingStream:
            def __init__(self) -> None:
                self.events = iter([Event(), Event()])
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.events)
                except StopIteration:
                    await asyncio.Future()

            async def aclose(self) -> None:
                self.closed = True

        operations: list[object] = []

        class FakeSession:
            async def start(self) -> None:
                operations.append("start")

            def send_frame(self, pcm: bytes) -> None:
                operations.append(("send", pcm))

            async def finish(self) -> None:
                operations.append("finish")

            async def aclose(self) -> None:
                operations.append("close")

        stream = BufferedThenBlockingStream()
        stop_event = asyncio.Event()
        stop_event.set()
        stats = await asyncio.wait_for(
            entrypoint.consume_replay_audio(
                track="track",
                room_name="room",
                participant_identity="replay-session",
                stop_event=stop_event,
                stream_factory=lambda *args, **kwargs: stream,
                transcription_session_factory=lambda **kwargs: FakeSession(),
            ),
            timeout=0.2,
        )

        assert stats.frame_count == 2
        assert operations == [
            "start",
            ("send", Frame.data),
            ("send", Frame.data),
            "finish",
            "close",
        ]
        assert stream.closed is True

    asyncio.run(scenario())


def test_drain_track_tasks_allows_normal_finish_before_cancelling() -> None:
    async def scenario() -> None:
        completed = asyncio.Event()
        cancelled = False

        async def cooperative_task() -> None:
            nonlocal cancelled
            try:
                await asyncio.sleep(0)
                completed.set()
            except asyncio.CancelledError:
                cancelled = True
                raise

        task = asyncio.create_task(cooperative_task())
        await entrypoint.drain_track_tasks((task,), timeout_seconds=0.1)

        assert completed.is_set()
        assert cancelled is False
        assert task.done()

    asyncio.run(scenario())


def test_drain_track_tasks_cancels_after_finite_grace_period() -> None:
    async def scenario() -> None:
        cancelled = asyncio.Event()

        async def hanging_task() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(hanging_task())
        await asyncio.sleep(0)
        await entrypoint.drain_track_tasks((task,), timeout_seconds=0.01)

        assert cancelled.is_set()
        assert task.cancelled()

    asyncio.run(scenario())
