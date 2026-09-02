from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.captions.models import CaptionEvent, CaptionStatus
from app.main import create_app
from app.persistence.database import Database
from app.persistence.models import SegmentRecord, SessionRecord, utc_now
from app.persistence.segments import SegmentRepository
from app.settings import Settings


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def caption(
    session_id: str,
    segment_id: str,
    *,
    start_ms: int | None,
    end_ms: int | None,
    text: str,
    revision: int,
) -> CaptionEvent:
    return CaptionEvent(
        session_id=session_id,
        segment_id=segment_id,
        revision=revision,
        status=CaptionStatus.FINAL,
        text=text,
        audio_start_ms=start_ms,
        audio_end_ms=end_ms,
        confidence=0.94,
        provider_event_id=f"stage4-{segment_id}-{revision}",
        received_at_ms=1_700_000_000_000 + revision,
    )


def settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="stage4-local-key",
        livekit_api_secret="stage4-local-secret-that-is-long-enough",
        livekit_room_name="stage4-local",
        database_url=database_url,
        log_level="ERROR",
    )


def verify() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="livecaption-stage4-") as temp_dir:
        database_path = Path(temp_dir) / "stage4.db"
        database_url = f"sqlite:///{database_path.as_posix()}"
        database = Database(database_url)
        database.create_schema()
        app = create_app(settings= settings(database_url), database=database)

        with TestClient(app) as client:
            older = client.post(
                "/api/sessions",
                json={
                    "source_type": "file",
                    "source_name": "older.wav",
                    "language": "zh-CN",
                },
            ).json()
            created = client.post(
                "/api/sessions",
                json={
                    "source_type": "file",
                    "source_name": "阶段四 验收.wav",
                    "language": "zh-CN",
                },
            ).json()
            session_id = str(created["id"])

            with Session(database.engine) as db_session:
                older_record = db_session.get(SessionRecord, older["id"])
                record = db_session.get(SessionRecord, session_id)
                ensure(older_record is not None, "older Session missing")
                ensure(record is not None, "verification Session missing")
                older_record.created_at = dt.datetime(2026, 7, 22, 9, tzinfo=dt.UTC)
                record.created_at = dt.datetime(2026, 7, 22, 10, tzinfo=dt.UTC)
                record.status = "completed"
                record.asr_provider = "fake-stage4"
                record.asr_model = "deterministic-export-model"
                record.final_result_count = 2
                record.first_partial_latency_ms = 110.0
                record.average_final_latency_ms = 620.0
                record.provider_error_count = 0
                record.sent_audio_chunk_count = 30
                record.sent_audio_bytes = 96_000
                record.started_at = record.created_at
                record.ended_at = record.created_at + dt.timedelta(seconds=4)

                repository = SegmentRepository(db_session)
                repository.upsert_final(
                    caption(
                        session_id,
                        "later",
                        start_ms=2_500,
                        end_ms=4_000,
                        text="你好 & <cue> -->\n第二行",
                        revision=4,
                    ),
                    track_id="track-stage4",
                    language="zh-CN",
                )
                repository.upsert_final(
                    caption(
                        session_id,
                        "earlier",
                        start_ms=1_000,
                        end_ms=None,
                        text="第一句",
                        revision=2,
                    ),
                    track_id="track-stage4",
                    language="zh-CN",
                )
                now = utc_now()
                db_session.add(
                    SegmentRecord(
                        id=str(uuid.uuid4()),
                        session_id=session_id,
                        segment_id="draft-sentinel",
                        track_id="track-stage4",
                        revision=99,
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

            history = client.get("/api/sessions")
            ensure(history.status_code == 200, "history endpoint failed")
            history_payload = history.json()
            ensure(history_payload[0]["id"] == session_id, "history order is unstable")
            ensure(
                history_payload[0]["average_final_latency_ms"] == 620.0,
                "persisted metrics missing from history",
            )

            snapshot = client.get(f"/api/sessions/{session_id}/segments")
            ensure(snapshot.status_code == 200, "snapshot endpoint failed")
            ensure(
                [item["segment_id"] for item in snapshot.json()] == ["earlier", "later"],
                "snapshot did not remain Final-only and audio ordered",
            )

            expected = {
                "json": ("application/json", "source.json"),
                "srt": ("application/x-subrip", "source.srt"),
                "vtt": ("text/vtt", "source.vtt"),
                "markdown": ("text/markdown", "source.md"),
            }
            contents: dict[str, bytes] = {}
            hashes: dict[str, str] = {}
            for export_format, (media_type, filename) in expected.items():
                first = client.get(
                    f"/api/sessions/{session_id}/export",
                    params={"format": export_format},
                )
                second = client.get(
                    f"/api/sessions/{session_id}/export",
                    params={"format": export_format},
                )
                ensure(first.status_code == 200, f"{export_format} export failed")
                ensure(first.content == second.content, f"{export_format} export changed")
                ensure(
                    first.headers["content-type"].startswith(media_type),
                    f"{export_format} media type mismatch",
                )
                ensure(
                    first.headers["content-disposition"]
                    == f'attachment; filename="{filename}"',
                    f"{export_format} filename mismatch",
                )
                ensure(
                    b"DRAFT MUST NOT LEAK" not in first.content,
                    f"{export_format} leaked Draft data",
                )
                contents[export_format] = first.content
                hashes[export_format] = hashlib.sha256(first.content).hexdigest()

            transcript = json.loads(contents["json"])
            ensure(
                [item["segment_id"] for item in transcript["segments"]]
                == ["earlier", "later"],
                "JSON segment order mismatch",
            )
            ensure(
                transcript["segments"][0]["audio_end_ms"] == 2_500,
                "missing end time was not normalized",
            )
            ensure(
                transcript["transcription"]["provider"] == "fake-stage4",
                "provider metadata missing",
            )
            lowered_json = contents["json"].lower()
            for forbidden in (b"api_key", b"authorization", b"raw_payload"):
                ensure(forbidden not in lowered_json, f"JSON leaked {forbidden!r}")
            ensure(
                b"00:00:01,000 --> 00:00:02,500" in contents["srt"],
                "SRT timing mismatch",
            )
            ensure(contents["vtt"].startswith(b"WEBVTT\n\n"), "VTT header missing")
            ensure(
                "Source: 阶段四 验收.wav" in contents["markdown"].decode("utf-8"),
                "Markdown source metadata missing",
            )

        return {
            "status": "ok",
            "history_count": len(history_payload),
            "snapshot_final_count": len(snapshot.json()),
            "export_formats": list(expected),
            "deterministic_hashes": hashes,
            "draft_excluded": True,
            "normalized_missing_end_ms": 2_500,
            "metadata_persisted": True,
            "clean_shutdown": True,
        }


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False))
