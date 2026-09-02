from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from app.artifacts.repository import ArtifactRepository
from app.packages import PackageBuilder, PackageRepository
from app.persistence.database import Database
from app.persistence.models import DerivedArtifactRecord
from app.processing.clean_script import CleanScriptWorkflow
from app.processing.bootstrap import build_processor_registry
from app.processing.chapter_outline import ChapterOutlineWorkflow
from app.processing.contracts import PackageReader, ProcessorRegistry
from app.processing.refined_translation import RefinedTranslationWorkflow
from app.processing.summary import SummaryWorkflow
from app.processing.timeline_fact_review import TimelineFactReviewWorkflow
from app.processing.repository import ProcessingJobRepository
from app.processing.service import ArtifactService
from app.settings import Settings
from app.text_processing.parser import ScriptOutputError
from app.text_processing.provider import (
    StructuredCompletionRequest,
    StructuredCompletionResult,
)
from tests.test_packages import NOW, seed_completed_session


class FakeStructuredProvider:
    provider_name = "fake"
    model = "fake-structured-v1"

    def __init__(self, *, foreign_reference: bool = False) -> None:
        self.foreign_reference = foreign_reference
        self.requests: list[StructuredCompletionRequest] = []

    async def complete_structured(
        self,
        request: StructuredCompletionRequest,
    ) -> StructuredCompletionResult:
        self.requests.append(request)
        items = request.input_payload["items"]
        item_ids = [item["item_id"] for item in items]
        if self.foreign_reference:
            item_ids = ["foreign-package-item"]
        return StructuredCompletionResult(
            content=json.dumps(
                {
                    "title": "Clean transcript",
                    "sections": [
                        {
                            "source_item_ids": item_ids,
                            "clean_text": "Final text.",
                            "notes": [],
                        }
                    ],
                    "warnings": [],
                }
            ),
            finish_reason="stop",
        )


def test_default_registry_contains_stage2c_through_stage2e_workflows(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="test-room",
        database_url="sqlite://",
        data_dir=tmp_path / "data",
        log_level="DEBUG",
    )

    registry = build_processor_registry(settings)

    assert isinstance(registry.get("clean_script"), CleanScriptWorkflow)
    assert isinstance(
        registry.get("refined_translation"),
        RefinedTranslationWorkflow,
    )
    assert isinstance(registry.get("summary"), SummaryWorkflow)
    assert isinstance(registry.get("chapter_outline"), ChapterOutlineWorkflow)
    assert isinstance(
        registry.get("timeline_fact_review"),
        TimelineFactReviewWorkflow,
    )


def _service(db_session, provider: FakeStructuredProvider) -> ArtifactService:
    registry = ProcessorRegistry()
    registry.register(
        CleanScriptWorkflow(
            provider,
            max_retries=0,
            max_input_chars=10_000,
            max_items_per_chunk=50,
            retry_delay_seconds=0,
        )
    )
    return ArtifactService(
        package_reader=PackageReader(PackageRepository(db_session)),
        artifact_repository=ArtifactRepository(db_session),
        registry=registry,
    )


def test_clean_script_consumes_package_only_and_maps_server_evidence() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            db_session.commit()
            provider = FakeStructuredProvider()

            with (
                patch(
                    "app.persistence.segments.SegmentRepository.list_final",
                    side_effect=AssertionError("processor read realtime segments"),
                ),
                patch(
                    "app.persistence.translations.TranslationSegmentRepository.list_final",
                    side_effect=AssertionError("processor read realtime translations"),
                ),
            ):
                first = asyncio.run(
                    _service(db_session, provider).generate(
                        package_id=package.package_id,
                        artifact_kind="clean_script",
                        options={},
                    )
                )
            second = asyncio.run(
                _service(db_session, provider).generate(
                    package_id=package.package_id,
                    artifact_kind="clean_script",
                    options={"style": "light"},
                )
            )

            assert first.artifact_version == 1
            assert second.artifact_version == 2
            assert first.package_version == package.package_version
            assert first.package_content_hash == package.content_hash
            assert first.evidence[0].source_segment_ids == ("source-final",)
            assert first.evidence[0].start_ms == 100
            assert first.evidence[0].end_ms == 1_100
            assert first.content["sections"][0]["source_segment_ids"] == [
                "source-final"
            ]
            assert second.options == {"style": "light"}
            assert provider.requests[1].input_payload["style"] == "light"
            assert "source_item_ids" in provider.requests[0].system_prompt
    finally:
        database.dispose()


def test_non_frozen_package_is_rejected() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            session_id = seed_completed_session(db_session)
            building = PackageRepository(db_session).create_building(
                package_id="building-package",
                session_id=session_id,
                version=1,
                source_revision_id=None,
                created_at=NOW,
            )
            db_session.flush()

            with pytest.raises(ValueError, match="not frozen"):
                PackageReader(PackageRepository(db_session)).read(building.id)
    finally:
        database.dispose()


def test_invalid_evidence_failure_saves_no_half_artifact() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            db_session.commit()

            with pytest.raises(ScriptOutputError):
                asyncio.run(
                    _service(
                        db_session,
                        FakeStructuredProvider(foreign_reference=True),
                    ).generate(
                        package_id=package.package_id,
                        artifact_kind="clean_script",
                        options={},
                    )
                )

            assert db_session.scalar(
                select(func.count()).select_from(DerivedArtifactRecord)
            ) == 0
    finally:
        database.dispose()


def test_processing_job_is_created_queued_without_running_inline() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            job = ProcessingJobRepository(db_session).create_queued(
                package_id=package.package_id,
                artifact_kind="clean_script",
                target_artifact_id=None,
                provider="fake",
                model="fake-structured-v1",
                options={},
            )

            assert job.status == "queued"
            assert job.progress == 0
            assert job.started_at is None
            assert job.ended_at is None
    finally:
        database.dispose()
