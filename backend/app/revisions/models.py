from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


RevisionStatus = Literal["saved", "approved", "superseded"]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RevisionItem(FrozenModel):
    item_id: str = Field(min_length=1)
    source_segment_ids: tuple[str, ...] = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str = Field(min_length=1)

    @field_validator("item_id")
    @classmethod
    def require_uuid_item_id(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except ValueError as error:
            raise ValueError("revision item ID must be a UUID") from error

    @field_validator("text")
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must contain visible text")
        return value

    @field_validator("source_segment_ids")
    @classmethod
    def require_unique_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("source segment IDs must not be blank")
        if len(set(value)) != len(value):
            raise ValueError("source segment IDs must be unique within an item")
        return value

    @model_validator(mode="after")
    def validate_timing(self) -> RevisionItem:
        if self.end_ms < self.start_ms:
            raise ValueError("revision item end must not precede start")
        return self


class TranscriptRevisionContent(FrozenModel):
    items: tuple[RevisionItem, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_items(self) -> TranscriptRevisionContent:
        item_ids = [item.item_id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("revision item IDs must be unique")
        ordered = sorted(
            self.items,
            key=lambda item: (item.start_ms, item.end_ms),
        )
        if list(self.items) != ordered:
            raise ValueError("revision items must be sorted by time")
        return self


class TranscriptRevision(FrozenModel):
    revision_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    parent_revision_id: str | None
    base_package_id: str = Field(min_length=1)
    language: str = Field(min_length=2, max_length=32)
    content: TranscriptRevisionContent
    content_hash: Digest
    change_summary: str | None
    status: RevisionStatus
    created_at: dt.datetime
    approved_at: dt.datetime | None
