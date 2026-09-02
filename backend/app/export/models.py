from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

from app.persistence.models import SessionRecord
from app.sessions.state import TERMINAL_SESSION_STATES
from app.timeline.models import Timeline


@dataclass(frozen=True, slots=True)
class ExportDocument:
    session: SessionRecord
    timeline: Timeline
    content: Literal["source", "translation"] = "source"
    exported_at: dt.datetime = field(
        default_factory=lambda: dt.datetime.now(dt.UTC)
    )

    @property
    def partial_export(self) -> bool:
        return self.session.status not in TERMINAL_SESSION_STATES


def format_timestamp(milliseconds: int, *, separator: str = ".") -> str:
    if milliseconds < 0:
        raise ValueError("timestamp must be non-negative")
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def normalize_caption_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def one_line(value: str) -> str:
    return " ".join(value.replace("\r", "\n").splitlines())
