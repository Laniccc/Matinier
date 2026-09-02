from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.media.contracts import MEDIA_EVENT_FAMILIES, MediaEvent, MediaSession


def make_event(**overrides: object) -> MediaEvent:
    values: dict[str, object] = {
        "schema_version": 1,
        "event_id": "evt-1",
        "session_id": "media-1",
        "sequence": 1,
        "event_type": "transcript.final",
        "media_time_ms": 1_000,
        "duration_ms": 500,
        "logical_id": "segment:1",
        "revision": 2,
        "finality": "final",
        "source": "host.transcription",
        "payload": {"text": "hello", "language": "en-US"},
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    return MediaEvent.model_validate(values)


def test_media_event_accepts_only_frozen_event_families_and_syntax() -> None:
    assert {
        "session",
        "playback",
        "transcript",
        "translation",
        "timeline",
        "audio",
        "video",
        "ocr",
        "chat",
        "user",
    } == MEDIA_EVENT_FAMILIES
    assert make_event().event_type == "transcript.final"

    for invalid in ("caption.final", "Transcript.final", "transcript", "transcript..final"):
        with pytest.raises(ValidationError):
            make_event(event_type=invalid)


def test_media_event_sequence_and_revision_are_positive() -> None:
    with pytest.raises(ValidationError):
        make_event(sequence=0)
    with pytest.raises(ValidationError):
        make_event(revision=0)


def test_media_event_requires_logical_id_when_revision_is_present() -> None:
    with pytest.raises(ValidationError):
        make_event(logical_id=None, revision=1)

    event = make_event(
        event_type="session.completed",
        logical_id=None,
        revision=None,
        finality="derived",
    )
    assert event.logical_id is None


def test_media_event_rejects_oversized_non_json_binary_and_sensitive_payloads() -> None:
    with pytest.raises(ValidationError):
        make_event(payload={"text": "x" * 70_000})
    with pytest.raises(ValidationError):
        make_event(payload={"audio": b"not-json"})
    for key in ("api_key", "accessToken", "password", "client-secret"):
        with pytest.raises(ValidationError):
            make_event(payload={key: "should-not-cross-the-boundary"})


def test_media_event_normalizes_aware_timestamps_to_utc_and_rejects_naive() -> None:
    event = make_event(
        created_at=datetime(2026, 8, 27, 8, tzinfo=timezone(timedelta(hours=8)))
    )
    assert event.created_at == datetime(2026, 8, 27, 0, tzinfo=UTC)
    assert event.created_at.tzinfo is UTC

    with pytest.raises(ValidationError):
        make_event(created_at=datetime(2026, 8, 27, 0))


def test_media_event_round_trip_is_stable_and_model_is_frozen() -> None:
    event = make_event()
    restored = MediaEvent.model_validate_json(event.model_dump_json())
    assert restored == event
    with pytest.raises(ValidationError):
        event.sequence = 2  # type: ignore[misc]


def test_media_session_is_generic_and_track_kinds_are_normalized() -> None:
    session = MediaSession(
        session_id="media-1",
        mode="live",
        source_kind="browser-tab",
        status="active",
        tracks=["transcript", "audio", "transcript"],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert session.tracks == ("audio", "transcript")

