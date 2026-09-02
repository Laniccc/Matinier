"""Create processing jobs and versioned derived artifacts.

Revision ID: 20260808_0015
Revises: 20260804_0014
Create Date: 2026-08-08
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260808_0015"
down_revision: str | None = "20260804_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "derived_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("package_version", sa.Integer(), nullable=False),
        sa.Column("package_content_hash", sa.String(length=64), nullable=False),
        sa.Column("artifact_kind", sa.String(length=64), nullable=False),
        sa.Column("artifact_version", sa.Integer(), nullable=False),
        sa.Column("target_language", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("workflow_version", sa.String(length=32), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("parent_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["result_packages.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_artifact_id"],
            ["derived_artifacts.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "package_id",
            "artifact_kind",
            "target_language",
            "artifact_version",
            name="uq_derived_artifacts_identity_version",
        ),
    )
    op.create_index(
        op.f("ix_derived_artifacts_package_id"),
        "derived_artifacts",
        ["package_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_derived_artifacts_artifact_kind"),
        "derived_artifacts",
        ["artifact_kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_derived_artifacts_status"),
        "derived_artifacts",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_derived_artifacts_parent_artifact_id"),
        "derived_artifacts",
        ["parent_artifact_id"],
        unique=False,
    )
    op.create_index(
        "uq_derived_artifacts_identity_version_no_language",
        "derived_artifacts",
        ["package_id", "artifact_kind", "artifact_version"],
        unique=True,
        sqlite_where=sa.text("target_language IS NULL"),
    )

    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("target_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("result_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("artifact_kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["result_packages.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_artifact_id"],
            ["derived_artifacts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_artifact_id"],
            ["derived_artifacts.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_processing_jobs_package_id"),
        "processing_jobs",
        ["package_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_processing_jobs_artifact_kind"),
        "processing_jobs",
        ["artifact_kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_processing_jobs_status"),
        "processing_jobs",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_processing_jobs_status"),
        table_name="processing_jobs",
    )
    op.drop_index(
        op.f("ix_processing_jobs_artifact_kind"),
        table_name="processing_jobs",
    )
    op.drop_index(
        op.f("ix_processing_jobs_package_id"),
        table_name="processing_jobs",
    )
    op.drop_table("processing_jobs")
    op.drop_index(
        "uq_derived_artifacts_identity_version_no_language",
        table_name="derived_artifacts",
    )
    op.drop_index(
        op.f("ix_derived_artifacts_parent_artifact_id"),
        table_name="derived_artifacts",
    )
    op.drop_index(
        op.f("ix_derived_artifacts_status"),
        table_name="derived_artifacts",
    )
    op.drop_index(
        op.f("ix_derived_artifacts_artifact_kind"),
        table_name="derived_artifacts",
    )
    op.drop_index(
        op.f("ix_derived_artifacts_package_id"),
        table_name="derived_artifacts",
    )
    op.drop_table("derived_artifacts")
