from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


StrictNonEmptyString = Annotated[str, Field(strict=True, min_length=1)]


class SourceSegmentSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_id: StrictNonEmptyString
    revision: int = Field(ge=1)
    language: StrictNonEmptyString
    raw_text: str = Field(strict=True)
    display_text: str = Field(strict=True)
    audio_start_ms: int = Field(ge=0)
    audio_end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_timing(self) -> SourceSegmentSnapshot:
        if self.audio_end_ms < self.audio_start_ms:
            raise ValueError("source segment end must not precede start")
        return self


class ScriptSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_segment_ids: tuple[StrictNonEmptyString, ...] = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    clean_text: StrictNonEmptyString
    notes: tuple[StrictNonEmptyString, ...]

    @field_validator("clean_text")
    @classmethod
    def clean_text_must_contain_visible_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("clean_text must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def validate_timing(self) -> ScriptSection:
        if self.end_ms < self.start_ms:
            raise ValueError("script section end must not precede start")
        return self


class ProcessedScriptContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: StrictNonEmptyString
    sections: tuple[ScriptSection, ...] = Field(min_length=1)
    warnings: tuple[StrictNonEmptyString, ...]

    @field_validator("title")
    @classmethod
    def title_must_contain_visible_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value.strip()
