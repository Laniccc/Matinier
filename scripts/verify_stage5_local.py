from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.captions.models import CaptionEvent, CaptionStatus
from app.main import create_app
from app.persistence.database import Database
from app.persistence.models import (
    DerivedArtifactRecord,
    SegmentRecord,
    SessionRecord,
    utc_now,
)
from app.persistence.segments import SegmentRepository
from app.processing.clean_script import CleanScriptWorkflow
from app.processing.contracts import ProcessorRegistry
from app.processing.runner import ProcessingJobRunner
from app.settings import Settings
from app.text_processing.provider import (
    StructuredCompletionRequest,
    StructuredCompletionResult,
)


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class FixedCompletionProvider:
    provider_name = "deepseek"
    model = "deepseek-v4-flash"

    def __init__(self) -> None:
        self.invalid = False
        self.call_count = 0

    async def complete_structured(
        self,
        request: StructuredCompletionRequest,
    ) -> StructuredCompletionResult:
        self.call_count += 1
        if self.invalid:
            return StructuredCompletionResult(
                content='{"title":"truncated"',
                finish_reason="stop",
            )
        items = request.input_payload["items"]
        payload = {
            "title": "阶段五整理版台本",
            "sections": [
                {
                    "source_item_ids": [item["item_id"] for item in items],
                    "clean_text": "大家好，欢迎使用实时字幕工作台。",
                    "notes": [],
                }
            ],
            "warnings": [],
        }
        return StructuredCompletionResult(
            content=json.dumps(payload, ensure_ascii=False),
            finish_reason="stop",
        )


def verifier_settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="stage5-local-key",
        livekit_api_secret="stage5-local-secret-that-is-long-enough",
        livekit_room_name="stage5-local",
        database_url=database_url,
        log_level="ERROR",
    )


def final_caption(
    session_id: str,
    segment_id: str,
    *,
    start_ms: int,
    end_ms: int,
    text: str,
) -> CaptionEvent:
    return CaptionEvent(
        session_id=session_id,
        segment_id=segment_id,
        revision=2,
        status=CaptionStatus.FINAL,
        text=text,
        audio_start_ms=start_ms,
        audio_end_ms=end_ms,
        confidence=0.95,
        provider_event_id=f"stage5-{segment_id}",
        received_at_ms=1_700_000_000_000 + start_ms,
    )


def source_signature(database: Database, session_id: str) -> list[tuple[object, ...]]:
    with Session(database.engine) as db_session:
        return [
            (
                record.id,
                record.segment_id,
                record.revision,
                record.raw_text,
                record.display_text,
                record.audio_start_ms,
                record.audio_end_ms,
                record.updated_at,
            )
            for record in db_session.scalars(
                select(SegmentRecord)
                .where(SegmentRecord.session_id == session_id)
                .order_by(SegmentRecord.id)
            )
        ]


def export_hashes(client: TestClient, session_id: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for export_format in ("json", "srt", "vtt", "markdown"):
        response = client.get(
            f"/api/sessions/{session_id}/export",
            params={"format": export_format},
        )
        ensure(response.status_code == 200, f"{export_format} export failed")
        hashes[export_format] = hashlib.sha256(response.content).hexdigest()
    return hashes


def verify() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="livecaption-stage5-") as temp_dir:
        database_path = Path(temp_dir) / "stage5.db"
        database_url = f"sqlite:///{database_path.as_posix()}"
        database = Database(database_url)
        database.create_schema()
        provider = FixedCompletionProvider()
        registry = ProcessorRegistry()
        registry.register(
            CleanScriptWorkflow(
                provider,
                max_retries=1,
                max_input_chars=12_000,
                max_items_per_chunk=40,
                retry_delay_seconds=0,
            )
        )
        runner = ProcessingJobRunner(database, registry)
        app = create_app(
            settings=verifier_settings(database_url),
            database=database,
            processing_job_runner=runner,
        )

        with TestClient(app) as client:
            created_response = client.post(
                "/api/sessions",
                json={
                    "source_type": "file",
                    "source_name": "stage5-verification.wav",
                    "language": "zh-CN",
                },
            )
            ensure(created_response.status_code == 201, "Session creation failed")
            session_id = str(created_response.json()["id"])
            with Session(database.engine) as db_session:
                session_record = db_session.get(SessionRecord, session_id)
                assert session_record is not None
                session_record.status = "completed"
                session_record.ended_at = utc_now()
                repository = SegmentRepository(db_session)
                repository.upsert_final(
                    final_caption(
                        session_id,
                        "seg-1",
                        start_ms=100,
                        end_ms=1_500,
                        text="大家 好",
                    ),
                    track_id="track-stage5",
                    language="zh-CN",
                )
                repository.upsert_final(
                    final_caption(
                        session_id,
                        "seg-2",
                        start_ms=1_600,
                        end_ms=3_000,
                        text="欢迎 使用实时字幕工作台",
                    ),
                    track_id="track-stage5",
                    language="zh-CN",
                )
                db_session.commit()

            original_signature = source_signature(database, session_id)
            original_hashes = export_hashes(client, session_id)
            first = client.post(f"/api/sessions/{session_id}/scripts")
            second = client.post(f"/api/sessions/{session_id}/scripts")
            ensure(first.status_code == 201, "first script generation failed")
            ensure(second.status_code == 201, "second script generation failed")
            ensure(first.json()["version"] == 1, "first version mismatch")
            ensure(second.json()["version"] == 1, "second artifact version mismatch")
            ensure(first.json()["package_version"] == 1, "first Package mismatch")
            ensure(second.json()["package_version"] == 2, "second Package mismatch")

            history = client.get(f"/api/sessions/{session_id}/scripts")
            ensure(history.status_code == 200, "script history failed")
            versions = [item["package_version"] for item in history.json()]
            ensure(versions == [2, 1], "Package history is not append-only")

            detail = client.get(f"/api/scripts/{first.json()['id']}")
            exported = client.get(
                f"/api/scripts/{first.json()['id']}/export"
            )
            ensure(detail.status_code == 200, "script detail failed")
            ensure(exported.status_code == 200, "script export failed")
            ensure(
                exported.headers["content-disposition"]
                == 'attachment; filename="clean-script-v1.md"',
                "processed Markdown filename mismatch",
            )
            referenced_ids = [
                segment_id
                for section in detail.json()["content"]["sections"]
                for segment_id in section["source_segment_ids"]
            ]
            ensure(
                referenced_ids == ["seg-1", "seg-2"],
                "source references are incomplete",
            )
            ensure(
                "Source segments: `seg-1`, `seg-2`" in exported.text,
                "processed Markdown lacks traceability",
            )

            provider.invalid = True
            invalid = client.post(f"/api/sessions/{session_id}/scripts")
            ensure(invalid.status_code == 502, "invalid output was accepted")
            with Session(database.engine) as db_session:
                stored_count = int(
                    db_session.scalar(
                        select(func.count()).select_from(DerivedArtifactRecord)
                    )
                    or 0
                )
            ensure(stored_count == 2, "invalid output created an Artifact row")
            ensure(
                source_signature(database, session_id) == original_signature,
                "source Final rows changed during post-processing",
            )
            ensure(
                export_hashes(client, session_id) == original_hashes,
                "source exports changed during post-processing",
            )
            lowered = (
                first.content
                + second.content
                + detail.content
                + exported.content
            ).lower()
            for forbidden in (b"api_key", b"authorization", b"raw_payload"):
                ensure(forbidden not in lowered, f"response leaked {forbidden!r}")

        return {
            "status": "ok",
            "versions": versions,
            "artifact_count": stored_count,
            "source_segment_count": len(original_signature),
            "referenced_source_ids": referenced_ids,
            "source_rows_unchanged": True,
            "source_exports_unchanged": True,
            "invalid_output_rejected": True,
            "provider_call_count": provider.call_count,
            "clean_shutdown": True,
        }


def main() -> None:
    result = verify()
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
