from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.errors import MediaDecodeError


class FFmpegDecodeError(MediaDecodeError):
    """Raised when FFmpeg cannot decode the requested audio input."""


@dataclass(frozen=True)
class PCMFrame:
    data: bytes
    sample_rate: int
    channels: int
    samples_per_channel: int

    @property
    def duration_seconds(self) -> float:
        return self.samples_per_channel / self.sample_rate


class FFmpegPCMDecoder:
    """Stream WAV/MP3 input as fixed-size little-endian PCM16 frames."""

    supported_suffixes = {".wav", ".mp3"}

    def __init__(
        self,
        input_path: str | Path,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
        frame_duration_ms: int = 20,
        ffmpeg_path: str = "ffmpeg",
        start_timeout_seconds: float = 10.0,
        process_factory: Callable[..., Any] = asyncio.create_subprocess_exec,
    ) -> None:
        self.input_path = Path(input_path)
        if self.input_path.suffix.lower() not in self.supported_suffixes:
            raise ValueError("Replay input must be a WAV or MP3 file")
        if sample_rate <= 0 or channels <= 0 or frame_duration_ms <= 0:
            raise ValueError("Audio format values must be positive")
        if (sample_rate * frame_duration_ms) % 1000:
            raise ValueError("Frame duration must produce a whole number of samples")
        if start_timeout_seconds <= 0:
            raise ValueError("FFmpeg start timeout must be positive")

        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_duration_ms = frame_duration_ms
        self.ffmpeg_path = ffmpeg_path
        self.start_timeout_seconds = start_timeout_seconds
        self._process_factory = process_factory
        self.samples_per_frame = sample_rate * frame_duration_ms // 1000
        self.bytes_per_sample_frame = channels * 2
        self.bytes_per_frame = self.samples_per_frame * self.bytes_per_sample_frame
        self._process: asyncio.subprocess.Process | None = None

    @property
    def active_process(self) -> asyncio.subprocess.Process | None:
        if self._process is not None and self._process.returncode is None:
            return self._process
        return None

    async def frames(self) -> AsyncIterator[PCMFrame]:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = await asyncio.wait_for(
                self._process_factory(
                    self.ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(self.input_path),
                    "-vn",
                    "-ac",
                    str(self.channels),
                    "-ar",
                    str(self.sample_rate),
                    "-f",
                    "s16le",
                    "pipe:1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=creationflags,
                ),
                timeout=self.start_timeout_seconds,
            )
        except TimeoutError as error:
            raise FFmpegDecodeError("FFmpeg start timed out") from error
        self._process = process
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())
        buffer = bytearray()

        try:
            while chunk := await process.stdout.read(64 * 1024):
                buffer.extend(chunk)
                while len(buffer) >= self.bytes_per_frame:
                    data = bytes(buffer[: self.bytes_per_frame])
                    del buffer[: self.bytes_per_frame]
                    yield self._to_frame(data)

            usable_bytes = len(buffer) - (len(buffer) % self.bytes_per_sample_frame)
            if usable_bytes:
                yield self._to_frame(bytes(buffer[:usable_bytes]))

            return_code = await process.wait()
            stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
            if return_code != 0:
                detail = stderr or f"exit code {return_code}"
                raise FFmpegDecodeError(f"FFmpeg failed: {detail}")
        finally:
            await self._reap_process(process)
            if not stderr_task.done():
                await stderr_task
            self._process = None

    def _to_frame(self, data: bytes) -> PCMFrame:
        return PCMFrame(
            data=data,
            sample_rate=self.sample_rate,
            channels=self.channels,
            samples_per_channel=len(data) // self.bytes_per_sample_frame,
        )

    @staticmethod
    async def _reap_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return

        try:
            process.terminate()
        except ProcessLookupError:
            pass

        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
