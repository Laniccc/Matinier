from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import func, inspect, select

from app.artifacts.repository import ArtifactRepository
from app.packages import PackageRepository, PackageValidator
from app.persistence.database import Database
from app.persistence.models import ProcessedScriptRecord, SessionRecord


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _upgrade(database_url: str, revision: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic.ini",
            "upgrade",
            revision,
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_alembic_migrates_script_and_builds_legacy_package_when_missing(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    _upgrade(database_url, "20260808_0015")
    database = Database(database_url)
    created_at = dt.datetime(2026, 7, 23, 8, tzinfo=dt.UTC)
    script_id = "legacy-script-id"
    snapshot = [
        {
            "segment_id": "seg-legacy",
            "revision": 2,
            "language": "zh-CN",
            "raw_text": "大家 好",
            "display_text": "大家 好",
            "audio_start_ms": 100,
            "audio_end_ms": 1_500,
        }
    ]
    content = {
        "title": "整理版台本",
        "sections": [
            {
                "source_segment_ids": ["seg-legacy"],
                "start_ms": 100,
                "end_ms": 1_500,
                "clean_text": "大家好。",
                "notes": [],
            }
        ],
        "warnings": [],
    }
    with database.session() as db_session:
        db_session.add(
            SessionRecord(
                id="legacy-session",
                room_name="legacy-room",
                status="completed",
                source_type="file",
                source_name="legacy.wav",
                language="zh-CN",
                created_at=created_at,
            )
        )
        db_session.commit()
        db_session.add(
            ProcessedScriptRecord(
                id=script_id,
                session_id="legacy-session",
                provider="deepseek",
                model="deepseek-v4-flash",
                version=1,
                source_segment_snapshot=json.dumps(snapshot),
                content_json=json.dumps(content),
                markdown_text="# 整理版台本\n",
                created_at=created_at,
            )
        )
        db_session.commit()
    database.dispose()

    _upgrade(database_url, "head")

    migrated = Database(database_url)
    try:
        with migrated.session() as db_session:
            artifact = ArtifactRepository(db_session).get(script_id)
            assert artifact is not None
            assert artifact.artifact_kind == "clean_script"
            assert artifact.identity_key == "clean_script"
            assert artifact.created_by == "model"
            assert artifact.status == "generated"
            assert artifact.workflow_version == "legacy-script-v1"
            assert artifact.content["legacy_source_segment_snapshot"] == snapshot
            assert artifact.content["legacy_markdown_text"] == "# 整理版台本\n"
            assert artifact.evidence[0].source_segment_ids == ("seg-legacy",)
            package = PackageRepository(db_session).load(artifact.package_id)
            assert PackageValidator().validate(package).valid is True
            assert db_session.scalar(
                select(func.count()).select_from(ProcessedScriptRecord)
            ) == 1
            indexes = {
                item["name"]: item
                for item in inspect(migrated.engine).get_indexes(
                    "derived_artifacts"
                )
            }
            approved_index = indexes[
                "uq_derived_artifacts_current_approved"
            ]
            assert approved_index["unique"] == 1
            assert approved_index["column_names"] == [
                "package_id",
                "identity_key",
            ]
            package_document_indexes = {
                item["name"]: item
                for item in inspect(migrated.engine).get_indexes(
                    "package_documents"
                )
            }
            no_language_index = package_document_indexes[
                "uq_package_documents_identity_no_language"
            ]
            assert no_language_index["unique"] == 1
            assert no_language_index["column_names"] == [
                "package_id",
                "document_kind",
            ]
    finally:
        migrated.dispose()
