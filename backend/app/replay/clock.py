from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol


class Clock(Protocol):
    """Injectable monotonic time used to test replay pacing without real waits."""

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass
class ReplayClock:
    """Pace frames against an absolute audio timeline to avoid cumulative drift."""

    clock: Clock = field(default_factory=SystemClock)
    audio_elapsed: float = 0.0
    monotonic_started_at: float | None = None

    async def wait_for_frame(self, duration_seconds: float) -> float:
        if duration_seconds <= 0:
            raise ValueError("frame duration must be positive")

        if self.monotonic_started_at is None:
            self.monotonic_started_at = self.clock.monotonic()

        target_send_time = self.monotonic_started_at + self.audio_elapsed
        sleep_seconds = target_send_time - self.clock.monotonic()
        if sleep_seconds > 0:
            await self.clock.sleep(sleep_seconds)

        self.audio_elapsed += duration_seconds
        return target_send_time

    async def wait_until_complete(self) -> None:
        if self.monotonic_started_at is None:
            return

        target_send_time = self.monotonic_started_at + self.audio_elapsed
        sleep_seconds = target_send_time - self.clock.monotonic()
        if sleep_seconds > 0:
            await self.clock.sleep(sleep_seconds)

