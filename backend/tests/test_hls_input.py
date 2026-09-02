from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from app.replay.decoder import PCMFrame


def test_hls_url_policy_accepts_public_and_rejects_unsafe_targets() -> None:
    from app.hls.url_policy import RemoteMediaUrlValidator

    async def resolver(host: str, _port: int) -> tuple[str, ...]:
        return {
            "media.example": ("93.184.216.34",),
            "stream.internal": ("192.168.1.20",),
        }[host]

    async def no_redirect(
        _url: str,
        _connect_timeout: float,
        _read_timeout: float,
    ) -> None:
        return None

    validator = RemoteMediaUrlValidator(
        resolver=resolver,
        redirect_probe=no_redirect,
    )
    accepted = asyncio.run(
        validator.validate(
            "https://media.example/live/index.m3u8?token=secret#fragment",
        )
    )
    assert accepted.allowed is True
    assert accepted.normalized_url is not None
    assert accepted.normalized_url.endswith("?token=secret")
    assert accepted.display_url == "https://media.example/live/index.m3u8"
    assert accepted.resolved_host == "media.example"
    assert accepted.resolved_ips == ("93.184.216.34",)

    unsafe_cases = {
        "file:///tmp/local.m3u8": "unsupported_scheme",
        "ftp://media.example/live.m3u8": "unsupported_scheme",
        "data:text/plain,media": "unsupported_scheme",
        "http://localhost/live.m3u8": "localhost_not_allowed",
        "http://127.0.0.1/live.m3u8": "non_public_address",
        "http://10.0.0.8/live.m3u8": "non_public_address",
        "http://192.168.1.20/live.m3u8": "non_public_address",
        "http://169.254.169.254/latest/meta-data": "non_public_address",
        "http://stream.internal/live.m3u8": "non_public_address",
    }
    for url, expected_reason in unsafe_cases.items():
        decision = asyncio.run(validator.validate(url))
        assert decision.allowed is False
        assert decision.rejection_reason == expected_reason


def test_hls_url_policy_revalidates_redirects_and_limits_hops() -> None:
    from app.hls.url_policy import RemoteMediaUrlValidator

    async def resolver(host: str, _port: int) -> tuple[str, ...]:
        return {
            "public.example": ("93.184.216.34",),
            "private.example": ("172.16.0.5",),
        }[host]

    async def redirects_to_private(
        url: str,
        _connect_timeout: float,
        _read_timeout: float,
    ) -> str | None:
        if url == "https://public.example/start":
            return "http://private.example/live.m3u8"
        return None

    redirect_rejected = asyncio.run(
        RemoteMediaUrlValidator(
            resolver=resolver,
            redirect_probe=redirects_to_private,
        ).validate("https://public.example/start")
    )
    assert redirect_rejected.allowed is False
    assert redirect_rejected.rejection_reason == "non_public_address"
    assert redirect_rejected.redirect_count == 1

    hop_limited = asyncio.run(
        RemoteMediaUrlValidator(
            resolver=resolver,
            redirect_probe=redirects_to_private,
            max_redirects=0,
        ).validate("https://public.example/start")
    )
    assert hop_limited.allowed is False
    assert hop_limited.rejection_reason == "redirect_limit_exceeded"


class QueueReader:
    def __init__(self, *chunks: bytes) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        for chunk in chunks:
            self._queue.put_nowait(chunk)

    async def read(self, _size: int = -1) -> bytes:
        chunk = await self._queue.get()
        return b"" if chunk is None else chunk

    def feed_eof(self) -> None:
        self._queue.put_nowait(None)


class FakeProcess:
    def __init__(self, stdout: QueueReader, stderr: QueueReader) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_count = 0
        self._done = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.terminate()

    async def wait(self) -> int:
        self.wait_count += 1
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode


def test_hls_decoder_streams_pcm_and_reaps_its_exact_process() -> None:
    from app.hls.decoder import FFmpegHLSDecoder

    async def scenario() -> None:
        stdout = QueueReader(b"\x01\x00" * 320)
        stderr = QueueReader()
        process = FakeProcess(stdout, stderr)
        invocation: dict[str, Any] = {}

        async def process_factory(*args: str, **kwargs: Any) -> FakeProcess:
            invocation["args"] = args
            invocation["kwargs"] = kwargs
            return process

        url = (
            "https://media.example/live/index.m3u8"
            "?token=secret;$(ignored)&pipe=|whoami"
        )
        decoder = FFmpegHLSDecoder(
            url,
            process_factory=process_factory,
            first_frame_timeout_seconds=0.2,
            read_timeout_seconds=0.2,
            stop_timeout_seconds=0.2,
        )
        frames = decoder.frames()
        frame = await frames.__anext__()
        assert frame.sample_rate == 16_000
        assert frame.channels == 1
        assert frame.samples_per_channel == 320
        assert frame.data == b"\x01\x00" * 320

        await decoder.aclose()
        await decoder.aclose()
        await frames.aclose()

        assert invocation["args"].count(url) == 1
        assert invocation["args"][invocation["args"].index("-i") + 1] == url
        assert "shell" not in invocation["kwargs"]
        assert "-re" not in invocation["args"]
        assert invocation["kwargs"]["stdout"] == asyncio.subprocess.PIPE
        assert process.terminated is True
        assert process.killed is False
        assert process.wait_count >= 1

    asyncio.run(scenario())


def test_hls_decoder_stops_cleanly_at_maximum_duration() -> None:
    from app.hls.decoder import FFmpegHLSDecoder

    async def scenario() -> None:
        now = [0.0]
        process = FakeProcess(
            QueueReader(b"\x01\x00" * 640),
            QueueReader(),
        )

        async def process_factory(*_args: str, **_kwargs: Any) -> FakeProcess:
            return process

        decoder = FFmpegHLSDecoder(
            "https://media.example/live.m3u8",
            process_factory=process_factory,
            first_frame_timeout_seconds=0.2,
            read_timeout_seconds=0.2,
            stop_timeout_seconds=0.2,
            max_duration_seconds=1.0,
            clock=lambda: now[0],
        )
        frames = decoder.frames()
        first = await frames.__anext__()
        assert first.samples_per_channel == 320
        now[0] = 1.0
        with pytest.raises(StopAsyncIteration):
            await frames.__anext__()
        assert process.terminated is True
        assert process.killed is False

    asyncio.run(scenario())


class FailingDecoder:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = False

    async def frames(self) -> AsyncIterator[PCMFrame]:
        yield PCMFrame(
            data=b"\x00\x00" * 320,
            sample_rate=16_000,
            channels=1,
            samples_per_channel=320,
        )
        from app.hls.decoder import HLSDecodeError

        raise HLSDecodeError("upstream failed")

    async def aclose(self) -> None:
        self.closed = True
        self.events.append("decoder_closed")


class FakePublication:
    sid = "TR_hls"

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def wait_for_subscription(self) -> None:
        self.events.append("subscribed")


class FakeLocalParticipant:
    def __init__(self, events: list[str], identity: str) -> None:
        self.events = events
        self.identity = identity
        self.track_name: str | None = None

    async def publish_track(
        self,
        track: object,
        _options: object,
    ) -> FakePublication:
        self.track_name = str(track)
        self.events.append("published")
        return FakePublication(self.events)

    async def unpublish_track(self, _sid: str) -> None:
        self.events.append("unpublished")


class AlreadyUnpublishedParticipant(FakeLocalParticipant):
    async def unpublish_track(self, _sid: str) -> None:
        self.events.append("unpublish_already_absent")
        raise RuntimeError("track already unpublished")


class FakeRoom:
    def __init__(self, events: list[str], identity: str) -> None:
        self.events = events
        self.local_participant = FakeLocalParticipant(events, identity)
        self.name = "managed-room"

    async def connect(self, _url: str, _token: str) -> None:
        self.events.append("connected")

    async def disconnect(self) -> None:
        self.events.append("disconnected")


class AlreadyReleasedRoom(FakeRoom):
    def __init__(self, events: list[str], identity: str) -> None:
        super().__init__(events, identity)
        self.local_participant = AlreadyUnpublishedParticipant(events, identity)

    async def disconnect(self) -> None:
        self.events.append("disconnect_already_absent")
        raise RuntimeError("room already disconnected")


class FakeAudioSource:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def capture_frame(self, _frame: object) -> None:
        self.events.append("captured")

    async def wait_for_playout(self) -> None:
        self.events.append("playout_completed")

    def clear_queue(self) -> None:
        self.events.append("queue_cleared")

    async def aclose(self) -> None:
        self.events.append("audio_closed")


class FakeReplayClock:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def wait_for_frame(self, duration_seconds: float) -> None:
        self.events.append(f"paced:{duration_seconds:.2f}")

    async def wait_until_complete(self) -> None:
        self.events.append("timeline_completed")


class SuccessfulDecoder:
    async def frames(self) -> AsyncIterator[PCMFrame]:
        for _ in range(2):
            yield PCMFrame(
                data=b"\x00\x00" * 320,
                sample_rate=16_000,
                channels=1,
                samples_per_channel=320,
            )

    async def aclose(self) -> None:
        return None


def test_hls_source_paces_frames_and_waits_for_playout_before_cleanup() -> None:
    from app.hls.source import HLSLiveSource

    async def scenario() -> None:
        events: list[str] = []
        room = FakeRoom(events, "hls-session-1")
        source = HLSLiveSource(
            session_id="session-1",
            decoder=SuccessfulDecoder(),  # type: ignore[arg-type]
            room_factory=lambda: room,
            audio_source_factory=lambda **_: FakeAudioSource(events),
            track_factory=lambda name, _audio: name,
            replay_clock=FakeReplayClock(events),
        )

        async def unexpected_failure(_error: BaseException) -> None:
            raise AssertionError("successful source must not report failure")

        await source.run(
            "ws://livekit.test",
            "token",
            on_source_failed=unexpected_failure,
        )

        assert events.count("paced:0.02") == 2
        first_pace = events.index("paced:0.02")
        first_capture = events.index("captured")
        assert first_pace < first_capture
        assert events.index("timeline_completed") < events.index(
            "playout_completed"
        )
        assert events.index("playout_completed") < events.index("unpublished")
        assert "queue_cleared" not in events

    asyncio.run(scenario())


def test_hls_source_reports_failure_before_unpublishing_track() -> None:
    from app.hls.decoder import HLSDecodeError
    from app.hls.source import HLSLiveSource

    async def scenario() -> None:
        session_id = "6616ff0c-a510-4a03-983b-b79f7c29f948"
        events: list[str] = []
        room = FakeRoom(events, f"hls-{session_id}")
        decoder = FailingDecoder(events)
        source = HLSLiveSource(
            session_id=session_id,
            decoder=decoder,
            room_factory=lambda: room,
            audio_source_factory=lambda **_: FakeAudioSource(events),
            track_factory=lambda name, _audio: name,
        )

        async def on_failure(_error: BaseException) -> None:
            events.append("failure_reported")

        with pytest.raises(HLSDecodeError, match="upstream failed"):
            await source.run(
                "ws://livekit.test",
                "token",
                on_source_failed=on_failure,
            )

        assert room.local_participant.track_name == f"caption-input-{session_id}"
        assert events.index("captured") < events.index("failure_reported")
        assert events.index("failure_reported") < events.index("unpublished")
        assert decoder.closed is True

    asyncio.run(scenario())


def test_hls_source_treats_already_released_track_and_room_as_clean() -> None:
    from app.hls.source import HLSLiveSource

    async def scenario() -> None:
        events: list[str] = []
        room = AlreadyReleasedRoom(events, "hls-session-1")
        source = HLSLiveSource(
            session_id="session-1",
            decoder=SuccessfulDecoder(),  # type: ignore[arg-type]
            room_factory=lambda: room,
            audio_source_factory=lambda **_: FakeAudioSource(events),
            track_factory=lambda name, _audio: name,
            replay_clock=FakeReplayClock(events),
        )

        await source.run(
            "ws://livekit.test",
            "token",
            on_source_failed=lambda _error: asyncio.sleep(0),
        )

        assert source.cleanup_errors == ()
        assert "track=already_stopped" in source.cleanup_detail
        assert "room=already_stopped" in source.cleanup_detail
        assert "audio_closed" in events

    asyncio.run(scenario())


@dataclass
class LongRunningSource:
    started: asyncio.Event
    stopped: asyncio.Event
    closed: bool = False

    async def run(
        self,
        _url: str,
        _token: str,
        *,
        on_source_failed: Any,
    ) -> None:
        self.started.set()
        await self.stopped.wait()

    async def aclose(self) -> None:
        self.closed = True
        self.stopped.set()


def test_hls_manager_stops_and_removes_long_running_task() -> None:
    from app.hls.manager import HLSInputManager, HLSStartRequest

    async def scenario() -> None:
        started = asyncio.Event()
        stopped = asyncio.Event()
        source = LongRunningSource(started, stopped)
        request = HLSStartRequest(
            session_id="6616ff0c-a510-4a03-983b-b79f7c29f948",
            room_id="80d501ff-f412-4834-91d8-75348fdc9669",
            room_name="managed-room",
            fetch_url="https://media.example/live/index.m3u8",
        )
        manager = HLSInputManager(
            livekit_url="ws://livekit.test",
            token_factory=lambda _request: "token",
            source_factory=lambda _request: source,
            stop_timeout_seconds=0.2,
        )

        await manager.start(request)
        await asyncio.wait_for(started.wait(), timeout=0.2)
        first = await manager.stop(request.session_id, graceful=True)
        second = await manager.stop(
            request.session_id,
            graceful=False,
            force=True,
        )
        assert first.outcome == "stopped"
        assert first.cleanup_status == "completed"
        assert second.outcome == "already_stopped"
        assert source.closed is True
        assert manager.active_session_ids == ()

    asyncio.run(scenario())
