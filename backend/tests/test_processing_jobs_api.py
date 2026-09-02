from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.packages import PackageBuilder
from app.persistence.database import Database
from app.processing.clean_script import CleanScriptWorkflow
from app.processing.contracts import ProcessorRegistry
from app.processing.runner import ProcessingJobRunner
from app.settings import Settings
from tests.test_packages import seed_completed_session
from tests.test_processing_core import FakeStructuredProvider


def test_processing_job_api_returns_immediately_then_exposes_artifact(
    tmp_path,
) -> None:
    database = Database("sqlite://")
    database.create_schema()
    with database.session() as db_session:
        package = PackageBuilder(db_session).build_baseline(
            seed_completed_session(db_session)
        )
        db_session.commit()

    provider = FakeStructuredProvider()
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
    runner = ProcessingJobRunner(database, registry, concurrency=1)
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

    with TestClient(
        create_app(
            settings=settings,
            database=database,
            processing_job_runner=runner,
        )
    ) as client:
        created = client.post(
            f"/api/packages/{package.package_id}/jobs",
            json={"artifact_kind": "clean_script", "options": {}},
        )
        assert created.status_code == 202
        job = created.json()
        assert job["status"] == "queued"

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = client.get(f"/api/processing-jobs/{job['job_id']}")
            assert response.status_code == 200
            job = response.json()
            if job["status"] == "completed":
                break
            time.sleep(0.01)

        assert job["status"] == "completed"
        assert job["progress"] == 100
        assert job["result_artifact_id"]

        listed_jobs = client.get(
            f"/api/packages/{package.package_id}/jobs"
        )
        assert listed_jobs.status_code == 200
        assert listed_jobs.json()[0]["job_id"] == job["job_id"]

        artifacts = client.get(
            f"/api/packages/{package.package_id}/artifacts"
        )
        assert artifacts.status_code == 200
        assert len(artifacts.json()) == 1
        artifact_id = artifacts.json()[0]["artifact_id"]

        artifact = client.get(f"/api/artifacts/{artifact_id}")
        assert artifact.status_code == 200
        assert artifact.json()["package_content_hash"] == package.content_hash
