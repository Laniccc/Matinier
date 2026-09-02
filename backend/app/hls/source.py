from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from livekit import rtc

from app.errors import LiveKitOperationError
from app.hls.decoder import FFmpegHLSDecoder
from app.replay.clock import ReplayClock
from app.worker.audio_stats import CAPTION_INPUT_TRACK_PREFIX


SourceFailureCallback = Callable[[BaseException], Awaitable[None]]
logger = logging.getLogger(__name__)


class HLSLiveKitError(LiveKitOperationError):
    """Raised when the HLS publisher cannot use its LiveKit connection."""


class HLSLiveSource:
    """Publish one decoded HLS audio stream into a managed LiveKit Room."""

    def __init__(
        self,
        *,
        session_id: str,
        decoder: FFmpegHLSDecoder,
        room_name: str | None = None,
        sample_rate: int = 16_000,
        channels: int = 1,
        frame_duration_ms: int = 20,
        connect_timeout_seconds: float = 15.0,
        subscription_timeout_seconds: float = 15.0,
        room_factory: Callable[[], Any] = rtc.Room,
        audio_source_factory: Callable[..., Any] = rtc.AudioSource,
        track_factory: Callable[[str, Any], Any] = (
            rtc.LocalAudioTrack.create_audio_track
        ),
        replay_clock: Any | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        if min(connect_timeout_seconds, subscription_timeout_seconds) <= 0:
            raise ValueError("LiveKit timeouts must be positive")
        self.session_id = session_id
        self.room_name = room_name
        self.track_name = f"{CAPTION_INPUT_TRACK_PREFIX}{session_id}"
        self.decoder = decoder
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_duration_ms = frame_duration_ms
        self.connect_timeout_seconds = connect_timeout_seconds
        self.subscription_timeout_seconds = subscription_timeout_seconds
        self.room_factory = room_factory
        self.audio_source_factory = audio_source_factory
        self.track_factory = track_factory
        self.replay_clock = replay_clock or ReplayClock()
        self._stop_requested = False
        self._cleanup_errors: list[str] = []
        self._cleanup_steps: list[str] = []
        self._room_connected = False
        self._track_published = False
        self._audio_frames = 0
        self._audio_bytes = 0
        self._last_event_at: dt.datetime | None = None
        self._started_at_ms = time.monotonic() * 1_000
        self._ffmpeg_started_logged = False

    @property
    def cleanup_errors(self) -> tuple[str, ...]:
        return tuple(self._cleanup_errors)

    @property
    def cleanup_detail(self) -> str:
        return ",".join(self._cleanup_steps) or "cleanup=not_started"

    def runtime_snapshot(self) -> dict[str, object]:
        process = self.decoder.active_process
        return {
            "room_connected": self._room_connected,
            "publisher_connected": self._room_connected,
            "track_published": self._track_published,
            "ffmpeg_running": (
                process is not None and process.returncode is None
            ),
            "audio_bytes": self._audio_bytes,
            "audio_frames": self._audio_frames,
            "last_event_at": self._last_event_at,
        }

    async def run(
        self,
        livekit_url: str,
        token: str,
        *,
        on_source_failed: SourceFailureCallback,
    ) -> None:
        room = self.room_factory()
        audio_source: Any | None = None
        publication: Any | None = None
        connect_attempted = False
        playout_completed = False
        try:
            connect_attempted = True
            try:
                await asyncio.wait_for(
                    room.connect(livekit_url, token),
                    timeout=self.connect_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError as error:
                raise HLSLiveKitError(
                    "LiveKit connection timed out"
                ) from error
            except BaseException as error:
                raise HLSLiveKitError(
                    "LiveKit connection failed"
                ) from error
            self._room_connected = True
            self._last_event_at = dt.datetime.now(dt.UTC)

            audio_source = self.audio_source_factory(
                sample_rate=self.sample_rate,
                num_channels=self.channels,
                queue_size_ms=max(100, self.frame_duration_ms * 5),
            )
            track = self.track_factory(self.track_name, audio_source)
            try:
                publication = await room.local_participant.publish_track(
                    track,
                    rtc.TrackPublishOptions(
                        source=rtc.TrackSource.SOURCE_MICROPHONE,
                    ),
                )
                await asyncio.wait_for(
                    publication.wait_for_subscription(),
                    timeout=self.subscription_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError as error:
                raise HLSLiveKitError(
                    "LiveKit track subscription timed out"
                ) from error
            except BaseException as error:
                raise HLSLiveKitError(
                    "LiveKit track publication failed"
                ) from error
            self._track_published = True
            self._last_event_at = dt.datetime.now(dt.UTC)
            logger.info(
                "HLS audio track published",
                extra=self._extra(
                    "track_published",
                    status="running",
                    track_sid=getattr(publication, "sid", None),
                ),
            )

            async for pcm_frame in self.decoder.frames():
                if not self._ffmpeg_started_logged:
                    self._ffmpeg_started_logged = True
                    logger.info(
                        "FFmpeg HLS decoder started",
                        extra=self._extra(
                            "ffmpeg_started",
                            status="running",
                            track_sid=getattr(publication, "sid", None),
                        ),
                    )
                await self.replay_clock.wait_for_frame(
                    pcm_frame.duration_seconds
                )
                await audio_source.capture_frame(
                    rtc.AudioFrame(
                        data=pcm_frame.data,
                        sample_rate=pcm_frame.sample_rate,
                        num_channels=pcm_frame.channels,
                        samples_per_channel=pcm_frame.samples_per_channel,
                    )
                )
                self._audio_frames += 1
                self._audio_bytes += len(pcm_frame.data)
                self._last_event_at = dt.datetime.now(dt.UTC)
            await self.replay_clock.wait_until_complete()
            await audio_source.wait_for_playout()
            playout_completed = True
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if not self._stop_requested:
                await on_source_failed(error)
            raise
        finally:
            await self._cleanup(
                room=room,
                audio_source=audio_source,
                publication=publication,
                connect_attempted=connect_attempted,
                playout_completed=playout_completed,
            )
            self._track_published = False
            self._room_connected = False
            self._last_event_at = dt.datetime.now(dt.UTC)

    def _extra(self, event: str, **fields: Any) -> dict[str, Any]:
        return {
            "process_name": "api",
            "session_id": self.session_id,
            "room_name": self.room_name,
            "participant_identity": f"hls-{self.session_id}",
            "source_type": "hls",
            "event": event,
            "elapsed_ms": round(
                time.monotonic() * 1_000 - self._started_at_ms,
                3,
            ),
            **fields,
        }

    async def aclose(self) -> None:
        self._stop_requested = True
        await self.decoder.aclose()

    async def _cleanup(
        self,
        *,
        room: Any,
        audio_source: Any | None,
        publication: Any | None,
        connect_attempted: bool,
        playout_completed: bool,
    ) -> None:
        await self._run_async_cleanup("decoder", self.decoder.aclose)
        if audio_source is not None and not playout_completed:
            self._run_sync_cleanup("queue", audio_source.clear_queue)
        elif audio_source is not None:
            self._cleanup_steps.append("queue=drained")
        else:
            self._cleanup_steps.append("queue=not_created")

        if publication is not None:
            await self._run_async_cleanup(
                "track",
                lambda: room.local_participant.unpublish_track(
                    publication.sid
                ),
                already_released_is_success=True,
            )
        else:
            self._cleanup_steps.append("track=not_published")

        if audio_source is not None:
            await self._run_async_cleanup("audio_source", audio_source.aclose)
        else:
            self._cleanup_steps.append("audio_source=not_created")

        if connect_attempted:
            await self._run_async_cleanup(
                "room",
                room.disconnect,
                already_released_is_success=True,
            )
            # Let Proactor/native LiveKit transports finish their close
            # callbacks before the Room can be collected.
            await asyncio.sleep(0)
        else:
            self._cleanup_steps.append("room=not_connected")

    async def _run_async_cleanup(
        self,
        name: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        already_released_is_success: bool = False,
    ) -> None:
        try:
            await operation()
            self._cleanup_steps.append(f"{name}=stopped")
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if already_released_is_success and self._is_already_released(error):
                self._cleanup_steps.append(f"{name}=already_stopped")
                return
            detail = f"{name}={type(error).__name__}"
            self._cleanup_steps.append(detail)
            self._cleanup_errors.append(detail)
            logger.error(
                "HLS cleanup step failed",
                extra={
                    "process_name": "api",
                    "session_id": self.session_id,
                    "event": "hls_cleanup_step_failed",
                    "cleanup_status": "failed",
                    "cleanup_step": name,
                    "internal_error_type": type(error).__name__,
                },
            )

    def _run_sync_cleanup(
        self,
        name: str,
        operation: Callable[[], Any],
    ) -> None:
        try:
            operation()
            self._cleanup_steps.append(f"{name}=cleared")
        except BaseException as error:
            detail = f"{name}={type(error).__name__}"
            self._cleanup_steps.append(detail)
            self._cleanup_errors.append(detail)
            logger.error(
                "HLS cleanup step failed",
                extra={
                    "process_name": "api",
                    "session_id": self.session_id,
                    "event": "hls_cleanup_step_failed",
                    "cleanup_status": "failed",
                    "cleanup_step": name,
                    "internal_error_type": type(error).__name__,
                },
            )

    @staticmethod
    def _is_already_released(error: BaseException) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "already",
                "not found",
                "not connected",
                "disconnected",
                "closed",
            )
        )
