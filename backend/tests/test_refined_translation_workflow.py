from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import func, select

from app.artifacts.repository import ArtifactRepository
from app.packages import PackageBuilder, PackageRepository
from app.persistence.database import Database
from app.persistence.models import DerivedArtifactRecord
from app.processing.contracts import PackageReader, ProcessorRegistry
from app.processing.refined_translation import RefinedTranslationWorkflow
from app.processing.service import ArtifactService
from app.text_processing.parser import ScriptOutputError
from app.text_processing.provider import (
    StructuredCompletionRequest,
    StructuredCompletionResult,
)
from tests.test_packages import seed_completed_session


class FakeTranslationProvider:
    provider_name = "fake"
    model = "fake-translation-v1"

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
        target_language = request.input_payload["target_language"]
        return StructuredCompletionResult(
            content=json.dumps(
                {
                    "sections": [
                        {
                            "source_item_ids": [
                                item["item_id"] for item in items
                            ],
                            "translated_text": f"Translation {target_language}",
                            "notes": [],
                        }
                    ],
                    "warnings": [],
                }
            ),
            finish_reason="stop",
        )


def _service(
    db_session,
    provider: FakeTranslationProvider,
    *,
    max_retries: int = 0,
) -> ArtifactService:
    registry = ProcessorRegistry()
    registry.register(
        RefinedTranslationWorkflow(
            provider,
            max_retries=max_retries,
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


def test_refined_translation_uses_source_truth_glossary_and_server_timing() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            db_session.commit()
            provider = FakeTranslationProvider()

            artifact = asyncio.run(
                _service(db_session, provider).generate(
                    package_id=package.package_id,
                    artifact_kind="refined_translation",
                    options={
                        "target_language": "EN-us",
                        "glossary": {" 最终文本 ": " final transcript "},
                        "style": " formal ",
                        "context_window_items": 0,
                    },
                )
            )

            assert artifact.target_language == "en-US"
            assert artifact.artifact_version == 1
            assert artifact.options == {
                "target_language": "en-US",
                "glossary": {"最终文本": "final transcript"},
                "style": "formal",
                "context_window_items": 0,
            }
            assert artifact.content["target_language"] == "en-US"
            section = artifact.content["sections"][0]
            assert section["source_text"] == "最终文本"
            assert section["translated_text"] == "Translation en-US"
            assert section["source_segment_ids"] == ["source-final"]
            assert section["start_ms"] == 100
            assert section["end_ms"] == 1_100
            assert artifact.evidence[0].start_ms == 100
            assert artifact.evidence[0].end_ms == 1_100

            payload = provider.requests[0].input_payload
            assert payload["glossary"] == {"最终文本": "final transcript"}
            assert payload["style"] == "formal"
            assert payload["items"][0]["text"] == "最终文本"
            assert payload["live_translation_reference"] == [
                {
                    "text": "Final text",
                    "source_segment_ids": ["source-final"],
                }
            ]
    finally:
        database.dispose()


def test_target_language_has_independent_versions_without_live_reference() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            db_session.commit()
            provider = FakeTranslationProvider()
            service = _service(db_session, provider)

            english_v1 = asyncio.run(
                service.generate(
                    package_id=package.package_id,
                    artifact_kind="refined_translation",
                    options={"target_language": "en-US"},
                )
            )
            japanese_v1 = asyncio.run(
                service.generate(
                    package_id=package.package_id,
                    artifact_kind="refined_translation",
                    options={"target_language": "ja-JP"},
                )
            )
            english_v2 = asyncio.run(
                service.generate(
                    package_id=package.package_id,
                    artifact_kind="refined_translation",
                    options={"target_language": "en-US"},
                )
            )

            assert english_v1.artifact_version == 1
            assert english_v2.artifact_version == 2
            assert japanese_v1.artifact_version == 1
            assert provider.requests[1].input_payload[
                "live_translation_reference"
            ] == []
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("content", "finish_reason"),
    [("{", "stop"), ("{}", "length")],
)
def test_invalid_or_truncated_translation_saves_no_artifact(
    content: str,
    finish_reason: str,
) -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            db_session.commit()
            provider = FakeTranslationProvider(
                [
                    StructuredCompletionResult(
                        content=content,
                        finish_reason=finish_reason,
                    )
                ]
            )

            with pytest.raises(ScriptOutputError):
                asyncio.run(
                    _service(db_session, provider).generate(
                        package_id=package.package_id,
                        artifact_kind="refined_translation",
                        options={"target_language": "en-US"},
                    )
                )
            assert db_session.scalar(
                select(func.count()).select_from(DerivedArtifactRecord)
            ) == 0
    finally:
        database.dispose()


def test_invalid_json_is_retried_and_only_complete_artifact_is_saved() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            db_session.commit()
            source_item_id = PackageReader.effective_source(
                package
            ).content.items[0].item_id
            provider = FakeTranslationProvider(
                [
                    StructuredCompletionResult(
                        content="not-json",
                        finish_reason="stop",
                    ),
                    StructuredCompletionResult(
                        content=json.dumps(
                            {
                                "sections": [
                                    {
                                        "source_item_ids": [source_item_id],
                                        "translated_text": "Retried translation",
                                        "notes": [],
                                    }
                                ],
                                "warnings": [],
                            }
                        ),
                        finish_reason="stop",
                    ),
                ]
            )

            artifact = asyncio.run(
                _service(db_session, provider, max_retries=1).generate(
                    package_id=package.package_id,
                    artifact_kind="refined_translation",
                    options={"target_language": "en-US"},
                )
            )

            assert len(provider.requests) == 2
            assert artifact.content["sections"][0]["translated_text"] == (
                "Retried translation"
            )
            assert db_session.scalar(
                select(func.count()).select_from(DerivedArtifactRecord)
            ) == 1
    finally:
        database.dispose()
