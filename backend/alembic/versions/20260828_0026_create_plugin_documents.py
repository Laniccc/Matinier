"""Create immutable, versioned plugin documents.

Revision ID: 20260828_0026
Revises: 20260828_0025
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260828_0026"
down_revision: str | None = "20260828_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plugin_documents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("plugin_version", sa.String(length=64), nullable=False),
        sa.Column("plugin_package_id", sa.String(length=36), nullable=True),
        sa.Column("media_session_id", sa.String(length=36), nullable=False),
        sa.Column("source_package_id", sa.String(length=36), nullable=False),
        sa.Column("identity_key", sa.String(length=192), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("schema_name", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("completeness", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="published",
            nullable=False,
        ),
        sa.Column("source_package_version", sa.Integer(), nullable=False),
        sa.Column("source_package_hash", sa.String(length=64), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("markdown_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plugin_package_id"],
            ["plugin_packages.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["media_session_id"],
            ["media_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_package_id"],
            ["result_packages.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plugin_id",
            "media_session_id",
            "identity_key",
            "document_version",
            name="uq_plugin_documents_identity_version",
        ),
    )
    op.create_index(
        "ix_plugin_documents_session_plugin_created",
        "plugin_documents",
        ["media_session_id", "plugin_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_documents_source_package",
        "plugin_documents",
        ["source_package_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_plugin_documents_source_package",
        table_name="plugin_documents",
    )
    op.drop_index(
        "ix_plugin_documents_session_plugin_created",
        table_name="plugin_documents",
    )
    op.drop_table("plugin_documents")
