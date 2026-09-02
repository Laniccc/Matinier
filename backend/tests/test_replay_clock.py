from __future__ import annotations

import asyncio

import pytest

from app.replay.clock import ReplayClock


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_replay_clock_tracks_total_audio_time() -> None:
    async def scenario() -> tuple[ReplayClock, FakeClock]:
        fake = FakeClock()
        replay_clock = ReplayClock(clock=fake)
        for _ in range(5):
            await replay_clock.wait_for_frame(0.02)
        await replay_clock.wait_until_complete()
        return replay_clock, fake

    replay_clock, fake = asyncio.run(scenario())

    assert replay_clock.monotonic_started_at == 0.0
    assert replay_clock.audio_elapsed == pytest.approx(0.1)
    assert fake.now == pytest.approx(0.1)


def test_replay_clock_compensates_processing_delay() -> None:
    async def scenario() -> tuple[ReplayClock, FakeClock]:
        fake = FakeClock()
        replay_clock = ReplayClock(clock=fake)
        await replay_clock.wait_for_frame(0.02)
        fake.advance(0.03)
        await replay_clock.wait_for_frame(0.02)
        await replay_clock.wait_for_frame(0.02)
        await replay_clock.wait_until_complete()
        return replay_clock, fake

    replay_clock, fake = asyncio.run(scenario())

    assert replay_clock.audio_elapsed == pytest.approx(0.06)
    assert fake.now == pytest.approx(0.06)
    assert fake.sleeps == pytest.approx([0.01, 0.02])
