from __future__ import annotations

import datetime as dt

import pytest
from livekit import rtc

from app.worker.audio_stats import (
    AudioFrameStats,
    is_target_caption_track,
    is_target_replay_track,
    resolve_caption_track_target,
)


class FakeFrame:
    def __init__(
        self,
        *,
        sample_rate: int,
        num_channels: int,
        samples_per_channel: int,
    ) -> None:
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.samples_per_channel = samples_per_channel


class FakeParticipant:
    def __init__(self, identity: str) -> None:
        self.identity = identity


class FakePublication:
    def __init__(self, name: str, kind: int) -> None:
        self.name = name
        self.kind = kind


def test_audio_frame_stats_accumulates_required_metrics() -> None:
    stats = AudioFrameStats()
    started = dt.datetime(2026, 7, 22, 6, 0, tzinfo=dt.UTC)
    ended = started + dt.timedelta(milliseconds=20)

    stats.observe(
        FakeFrame(sample_rate=16_000, num_channels=1, samples_per_channel=320),
        received_at=started,
    )
    stats.observe(
        FakeFrame(sample_rate=16_000, num_channels=1, samples_per_channel=320),
        received_at=ended,
    )

    assert stats.frame_count == 2
    assert stats.sample_rate == 16_000
    assert stats.channels == 1
    assert stats.samples_per_channel == 320
    assert stats.audio_duration_seconds == pytest.approx(0.04)
    assert stats.received_started_at == started
    assert stats.received_ended_at == ended


def test_only_replay_audio_track_is_targeted() -> None:
    target = FakePublication("replay-audio", rtc.TrackKind.KIND_AUDIO)
    wrong_name = FakePublication("microphone", rtc.TrackKind.KIND_AUDIO)
    wrong_kind = FakePublication("replay-audio", rtc.TrackKind.KIND_VIDEO)

    assert is_target_replay_track(FakeParticipant("replay-session-id"), target)
    assert not is_target_replay_track(FakeParticipant("browser-alice"), target)
    assert not is_target_replay_track(FakeParticipant("replay-session-id"), wrong_name)
    assert not is_target_replay_track(FakeParticipant("replay-session-id"), wrong_kind)


def test_caption_track_resolver_accepts_browser_inputs_and_legacy_replays() -> None:
    session_id = "1c745f13-926f-41b9-a140-ecc0f3c2065a"
    browser_target = FakePublication(
        f"caption-input-{session_id}",
        rtc.TrackKind.KIND_AUDIO,
    )
    replay_target = FakePublication("replay-audio", rtc.TrackKind.KIND_AUDIO)

    resolved_browser = resolve_caption_track_target(
        FakeParticipant("operator-alice"),
        browser_target,
    )
    resolved_replay = resolve_caption_track_target(
        FakeParticipant("replay-legacy-session"),
        replay_target,
    )

    assert resolved_browser is not None
    assert resolved_browser.session_id == session_id
    assert resolved_browser.is_legacy_replay is False
    assert resolved_replay is not None
    assert resolved_replay.session_id == "legacy-session"
    assert resolved_replay.is_legacy_replay is True
    assert is_target_caption_track(
        FakeParticipant("operator-alice"),
        browser_target,
    )


def test_caption_track_resolver_rejects_invalid_ids_and_non_audio_tracks() -> None:
    assert (
        resolve_caption_track_target(
            FakeParticipant("operator-alice"),
            FakePublication(
                "caption-input-not-a-uuid",
                rtc.TrackKind.KIND_AUDIO,
            ),
        )
        is None
    )
    assert (
        resolve_caption_track_target(
            FakeParticipant("operator-alice"),
            FakePublication(
                "caption-input-1c745f13-926f-41b9-a140-ecc0f3c2065a",
                rtc.TrackKind.KIND_VIDEO,
            ),
        )
        is None
    )
