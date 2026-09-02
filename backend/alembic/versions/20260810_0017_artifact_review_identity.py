"""Add stable Artifact review identity and approval uniqueness.

Revision ID: 20260810_0017
Revises: 20260808_0016
Create Date: 2026-08-10
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260810_0017"
down_revision: str | None = "20260808_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json(value: object) -> dict[str, object]:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value) if isinstance(value, dict) else {}


def _identity(row: sa.RowMapping) -> str:
    kind = str(row["artifact_kind"])
    if kind == "refined_translation":
        language = row["target_language"]
        if not language:
            raise RuntimeError(
                f"Artifact {row['id']} has no refined-translation language"
            )
        return f"refined_translation:{language}"
    if kind == "timeline_fact_review":
        options = _json(row["options_json"])
        target_id = options.get("target_artifact_id") or row["parent_artifact_id"]
        if not target_id:
            raise RuntimeError(
                f"Artifact {row['id']} has no fact-review target"
            )
        return f"timeline_fact_review:{target_id}"
    return kind


def upgrade() -> None:
    op.add_column(
        "derived_artifacts",
        sa.Column("identity_key", sa.String(length=256), nullable=True),
    )
    connection = op.get_bind()
    artifacts = sa.table(
        "derived_artifacts",
        sa.column("id", sa.String()),
        sa.column("package_id", sa.String()),
        sa.column("artifact_kind", sa.String()),
        sa.column("artifact_version", sa.Integer()),
        sa.column("target_language", sa.String()),
        sa.column("status", sa.String()),
        sa.column("options_json", sa.JSON()),
        sa.column("parent_artifact_id", sa.String()),
        sa.column("identity_key", sa.String()),
        sa.column("created_at", sa.DateTime()),
    )
    rows = list(connection.execute(
        sa.select(
            artifacts.c.id,
            artifacts.c.package_id,
            artifacts.c.artifact_kind,
            artifacts.c.artifact_version,
            artifacts.c.target_language,
            artifacts.c.status,
            artifacts.c.options_json,
            artifacts.c.parent_artifact_id,
            artifacts.c.created_at,
        )
    ).mappings())
    identities: dict[str, str] = {}
    for row in rows:
        identities[str(row["id"])] = _identity(row)
        connection.execute(
            sa.update(artifacts)
            .where(artifacts.c.id == row["id"])
            .values(identity_key=identities[str(row["id"])])
        )

    approved: dict[tuple[str, str], list[sa.RowMapping]] = {}
    for row in rows:
        if row["status"] == "approved":
            key = (
                str(row["package_id"]),
                identities[str(row["id"])],
            )
            approved.setdefault(key, []).append(row)
    for versions in approved.values():
        current = max(
            versions,
            key=lambda row: (
                int(row["artifact_version"]),
                row["created_at"],
                str(row["id"]),
            ),
        )
        superseded_ids = [
            row["id"] for row in versions if row["id"] != current["id"]
        ]
        if superseded_ids:
            connection.execute(
                sa.update(artifacts)
                .where(artifacts.c.id.in_(superseded_ids))
                .values(status="superseded")
            )

    op.drop_index(
        "uq_derived_artifacts_identity_version_no_language",
        table_name="derived_artifacts",
    )
    with op.batch_alter_table("derived_artifacts") as batch_op:
        batch_op.drop_constraint(
            "uq_derived_artifacts_identity_version",
            type_="unique",
        )
        batch_op.alter_column(
            "identity_key",
            existing_type=sa.String(length=256),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_derived_artifacts_identity_version",
            ["package_id", "identity_key", "artifact_version"],
        )
    op.create_index(
        "uq_derived_artifacts_current_approved",
        "derived_artifacts",
        ["package_id", "identity_key"],
        unique=True,
        sqlite_where=sa.text("status = 'approved'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_derived_artifacts_current_approved",
        table_name="derived_artifacts",
    )
    connection = op.get_bind()
    artifacts = sa.table(
        "derived_artifacts",
        sa.column("id", sa.String()),
        sa.column("package_id", sa.String()),
        sa.column("artifact_kind", sa.String()),
        sa.column("target_language", sa.String()),
        sa.column("artifact_version", sa.Integer()),
        sa.column("created_at", sa.DateTime()),
    )
    connection.execute(
        sa.update(artifacts).values(
            artifact_version=artifacts.c.artifact_version + 1_000_000
        )
    )
    rows = connection.execute(
        sa.select(
            artifacts.c.id,
            artifacts.c.package_id,
            artifacts.c.artifact_kind,
            artifacts.c.target_language,
        ).order_by(artifacts.c.created_at, artifacts.c.id)
    ).mappings()
    versions: dict[tuple[object, object, object], int] = {}
    for row in rows:
        key = (
            row["package_id"],
            row["artifact_kind"],
            row["target_language"],
        )
        versions[key] = versions.get(key, 0) + 1
        connection.execute(
            sa.update(artifacts)
            .where(artifacts.c.id == row["id"])
            .values(artifact_version=versions[key])
        )

    with op.batch_alter_table("derived_artifacts") as batch_op:
        batch_op.drop_constraint(
            "uq_derived_artifacts_identity_version",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_derived_artifacts_identity_version",
            [
                "package_id",
                "artifact_kind",
                "target_language",
                "artifact_version",
            ],
        )
        batch_op.drop_column("identity_key")
    op.create_index(
        "uq_derived_artifacts_identity_version_no_language",
        "derived_artifacts",
        ["package_id", "artifact_kind", "artifact_version"],
        unique=True,
        sqlite_where=sa.text("target_language IS NULL"),
    )
