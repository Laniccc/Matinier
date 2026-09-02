from __future__ import annotations

import datetime as dt
import html
import json

from app.export import (
    ExportDocument,
    export_markdown,
    export_srt,
    export_transcript_json,
    export_vtt,
)
from app.persistence.models import SegmentRecord, SessionRecord
from app.timeline.service import normalize_timeline


NOW = dt.datetime(2026, 7, 22, 12, tzinfo=dt.UTC)


def session() -> SessionRecord:
    return SessionRecord(
        id="session-1",
        room_name="live-caption-session-1",
        status="completed",
        source_type="file",
        source_name="中文 speech.wav",
        language="zh-CN",
        asr_provider="bailian",
        asr_model="fun-asr-realtime",
        final_result_count=2,
        first_partial_latency_ms=120.5,
        average_final_latency_ms=680.25,
        provider_error_count=0,
        sent_audio_chunk_count=40,
        sent_audio_bytes=128_000,
        started_at=NOW,
        ended_at=NOW + dt.timedelta(seconds=4),
        created_at=NOW - dt.timedelta(seconds=1),
    )


def segment(
    segment_id: str,
    *,
    start_ms: int | None,
    end_ms: int | None,
    raw_text: str,
    display_text: str | None = None,
) -> SegmentRecord:
    return SegmentRecord(
        id=f"row-{segment_id}",
        session_id="session-1",
        segment_id=segment_id,
        track_id="track-1",
        revision=4,
        language="zh-CN",
        raw_text=raw_text,
        display_text=display_text or raw_text,
        audio_start_ms=start_ms,
        audio_end_ms=end_ms,
        confidence=0.92,
        status="final",
        received_at_ms=1_700_000_000_000,
        finalized_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def document() -> ExportDocument:
    timeline = normalize_timeline(
        [
            segment(
                "later",
                start_ms=2_500,
                end_ms=4_000,
                raw_text="原始第二句",
                display_text="你好 & <cue> -->\n第二行",
            ),
            segment(
                "earlier",
                start_ms=1_000,
                end_ms=None,
                raw_text="第一句",
            ),
        ]
    )
    return ExportDocument(session=session(), timeline=timeline)


def test_json_export_is_parseable_complete_safe_and_deterministic() -> None:
    value = document()

    first = export_transcript_json(value)
    second = export_transcript_json(value)
    payload = json.loads(first)

    assert first == second
    assert first.endswith(b"\n")
    assert payload["schema_version"] == 1
    assert payload["session"]["id"] == "session-1"
    assert payload["session"]["source"] == {
        "type": "file",
        "name": "中文 speech.wav",
        "language": "zh-CN",
    }
    assert payload["transcription"]["provider"] == "bailian"
    assert payload["transcription"]["model"] == "fun-asr-realtime"
    assert payload["transcription"]["metrics"]["average_final_latency_ms"] == 680.25
    assert payload["timeline"] == {"duration_ms": 4_000, "segment_count": 2}
    assert [item["segment_id"] for item in payload["segments"]] == [
        "earlier",
        "later",
    ]
    assert payload["segments"][0]["audio_end_ms"] == 2_500
    assert payload["segments"][1]["raw_text"] == "原始第二句"
    assert payload["segments"][1]["display_text"].endswith("第二行")
    lowered = first.lower()
    for forbidden in (b"api_key", b"authorization", b"raw_payload", b"provider_event_id"):
        assert forbidden not in lowered


def test_srt_and_vtt_use_standard_timing_order_and_safe_text() -> None:
    value = document()
    srt = export_srt(value).decode("utf-8")
    vtt = export_vtt(value).decode("utf-8")

    assert srt.startswith("1\n00:00:01,000 --> 00:00:02,500\n第一句")
    assert "2\n00:00:02,500 --> 00:00:04,000" in srt
    assert "你好 & <cue> -->\n第二行" in srt
    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:01.000 --> 00:00:02.500" in vtt
    assert "00:00:02.500 --> 00:00:04.000" in vtt
    assert "你好 &amp; &lt;cue&gt; --&gt;\n第二行" in vtt
    assert export_srt(value) == export_srt(value)
    assert export_vtt(value) == export_vtt(value)


def test_markdown_contains_metadata_duration_and_timeline() -> None:
    markdown = export_markdown(document()).decode("utf-8")

    assert markdown.startswith("# Session session-1\n")
    assert "Source: 中文 speech.wav" in markdown
    assert "Language: zh-CN" in markdown
    assert "Duration: 00:00:04.000" in markdown
    assert "Provider: bailian" in markdown
    assert "Model: fun-asr-realtime" in markdown
    assert "[00:00:01.000 - 00:00:02.500]\n第一句" in markdown
    assert "[00:00:02.500 - 00:00:04.000]" in markdown
    assert export_markdown(document()) == export_markdown(document())


def test_empty_session_export_behavior_is_explicit() -> None:
    empty = ExportDocument(session=session(), timeline=normalize_timeline([]))

    payload = json.loads(export_transcript_json(empty))
    assert payload["segments"] == []
    assert payload["timeline"] == {"duration_ms": 0, "segment_count": 0}
    assert export_srt(empty) == b""
    assert export_vtt(empty) == b"WEBVTT\n\n"
    assert "_No Final captions._" in export_markdown(empty).decode("utf-8")


def test_running_export_metadata_and_timeline_boundaries_are_explicit() -> None:
    running_session = session()
    running_session.status = "running"
    exported_at = NOW + dt.timedelta(minutes=5)
    value = ExportDocument(
        session=running_session,
        exported_at=exported_at,
        timeline=normalize_timeline(
            [
                segment(
                    "negative",
                    start_ms=-500,
                    end_ms=-100,
                    raw_text="negative",
                ),
                segment(
                    "reversed",
                    start_ms=2_000,
                    end_ms=1_000,
                    raw_text="reversed",
                ),
                segment(
                    "missing",
                    start_ms=None,
                    end_ms=None,
                    raw_text="missing",
                ),
            ]
        ),
    )

    payload = json.loads(export_transcript_json(value))
    assert payload["session_status"] == "running"
    assert payload["exported_at"] == exported_at.isoformat()
    assert payload["partial_export"] is True
    assert [(item["audio_start_ms"], item["audio_end_ms"]) for item in payload["segments"]] == [
        (0, 0),
        (2_000, 2_000),
        (2_000, 4_000),
    ]
    assert "Partial export: true" in export_markdown(value).decode("utf-8")
    assert "NOTE session_status=running" in export_vtt(value).decode("utf-8")
    assert "00:00:00,000 --> 00:00:00,000" in export_srt(value).decode("utf-8")


def test_multilingual_utf8_round_trip_across_every_export_format() -> None:
    text = "中文 English 日本語 العربية مرحبا 😀 ‏RTL & <特殊标点> — ‘quoted’"
    value = ExportDocument(
        session=session(),
        timeline=normalize_timeline(
            [
                segment(
                    "multilingual",
                    start_ms=123,
                    end_ms=4_567,
                    raw_text=text,
                )
            ]
        ),
    )

    json_payload = json.loads(export_transcript_json(value).decode("utf-8"))
    markdown = export_markdown(value).decode("utf-8")
    srt = export_srt(value).decode("utf-8")
    vtt = export_vtt(value).decode("utf-8")

    assert json_payload["segments"][0]["display_text"] == text
    assert text in markdown
    assert text in srt
    assert text in html.unescape(vtt)
    assert "00:00:00,123 --> 00:00:04,567" in srt
    assert "00:00:00.123 --> 00:00:04.567" in vtt
