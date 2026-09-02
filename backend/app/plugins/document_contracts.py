from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from app.media.contracts import validate_bounded_json
from app.plugins.capabilities import CapabilityInput, CapabilityOutput


MAX_PLUGIN_DOCUMENT_BYTES = 192 * 1024

_IDENTITY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$"
_SCHEMA_NAME_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_-]*)+$"
_SCHEMA_VERSION_PATTERN = r"^[0-9]+(?:\.[0-9]+){1,2}$"
_LANGUAGE_PATTERN = r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$"
_ITEM_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$"
_RAW_HTML = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")


class PluginDocumentEvidenceRef(CapabilityInput):
    item_id: str = Field(pattern=_ITEM_ID_PATTERN)
    source_segment_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @field_validator("source_segment_ids")
    @classmethod
    def valid_source_segment_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        pattern = re.compile(_ITEM_ID_PATTERN)
        if any(pattern.fullmatch(item) is None for item in value):
            raise ValueError("invalid source segment ID")
        if len(set(value)) != len(value):
            raise ValueError("source segment IDs must be unique")
        return value

    @model_validator(mode="after")
    def ordered_range(self) -> "PluginDocumentEvidenceRef":
        if self.end_ms < self.start_ms:
            raise ValueError("evidence end_ms must be greater than or equal to start_ms")
        return self


class PluginDocumentPublishInput(CapabilityInput):
    identity_key: str = Field(pattern=_IDENTITY_KEY_PATTERN)
    schema_name: str = Field(pattern=_SCHEMA_NAME_PATTERN)
    schema_version: str = Field(pattern=_SCHEMA_VERSION_PATTERN)
    language: str = Field(
        min_length=2,
        max_length=32,
        pattern=_LANGUAGE_PATTERN,
    )
    trigger: Literal[
        "manual",
        "session_completed",
        "session_failed",
        "session_cancelled",
    ]
    completeness: Literal["interim", "complete", "partial_terminal"]
    source_package_id: str = Field(min_length=1, max_length=128)
    content: dict[str, object]
    markdown: str
    evidence_refs: tuple[PluginDocumentEvidenceRef, ...] = Field(
        default=(),
        max_length=2_000,
    )

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: dict[str, object]) -> dict[str, object]:
        validate_bounded_json(
            value,
            max_bytes=MAX_PLUGIN_DOCUMENT_BYTES,
            reject_sensitive_keys=True,
        )
        return value

    @field_validator("markdown")
    @classmethod
    def safe_bounded_markdown(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_PLUGIN_DOCUMENT_BYTES:
            raise ValueError("document Markdown exceeds byte limit")
        if _RAW_HTML.search(value):
            raise ValueError("unsafe markdown raw HTML is forbidden")
        for match in _MARKDOWN_LINK.finditer(value):
            destination = match.group(1)
            parsed = urlsplit(destination)
            if parsed.scheme and parsed.scheme.casefold() != "https":
                raise ValueError("unsafe markdown link is forbidden")
        lowered = value.casefold()
        if any(scheme in lowered for scheme in ("javascript:", "data:", "file:")):
            raise ValueError("unsafe markdown link is forbidden")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence_refs(
        cls,
        value: tuple[PluginDocumentEvidenceRef, ...],
    ) -> tuple[PluginDocumentEvidenceRef, ...]:
        item_ids = [item.item_id for item in value]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("document evidence item IDs must be unique")
        return value


class PluginDocumentPublishOutput(CapabilityOutput):
    document_id: str = Field(min_length=1, max_length=128)
    document_version: int = Field(ge=1)
    identity_key: str = Field(pattern=_IDENTITY_KEY_PATTERN)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    language: str = Field(
        min_length=2,
        max_length=32,
        pattern=_LANGUAGE_PATTERN,
    )
    trigger: Literal[
        "manual",
        "session_completed",
        "session_failed",
        "session_cancelled",
    ]
    completeness: Literal["interim", "complete", "partial_terminal"]


__all__ = [
    "MAX_PLUGIN_DOCUMENT_BYTES",
    "PluginDocumentEvidenceRef",
    "PluginDocumentPublishInput",
    "PluginDocumentPublishOutput",
]
