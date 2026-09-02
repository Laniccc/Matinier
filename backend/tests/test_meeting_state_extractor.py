from __future__ import annotations

import asyncio
import datetime as dt
import json

from app.meeting_state.contracts import ProjectionSegment
from app.meeting_state.extractor import MeetingStateExtractor, parse_meeting_state_delta
from app.meeting_state.models import MeetingState
from app.text_processing.provider import StructuredCompletionResult


def _segment() -> ProjectionSegment:
    now = dt.datetime.now(dt.UTC)
    return ProjectionSegment(
        session_id="session-extractor",
        segment_id="segment-1",
        revision=1,
        track_id="track-1",
        language="zh-CN",
        raw_text="请在 2026 年 8 月 28 日前整理会议纪要。",
        display_text="请在 2026 年 8 月 28 日前整理会议纪要。",
        audio_start_ms=0,
        audio_end_ms=1_000,
        confidence=0.99,
        received_at_ms=1_000,
        finalized_at=now,
        updated_at=now,
    )


def _valid_payload() -> dict[str, object]:
    grounded = {
        "resolution": "known",
        "source_segment_ids": ["segment-1"],
        "confidence": 0.9,
        "explanation": None,
    }
    missing = {
        "value": None,
        "resolution": "missing",
        "source_segment_ids": ["segment-1"],
        "confidence": 0.9,
        "explanation": None,
    }
    return {
        "topics": [],
        "entities": [],
        "decisions": [],
        "highlights": [],
        "conflicts": [],
        "action_operations": [
            {
                "kind": "create",
                "content": {
                    "title": {"value": "整理会议纪要", **grounded},
                    "deliverable": {"value": "Markdown 会议纪要", **grounded},
                    "assignee": missing,
                    "due_at": {"value": "2026-08-28", **grounded},
                    "priority": missing,
                    "blocking_conflict_segment_ids": {
                        "value": None,
                        "resolution": "missing",
                        "source_segment_ids": [],
                        "confidence": 0.0,
                        "explanation": None,
                    },
                },
                "change_summary": None,
            }
        ],
        "warnings": [],
    }


def test_parser_normalizes_bounded_candidate_shape_quirks() -> None:
    segment = _segment()
    delta = parse_meeting_state_delta(
        json.dumps(_valid_payload(), ensure_ascii=False),
        session_id=segment.session_id,
        segments=(segment,),
    )

    operation = delta.action_operations[0]
    assert operation.content.due_at.value == dt.datetime(
        2026, 8, 28, tzinfo=dt.UTC
    )
    assert operation.content.blocking_conflict_segment_ids == ()


class _RepairProvider:
    provider_name = "test"
    model = "test"

    def __init__(self) -> None:
        self.requests = []

    async def complete_structured(self, request):
        self.requests.append(request)
        content = (
            '{"topics":[{"text":"invalid","source_segment_ids":["foreign"]}]}'
            if len(self.requests) == 1
            else json.dumps(_valid_payload(), ensure_ascii=False)
        )
        return StructuredCompletionResult(content=content, finish_reason="stop")


def test_extractor_repairs_one_invalid_provider_output() -> None:
    async def scenario() -> None:
        segment = _segment()
        provider = _RepairProvider()
        extractor = MeetingStateExtractor(provider)
        delta = await extractor.extract(
            state=MeetingState(session_id=segment.session_id, version=0),
            segments=(segment,),
        )

        assert len(provider.requests) == 2
        assert provider.requests[1].input_payload["invalid_output"]
        assert len(delta.action_operations) == 1

    asyncio.run(scenario())
