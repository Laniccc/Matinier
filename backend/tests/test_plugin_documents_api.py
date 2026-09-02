from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.plugins.document_repository import PluginDocumentRepository
from tests.test_plugin_document_capability import (
    PLUGIN_ID,
    PLUGIN_VERSION,
    publish_input,
    seed_document_inputs,
)


def _seed_documents(client: TestClient):
    with Session(client.app.state.database.engine) as db_session:
        media, package, plugin_package, evidence = seed_document_inputs(db_session)
        repository = PluginDocumentRepository(db_session)
        first = repository.publish(
            plugin_id=PLUGIN_ID,
            plugin_version=PLUGIN_VERSION,
            plugin_package_id=plugin_package.id,
            media_session_id=media.id,
            source_package_id=package.package_id,
            value=publish_input(package.package_id, evidence),
        )
        second = repository.publish(
            plugin_id=PLUGIN_ID,
            plugin_version=PLUGIN_VERSION,
            plugin_package_id=plugin_package.id,
            media_session_id=media.id,
            source_package_id=package.package_id,
            value=publish_input(
                package.package_id,
                evidence,
                content={"title": "课程内容整理", "sections": [{"id": "topic-1"}]},
                markdown="# 课程内容整理\n\n## Topic 1\n",
            ),
        )
        english = repository.publish(
            plugin_id=PLUGIN_ID,
            plugin_version=PLUGIN_VERSION,
            plugin_package_id=plugin_package.id,
            media_session_id=media.id,
            source_package_id=package.package_id,
            value=publish_input(
                package.package_id,
                evidence,
                identity_key="course-notes:en-US",
                language="en-US",
                content={"title": "Course notes", "sections": []},
                markdown="# Course notes\n",
            ),
        )
        db_session.commit()
        return media.id, first.id, second.id, english.id


def test_plugin_document_api_lists_filters_and_returns_version_details(
    client: TestClient,
) -> None:
    media_id, first_id, second_id, english_id = _seed_documents(client)

    listed = client.get(f"/api/media-sessions/{media_id}/plugin-documents")
    chinese = client.get(
        f"/api/media-sessions/{media_id}/plugin-documents",
        params={"plugin_id": PLUGIN_ID, "language": "zh-CN"},
    )
    identity = client.get(
        f"/api/media-sessions/{media_id}/plugin-documents",
        params={"identity_key": "course-notes:en-US"},
    )
    detail = client.get(f"/api/plugin-documents/{second_id}")

    assert listed.status_code == 200
    assert [item["document_id"] for item in listed.json()] == [
        english_id,
        second_id,
        first_id,
    ]
    assert [item["document_version"] for item in chinese.json()] == [2, 1]
    assert [item["document_id"] for item in identity.json()] == [english_id]
    assert detail.status_code == 200
    assert detail.json()["content"]["sections"][0]["id"] == "topic-1"
    assert detail.json()["evidence_refs"][0]["source_segment_ids"] == [
        "source-final"
    ]

    assert client.get("/api/plugin-documents/missing").status_code == 404
    assert client.get(
        "/api/media-sessions/missing/plugin-documents"
    ).status_code == 404


def test_plugin_document_exports_are_deterministic_trusted_attachments(
    client: TestClient,
) -> None:
    _media_id, _first_id, second_id, _english_id = _seed_documents(client)

    markdown = client.get(
        f"/api/plugin-documents/{second_id}/export",
        params={"format": "markdown"},
    )
    markdown_repeat = client.get(
        f"/api/plugin-documents/{second_id}/export",
        params={"format": "markdown"},
    )
    exported_json = client.get(
        f"/api/plugin-documents/{second_id}/export",
        params={"format": "json"},
    )
    unsupported = client.get(
        f"/api/plugin-documents/{second_id}/export",
        params={"format": "html"},
    )

    assert markdown.status_code == 200
    assert markdown.content == markdown_repeat.content
    assert markdown.text == "# 课程内容整理\n\n## Topic 1\n"
    assert markdown.headers["content-disposition"] == (
        'attachment; filename="course-notes-zh-CN-v2.md"'
    )
    assert markdown.headers["x-matinier-document-hash"]
    assert exported_json.status_code == 200
    assert exported_json.headers["content-disposition"] == (
        'attachment; filename="course-notes-zh-CN-v2.json"'
    )
    assert exported_json.headers["x-matinier-document-hash"] == (
        markdown.headers["x-matinier-document-hash"]
    )
    payload = json.loads(exported_json.content)
    assert payload["metadata"]["document_version"] == 2
    assert payload["content"]["sections"][0]["id"] == "topic-1"
    assert payload["evidence_refs"][0]["item_id"]
    assert unsupported.status_code == 400
    assert client.get(
        "/api/plugin-documents/missing/export",
        params={"format": "json"},
    ).status_code == 404
