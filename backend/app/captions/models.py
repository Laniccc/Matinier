from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CaptionStatus(StrEnum):
    DRAFT = "draft"
    FINAL = "final"


@dataclass(frozen=True, slots=True)
class CaptionEvent:
    """Provider-neutral caption revision sent to storage and browser clients."""

    session_id: str
    segment_id: str
    revision: int
    status: CaptionStatus
    text: str
    audio_start_ms: int | None
    audio_end_ms: int | None
    confidence: float | None
    provider_event_id: str
    received_at_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", CaptionStatus(self.status))
        if not self.session_id:
            raise ValueError("session_id is required")
        if not self.segment_id:
            raise ValueError("segment_id is required")
        if self.revision <= 0:
            raise ValueError("revision must be positive")

    @property
    def is_final(self) -> bool:
        return self.status is CaptionStatus.FINAL
