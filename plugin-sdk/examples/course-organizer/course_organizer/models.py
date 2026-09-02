from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


FINAL_CATEGORIES = (
    "核心概念",
    "原理/机制",
    "方法/步骤",
    "案例",
    "公式/数据",
    "易错点与待确认问题",
)

NoteType = Literal[
    "knowledge_candidate",
    "example",
    "transition",
    "background",
    "needs_confirmation",
]
ConfidenceStatus = Literal["confirmed", "needs_confirmation", "rule_fallback"]
ConfirmationStatus = Literal["confirmed", "needs_confirmation"]
EvidenceConfirmationStatus = Literal["supported", "needs_confirmation"]

_LANGUAGE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


def _required(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")


def _unique(values: tuple[str, ...], label: str, *, required: bool = False) -> None:
    if required and not values:
        raise ValueError(f"{label} is required")
    if any(not isinstance(item, str) or not item for item in values):
        raise ValueError(f"{label} contains an invalid ID")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


def _range(start_ms: int, end_ms: int) -> None:
    if start_ms < 0 or end_ms < start_ms:
        raise ValueError("invalid media range")


def _language(value: str) -> None:
    if _LANGUAGE.fullmatch(value) is None:
        raise ValueError("invalid language")


@dataclass(frozen=True, slots=True)
class TranscriptFragment:
    segment_id: str
    revision: int
    text: str
    start_ms: int
    end_ms: int
    language: str
    confidence: float | None = None
    source_segment_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required(self.segment_id, "segment ID")
        _required(self.text, "fragment text")
        _unique(self.source_segment_ids, "source Segment IDs")
        if self.revision < 1:
            raise ValueError("fragment revision must be positive")
        _range(self.start_ms, self.end_ms)
        _language(self.language)
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("fragment confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ClassifiedSpan:
    classification: NoteType
    text: str
    start_ms: int
    end_ms: int
    source_segment_ids: tuple[str, ...]
    language: str
    confidence_status: ConfidenceStatus

    def __post_init__(self) -> None:
        if self.classification not in {
            "knowledge_candidate",
            "example",
            "transition",
            "background",
            "needs_confirmation",
        }:
            raise ValueError("unsupported note classification")
        _required(self.text, "span text")
        _range(self.start_ms, self.end_ms)
        _unique(self.source_segment_ids, "source Segment IDs", required=True)
        _language(self.language)
        if self.confidence_status not in {
            "confirmed",
            "needs_confirmation",
            "rule_fallback",
        }:
            raise ValueError("unsupported confidence status")
        if (
            self.classification == "needs_confirmation"
            and self.confidence_status == "confirmed"
        ):
            raise ValueError("uncertain span cannot be confirmed deterministically")


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    item_id: str
    text: str
    source_segment_ids: tuple[str, ...]
    start_ms: int
    end_ms: int
    confirmation_status: EvidenceConfirmationStatus

    def __post_init__(self) -> None:
        _required(self.item_id, "evidence item ID")
        _required(self.text, "evidence text")
        _unique(self.source_segment_ids, "source Segment IDs", required=True)
        _range(self.start_ms, self.end_ms)
        if self.confirmation_status not in {"supported", "needs_confirmation"}:
            raise ValueError("unsupported evidence confirmation status")


@dataclass(frozen=True, slots=True)
class RealtimeNote:
    id: str
    note_type: NoteType
    title: str
    body: str
    start_ms: int
    end_ms: int
    source_segment_ids: tuple[str, ...]
    evidence_item_ids: tuple[str, ...]
    confidence_status: ConfidenceStatus
    language: str
    related_note_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _required(self.id, "note ID")
        _required(self.title, "note title")
        _required(self.body, "note body")
        if self.note_type not in {
            "knowledge_candidate",
            "example",
            "transition",
            "background",
            "needs_confirmation",
        }:
            raise ValueError("unsupported note type")
        _range(self.start_ms, self.end_ms)
        _unique(self.source_segment_ids, "source Segment IDs", required=True)
        _unique(self.evidence_item_ids, "evidence item IDs", required=True)
        _unique(self.related_note_ids, "related note IDs")
        if self.id in self.related_note_ids:
            raise ValueError("note cannot relate to itself")
        if self.confidence_status not in {
            "confirmed",
            "needs_confirmation",
            "rule_fallback",
        }:
            raise ValueError("unsupported confidence status")
        if self.note_type == "needs_confirmation" and self.confidence_status == "confirmed":
            raise ValueError("uncertain note cannot be confirmed")
        _language(self.language)


@dataclass(frozen=True, slots=True)
class KnowledgeItem:
    id: str
    category: str
    topic_path: tuple[str, ...]
    title: str
    statement: str
    explanation: str
    evidence_item_ids: tuple[str, ...]
    source_segment_ids: tuple[str, ...]
    time_ranges: tuple[tuple[int, int], ...]
    related_item_ids: tuple[str, ...]
    confirmation_status: ConfirmationStatus

    def __post_init__(self) -> None:
        _required(self.id, "knowledge item ID")
        if self.category not in FINAL_CATEGORIES:
            raise ValueError("unknown final category")
        if not self.topic_path or any(not item.strip() for item in self.topic_path):
            raise ValueError("topic path is required")
        _required(self.title, "knowledge title")
        _required(self.statement, "knowledge statement")
        _required(self.explanation, "knowledge explanation")
        _unique(self.evidence_item_ids, "evidence item IDs", required=True)
        _unique(self.source_segment_ids, "source Segment IDs", required=True)
        if not self.time_ranges:
            raise ValueError("knowledge time ranges are required")
        for start_ms, end_ms in self.time_ranges:
            _range(start_ms, end_ms)
        _unique(self.related_item_ids, "related knowledge item IDs")
        if self.id in self.related_item_ids:
            raise ValueError("knowledge item cannot relate to itself")
        if self.confirmation_status not in {"confirmed", "needs_confirmation"}:
            raise ValueError("unsupported confirmation status")


@dataclass(frozen=True, slots=True)
class CourseCategory:
    name: str
    items: tuple[KnowledgeItem, ...]

    def __post_init__(self) -> None:
        if self.name not in FINAL_CATEGORIES:
            raise ValueError("unknown course category")
        if any(item.category != self.name for item in self.items):
            raise ValueError("category contains a mismatched knowledge item")
        ids = tuple(item.id for item in self.items)
        _unique(ids, "category knowledge IDs")


@dataclass(frozen=True, slots=True)
class CourseDocument:
    title: str
    language: str
    realtime_notes: tuple[RealtimeNote, ...]
    categories: tuple[CourseCategory, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        _required(self.title, "course document title")
        _language(self.language)
        note_ids = tuple(item.id for item in self.realtime_notes)
        _unique(note_ids, "realtime note IDs")
        category_names = tuple(item.name for item in self.categories)
        if category_names != FINAL_CATEGORIES:
            raise ValueError("course document categories must match the fixed categories")
        knowledge_ids = tuple(
            item.id for category in self.categories for item in category.items
        )
        _unique(knowledge_ids, "knowledge item IDs")
        if any(not isinstance(item, str) or not item.strip() for item in self.warnings):
            raise ValueError("document warnings must be non-empty strings")


__all__ = [
    "FINAL_CATEGORIES",
    "ClassifiedSpan",
    "CourseCategory",
    "CourseDocument",
    "EvidenceItem",
    "KnowledgeItem",
    "RealtimeNote",
    "TranscriptFragment",
]
