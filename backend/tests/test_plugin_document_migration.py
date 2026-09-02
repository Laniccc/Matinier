from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _migrate(database_url: str, operation: str, revision: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic.ini",
            operation,
            revision,
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_migration_0026_adds_plugin_documents_with_required_foreign_keys(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "plugin-documents.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    _migrate(database_url, "upgrade", "20260828_0025")

    seed_engine = sa.create_engine(database_url)
    try:
        with seed_engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO plugin_packages (
                        id, plugin_id, version, content_digest, manifest_hash,
                        image_digest, runtime_image_ref, signature_status,
                        package_path, publisher_id, manifest_json, installed_at
                    ) VALUES (
                        'plugin-package-1', 'com.example.existing', '1.0.0',
                        :content_digest, :manifest_hash, :image_digest, NULL,
                        'verified', 'plugins/existing', NULL, '{}',
                        '2026-08-28 00:00:00'
                    )
                    """
                ),
                {
                    "content_digest": "sha256:" + "a" * 64,
                    "manifest_hash": "sha256:" + "b" * 64,
                    "image_digest": "sha256:" + "c" * 64,
                },
            )
    finally:
        seed_engine.dispose()

    _migrate(database_url, "upgrade", "20260828_0026")

    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        assert "plugin_documents" in inspector.get_table_names()
        columns = {item["name"]: item for item in inspector.get_columns("plugin_documents")}
        assert columns["plugin_package_id"]["nullable"] is True
        assert columns["media_session_id"]["nullable"] is False
        assert columns["source_package_id"]["nullable"] is False
        foreign_keys = {
            tuple(item["constrained_columns"]): item
            for item in inspector.get_foreign_keys("plugin_documents")
        }
        assert foreign_keys[("plugin_package_id",)]["options"]["ondelete"] == "SET NULL"
        assert foreign_keys[("media_session_id",)]["options"]["ondelete"] == "CASCADE"
        assert foreign_keys[("source_package_id",)]["options"]["ondelete"] == "RESTRICT"
        unique_columns = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints("plugin_documents")
        }
        assert (
            "plugin_id",
            "media_session_id",
            "identity_key",
            "document_version",
        ) in unique_columns
    finally:
        engine.dispose()

    _migrate(database_url, "downgrade", "20260828_0025")
    downgraded_engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(downgraded_engine)
        assert "plugin_documents" not in inspector.get_table_names()
        with downgraded_engine.connect() as connection:
            assert connection.scalar(
                sa.text("SELECT COUNT(*) FROM plugin_packages")
            ) == 1
    finally:
        downgraded_engine.dispose()
