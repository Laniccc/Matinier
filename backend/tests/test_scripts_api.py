from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.persistence.models import (
    DerivedArtifactRecord,
    ProcessingJobRecord,
    ProcessedScriptRecord,
    SegmentRecord,
)
from app.persistence.segments import SegmentRepository
from app.captions.models import CaptionEvent, CaptionStatus
from app.processing.clean_script import CleanScriptWorkflow
from app.text_processing.parser import ScriptOutputError
from app.text_processing.provider import (
    StructuredCompletionRequest,
    StructuredCompletionResult,
)


def create_session(client: TestClient) -> dict:
    response = client.post(
        "/api/sessions",
        json={
            "source_type": "file",
            "source_name": "stage5-speech.wav",
            "language": "zh-CN",
        },
    )
    assert response.status_code == 201
    return response.json()


def seed_finals(client: TestClient, session_id: str) -> None:
    with Session(client.app.state.database.engine) as db_session:
        repository = SegmentRepository(db_session)
        for segment_id, start_ms, end_ms, text in (
            ("seg-1", 100, 1_500, "大家 好"),
            ("seg-2", 1_600, 3_000, "欢迎 使用"),
        ):
            repository.upsert_final(
                CaptionEvent(
                    session_id=session_id,
                    segment_id=segment_id,
                    revision=2,
                    status=CaptionStatus.FINAL,
                    text=text,
                    audio_start_ms=start_ms,
                    audio_end_ms=end_ms,
                    confidence=0.95,
                    provider_event_id=f"provider-{segment_id}",
                    received_at_ms=1_700_000_000_000 + start_ms,
                ),
                track_id="track-1",
                language="zh-CN",
            )
        db_session.commit()


class FakeStructuredProvider:
    provider_name = "fake"
    model = "fake-clean-script-v1"

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls = 0

    async def complete_structured(
        self,
        request: StructuredCompletionRequest,
    ) -> StructuredCompletionResult:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        items = request.input_payload["items"]
        return StructuredCompletionResult(
            content=json.dumps(
                {
                    "title": "欢迎台本",
                    "sections": [
                        {
                            "source_item_ids": [item["item_id"] for item in items],
                            "clean_text": "大家好，欢迎使用。",
                            "notes": [],
                        }
                    ],
                    "warnings": [],
                },
                ensure_ascii=False,
            ),
            finish_reason="stop",
        )


def configure_provider(
    client: TestClient,
    provider: FakeStructuredProvider,
) -> None:
    client.app.state.processing_job_runner.registry.register(
        CleanScriptWorkflow(
            provider,
            max_retries=0,
            max_input_chars=10_000,
            max_items_per_chunk=50,
            retry_delay_seconds=0,
        ),
        replace=True,
    )


def counts(client: TestClient) -> tuple[int, int]:
    with Session(client.app.state.database.engine) as db_session:
        return (
            int(
                db_session.scalar(
                    select(func.count()).select_from(DerivedArtifactRecord)
                )
                or 0
            ),
            int(
                db_session.scalar(
                    select(func.count()).select_from(ProcessedScriptRecord)
                )
                or 0
            ),
        )


def test_deprecated_script_routes_delegate_to_package_artifacts(
    client: TestClient,
) -> None:
    created = create_session(client)
    seed_finals(client, created["id"])
    provider = FakeStructuredProvider()
    configure_provider(client, provider)

    first = client.post(f"/api/sessions/{created['id']}/scripts")
    second = client.post(f"/api/sessions/{created['id']}/scripts")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["version"] == 1
    assert second.json()["version"] == 1
    assert first.json()["package_version"] == 1
    assert second.json()["package_version"] == 2
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["provider"] == "fake"
    assert first.json()["content"]["title"] == "欢迎台本"
    assert first.json()["content"]["sections"][0]["source_segment_ids"] == [
        "seg-1",
        "seg-2",
    ]
    assert [
        item["segment_id"] for item in first.json()["source_segment_snapshot"]
    ] == ["seg-1", "seg-2"]
    assert "大家好，欢迎使用。" in first.json()["markdown_text"]

    history = client.get(f"/api/sessions/{created['id']}/scripts")
    assert history.status_code == 200
    assert [item["package_version"] for item in history.json()] == [2, 1]
    assert "content" not in history.json()[0]

    detail = client.get(f"/api/scripts/{first.json()['id']}")
    assert detail.status_code == 200
    assert detail.json() == first.json()

    exported = client.get(f"/api/scripts/{first.json()['id']}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/markdown")
    assert (
        exported.headers["content-disposition"]
        == 'attachment; filename="clean-script-v1.md"'
    )
    assert exported.text == first.json()["markdown_text"]
    assert provider.calls == 2
    assert counts(client) == (2, 0)


def test_empty_session_and_missing_key_do_not_save_artifacts(
    client: TestClient,
) -> None:
    empty = create_session(client)
    provider = FakeStructuredProvider()
    configure_provider(client, provider)
    empty_response = client.post(f"/api/sessions/{empty['id']}/scripts")
    assert empty_response.status_code == 409
    assert empty_response.json() == {"detail": "Session has no Final captions"}
    assert provider.calls == 0

    configured = create_session(client)
    seed_finals(client, configured["id"])
    # Restore the default unconfigured DeepSeek workflow.
    from app.processing.bootstrap import build_processor_registry

    default_workflow = build_processor_registry(
        client.app.state.settings
    ).get("clean_script")
    client.app.state.processing_job_runner.registry.register(
        default_workflow,
        replace=True,
    )
    missing_key = client.post(f"/api/sessions/{configured['id']}/scripts")
    assert missing_key.status_code == 503
    assert missing_key.json() == {"detail": "DeepSeek is not configured"}
    assert counts(client) == (0, 0)


def test_failure_is_sanitized_and_stage1_segments_are_unchanged(
    client: TestClient,
) -> None:
    created = create_session(client)
    seed_finals(client, created["id"])
    provider = FakeStructuredProvider(
        fail=ScriptOutputError("invalid output contains secret")
    )
    configure_provider(client, provider)
    with Session(client.app.state.database.engine) as db_session:
        before = [
            (
                row.id,
                row.segment_id,
                row.revision,
                row.raw_text,
                row.display_text,
                row.audio_start_ms,
                row.audio_end_ms,
                row.updated_at,
            )
            for row in db_session.scalars(
                select(SegmentRecord)
                .where(SegmentRecord.session_id == created["id"])
                .order_by(SegmentRecord.id)
            )
        ]

    response = client.post(f"/api/sessions/{created['id']}/scripts")

    assert response.status_code == 502
    assert response.json() == {"detail": "Script generation failed"}
    assert "secret" not in response.text
    assert counts(client) == (0, 0)
    with Session(client.app.state.database.engine) as db_session:
        failed_job = db_session.scalar(
            select(ProcessingJobRecord).where(
                ProcessingJobRecord.status == "failed"
            )
        )
        assert failed_job is not None
        assert "secret" not in (failed_job.error_message or "")
        after = [
            (
                row.id,
                row.segment_id,
                row.revision,
                row.raw_text,
                row.display_text,
                row.audio_start_ms,
                row.audio_end_ms,
                row.updated_at,
            )
            for row in db_session.scalars(
                select(SegmentRecord)
                .where(SegmentRecord.session_id == created["id"])
                .order_by(SegmentRecord.id)
            )
        ]
    assert after == before


def test_missing_sessions_and_script_artifacts_return_404(
    client: TestClient,
) -> None:
    assert client.post("/api/sessions/missing/scripts").status_code == 404
    assert client.get("/api/sessions/missing/scripts").status_code == 404
    assert client.get("/api/scripts/missing").status_code == 404
    assert client.get("/api/scripts/missing/export").status_code == 404
