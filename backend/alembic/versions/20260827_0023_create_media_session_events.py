"""Create generic MediaSession and MediaEvent persistence.

Revision ID: 20260827_0023
Revises: 20260812_0022
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260827_0023"
down_revision: str | None = "20260812_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("legacy_session_id", sa.String(length=36), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("owner_scope", sa.String(length=256), nullable=True),
        sa.Column(
            "next_sequence",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["legacy_session_id"],
            ["sessions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "legacy_session_id",
            name="uq_media_sessions_legacy_session_id",
        ),
    )
    op.create_index(
        op.f("ix_media_sessions_legacy_session_id"),
        "media_sessions",
        ["legacy_session_id"],
        unique=True,
    )
    op.create_index(
        "ix_media_sessions_status_updated",
        "media_sessions",
        ["status", "updated_at"],
        unique=False,
    )

    op.create_table(
        "media_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("media_session_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "schema_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("media_time_ms", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("logical_id", sa.String(length=256), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("finality", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=96), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["media_session_id"],
            ["media_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "media_session_id",
            "sequence",
            name="uq_media_events_session_sequence",
        ),
        sa.UniqueConstraint(
            "media_session_id",
            "source",
            "logical_id",
            "revision",
            name="uq_media_events_source_revision",
        ),
    )
    op.create_index(
        "ix_media_events_session_cursor",
        "media_events",
        ["media_session_id", "sequence"],
        unique=False,
    )
    op.create_index(
        "ix_media_events_type_created",
        "media_events",
        ["event_type", "created_at"],
        unique=False,
    )

    op.create_table(
        "media_bridge_offsets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("media_session_id", sa.String(length=36), nullable=False),
        sa.Column("source_table", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=96), nullable=False),
        sa.Column("source_logical_id", sa.String(length=256), nullable=False),
        sa.Column("processed_revision", sa.Integer(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["media_session_id"],
            ["media_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "media_session_id",
            "source_table",
            "source_type",
            "source_logical_id",
            name="uq_media_bridge_offsets_source",
        ),
    )
    op.create_index(
        "ix_media_bridge_offsets_session_source",
        "media_bridge_offsets",
        ["media_session_id", "source_table"],
        unique=False,
    )

    op.create_table(
        "media_consumer_cursors",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("media_session_id", sa.String(length=36), nullable=False),
        sa.Column("consumer_id", sa.String(length=192), nullable=False),
        sa.Column(
            "last_delivered_sequence",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "last_acknowledged_sequence",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["media_session_id"],
            ["media_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "media_session_id",
            "consumer_id",
            name="uq_media_consumer_cursors_session_consumer",
        ),
    )
    op.create_index(
        "ix_media_consumer_cursors_consumer",
        "media_consumer_cursors",
        ["consumer_id", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_media_consumer_cursors_consumer",
        table_name="media_consumer_cursors",
    )
    op.drop_table("media_consumer_cursors")
    op.drop_index(
        "ix_media_bridge_offsets_session_source",
        table_name="media_bridge_offsets",
    )
    op.drop_table("media_bridge_offsets")
    op.drop_index("ix_media_events_type_created", table_name="media_events")
    op.drop_index("ix_media_events_session_cursor", table_name="media_events")
    op.drop_table("media_events")
    op.drop_index("ix_media_sessions_status_updated", table_name="media_sessions")
    op.drop_index(
        op.f("ix_media_sessions_legacy_session_id"),
        table_name="media_sessions",
    )
    op.drop_table("media_sessions")

