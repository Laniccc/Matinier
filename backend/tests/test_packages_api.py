from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.packages.exporter import verify_package_zip
from app.persistence.models import ResultPackageRecord, SessionRecord
from tests.test_packages import seed_completed_session


def test_package_api_builds_lists_validates_and_exports(client: TestClient) -> None:
    with client.app.state.database.session() as db_session:
        session_id = seed_completed_session(db_session)
        db_session.commit()

    created = client.post(f"/api/sessions/{session_id}/packages")
    assert created.status_code == 201
    package = created.json()
    assert package["status"] == "frozen"
    assert package["manifest"]["schema"] == "matinier.transcript-package"

    listed = client.get(f"/api/sessions/{session_id}/packages")
    assert listed.status_code == 200
    assert listed.json()[0]["package_id"] == package["package_id"]

    manifest = client.get(
        f"/api/packages/{package['package_id']}/manifest"
    )
    assert manifest.status_code == 200
    assert manifest.json()["content_hash"] == package["content_hash"]

    validation = client.post(
        f"/api/packages/{package['package_id']}/validate"
    )
    assert validation.status_code == 200
    assert validation.json()["valid"] is True

    exported = client.get(f"/api/packages/{package['package_id']}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    valid, errors = verify_package_zip(exported.content)
    assert valid is True
    assert errors == ()


def test_package_api_rejects_completed_session_without_finals(
    client: TestClient,
) -> None:
    session_id = "completed-without-finals"
    with client.app.state.database.session() as db_session:
        db_session.add(
            SessionRecord(
                id=session_id,
                room_name="silent-room",
                status="completed",
                source_type="file",
                source_name="silent.wav",
                language="zh-CN",
                final_result_count=0,
                created_at=dt.datetime(2026, 8, 11, tzinfo=dt.UTC),
            )
        )
        db_session.commit()

    response = client.post(f"/api/sessions/{session_id}/packages")

    assert response.status_code == 409
    assert response.json() == {"detail": "Session has no Final captions"}
    with client.app.state.database.session() as db_session:
        assert db_session.scalar(
            select(func.count()).select_from(ResultPackageRecord)
        ) == 0
