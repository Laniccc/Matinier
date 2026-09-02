from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from livekit import rtc

from app.errors import LiveKitOperationError
from app.replay.clock import ReplayClock
from app.replay.decoder import FFmpegPCMDecoder


logger = logging.getLogger(__name__)
TRACK_NAME = "replay-audio"


class ReplayLiveKitError(LiveKitOperationError):
    """Raised when Replay cannot establish or use its LiveKit connection."""


@dataclass(frozen=True)
class ReplayResult:
    participant_identity: str
    track_name: str
    frame_count: int
    audio_duration_seconds: float
    playback_elapsed_seconds: float


class ReplaySource:
    """Decode and publish a fixed audio file at its real-time cadence."""

    def __init__(
        self,
        input_path: str | Path,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
        frame_duration_ms: int = 20,
        connect_timeout: float = 15.0,
        subscription_timeout: float = 15.0,
        ffmpeg_start_timeout: float = 10.0,
        ffmpeg_path: str = "ffmpeg",
        track_name: str = TRACK_NAME,
        decoder: Any | None = None,
        replay_clock: Any | None = None,
        room_factory: Callable[[], Any] = rtc.Room,
        audio_source_factory: Callable[..., Any] = rtc.AudioSource,
        track_factory: Callable[[str, Any], Any] = rtc.LocalAudioTrack.create_audio_track,
    ) -> None:
        self.input_path = Path(input_path)
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_duration_ms = frame_duration_ms
        if not track_name:
            raise ValueError("track_name is required")
        self.track_name = track_name
        if connect_timeout <= 0:
            raise ValueError("LiveKit connect timeout must be positive")
        self.connect_timeout = connect_timeout
        self.subscription_timeout = subscription_timeout
        self.decoder = decoder or FFmpegPCMDecoder(
            self.input_path,
            sample_rate=sample_rate,
            channels=channels,
            frame_duration_ms=frame_duration_ms,
            ffmpeg_path=ffmpeg_path,
            start_timeout_seconds=ffmpeg_start_timeout,
        )
        self.replay_clock = replay_clock or ReplayClock()
        self.room_factory = room_factory
        self.audio_source_factory = audio_source_factory
        self.track_factory = track_factory

    async def run(self, livekit_url: str, token: str) -> ReplayResult:
        room = self.room_factory()
        audio_source: Any | None = None
        publication: Any | None = None
        connected = False
        connect_attempted = False
        playout_completed = False
        frame_count = 0
        playback_started_at: float | None = None

        try:
            connect_attempted = True
            try:
                await asyncio.wait_for(
                    room.connect(livekit_url, token),
                    timeout=self.connect_timeout,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError as error:
                raise ReplayLiveKitError(
                    "LiveKit connection timed out"
                ) from error
            except BaseException as error:
                raise ReplayLiveKitError(
                    "LiveKit connection failed"
                ) from error
            connected = True
            participant_identity = room.local_participant.identity
            session_id = self._session_id_from_identity(participant_identity)

            audio_source = self.audio_source_factory(
                sample_rate=self.sample_rate,
                num_channels=self.channels,
                queue_size_ms=max(100, self.frame_duration_ms * 5),
            )
            track = self.track_factory(self.track_name, audio_source)
            publish_options = rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_MICROPHONE,
            )
            publication = await room.local_participant.publish_track(
                track,
                publish_options,
            )
            try:
                await asyncio.wait_for(
                    publication.wait_for_subscription(),
                    timeout=self.subscription_timeout,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError as error:
                raise ReplayLiveKitError(
                    "LiveKit track subscription timed out"
                ) from error
            except BaseException as error:
                raise ReplayLiveKitError(
                    "LiveKit track subscription failed"
                ) from error
            logger.info(
                "replay track subscribed",
                extra={
                    "process_name": "replay",
                    "session_id": session_id,
                    "room_name": getattr(room, "name", None),
                    "participant_identity": participant_identity,
                    "event": "replay_track_subscribed",
                },
            )

            playback_started_at = time.monotonic()
            async for pcm_frame in self.decoder.frames():
                await self.replay_clock.wait_for_frame(pcm_frame.duration_seconds)
                await audio_source.capture_frame(
                    rtc.AudioFrame(
                        data=pcm_frame.data,
                        sample_rate=pcm_frame.sample_rate,
                        num_channels=pcm_frame.channels,
                        samples_per_channel=pcm_frame.samples_per_channel,
                    )
                )
                frame_count += 1

            await self.replay_clock.wait_until_complete()
            await audio_source.wait_for_playout()
            playout_completed = True
            playback_elapsed_seconds = time.monotonic() - playback_started_at
            result = ReplayResult(
                participant_identity=participant_identity,
                track_name=self.track_name,
                frame_count=frame_count,
                audio_duration_seconds=self.replay_clock.audio_elapsed,
                playback_elapsed_seconds=playback_elapsed_seconds,
            )
            logger.info(
                "replay audio completed",
                extra={
                    "process_name": "replay",
                    "session_id": session_id,
                    "room_name": getattr(room, "name", None),
                    "participant_identity": participant_identity,
                    "event": "replay_audio_sent",
                    "audio_duration_seconds": self.replay_clock.audio_elapsed,
                    "playback_elapsed_seconds": playback_elapsed_seconds,
                },
            )
            return result
        finally:
            try:
                if audio_source is not None and not playout_completed:
                    audio_source.clear_queue()
            finally:
                try:
                    if publication is not None:
                        await room.local_participant.unpublish_track(publication.sid)
                finally:
                    try:
                        if audio_source is not None:
                            await audio_source.aclose()
                    finally:
                        if connected or connect_attempted:
                            await room.disconnect()

    @staticmethod
    def _session_id_from_identity(identity: str) -> str | None:
        if identity.startswith("replay-"):
            return identity.removeprefix("replay-")
        return None
