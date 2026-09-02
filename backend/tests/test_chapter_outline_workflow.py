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
from app.processing.chapter_outline import ChapterOutlineWorkflow
from app.processing.contracts import PackageReader, ProcessorRegistry
from app.processing.service import ArtifactService
from app.text_processing.parser import ScriptOutputError
from app.text_processing.provider import (
    StructuredCompletionRequest,
    StructuredCompletionResult,
)
from tests.test_packages import NOW, seed_completed_session


class FakeChapterProvider:
    provider_name = "fake"
    model = "fake-chapters-v1"

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
                    "chapters": [
                        {
                            "title": f"Chapter {items[0]['text']}",
                            "summary": f"Summary {items[0]['text']}",
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
    provider: FakeChapterProvider,
    *,
    max_items_per_chunk: int = 50,
) -> ArtifactService:
    registry = ProcessorRegistry()
    registry.register(
        ChapterOutlineWorkflow(
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


def _package_with_three_items(db_session):
    session_id = seed_completed_session(db_session)
    _add_final(db_session, session_id, "source-2", 1_200, 2_000, "第二项")
    _add_final(db_session, session_id, "source-3", 2_100, 3_000, "第三项")
    package = PackageBuilder(db_session).build_baseline(session_id)
    db_session.commit()
    return package


def test_chapter_outline_derives_ordered_timing_and_excerpts() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = _package_with_three_items(db_session)
            provider = FakeChapterProvider()

            artifact = asyncio.run(
                _service(db_session, provider).generate(
                    package_id=package.package_id,
                    artifact_kind="chapter_outline",
                    options={"style": " editorial "},
                )
            )

            source_items = PackageReader.effective_source(package).content.items
            chapter = artifact.content["chapters"][0]
            assert artifact.options == {"style": "editorial"}
            assert chapter["evidence_item_ids"] == [
                item.item_id for item in source_items
            ]
            assert chapter["start_ms"] == 100
            assert chapter["end_ms"] == 3_000
            assert [item["text"] for item in chapter["evidence_excerpts"]] == [
                item.text for item in source_items
            ]
            assert chapter["source_segment_ids"] == [
                "source-final",
                "source-2",
                "source-3",
            ]
            assert provider.requests[0].input_payload["style"] == "editorial"
    finally:
        database.dispose()


def test_chapter_outline_rejects_global_time_reversal_without_artifact() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = _package_with_three_items(db_session)
            items = PackageReader.effective_source(package).content.items
            provider = FakeChapterProvider(
                [
                    StructuredCompletionResult(
                        content=json.dumps(
                            {
                                "chapters": [
                                    {
                                        "title": "Later",
                                        "summary": "Later chapter",
                                        "evidence_item_ids": [items[2].item_id],
                                    },
                                    {
                                        "title": "Earlier",
                                        "summary": "Earlier chapter",
                                        "evidence_item_ids": [items[0].item_id],
                                    },
                                ],
                                "warnings": [],
                            }
                        ),
                        finish_reason="stop",
                    )
                ]
            )

            with pytest.raises(ScriptOutputError, match="not ordered"):
                asyncio.run(
                    _service(db_session, provider).generate(
                        package_id=package.package_id,
                        artifact_kind="chapter_outline",
                        options={},
                    )
                )
            assert db_session.scalar(
                select(func.count()).select_from(DerivedArtifactRecord)
            ) == 0
    finally:
        database.dispose()


def test_chapter_outline_chunk_merge_is_stable_and_time_ordered() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = _package_with_three_items(db_session)
            provider = FakeChapterProvider()

            artifact = asyncio.run(
                _service(
                    db_session,
                    provider,
                    max_items_per_chunk=1,
                ).generate(
                    package_id=package.package_id,
                    artifact_kind="chapter_outline",
                    options={},
                )
            )

            items = PackageReader.effective_source(package).content.items
            chapters = artifact.content["chapters"]
            assert len(provider.requests) == 3
            assert [chapter["evidence_item_ids"][0] for chapter in chapters] == [
                item.item_id for item in items
            ]
            assert [chapter["start_ms"] for chapter in chapters] == [
                item.start_ms for item in items
            ]
            assert [evidence.evidence_key for evidence in artifact.evidence] == [
                "chapter:0",
                "chapter:1",
                "chapter:2",
            ]
    finally:
        database.dispose()
