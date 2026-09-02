from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from livekit import rtc


CAPTION_INPUT_TRACK_PREFIX = "caption-input-"


@dataclass(frozen=True)
class CaptionTrackTarget:
    session_id: str
    is_legacy_replay: bool


@dataclass
class AudioFrameStats:
    frame_count: int = 0
    audio_bytes: int = 0
    sample_rate: int | None = None
    channels: int | None = None
    samples_per_channel: int | None = None
    audio_duration_seconds: float = 0.0
    received_started_at: dt.datetime | None = None
    received_ended_at: dt.datetime | None = None

    def observe(
        self,
        frame: Any,
        *,
        received_at: dt.datetime | None = None,
    ) -> None:
        observed_at = received_at or dt.datetime.now(dt.UTC)
        if self.received_started_at is None:
            self.received_started_at = observed_at
        self.received_ended_at = observed_at
        self.frame_count += 1
        frame_data = getattr(frame, "data", None)
        self.audio_bytes += (
            len(frame_data)
            if frame_data is not None
            else frame.samples_per_channel * frame.num_channels * 2
        )
        self.sample_rate = frame.sample_rate
        self.channels = frame.num_channels
        self.samples_per_channel = frame.samples_per_channel
        self.audio_duration_seconds += frame.samples_per_channel / frame.sample_rate

    def log_fields(self) -> dict[str, object]:
        return {
            "frame_count": self.frame_count,
            "audio_bytes": self.audio_bytes,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "samples_per_channel": self.samples_per_channel,
            "audio_duration_seconds": round(self.audio_duration_seconds, 6),
            "received_started_at": (
                self.received_started_at.isoformat()
                if self.received_started_at is not None
                else None
            ),
            "received_ended_at": (
                self.received_ended_at.isoformat()
                if self.received_ended_at is not None
                else None
            ),
        }


def is_target_replay_track(participant: Any, publication: Any) -> bool:
    return (
        participant.identity.startswith("replay-")
        and publication.name == "replay-audio"
        and publication.kind == rtc.TrackKind.KIND_AUDIO
    )


def resolve_caption_track_target(
    participant: Any,
    publication: Any,
) -> CaptionTrackTarget | None:
    if publication.kind != rtc.TrackKind.KIND_AUDIO:
        return None
    if (
        participant.identity.startswith("replay-")
        and publication.name == "replay-audio"
    ):
        return CaptionTrackTarget(
            session_id=participant.identity.removeprefix("replay-"),
            is_legacy_replay=True,
        )
    if not publication.name.startswith(CAPTION_INPUT_TRACK_PREFIX):
        return None
    session_id = publication.name.removeprefix(CAPTION_INPUT_TRACK_PREFIX)
    try:
        parsed = uuid.UUID(session_id)
    except (ValueError, AttributeError):
        return None
    if str(parsed) != session_id.lower():
        return None
    return CaptionTrackTarget(
        session_id=session_id,
        is_legacy_replay=False,
    )


def is_target_caption_track(participant: Any, publication: Any) -> bool:
    return resolve_caption_track_target(participant, publication) is not None
