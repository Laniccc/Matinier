from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import func, select

from app.artifacts.repository import ArtifactRepository
from app.packages import PackageBuilder, PackageRepository
from app.persistence.database import Database
from app.persistence.models import DerivedArtifactRecord
from app.processing.contracts import EvidenceValidator, PackageReader, ProcessorRegistry
from app.processing.service import ArtifactService
from app.processing.timeline_fact_review import TimelineFactReviewWorkflow
from app.text_processing.parser import ScriptOutputError
from app.text_processing.provider import (
    StructuredCompletionRequest,
    StructuredCompletionResult,
)
from tests.test_packages import seed_completed_session


class FakeFactReviewProvider:
    provider_name = "fake"
    model = "fake-fact-review-v1"

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
        evidence_item_id = request.input_payload["package_items"][0]["item_id"]
        return StructuredCompletionResult(
            content=json.dumps(
                {
                    "reviews": [
                        {
                            "claim_id": claim["claim_id"],
                            "status": "supported",
                            "evidence_item_ids": [evidence_item_id],
                            "explanation": "Supported by the Package transcript",
                        }
                        for claim in reversed(request.input_payload["claims"])
                    ],
                    "warnings": [],
                }
            ),
            finish_reason="stop",
        )


def _service(db_session, provider: FakeFactReviewProvider) -> ArtifactService:
    registry = ProcessorRegistry()
    registry.register(
        TimelineFactReviewWorkflow(
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


def _target_content(kind: str, item_id: str) -> dict[str, object]:
    if kind == "summary":
        return {
            "brief": "Brief",
            "key_points": [
                {"text": "Summary claim", "evidence_item_ids": [item_id]}
            ],
            "warnings": [],
        }
    if kind == "chapter_outline":
        return {
            "chapters": [
                {
                    "title": "Opening",
                    "summary": "Chapter claim",
                    "evidence_item_ids": [item_id],
                }
            ],
            "warnings": [],
        }
    text_field = "clean_text" if kind == "clean_script" else "translated_text"
    return {
        "sections": [
            {
                "source_item_ids": [item_id],
                text_field: "Section claim",
                "notes": [],
            }
        ],
        "warnings": [],
        **({"target_language": "en-US"} if kind == "refined_translation" else {}),
        **({"title": "Clean"} if kind == "clean_script" else {}),
    }


def _create_target(db_session, package, kind: str):
    item = PackageReader.effective_source(package).content.items[0]
    key_prefix = {
        "summary": "key_point",
        "chapter_outline": "chapter",
        "clean_script": "section",
        "refined_translation": "section",
    }[kind]
    evidence = EvidenceValidator().derive(
        package,
        evidence_key=f"{key_prefix}:0",
        source_item_ids=(item.item_id,),
    )
    return ArtifactRepository(db_session).create_generated(
        package=package,
        artifact_kind=kind,
        target_language="en-US" if kind == "refined_translation" else None,
        provider="fake",
        model="fake-target-v1",
        workflow_version="1.0",
        options={},
        content=_target_content(kind, item.item_id),
        evidence=(evidence,),
    )


def _outcome(
    *,
    status: str,
    evidence_item_ids: list[str],
    explanation: str | None,
) -> StructuredCompletionResult:
    return StructuredCompletionResult(
        content=json.dumps(
            {
                "reviews": [
                    {
                        "claim_id": "claim:0",
                        "status": status,
                        "evidence_item_ids": evidence_item_ids,
                        "explanation": explanation,
                    }
                ],
                "warnings": [],
            }
        ),
        finish_reason="stop",
    )


@pytest.mark.parametrize(
    "target_kind",
    ["summary", "clean_script", "refined_translation", "chapter_outline"],
)
def test_fact_review_binds_allowed_target_without_mutating_it(
    target_kind: str,
) -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            target = _create_target(db_session, package, target_kind)
            db_session.commit()
            db_session.expire_all()
            target = ArtifactRepository(db_session).get(target.artifact_id)
            assert target is not None
            target_before = target.model_dump(mode="json")
            provider = FakeFactReviewProvider()

            artifact = asyncio.run(
                _service(db_session, provider).generate(
                    package_id=package.package_id,
                    artifact_kind="timeline_fact_review",
                    options={},
                    target_artifact_id=target.artifact_id,
                )
            )

            assert artifact.parent_artifact_id == target.artifact_id
            assert artifact.options == {
                "target_artifact_id": target.artifact_id,
                "target_artifact_version": target.artifact_version,
            }
            assert artifact.content["target_artifact"] == {
                "artifact_id": target.artifact_id,
                "artifact_kind": target_kind,
                "artifact_version": 1,
                "package_id": package.package_id,
                "package_content_hash": package.content_hash,
            }
            review = artifact.content["reviews"][0]
            assert review["status"] == "supported"
            assert review["start_ms"] == 100
            assert review["end_ms"] == 1_100
            assert review["evidence_excerpts"][0]["text"] == "最终文本"
            assert artifact.evidence[0].evidence_key == "claim:0"
            assert ArtifactRepository(db_session).get(
                target.artifact_id
            ).model_dump(mode="json") == target_before
            payload = provider.requests[0].input_payload
            assert payload["review_scope"] == "package_internal_only"
            assert set(payload) == {
                "review_scope",
                "target_artifact",
                "claims",
                "package_items",
            }
            assert "web" not in json.dumps(payload).lower()
    finally:
        database.dispose()


def test_unsupported_fact_review_may_have_no_evidence() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            target = _create_target(db_session, package, "summary")
            db_session.commit()
            provider = FakeFactReviewProvider(
                [
                    _outcome(
                        status="unsupported",
                        evidence_item_ids=[],
                        explanation="No Package evidence",
                    )
                ]
            )

            artifact = asyncio.run(
                _service(db_session, provider).generate(
                    package_id=package.package_id,
                    artifact_kind="timeline_fact_review",
                    options={},
                    target_artifact_id=target.artifact_id,
                )
            )

            assert artifact.evidence == ()
            review = artifact.content["reviews"][0]
            assert review["status"] == "unsupported"
            assert review["evidence_item_ids"] == []
            assert review["time_ranges"] == []
            assert review["evidence_excerpts"] == []
            assert review["start_ms"] is None
            assert review["end_ms"] is None
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("status", "explanation"),
    [("supported", "Claimed support"), ("contradicted", None)],
)
def test_fact_review_rejects_status_without_required_support(
    status: str,
    explanation: str | None,
) -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            target = _create_target(db_session, package, "summary")
            db_session.commit()
            provider = FakeFactReviewProvider(
                [
                    _outcome(
                        status=status,
                        evidence_item_ids=[],
                        explanation=explanation,
                    )
                ]
            )

            with pytest.raises(ScriptOutputError, match="not valid"):
                asyncio.run(
                    _service(db_session, provider).generate(
                        package_id=package.package_id,
                        artifact_kind="timeline_fact_review",
                        options={},
                        target_artifact_id=target.artifact_id,
                    )
                )
            assert db_session.scalar(
                select(func.count()).select_from(DerivedArtifactRecord)
            ) == 1
    finally:
        database.dispose()


def test_fact_review_rejects_foreign_package_evidence() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            target = _create_target(db_session, package, "summary")
            db_session.commit()
            provider = FakeFactReviewProvider(
                [
                    _outcome(
                        status="supported",
                        evidence_item_ids=["foreign-item"],
                        explanation="Invalid evidence",
                    )
                ]
            )

            with pytest.raises(ScriptOutputError, match="foreign Package items"):
                asyncio.run(
                    _service(db_session, provider).generate(
                        package_id=package.package_id,
                        artifact_kind="timeline_fact_review",
                        options={},
                        target_artifact_id=target.artifact_id,
                    )
                )
            assert db_session.scalar(
                select(func.count()).select_from(DerivedArtifactRecord)
            ) == 1
    finally:
        database.dispose()
