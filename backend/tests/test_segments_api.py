from __future__ import annotations

import datetime as dt
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.captions.models import CaptionEvent, CaptionStatus
from app.persistence.models import SegmentRecord, utc_now
from app.persistence.segments import SegmentRepository


def create_file_session(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/sessions",
        json={
            "source_type": "file",
            "source_name": "speech.wav",
            "language": "zh-CN",
        },
    )
    assert response.status_code == 201
    return response.json()


def final_caption(
    session_id: str,
    segment_id: str,
    *,
    revision: int = 1,
    start_ms: int | None,
    end_ms: int | None,
    text: str,
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
        provider_event_id=f"provider-{segment_id}-{revision}",
        received_at_ms=1_700_000_000_000 + revision,
    )


def test_existing_session_without_finals_returns_empty_snapshot(client) -> None:
    created = create_file_session(client)

    response = client.get(f"/api/sessions/{created['id']}/segments")

    assert response.status_code == 200
    assert response.json() == []


def test_missing_session_snapshot_returns_404(client) -> None:
    response = client.get("/api/sessions/missing/segments")

    assert response.status_code == 404
    assert response.json() == {"detail": "Session not found"}


def test_snapshot_returns_only_finals_in_audio_order(client) -> None:
    created = create_file_session(client)
    session_id = str(created["id"])
    with Session(client.app.state.database.engine) as db_session:
        repository = SegmentRepository(db_session)
        repository.upsert_final(
            final_caption(
                session_id,
                "later",
                start_ms=900,
                end_ms=1_200,
                text="later caption",
            ),
            track_id="replay-audio",
            language="zh-CN",
        )
        repository.upsert_final(
            final_caption(
                session_id,
                "earlier",
                start_ms=100,
                end_ms=500,
                text="earlier caption",
            ),
            track_id="replay-audio",
            language="zh-CN",
        )
        now = utc_now()
        db_session.add(
            SegmentRecord(
                id=str(uuid.uuid4()),
                session_id=session_id,
                segment_id="draft-that-must-not-leak",
                track_id="replay-audio",
                revision=4,
                language="zh-CN",
                raw_text="draft",
                display_text="draft",
                audio_start_ms=50,
                audio_end_ms=None,
                confidence=None,
                status="draft",
                received_at_ms=1_700_000_000_000,
                finalized_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db_session.commit()

    response = client.get(f"/api/sessions/{session_id}/segments")

    assert response.status_code == 200
    payload = response.json()
    assert [item["segment_id"] for item in payload] == ["earlier", "later"]
    assert payload[0] == {
        "id": payload[0]["id"],
        "session_id": session_id,
        "segment_id": "earlier",
        "track_id": "replay-audio",
        "revision": 1,
        "language": "zh-CN",
        "raw_text": "earlier caption",
        "display_text": "earlier caption",
        "audio_start_ms": 100,
        "audio_end_ms": 500,
        "confidence": 0.95,
        "status": "final",
        "received_at_ms": 1_700_000_000_001,
        "finalized_at": payload[0]["finalized_at"],
        "created_at": payload[0]["created_at"],
        "updated_at": payload[0]["updated_at"],
    }
    for field in ("raw_payload", "provider_event_id", "api_key", "authorization"):
        assert field not in payload[0]
