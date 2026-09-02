from __future__ import annotations

import datetime as dt
import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.captions.models import CaptionEvent, CaptionStatus
from app.persistence.models import (
    SegmentRecord,
    SessionRecord,
    TranslationSegmentRecord,
    utc_now,
)
from app.persistence.segments import SegmentRepository


def create_session(client: TestClient, *, source_name: str = "speech.wav") -> dict:
    response = client.post(
        "/api/sessions",
        json={
            "source_type": "file",
            "source_name": source_name,
            "language": "zh-CN",
        },
    )
    assert response.status_code == 201
    return response.json()


def final_caption(
    session_id: str,
    segment_id: str,
    *,
    start_ms: int | None,
    end_ms: int | None,
    text: str,
    revision: int = 1,
) -> CaptionEvent:
    return CaptionEvent(
        session_id=session_id,
        segment_id=segment_id,
        revision=revision,
        status=CaptionStatus.FINAL,
        text=text,
        audio_start_ms=start_ms,
        audio_end_ms=end_ms,
        confidence=0.95,
        provider_event_id=f"provider-{segment_id}",
        received_at_ms=1_700_000_000_000 + revision,
    )


def seed_completed_session(client: TestClient) -> dict:
    created = create_session(client, source_name="中文 speech.wav")
    session_id = str(created["id"])
    with Session(client.app.state.database.engine) as db_session:
        record = db_session.get(SessionRecord, session_id)
        assert record is not None
        record.status = "completed"
        record.asr_provider = "bailian"
        record.asr_model = "fun-asr-realtime"
        record.final_result_count = 2
        record.first_partial_latency_ms = 120.0
        record.average_final_latency_ms = 680.0
        record.provider_error_count = 0
        record.sent_audio_chunk_count = 40
        record.sent_audio_bytes = 128_000
        record.started_at = dt.datetime(2026, 7, 22, 12, tzinfo=dt.UTC)
        record.ended_at = record.started_at + dt.timedelta(seconds=4)

        repository = SegmentRepository(db_session)
        repository.upsert_final(
            final_caption(
                session_id,
                "later",
                start_ms=2_500,
                end_ms=4_000,
                text="你好 & <cue> --> 日本語 العربية English 😀\n第二行",
            ),
            track_id="track-1",
            language="zh-CN",
        )
        repository.upsert_final(
            final_caption(
                session_id,
                "earlier",
                start_ms=1_000,
                end_ms=None,
                text="第一句",
            ),
            track_id="track-1",
            language="zh-CN",
        )
        now = utc_now()
        db_session.add(
            SegmentRecord(
                id=str(uuid.uuid4()),
                session_id=session_id,
                segment_id="draft-sentinel",
                track_id="track-1",
                revision=9,
                language="zh-CN",
                raw_text="DRAFT MUST NOT LEAK",
                display_text="DRAFT MUST NOT LEAK",
                audio_start_ms=0,
                audio_end_ms=None,
                confidence=None,
                status="draft",
                received_at_ms=1_700_000_000_999,
                finalized_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db_session.commit()
    return created


def test_all_export_formats_are_attachments_final_only_and_stable(
    client: TestClient,
) -> None:
    created = seed_completed_session(client)
    session_id = created["id"]
    expectations = {
        "json": ("application/json", 'attachment; filename="source.json"'),
        "srt": ("application/x-subrip", 'attachment; filename="source.srt"'),
        "vtt": ("text/vtt", 'attachment; filename="source.vtt"'),
        "markdown": ("text/markdown", 'attachment; filename="source.md"'),
    }

    responses = {}
    for export_format, (media_type, disposition) in expectations.items():
        first = client.get(
            f"/api/sessions/{session_id}/export",
            params={"format": export_format},
        )
        second = client.get(
            f"/api/sessions/{session_id}/export",
            params={"format": export_format},
        )
        assert first.status_code == 200
        assert first.content == second.content
        assert first.headers["content-type"].startswith(media_type)
        assert first.headers["content-disposition"] == disposition
        assert first.headers["x-livecaption-session-status"] == "completed"
        assert first.headers["x-livecaption-partial-export"] == "false"
        assert first.headers["x-livecaption-content"] == "source"
        assert b"DRAFT MUST NOT LEAK" not in first.content
        decoded = first.content.decode("utf-8")
        for token in ("你好", "日本語", "العربية", "English", "😀"):
            assert token in decoded
        responses[export_format] = first

    payload = json.loads(responses["json"].content)
    assert [item["segment_id"] for item in payload["segments"]] == [
        "earlier",
        "later",
    ]
    assert payload["segments"][0]["audio_end_ms"] == 2_500
    assert payload["transcription"]["provider"] == "bailian"
    assert payload["transcription"]["model"] == "fun-asr-realtime"
    assert payload["transcription"]["metrics"]["sent_audio_bytes"] == 128_000
    assert "00:00:01,000 --> 00:00:02,500" in responses["srt"].text
    assert responses["vtt"].text.startswith("WEBVTT\n\n")
    assert "你好 &amp; &lt;cue&gt; --&gt;" in responses["vtt"].text
    assert "Source: 中文 speech.wav" in responses["markdown"].text


def test_empty_session_exports_are_explicit(client: TestClient) -> None:
    created = create_session(client)
    session_id = created["id"]

    json_response = client.get(
        f"/api/sessions/{session_id}/export", params={"format": "json"}
    )
    srt_response = client.get(
        f"/api/sessions/{session_id}/export", params={"format": "srt"}
    )
    vtt_response = client.get(
        f"/api/sessions/{session_id}/export", params={"format": "vtt"}
    )
    markdown_response = client.get(
        f"/api/sessions/{session_id}/export", params={"format": "markdown"}
    )

    assert json_response.json()["segments"] == []
    assert json_response.json()["session_status"] == "created"
    assert json_response.json()["partial_export"] is True
    assert json_response.headers["x-livecaption-partial-export"] == "true"
    assert srt_response.content == b""
    assert vtt_response.text.startswith("WEBVTT\n\nNOTE session_status=created")
    assert "_No Final captions._" in markdown_response.text


def test_translation_export_reads_persisted_translation_finals_independently(
    client: TestClient,
) -> None:
    created = seed_completed_session(client)
    session_id = created["id"]
    with Session(client.app.state.database.engine) as db_session:
        record = db_session.get(SessionRecord, session_id)
        assert record is not None
        record.target_language = "en-US"
        record.translation_status = "completed"
        now = utc_now()
        db_session.add(
            TranslationSegmentRecord(
                id=str(uuid.uuid4()),
                session_id=session_id,
                segment_id="translation-1",
                revision=3,
                source_language="zh-CN",
                target_language="en-US",
                text="Persisted translation Final",
                audio_start_ms=900,
                audio_end_ms=4_100,
                source_segment_ids=["earlier", "later"],
                status="final",
                received_at_ms=1_700_000_000_123,
                finalized_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db_session.commit()

    response = client.get(
        f"/api/sessions/{session_id}/export",
        params={"format": "json", "content": "translation"},
    )

    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        'attachment; filename="translation.json"'
    )
    assert response.headers["x-livecaption-content"] == "translation"
    payload = response.json()
    assert payload["content"] == "translation"
    assert [item["display_text"] for item in payload["segments"]] == [
        "Persisted translation Final"
    ]
    assert "第一句" not in response.text


def test_export_missing_session_and_invalid_format_are_clear(client: TestClient) -> None:
    missing = client.get(
        "/api/sessions/missing/export", params={"format": "json"}
    )
    created = create_session(client)
    invalid = client.get(
        f"/api/sessions/{created['id']}/export", params={"format": "pdf"}
    )

    assert missing.status_code == 404
    assert missing.json() == {"detail": "Session not found"}
    assert invalid.status_code == 422


def test_export_internal_error_is_sanitized(
    client: TestClient,
    monkeypatch,
) -> None:
    from app.api import exports as exports_api

    created = create_session(client)

    def fail(_document) -> bytes:
        raise RuntimeError("internal secret sk-do-not-return")

    monkeypatch.setattr(exports_api, "export_transcript_json", fail)
    response = client.get(
        f"/api/sessions/{created['id']}/export", params={"format": "json"}
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Export failed"}
    assert "sk-do-not-return" not in response.text
