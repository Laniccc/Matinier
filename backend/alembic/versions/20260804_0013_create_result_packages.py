"""Create immutable transcript result packages.

Revision ID: 20260804_0013
Revises: 20260803_0012
Create Date: 2026-08-04
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260804_0013"
down_revision: str | None = "20260803_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "result_packages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_name", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("source_revision_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "version",
            name="uq_result_packages_session_version",
        ),
    )
    op.create_index(
        op.f("ix_result_packages_session_id"),
        "result_packages",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_result_packages_status"),
        "result_packages",
        ["status"],
        unique=False,
    )
    op.create_table(
        "package_documents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("document_kind", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["result_packages.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "package_id",
            "document_kind",
            "language",
            name="uq_package_documents_identity",
        ),
    )
    op.create_index(
        op.f("ix_package_documents_package_id"),
        "package_documents",
        ["package_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_package_documents_package_id"),
        table_name="package_documents",
    )
    op.drop_table("package_documents")
    op.drop_index(
        op.f("ix_result_packages_status"),
        table_name="result_packages",
    )
    op.drop_index(
        op.f("ix_result_packages_session_id"),
        table_name="result_packages",
    )
    op.drop_table("result_packages")
