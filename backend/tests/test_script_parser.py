from __future__ import annotations

import datetime as dt
import json

import pytest

from app.text_processing.markdown import render_processed_script_markdown
from app.text_processing.models import SourceSegmentSnapshot
from app.text_processing.parser import ScriptOutputError, parse_script_content


def source_segments() -> tuple[SourceSegmentSnapshot, ...]:
    return (
        SourceSegmentSnapshot(
            segment_id="seg-1",
            revision=3,
            language="zh-CN",
            raw_text="大家 好",
            display_text="大家 好",
            audio_start_ms=100,
            audio_end_ms=1_500,
        ),
        SourceSegmentSnapshot(
            segment_id="seg-2",
            revision=2,
            language="zh-CN",
            raw_text="欢迎 使用",
            display_text="欢迎 使用",
            audio_start_ms=1_600,
            audio_end_ms=3_000,
        ),
    )


def valid_payload() -> dict:
    return {
        "title": "欢迎台本",
        "sections": [
            {
                "source_segment_ids": ["seg-1", "seg-2"],
                "start_ms": 100,
                "end_ms": 3_000,
                "clean_text": "大家好，欢迎使用。",
                "notes": [],
            }
        ],
        "warnings": [],
    }


def test_valid_script_json_is_strictly_parsed_and_traceable() -> None:
    parsed = parse_script_content(
        json.dumps(valid_payload(), ensure_ascii=False),
        source_segments(),
    )

    assert parsed.title == "欢迎台本"
    assert parsed.sections[0].source_segment_ids == ("seg-1", "seg-2")
    assert parsed.sections[0].start_ms == 100
    assert parsed.sections[0].end_ms == 3_000
    assert parsed.sections[0].clean_text == "大家好，欢迎使用。"


@pytest.mark.parametrize(
    "content",
    [
        "",
        "   ",
        '{"title":"截断"',
        json.dumps(
            {
                "title": "缺字段",
                "sections": [
                    {
                        "source_segment_ids": ["seg-1", "seg-2"],
                        "start_ms": 100,
                        "end_ms": 3_000,
                        "clean_text": "文本",
                    }
                ],
                "warnings": [],
            }
        ),
        json.dumps({**valid_payload(), "unexpected": True}),
    ],
)
def test_empty_invalid_or_schema_mismatched_content_fails(content: str) -> None:
    with pytest.raises(ScriptOutputError):
        parse_script_content(content, source_segments())


@pytest.mark.parametrize(
    "source_ids",
    [
        ["foreign"],
        ["seg-1", "seg-1"],
        ["seg-1"],
    ],
)
def test_foreign_duplicate_or_missing_source_references_fail(
    source_ids: list[str],
) -> None:
    payload = valid_payload()
    payload["sections"][0]["source_segment_ids"] = source_ids

    with pytest.raises(ScriptOutputError):
        parse_script_content(json.dumps(payload), source_segments())


@pytest.mark.parametrize(
    ("start_ms", "end_ms"),
    [
        (101, 3_000),
        (100, 2_999),
        (3_000, 100),
    ],
)
def test_section_timing_must_match_referenced_source(
    start_ms: int,
    end_ms: int,
) -> None:
    payload = valid_payload()
    payload["sections"][0]["start_ms"] = start_ms
    payload["sections"][0]["end_ms"] = end_ms

    with pytest.raises(ScriptOutputError):
        parse_script_content(json.dumps(payload), source_segments())


def test_markdown_contains_traceability_and_generation_metadata() -> None:
    parsed = parse_script_content(
        json.dumps(valid_payload(), ensure_ascii=False),
        source_segments(),
    )

    markdown = render_processed_script_markdown(
        parsed,
        provider="deepseek",
        model="deepseek-v4-flash",
        version=2,
        created_at=dt.datetime(2026, 7, 23, 9, 30, tzinfo=dt.UTC),
    )

    assert markdown.startswith("# 欢迎台本\n")
    assert "Provider: deepseek" in markdown
    assert "Model: deepseek-v4-flash" in markdown
    assert "Version: 2" in markdown
    assert "Generated: 2026-07-23T09:30:00+00:00" in markdown
    assert "[00:00:00.100 - 00:00:03.000]" in markdown
    assert "大家好，欢迎使用。" in markdown
    assert "Source segments: `seg-1`, `seg-2`" in markdown
