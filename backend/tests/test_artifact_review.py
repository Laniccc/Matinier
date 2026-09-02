from __future__ import annotations

import copy

from app.artifacts import (
    ArtifactEvidence,
    ArtifactRepository,
    ArtifactReviewService,
)
from app.packages import PackageBuilder
from app.persistence.database import Database
from tests.test_packages import seed_completed_session


def _create_clean(repository: ArtifactRepository, package, text: str):
    source = next(
        document
        for document in package.documents
        if document.document_id
        == package.manifest.effective_source_document_id
    )
    item = source.content.items[0]
    return repository.create_generated(
        package=package,
        artifact_kind="clean_script",
        target_language=None,
        provider="fake",
        model="fake-v1",
        workflow_version="test-v1",
        options={},
        content={
            "title": "Clean transcript",
            "sections": [
                {
                    "source_item_ids": [item.item_id],
                    "source_segment_ids": list(item.source_segment_ids),
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                    "clean_text": text,
                    "notes": [],
                }
            ],
            "warnings": [],
        },
        evidence=(
            ArtifactEvidence(
                evidence_key="section:0",
                source_item_ids=(item.item_id,),
                source_segment_ids=item.source_segment_ids,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
            ),
        ),
    )


def test_human_versions_append_parent_chain_and_stay_on_original_package() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            session_id = seed_completed_session(db_session)
            package_v1 = PackageBuilder(db_session).build_baseline(session_id)
            repository = ArtifactRepository(db_session)
            generated = _create_clean(repository, package_v1, "model text")
            package_v2 = PackageBuilder(db_session).build_baseline(session_id)
            other_package_artifact = _create_clean(
                repository,
                package_v2,
                "new package text",
            )
            service = ArtifactReviewService(repository)
            first_content = copy.deepcopy(generated.content)
            first_content["sections"][0]["clean_text"] = "human v2"
            human_v2 = service.create_version(
                generated.artifact_id,
                content=first_content,
            )
            second_content = copy.deepcopy(human_v2.content)
            second_content["sections"][0]["clean_text"] = "human v3"
            human_v3 = service.create_version(
                human_v2.artifact_id,
                content=second_content,
            )
            db_session.commit()

            history = service.history(human_v3.artifact_id)
            assert [item.artifact_version for item in history] == [3, 2, 1]
            assert generated.content["sections"][0]["clean_text"] == "model text"
            assert human_v2.parent_artifact_id == generated.artifact_id
            assert human_v3.parent_artifact_id == human_v2.artifact_id
            assert human_v3.created_by == "human"
            assert human_v3.status == "reviewed"
            assert human_v3.provider is None and human_v3.model is None
            assert human_v3.package_id == package_v1.package_id
            assert all(item.package_id == package_v1.package_id for item in history)
            assert other_package_artifact not in history
            assert repository.get(generated.artifact_id) is not None
    finally:
        database.dispose()


def test_approving_new_version_supersedes_only_same_identity() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            repository = ArtifactRepository(db_session)
            generated = _create_clean(repository, package, "model text")
            service = ArtifactReviewService(repository)
            first_approved = service.approve(generated.artifact_id)
            edited = copy.deepcopy(first_approved.content)
            edited["sections"][0]["clean_text"] = "approved human text"
            human = service.create_version(
                first_approved.artifact_id,
                content=edited,
            )
            current = service.approve(human.artifact_id)
            db_session.commit()

            assert repository.get(generated.artifact_id).status == "superseded"
            assert current.status == "approved"
            assert current.approved_at is not None
            approved = repository.list_approved_for_package(package.package_id)
            assert [item.artifact_id for item in approved] == [
                current.artifact_id
            ]
    finally:
        database.dispose()


def test_fact_reviews_of_different_targets_have_independent_identity() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with database.session() as db_session:
            package = PackageBuilder(db_session).build_baseline(
                seed_completed_session(db_session)
            )
            repository = ArtifactRepository(db_session)
            target_v1 = _create_clean(repository, package, "target v1")
            target_v2 = repository.create_human_version(
                parent_artifact_id=target_v1.artifact_id,
                content=target_v1.content,
            )
            reviews = []
            for target in (target_v1, target_v2):
                reviews.append(
                    repository.create_generated(
                        package=package,
                        artifact_kind="timeline_fact_review",
                        target_language=None,
                        provider="fake",
                        model="fake-v1",
                        workflow_version="test-v1",
                        options={"target_artifact_id": target.artifact_id},
                        content={
                            "target_artifact": {
                                "artifact_id": target.artifact_id,
                            },
                            "reviews": [
                                {
                                    "claim_id": "claim:0",
                                    "target_path": "sections[0]",
                                    "claim_text": "reviewed claim",
                                    "status": "ambiguous",
                                    "explanation": None,
                                    "evidence_item_ids": [],
                                    "source_segment_ids": [],
                                    "start_ms": None,
                                    "end_ms": None,
                                    "time_ranges": [],
                                    "evidence_excerpts": [],
                                }
                            ],
                            "warnings": [],
                        },
                        evidence=(),
                        parent_artifact_id=target.artifact_id,
                    )
                )
            approved = [repository.approve(item) for item in reviews]
            db_session.commit()

            assert [item.artifact_version for item in approved] == [1, 1]
            assert approved[0].identity_key != approved[1].identity_key
            assert len(repository.list_approved_for_package(package.package_id)) == 2
            edited = copy.deepcopy(approved[0].content)
            edited["reviews"][0]["status"] = "partially_supported"
            human_review = ArtifactReviewService(repository).create_version(
                approved[0].artifact_id,
                content=edited,
            )
            assert human_review.status == "reviewed"
    finally:
        database.dispose()
