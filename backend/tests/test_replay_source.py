from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.replay.decoder import PCMFrame
from app.replay.source import ReplayLiveKitError, ReplaySource


def _frame(value: int = 0) -> PCMFrame:
    return PCMFrame(
        data=bytes([value, 0]) * 320,
        sample_rate=16_000,
        channels=1,
        samples_per_channel=320,
    )


class FakeDecoder:
    def __init__(self, *, block_after_first: bool = False) -> None:
        self.block_after_first = block_after_first

    async def frames(self):
        yield _frame(1)
        if self.block_after_first:
            await asyncio.Event().wait()
        yield _frame(2)


class FakeReplayClock:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.audio_elapsed = 0.0
        self.monotonic_started_at = 10.0

    async def wait_for_frame(self, duration: float) -> float:
        self.events.append("pace")
        self.audio_elapsed += duration
        return self.monotonic_started_at + self.audio_elapsed

    async def wait_until_complete(self) -> None:
        self.events.append("clock_complete")


class FakePublication:
    sid = "TR_replay"

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def wait_for_subscription(self) -> None:
        self.events.append("wait_subscription")


class FakeLocalParticipant:
    identity = "replay-session-id"

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.published_track: Any = None
        self.publish_options: Any = None

    async def publish_track(self, track: Any, options: Any) -> FakePublication:
        self.events.append("publish")
        self.published_track = track
        self.publish_options = options
        return FakePublication(self.events)

    async def unpublish_track(self, track_sid: str) -> None:
        assert track_sid == "TR_replay"
        self.events.append("unpublish")


class FakeRoom:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.local_participant = FakeLocalParticipant(events)

    async def connect(self, url: str, token: str) -> None:
        assert url == "ws://livekit.test"
        assert token == "replay-token"
        self.events.append("connect")

    async def disconnect(self) -> None:
        self.events.append("disconnect")


class BlockingConnectRoom(FakeRoom):
    async def connect(self, url: str, token: str) -> None:
        self.events.append("connect_started")
        await asyncio.Future()


class FakeAudioSource:
    def __init__(self, events: list[str], captured: asyncio.Event | None = None) -> None:
        self.events = events
        self.frames: list[Any] = []
        self.captured = captured

    async def capture_frame(self, frame: Any) -> None:
        self.frames.append(frame)
        self.events.append("capture")
        if self.captured is not None:
            self.captured.set()

    async def wait_for_playout(self) -> None:
        self.events.append("wait_playout")

    def clear_queue(self) -> None:
        self.events.append("clear_queue")

    async def aclose(self) -> None:
        self.events.append("audio_close")


def _make_source(
    *,
    decoder: FakeDecoder,
    events: list[str],
    captured: asyncio.Event | None = None,
) -> tuple[ReplaySource, FakeRoom, FakeAudioSource, dict[str, Any]]:
    room = FakeRoom(events)
    audio_source = FakeAudioSource(events, captured)
    track_details: dict[str, Any] = {}

    def create_track(name: str, source: Any) -> object:
        track_details.update(name=name, source=source)
        return object()

    replay = ReplaySource(
        Path("demo_audio.wav"),
        decoder=decoder,
        replay_clock=FakeReplayClock(events),
        room_factory=lambda: room,
        audio_source_factory=lambda **_: audio_source,
        track_factory=create_track,
        subscription_timeout=1.0,
    )
    return replay, room, audio_source, track_details


def test_replay_source_publishes_paced_audio_and_cleans_up() -> None:
    events: list[str] = []
    replay, room, audio_source, track_details = _make_source(
        decoder=FakeDecoder(),
        events=events,
    )

    result = asyncio.run(replay.run("ws://livekit.test", "replay-token"))

    assert result.participant_identity == "replay-session-id"
    assert result.track_name == "replay-audio"
    assert result.frame_count == 2
    assert result.audio_duration_seconds == pytest.approx(0.04)
    assert result.playback_elapsed_seconds >= 0
    assert track_details == {"name": "replay-audio", "source": audio_source}
    assert len(audio_source.frames) == 2
    assert audio_source.frames[0].sample_rate == 16_000
    assert audio_source.frames[0].num_channels == 1
    assert audio_source.frames[0].samples_per_channel == 320
    assert events == [
        "connect",
        "publish",
        "wait_subscription",
        "pace",
        "capture",
        "pace",
        "capture",
        "clock_complete",
        "wait_playout",
        "unpublish",
        "audio_close",
        "disconnect",
    ]
    assert room.local_participant.publish_options.source != 0


def test_replay_source_cancellation_clears_queue_and_unpublishes() -> None:
    async def scenario() -> list[str]:
        events: list[str] = []
        captured = asyncio.Event()
        replay, _, _, _ = _make_source(
            decoder=FakeDecoder(block_after_first=True),
            events=events,
            captured=captured,
        )
        task = asyncio.create_task(replay.run("ws://livekit.test", "replay-token"))
        await captured.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return events

    events = asyncio.run(scenario())

    assert "wait_playout" not in events
    assert events[-4:] == ["clear_queue", "unpublish", "audio_close", "disconnect"]


def test_replay_livekit_connect_has_a_finite_timeout_and_disconnects() -> None:
    events: list[str] = []
    room = BlockingConnectRoom(events)
    replay = ReplaySource(
        Path("demo_audio.wav"),
        decoder=FakeDecoder(),
        replay_clock=FakeReplayClock(events),
        room_factory=lambda: room,
        audio_source_factory=lambda **_: FakeAudioSource(events),
        track_factory=lambda *_: object(),
        connect_timeout=0.01,
    )

    with pytest.raises(ReplayLiveKitError, match="timed out"):
        asyncio.run(replay.run("ws://livekit.test", "replay-token"))

    assert events == ["connect_started", "disconnect"]
