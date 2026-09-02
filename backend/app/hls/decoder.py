from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.errors import MediaDecodeError
from app.replay.decoder import PCMFrame


class HLSDecodeError(MediaDecodeError):
    """Raised when an HLS audio stream cannot be decoded."""

    code = "hls_stream_error"


class HLSStartTimeoutError(HLSDecodeError):
    code = "hls_start_timeout"


class HLSNoAudioError(HLSDecodeError):
    code = "hls_no_audio"


class FFmpegHLSDecoder:
    """Decode an HTTP(S) HLS stream into paced PCM16 audio frames."""

    def __init__(
        self,
        input_url: str,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
        frame_duration_ms: int = 20,
        ffmpeg_path: str = "ffmpeg",
        first_frame_timeout_seconds: float = 15.0,
        read_timeout_seconds: float = 20.0,
        stop_timeout_seconds: float = 5.0,
        max_duration_seconds: float = 14_400.0,
        process_factory: Callable[..., Any] = asyncio.create_subprocess_exec,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not input_url:
            raise ValueError("HLS input URL is required")
        if sample_rate <= 0 or channels <= 0 or frame_duration_ms <= 0:
            raise ValueError("Audio format values must be positive")
        if (sample_rate * frame_duration_ms) % 1000:
            raise ValueError("Frame duration must produce whole samples")
        if min(
            first_frame_timeout_seconds,
            read_timeout_seconds,
            stop_timeout_seconds,
            max_duration_seconds,
        ) <= 0:
            raise ValueError("HLS decoder timeouts and duration must be positive")

        self.input_url = input_url
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_duration_ms = frame_duration_ms
        self.ffmpeg_path = ffmpeg_path
        self.first_frame_timeout_seconds = first_frame_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.stop_timeout_seconds = stop_timeout_seconds
        self.max_duration_seconds = max_duration_seconds
        self._process_factory = process_factory
        self._clock = clock
        self.samples_per_frame = sample_rate * frame_duration_ms // 1000
        self.bytes_per_sample_frame = channels * 2
        self.bytes_per_frame = (
            self.samples_per_frame * self.bytes_per_sample_frame
        )
        self._process: asyncio.subprocess.Process | None = None
        self._closing = False
        self._reap_lock = asyncio.Lock()

    @property
    def active_process(self) -> asyncio.subprocess.Process | None:
        process = self._process
        if process is not None and process.returncode is None:
            return process
        return None

    async def frames(self) -> AsyncIterator[PCMFrame]:
        if self._closing:
            return
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        read_timeout_microseconds = int(self.read_timeout_seconds * 1_000_000)
        try:
            process = await asyncio.wait_for(
                self._process_factory(
                    self.ffmpeg_path,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-rw_timeout",
                    str(read_timeout_microseconds),
                    "-reconnect",
                    "1",
                    "-reconnect_streamed",
                    "1",
                    "-reconnect_delay_max",
                    "5",
                    "-i",
                    self.input_url,
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
                timeout=self.first_frame_timeout_seconds,
            )
        except TimeoutError as error:
            raise HLSStartTimeoutError("FFmpeg start timed out") from error

        self._process = process
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
        buffer = bytearray()
        emitted_frame = False
        duration_limit_reached = False
        started_at = self._clock()

        try:
            while not self._closing:
                remaining_duration = self.max_duration_seconds - (
                    self._clock() - started_at
                )
                if remaining_duration <= 0:
                    duration_limit_reached = True
                    break
                timeout = (
                    self.read_timeout_seconds
                    if emitted_frame
                    else self.first_frame_timeout_seconds
                )
                try:
                    chunk = await asyncio.wait_for(
                        process.stdout.read(64 * 1024),
                        timeout=min(timeout, remaining_duration),
                    )
                except TimeoutError as error:
                    if (
                        self._clock() - started_at
                        >= self.max_duration_seconds
                    ):
                        duration_limit_reached = True
                        break
                    if emitted_frame:
                        raise HLSDecodeError(
                            "HLS audio stream stopped producing data"
                        ) from error
                    raise HLSStartTimeoutError(
                        "HLS stream did not produce an audio frame in time"
                    ) from error
                if not chunk:
                    break
                buffer.extend(chunk)
                while len(buffer) >= self.bytes_per_frame:
                    if (
                        self._clock() - started_at
                        >= self.max_duration_seconds
                    ):
                        duration_limit_reached = True
                        break
                    data = bytes(buffer[: self.bytes_per_frame])
                    del buffer[: self.bytes_per_frame]
                    emitted_frame = True
                    yield self._to_frame(data)
                if duration_limit_reached:
                    break

            if not self._closing and not duration_limit_reached:
                usable_bytes = len(buffer) - (
                    len(buffer) % self.bytes_per_sample_frame
                )
                if usable_bytes:
                    emitted_frame = True
                    yield self._to_frame(bytes(buffer[:usable_bytes]))

                return_code = await process.wait()
                stderr = await stderr_task
                if return_code != 0:
                    if "does not contain any stream" in stderr.lower():
                        raise HLSNoAudioError(
                            "HLS playlist does not contain an audio stream"
                        )
                    raise HLSDecodeError(
                        "FFmpeg could not read the HLS audio stream"
                    )
                if not emitted_frame:
                    raise HLSNoAudioError(
                        "HLS playlist did not produce audio"
                    )
        finally:
            await self._reap_process(process)
            if not stderr_task.done():
                await stderr_task
            if self._process is process:
                self._process = None

    async def aclose(self) -> None:
        self._closing = True
        process = self._process
        if process is not None:
            await self._reap_process(process)

    def _to_frame(self, data: bytes) -> PCMFrame:
        return PCMFrame(
            data=data,
            sample_rate=self.sample_rate,
            channels=self.channels,
            samples_per_channel=len(data) // self.bytes_per_sample_frame,
        )

    async def _reap_process(self, process: asyncio.subprocess.Process) -> None:
        async with self._reap_lock:
            if process.returncode is not None:
                await process.wait()
                return
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self.stop_timeout_seconds,
                )
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    @staticmethod
    async def _drain_stderr(stream: asyncio.StreamReader) -> str:
        retained = bytearray()
        while chunk := await stream.read(4096):
            if len(retained) < 64 * 1024:
                remaining = 64 * 1024 - len(retained)
                retained.extend(chunk[:remaining])
        return retained.decode("utf-8", errors="replace").strip()
