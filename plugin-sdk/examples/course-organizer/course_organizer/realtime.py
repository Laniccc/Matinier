from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .evidence import parse_realtime_notes
from .filtering import filter_fragments
from .models import EvidenceItem, RealtimeNote, TranscriptFragment
from .prompts import realtime_notes_prompt


@dataclass(frozen=True, slots=True)
class RealtimeConfig:
    min_window_ms: int = 60_000
    max_window_ms: int = 90_000
    trigger_chars: int = 1_600
    retry_delays_seconds: tuple[int, ...] = (2, 10, 30)
    max_notes: int = 500

    def __post_init__(self) -> None:
        if self.min_window_ms < 1 or self.max_window_ms < self.min_window_ms:
            raise ValueError("invalid realtime window bounds")
        if self.trigger_chars < 1 or self.max_notes < 1:
            raise ValueError("invalid realtime character or note limit")
        if any(item <= 0 for item in self.retry_delays_seconds):
            raise ValueError("retry delays must be positive")


def window_ready(fragments: tuple[TranscriptFragment, ...], config: RealtimeConfig) -> bool:
    if not fragments:
        return False
    start_ms = min(item.start_ms for item in fragments)
    end_ms = max(item.end_ms for item in fragments)
    characters = sum(len(item.text) for item in fragments)
    return end_ms - start_ms >= config.min_window_ms or characters >= config.trigger_chars


def build_window_evidence(
    fragments: tuple[TranscriptFragment, ...],
) -> dict[str, EvidenceItem]:
    output: dict[str, EvidenceItem] = {}
    for span in filter_fragments(fragments):
        digest = hashlib.sha256(
            "\x1f".join(
                (
                    span.classification,
                    span.text,
                    str(span.start_ms),
                    str(span.end_ms),
                    *span.source_segment_ids,
                )
            ).encode("utf-8")
        ).hexdigest()[:20]
        item_id = f"realtime-{digest}"
        output[item_id] = EvidenceItem(
            item_id=item_id,
            text=span.text,
            source_segment_ids=span.source_segment_ids,
            start_ms=span.start_ms,
            end_ms=span.end_ms,
            confirmation_status=(
                "needs_confirmation"
                if span.classification == "needs_confirmation"
                else "supported"
            ),
        )
    return output


def model_input(
    evidence: dict[str, EvidenceItem],
    *,
    output_language: str,
) -> dict[str, object]:
    items = []
    spans_by_id = {
        item.item_id: item
        for item in evidence.values()
    }
    for item_id, item in spans_by_id.items():
        classification = "needs_confirmation" if (
            item.confirmation_status == "needs_confirmation"
        ) else _fallback_classification(item.text)
        items.append(
            {
                "item_id": item_id,
                "text": item.text,
                "classification": classification,
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "source_segment_ids": list(item.source_segment_ids),
                "confirmation_status": item.confirmation_status,
            }
        )
    return {
        "input_category": "session_transcript",
        "system_prompt": realtime_notes_prompt(),
        "user_prompt": "Return sparse realtime course notes as one JSON object.",
        "input_payload": {
            "task": "course.realtime_notes",
            "output_language": output_language,
            "items": items,
        },
        "response_format": "json_object",
        "max_output_tokens": 2_048,
        "timeout_seconds": 30,
    }


def parse_model_response(
    response: dict[str, object],
    *,
    evidence: dict[str, EvidenceItem],
) -> tuple[RealtimeNote, ...]:
    output = response.get("output")
    return parse_realtime_notes(output, allowed_evidence=evidence)


def fallback_notes(
    evidence: dict[str, EvidenceItem],
    *,
    output_language: str,
) -> tuple[RealtimeNote, ...]:
    notes: list[RealtimeNote] = []
    for item in evidence.values():
        classification = (
            "needs_confirmation"
            if item.confirmation_status == "needs_confirmation"
            else _fallback_classification(item.text)
        )
        if classification in {"transition", "background"}:
            continue
        digest = hashlib.sha256(f"fallback\x1f{item.item_id}".encode()).hexdigest()[:20]
        notes.append(
            RealtimeNote(
                id=f"rule-note-{digest}",
                note_type=classification,  # type: ignore[arg-type]
                title=_title(item.text),
                body=item.text[:1_000],
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                source_segment_ids=item.source_segment_ids,
                evidence_item_ids=(item.item_id,),
                confidence_status=(
                    "needs_confirmation"
                    if classification == "needs_confirmation"
                    else "rule_fallback"
                ),
                language=output_language,
                related_note_ids=(),
            )
        )
    return tuple(notes)


def bound_notes(notes: tuple[RealtimeNote, ...]) -> tuple[RealtimeNote, ...]:
    return tuple(
        RealtimeNote(
            id=item.id[:128],
            note_type=item.note_type,
            title=item.title[:200],
            body=item.body[:1_000],
            start_ms=item.start_ms,
            end_ms=item.end_ms,
            source_segment_ids=item.source_segment_ids[:128],
            evidence_item_ids=item.evidence_item_ids[:128],
            confidence_status=item.confidence_status,
            language=item.language,
            related_note_ids=item.related_note_ids[:32],
        )
        for item in notes[:500]
    )


def _fallback_classification(text: str) -> str:
    lowered = text.casefold()
    if any(token in lowered for token in ("例如", "举例", "演示", "example", "demo")):
        return "example"
    if any(token in lowered for token in ("平台", "按钮", "广告", "coupon", "click")):
        return "background"
    if any(
        token in lowered
        for token in (
            "是",
            "因为",
            "所以",
            "梯度",
            "=",
            "defined",
            "because",
            "therefore",
            "unlike",
        )
    ):
        return "knowledge_candidate"
    return "transition"


def _title(text: str) -> str:
    compact = " ".join(text.split())
    return compact[:80] or "待整理知识点"


__all__ = [
    "RealtimeConfig",
    "bound_notes",
    "build_window_evidence",
    "fallback_notes",
    "model_input",
    "parse_model_response",
    "window_ready",
]
