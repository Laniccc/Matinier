from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_packages import seed_completed_session
from tests.test_revisions import _add_second_final


def test_revision_api_versions_approves_exports_and_builds_package_v2(
    client: TestClient,
) -> None:
    with client.app.state.database.session() as db_session:
        session_id = seed_completed_session(db_session)
        _add_second_final(db_session, session_id)
        db_session.commit()

    baseline_response = client.post(f"/api/sessions/{session_id}/packages")
    assert baseline_response.status_code == 201
    baseline = baseline_response.json()

    created_response = client.post(
        f"/api/packages/{baseline['package_id']}/revisions",
        json={},
    )
    assert created_response.status_code == 201
    created = created_response.json()
    assert created["version"] == 1
    assert created["status"] == "saved"

    items = created["content"]["items"]
    items[0]["text"] = "API 校对后的字幕。"
    version_response = client.post(
        f"/api/revisions/{created['revision_id']}/versions",
        json={
            "content": {"items": items},
            "change_summary": "API edit",
        },
    )
    assert version_response.status_code == 201
    version = version_response.json()
    assert version["version"] == 2
    assert version["parent_revision_id"] == created["revision_id"]

    approved_response = client.post(
        f"/api/revisions/{version['revision_id']}/approve"
    )
    assert approved_response.status_code == 200
    assert approved_response.json()["status"] == "approved"

    listed_response = client.get(f"/api/sessions/{session_id}/revisions")
    assert listed_response.status_code == 200
    assert [item["version"] for item in listed_response.json()] == [2, 1]

    revision_export = client.get(
        f"/api/revisions/{version['revision_id']}/export?format=vtt"
    )
    assert revision_export.status_code == 200
    assert revision_export.content.startswith(b"WEBVTT")

    package_response = client.post(
        f"/api/revisions/{version['revision_id']}/packages"
    )
    assert package_response.status_code == 201
    package = package_response.json()
    assert package["package_version"] == 2
    assert package["source_revision_id"] == version["revision_id"]
    assert any(
        item["document_kind"] == "source_approved"
        and item["content"]["items"][0]["text"] == "API 校对后的字幕。"
        for item in package["documents"]
    )

    reloaded_baseline = client.get(
        f"/api/packages/{baseline['package_id']}"
    )
    assert reloaded_baseline.status_code == 200
    assert reloaded_baseline.json()["status"] == "superseded"
