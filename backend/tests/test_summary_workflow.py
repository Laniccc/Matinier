from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import func, select

from app.artifacts.repository import ArtifactRepository
from app.captions.models import CaptionEvent, CaptionStatus
from app.packages import PackageBuilder, PackageRepository
from app.persistence.database import Database
from app.persistence.models import DerivedArtifactRecord
from app.persistence.segments import SegmentRepository
from app.processing.contracts import PackageReader, ProcessorRegistry
from app.processing.service import ArtifactService
from app.processing.summary import SummaryWorkflow
from app.text_processing.parser import ScriptOutputError
from app.text_processing.provider import (
    StructuredCompletionRequest,
    StructuredCompletionResult,
)
from tests.test_packages import NOW, seed_completed_session


class FakeSummaryProvider:
    provider_name = "fake"
    model = "fake-summary-v1"

    def __init__(
        self,
        outcomes: list[StructuredCompletionResult] | None = None,
    ) -> None:
        self.outcomes = list(outcomes or [])
        self.requests: list[StructuredCompletionRequest] = []

    async def complete_structured(
        self,
        request: StructuredCompletionRequest,
    ) -> StructuredCompletionResult:
        self.requests.append(request)
        if self.outcomes:
            return self.outcomes.pop(0)
        items = request.input_payload["items"]
        return StructuredCompletionResult(
            content=json.dumps(
                {
                    "brief": f"Brief: {items[0]['text']}",
                    "key_points": [
                        {
                            "text": f"Point: {items[0]['text']}",
                            "evidence_item_ids": [
                                item["item_id"] for item in reversed(items)
                            ],
                        }
                    ],
                    "warnings": [],
                },
                ensure_ascii=False,
            ),
            finish_reason="stop",
        )


def _service(
    db_session,
    provider: FakeSummaryProvider,
    *,
    max_items_per_chunk: int = 50,
) -> ArtifactService:
    registry = ProcessorRegistry()
    registry.register(
        SummaryWorkflow(
            provider,
            max_retries=0,
            max_input_chars=10_000,
            max_items_per_chunk=max_items_per_chunk,
            retry_delay_seconds=0,
        )
    )
    return ArtifactService(
        package_reader=PackageReader(PackageRepository(db_session)),
        artifact_repository=ArtifactRepository(db_session),
        registry=registry,
    )


def _add_final(
    db_session,
    session_id: str,
    segment_id: str,
    start_ms: int,
    end_ms: int,
    text: str,
) -> None:
    SegmentRepository(db_session).upsert_final(
        CaptionEvent(
            session_id=session_id,
            segment_id=segment_id,
            revision=1,
            status=CaptionStatus.FINAL,
            text=text,
            audio_start_ms=start_ms,
            audio_end_ms=end_ms,
            confidence=0.9,
            provider_event_id=f"provider-{segment_id}",
            received_at_ms=int(NOW.timestamp() * 1_000) + start_ms,
        ),
        track_id="track-1",
        language="zh-CN",
    )


def test_summary_derives_ordered_times_and_excerpts_from_package() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            db_session.commit()
            provider = FakeSummaryProvider()

            artifact = asyncio.run(
                _service(db_session, provider).generate(
                    package_id=package.package_id,
                    artifact_kind="summary",
                    options={"style": " concise "},
                )
            )

            item = PackageReader.effective_source(package).content.items[0]
            assert artifact.artifact_version == 1
            assert artifact.options == {"style": "concise"}
            assert artifact.content["brief"] == "Brief: 最终文本"
            point = artifact.content["key_points"][0]
            assert point["evidence_item_ids"] == [item.item_id]
            assert point["source_segment_ids"] == ["source-final"]
            assert point["start_ms"] == 100
            assert point["end_ms"] == 1_100
            assert point["time_ranges"] == [
                {"item_id": item.item_id, "start_ms": 100, "end_ms": 1_100}
            ]
            assert point["evidence_excerpts"] == [
                {
                    "item_id": item.item_id,
                    "text": "最终文本",
                    "source_segment_ids": ["source-final"],
                    "start_ms": 100,
                    "end_ms": 1_100,
                }
            ]
            assert artifact.evidence[0].evidence_key == "key_point:0"
            assert provider.requests[0].input_payload["style"] == "concise"
            assert set(provider.requests[0].input_payload) == {"style", "items"}
    finally:
        database.dispose()


def test_summary_rejects_foreign_item_id_without_saving_artifact() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            db_session.commit()
            provider = FakeSummaryProvider(
                [
                    StructuredCompletionResult(
                        content=json.dumps(
                            {
                                "brief": "Invalid",
                                "key_points": [
                                    {
                                        "text": "Foreign claim",
                                        "evidence_item_ids": ["foreign-item"],
                                    }
                                ],
                                "warnings": [],
                            }
                        ),
                        finish_reason="stop",
                    )
                ]
            )

            with pytest.raises(ScriptOutputError, match="foreign Package items"):
                asyncio.run(
                    _service(db_session, provider).generate(
                        package_id=package.package_id,
                        artifact_kind="summary",
                        options={},
                    )
                )
            assert db_session.scalar(
                select(func.count()).select_from(DerivedArtifactRecord)
            ) == 0
    finally:
        database.dispose()


def test_long_summary_chunks_merge_in_package_order_stably() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            session_id = seed_completed_session(db_session)
            _add_final(db_session, session_id, "source-2", 1_200, 2_000, "第二项")
            _add_final(db_session, session_id, "source-3", 2_100, 3_000, "第三项")
            package = PackageBuilder(db_session).build_baseline(session_id)
            db_session.commit()
            provider = FakeSummaryProvider()

            artifact = asyncio.run(
                _service(
                    db_session,
                    provider,
                    max_items_per_chunk=1,
                ).generate(
                    package_id=package.package_id,
                    artifact_kind="summary",
                    options={},
                )
            )

            source_items = PackageReader.effective_source(package).content.items
            assert len(provider.requests) == 3
            assert artifact.content["brief"].split("\n\n") == [
                f"Brief: {item.text}" for item in source_items
            ]
            assert [
                point["evidence_item_ids"][0]
                for point in artifact.content["key_points"]
            ] == [item.item_id for item in source_items]
            assert [entry.evidence_key for entry in artifact.evidence] == [
                "key_point:0",
                "key_point:1",
                "key_point:2",
            ]
            assert [entry.start_ms for entry in artifact.evidence] == [
                item.start_ms for item in source_items
            ]
    finally:
        database.dispose()
