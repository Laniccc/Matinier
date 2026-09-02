from __future__ import annotations

import pytest

from app.captions.models import CaptionStatus
from app.captions.reconciler import TranscriptReconciler
from app.transcription.models import ASREvent, ASREventType


SESSION_ID = "session-1"


def asr_event(
    event_type: ASREventType,
    provider_event_id: str,
    *,
    segment_id: str | None = "segment-1",
    text: str | None = "hello",
    begin_time_ms: int | None = 100,
    end_time_ms: int | None = None,
    confidence: float | None = None,
) -> ASREvent:
    return ASREvent(
        event_type=event_type,
        provider_event_id=provider_event_id,
        segment_id=segment_id,
        text=text,
        begin_time_ms=begin_time_ms,
        end_time_ms=end_time_ms,
        confidence=confidence,
        received_at_ms=1_000,
    )


def test_partial_revisions_are_replaced_and_final_commits_one_segment() -> None:
    reconciler = TranscriptReconciler(session_id=SESSION_ID)

    partial_v1 = reconciler.consume(
        asr_event(ASREventType.PARTIAL_RESULT, "event-1", text="hello")
    )
    partial_v2 = reconciler.consume(
        asr_event(ASREventType.PARTIAL_RESULT, "event-2", text="hello world")
    )
    final_v3 = reconciler.consume(
        asr_event(
            ASREventType.FINAL_RESULT,
            "event-3",
            text="hello world!",
            end_time_ms=900,
            confidence=0.94,
        )
    )

    assert partial_v1 is not None
    assert partial_v1.revision == 1
    assert partial_v1.status is CaptionStatus.DRAFT
    assert partial_v2 is not None
    assert partial_v2.revision == 2
    assert final_v3 is not None
    assert final_v3.revision == 3
    assert final_v3.status is CaptionStatus.FINAL
    assert final_v3.is_final is True
    assert final_v3.audio_end_ms == 900
    assert reconciler.active_draft_segments == ()
    assert reconciler.final_segments == (final_v3,)


def test_final_cannot_be_overwritten_by_late_partial() -> None:
    reconciler = TranscriptReconciler(session_id=SESSION_ID)
    final = reconciler.consume(
        asr_event(
            ASREventType.FINAL_RESULT,
            "event-final",
            text="stable",
            end_time_ms=600,
        )
    )

    late_partial = reconciler.consume(
        asr_event(
            ASREventType.PARTIAL_RESULT,
            "event-late",
            text="stale",
        )
    )

    assert late_partial is None
    assert final is not None
    assert reconciler.final_segments == (final,)


def test_duplicate_provider_id_and_duplicate_final_are_idempotent() -> None:
    reconciler = TranscriptReconciler(session_id=SESSION_ID)
    partial = reconciler.consume(
        asr_event(ASREventType.PARTIAL_RESULT, "event-1", text="same")
    )

    assert reconciler.consume(
        asr_event(ASREventType.PARTIAL_RESULT, "event-1", text="changed")
    ) is None
    assert reconciler.consume(
        asr_event(ASREventType.PARTIAL_RESULT, "event-2", text="same")
    ) is None

    final = reconciler.consume(
        asr_event(
            ASREventType.FINAL_RESULT,
            "event-3",
            text="same",
            end_time_ms=500,
        )
    )
    duplicate = reconciler.consume(
        asr_event(
            ASREventType.FINAL_RESULT,
            "event-4",
            text="same",
            end_time_ms=500,
        )
    )

    assert partial is not None
    assert final is not None
    assert final.revision == 2
    assert duplicate is None
    assert reconciler.final_segments == (final,)


def test_final_segments_are_sorted_by_audio_time_then_completion_order() -> None:
    reconciler = TranscriptReconciler(session_id=SESSION_ID)
    later = reconciler.consume(
        asr_event(
            ASREventType.FINAL_RESULT,
            "later",
            segment_id="segment-later",
            text="later",
            begin_time_ms=900,
            end_time_ms=1_200,
        )
    )
    earlier = reconciler.consume(
        asr_event(
            ASREventType.FINAL_RESULT,
            "earlier",
            segment_id="segment-earlier",
            text="earlier",
            begin_time_ms=100,
            end_time_ms=400,
        )
    )

    assert later is not None
    assert earlier is not None
    assert reconciler.final_segments == (earlier, later)


def test_non_caption_lifecycle_events_are_ignored() -> None:
    reconciler = TranscriptReconciler(session_id=SESSION_ID)

    assert reconciler.consume(
        asr_event(
            ASREventType.STREAM_STARTED,
            "started",
            segment_id=None,
            text=None,
        )
    ) is None
    assert reconciler.active_draft_segments == ()
    assert reconciler.final_segments == ()


def test_caption_event_requires_a_stable_segment_id() -> None:
    reconciler = TranscriptReconciler(session_id=SESSION_ID)

    with pytest.raises(ValueError, match="segment_id"):
        reconciler.consume(
            asr_event(
                ASREventType.PARTIAL_RESULT,
                "missing-segment",
                segment_id=None,
            )
        )


def test_blank_results_do_not_allocate_revisions_or_final_segments() -> None:
    reconciler = TranscriptReconciler(session_id=SESSION_ID)

    assert reconciler.consume(
        asr_event(ASREventType.PARTIAL_RESULT, "blank-draft", text="  ")
    ) is None
    assert reconciler.consume(
        asr_event(ASREventType.FINAL_RESULT, "blank-final", text="\n")
    ) is None
    first_caption = reconciler.consume(
        asr_event(ASREventType.FINAL_RESULT, "meaningful", text="hello")
    )

    assert first_caption is not None
    assert first_caption.revision == 1
    assert reconciler.final_segments == (first_caption,)
