from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.persistence.database import Database
from tests.test_packages import seed_completed_session


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_stage2_processing_verifier_runs_package_job_artifact_chain(
    tmp_path,
) -> None:
    database_path = tmp_path / "stage2e-verifier.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    database = Database(database_url)
    database.create_schema()
    try:
        with database.session() as db_session:
            session_id = seed_completed_session(db_session)
            db_session.commit()
    finally:
        database.dispose()

    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "verify_stage2_processing.py"),
            "--database-url",
            database_url,
            "--session-id",
            session_id,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    output_lines = completed.stdout.strip().splitlines()
    assert output_lines
    result = json.loads(output_lines[-1])
    assert result["status"] == "ok"
    assert result["session_id"] == session_id
    assert result["package_version"] == 1
    assert result["package_hash_unchanged"] is True
    assert result["source_item_count"] == 1
    clean = result["clean_script"]
    assert clean["job_status"] == "completed"
    assert clean["job_progress"] == 100
    assert clean["artifact_version"] == 1
    assert clean["evidence_count"] == 1
    assert set(clean["export_bytes"]) == {"json", "markdown"}
    assert all(size > 0 for size in clean["export_bytes"].values())
    refined = result["refined_translation"]
    assert refined["job_status"] == "completed"
    assert refined["job_progress"] == 100
    assert refined["artifact_version"] == 1
    assert refined["target_language"] == "en-US"
    assert refined["evidence_count"] == 1
    assert set(refined["export_bytes"]) == {
        "json",
        "markdown",
        "srt",
        "vtt",
    }
    assert all(size > 0 for size in refined["export_bytes"].values())
    summary = result["summary"]
    assert summary["job_status"] == "completed"
    assert summary["job_progress"] == 100
    assert summary["artifact_version"] == 1
    assert summary["evidence_count"] == 1
    assert summary["key_point_count"] == 1
    chapter = result["chapter_outline"]
    assert chapter["job_status"] == "completed"
    assert chapter["job_progress"] == 100
    assert chapter["artifact_version"] == 1
    assert chapter["evidence_count"] == 1
    assert chapter["chapter_count"] == 1
    review = result["timeline_fact_review"]
    assert review["job_status"] == "completed"
    assert review["job_progress"] == 100
    assert review["artifact_version"] == 1
    assert review["target_artifact_id"] == summary["artifact_id"]
    assert review["evidence_count"] == 1
    assert review["review_count"] == 1
    assert result["evidence_valid"] is True
    assert result["provider"] == "fake"
    assert result["provider_request_count"] == 5
    assert result["live_reference_item_count"] == 1
    assert result["stage1_final_count"] == 1
    assert result["stage1_final_unchanged"] is True
