"""Create immutable transcript revision snapshots.

Revision ID: 20260804_0014
Revises: 20260804_0013
Create Date: 2026-08-04
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260804_0014"
down_revision: str | None = "20260804_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transcript_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_revision_id", sa.String(length=36), nullable=True),
        sa.Column("base_package_id", sa.String(length=36), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("change_summary", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["base_package_id"],
            ["result_packages.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_revision_id"],
            ["transcript_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "language",
            "version",
            name="uq_transcript_revisions_session_language_version",
        ),
    )
    op.create_index(
        op.f("ix_transcript_revisions_session_id"),
        "transcript_revisions",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_transcript_revisions_parent_revision_id"),
        "transcript_revisions",
        ["parent_revision_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_transcript_revisions_base_package_id"),
        "transcript_revisions",
        ["base_package_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_transcript_revisions_status"),
        "transcript_revisions",
        ["status"],
        unique=False,
    )
    op.create_index(
        "uq_transcript_revisions_current_approved",
        "transcript_revisions",
        ["session_id", "language"],
        unique=True,
        sqlite_where=sa.text("status = 'approved'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_transcript_revisions_current_approved",
        table_name="transcript_revisions",
    )
    op.drop_index(
        op.f("ix_transcript_revisions_status"),
        table_name="transcript_revisions",
    )
    op.drop_index(
        op.f("ix_transcript_revisions_base_package_id"),
        table_name="transcript_revisions",
    )
    op.drop_index(
        op.f("ix_transcript_revisions_parent_revision_id"),
        table_name="transcript_revisions",
    )
    op.drop_index(
        op.f("ix_transcript_revisions_session_id"),
        table_name="transcript_revisions",
    )
    op.drop_table("transcript_revisions")
