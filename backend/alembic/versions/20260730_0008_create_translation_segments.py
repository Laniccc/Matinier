"""Create persisted realtime translation segments.

Revision ID: 20260730_0008
Revises: 20260730_0007
Create Date: 2026-07-30
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0008"
down_revision: str | None = "20260730_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "translation_segments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("segment_id", sa.String(length=255), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_language", sa.String(length=32), nullable=False),
        sa.Column("target_language", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("audio_start_ms", sa.Integer(), nullable=True),
        sa.Column("audio_end_ms", sa.Integer(), nullable=True),
        sa.Column("source_segment_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("received_at_ms", sa.Integer(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "target_language",
            "segment_id",
            name="uq_translation_segments_session_language_segment",
        ),
    )
    op.create_index(
        "ix_translation_segments_session_id",
        "translation_segments",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_translation_segments_status",
        "translation_segments",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_translation_segments_status",
        table_name="translation_segments",
    )
    op.drop_index(
        "ix_translation_segments_session_id",
        table_name="translation_segments",
    )
    op.drop_table("translation_segments")
