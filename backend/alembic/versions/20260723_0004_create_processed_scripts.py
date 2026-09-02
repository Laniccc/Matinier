"""Create append-only processed scripts.

Revision ID: 20260723_0004
Revises: 20260722_0003
Create Date: 2026-07-23
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260723_0004"
down_revision: str | None = "20260722_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processed_scripts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_segment_snapshot", sa.Text(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("markdown_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "version",
            name="uq_processed_scripts_session_version",
        ),
    )
    op.create_index(
        "ix_processed_scripts_session_id",
        "processed_scripts",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_processed_scripts_session_id",
        table_name="processed_scripts",
    )
    op.drop_table("processed_scripts")
