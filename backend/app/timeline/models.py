from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TimelineSegment:
    id: str
    session_id: str
    segment_id: str
    track_id: str
    revision: int
    language: str
    raw_text: str
    display_text: str
    audio_start_ms: int
    audio_end_ms: int
    confidence: float | None
    received_at_ms: int
    finalized_at: dt.datetime
    created_at: dt.datetime
    updated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class Timeline:
    segments: tuple[TimelineSegment, ...]
    duration_ms: int
