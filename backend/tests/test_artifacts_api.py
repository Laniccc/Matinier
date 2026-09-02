from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from app.artifacts.models import ArtifactEvidence
from app.artifacts.repository import ArtifactRepository
from app.packages import PackageBuilder
from tests.test_packages import seed_completed_session


def _seed_artifacts(client: TestClient) -> tuple[str, str]:
    database = client.app.state.database
    with database.session() as db_session:
        package = PackageBuilder(db_session).build_baseline(
            seed_completed_session(db_session)
        )
        source = next(
            document
            for document in package.documents
            if document.document_id
            == package.manifest.effective_source_document_id
        )
        item = source.content.items[0]
        evidence = (
            ArtifactEvidence(
                evidence_key="section:0",
                source_item_ids=(item.item_id,),
                source_segment_ids=item.source_segment_ids,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
            ),
        )
        repository = ArtifactRepository(db_session)
        refined = repository.create_generated(
            package=package,
            artifact_kind="refined_translation",
            target_language="en-US",
            provider="fake",
            model="fake-v1",
            workflow_version="1.0",
            options={"target_language": "en-US"},
            content={
                "target_language": "en-US",
                "sections": [
                    {
                        "source_item_ids": [item.item_id],
                        "source_segment_ids": list(item.source_segment_ids),
                        "start_ms": 999_000,
                        "end_ms": 999_999,
                        "source_text": item.text,
                        "translated_text": "Final translation",
                        "notes": [],
                    }
                ],
                "warnings": [],
            },
            evidence=evidence,
        )
        clean = repository.create_generated(
            package=package,
            artifact_kind="clean_script",
            target_language=None,
            provider="fake",
            model="fake-v1",
            workflow_version="2.0",
            options={},
            content={
                "title": "Clean transcript",
                "sections": [
                    {
                        "source_item_ids": [item.item_id],
                        "source_segment_ids": list(item.source_segment_ids),
                        "start_ms": item.start_ms,
                        "end_ms": item.end_ms,
                        "clean_text": item.text,
                        "notes": [],
                    }
                ],
                "warnings": [],
            },
            evidence=evidence,
        )
        db_session.commit()
        return refined.artifact_id, clean.artifact_id


def test_artifact_export_api_exposes_supported_formats_and_stable_headers(
    client: TestClient,
) -> None:
    refined_id, clean_id = _seed_artifacts(client)

    json_response = client.get(
        f"/api/artifacts/{refined_id}/export?format=json"
    )
    srt_response = client.get(
        f"/api/artifacts/{refined_id}/export?format=srt"
    )
    markdown_response = client.get(
        f"/api/artifacts/{clean_id}/export?format=markdown"
    )
    unsupported = client.get(
        f"/api/artifacts/{clean_id}/export?format=vtt"
    )
    missing = client.get("/api/artifacts/missing/export?format=json")

    assert json_response.status_code == 200
    assert json_response.headers["content-type"].startswith("application/json")
    assert (
        json_response.headers["content-disposition"]
        == 'attachment; filename="refined-translation-en-US-v1.json"'
    )
    assert json_response.headers["x-matinier-artifact-kind"] == (
        "refined_translation"
    )
    assert json_response.json()["content"]["sections"][0]["start_ms"] == 100
    assert srt_response.status_code == 200
    assert "00:00:00,100 --> 00:00:01,100" in srt_response.text
    assert markdown_response.status_code == 200
    assert "# Clean transcript" in markdown_response.text
    assert unsupported.status_code == 409
    assert missing.status_code == 404


def test_artifact_review_api_appends_history_and_replaces_current_approval(
    client: TestClient,
) -> None:
    _, clean_id = _seed_artifacts(client)
    original = client.get(f"/api/artifacts/{clean_id}").json()
    edited_content = copy.deepcopy(original["content"])
    edited_content["sections"][0]["clean_text"] = "Human-reviewed text"

    created = client.post(
        f"/api/artifacts/{clean_id}/versions",
        json={"content": edited_content},
    )
    assert created.status_code == 201
    human = created.json()
    assert human["artifact_version"] == 2
    assert human["parent_artifact_id"] == clean_id
    assert human["created_by"] == "human"
    assert human["status"] == "reviewed"

    assert client.post(f"/api/artifacts/{clean_id}/approve").status_code == 200
    approved = client.post(
        f"/api/artifacts/{human['artifact_id']}/approve"
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    old = client.get(f"/api/artifacts/{clean_id}").json()
    assert old["status"] == "superseded"
    assert old["content"]["sections"][0]["clean_text"] != (
        human["content"]["sections"][0]["clean_text"]
    )
    history = client.get(
        f"/api/artifacts/{human['artifact_id']}/history"
    )
    assert history.status_code == 200
    assert [item["artifact_version"] for item in history.json()] == [2, 1]
    current = client.get(
        f"/api/packages/{human['package_id']}/approved-artifacts"
    )
    assert current.status_code == 200
    assert [item["artifact_id"] for item in current.json()] == [
        human["artifact_id"]
    ]


def test_artifact_version_api_rejects_provenance_changes(
    client: TestClient,
) -> None:
    _, clean_id = _seed_artifacts(client)
    content = client.get(f"/api/artifacts/{clean_id}").json()["content"]
    content["sections"][0]["source_item_ids"] = ["invented-item"]

    response = client.post(
        f"/api/artifacts/{clean_id}/versions",
        json={"content": content},
    )

    assert response.status_code == 409
    assert "provenance field" in response.json()["detail"]
