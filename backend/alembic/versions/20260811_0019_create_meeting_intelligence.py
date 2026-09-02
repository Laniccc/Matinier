"""Create incremental meeting-intelligence persistence.

Revision ID: 20260811_0019
Revises: 20260811_0018
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260811_0019"
down_revision: str | None = "20260811_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "meeting_projection_offsets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("segment_id", sa.String(length=255), nullable=False),
        sa.Column("processed_revision", sa.Integer(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "segment_id",
            name="uq_meeting_projection_offsets_session_segment",
        ),
    )
    op.create_index(
        op.f("ix_meeting_projection_offsets_session_id"),
        "meeting_projection_offsets",
        ["session_id"],
        unique=False,
    )

    op.create_table(
        "meeting_state_heads",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("source_frontier_json", sa.JSON(), nullable=False),
        sa.Column(
            "latest_final_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "projected_through",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("lag_ms", sa.Integer(), nullable=False),
        sa.Column("pending_segment_count", sa.Integer(), nullable=False),
        sa.Column(
            "last_success_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_error_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        op.f("ix_meeting_state_heads_status"),
        "meeting_state_heads",
        ["status"],
        unique=False,
    )

    op.create_table(
        "meeting_marks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_segment_ids_json", sa.JSON(), nullable=False),
        sa.Column("audio_start_ms", sa.Integer(), nullable=True),
        sa.Column("audio_end_ms", sa.Integer(), nullable=True),
        sa.Column("source_state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_meeting_marks_session_id"),
        "meeting_marks",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_meeting_marks_kind"),
        "meeting_marks",
        ["kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_meeting_marks_status"),
        "meeting_marks",
        ["status"],
        unique=False,
    )

    op.create_table(
        "action_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("lineage_root_id", sa.String(length=36), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("content_status", sa.String(length=32), nullable=False),
        sa.Column("execution_status", sa.String(length=32), nullable=False),
        sa.Column(
            "superseded_by_candidate_id",
            sa.String(length=36),
            nullable=True,
        ),
        sa.Column(
            "derived_from_candidate_id",
            sa.String(length=36),
            nullable=True,
        ),
        sa.Column("derived_from_revision", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["derived_from_candidate_id"],
            ["action_candidates.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_candidate_id"],
            ["action_candidates.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "session_id",
        "lineage_root_id",
        "content_status",
        "execution_status",
        "superseded_by_candidate_id",
        "derived_from_candidate_id",
    ):
        op.create_index(
            op.f(f"ix_action_candidates_{column}"),
            "action_candidates",
            [column],
            unique=False,
        )

    op.create_table(
        "action_candidate_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("readiness", sa.String(length=32), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("change_kind", sa.String(length=32), nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=True),
        sa.Column("change_summary", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["action_candidates.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_id",
            "revision",
            name="uq_action_candidate_revisions_candidate_revision",
        ),
    )
    op.create_index(
        op.f("ix_action_candidate_revisions_candidate_id"),
        "action_candidate_revisions",
        ["candidate_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_action_candidate_revisions_readiness"),
        "action_candidate_revisions",
        ["readiness"],
        unique=False,
    )

    op.create_table(
        "identity_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("linear_team_id", sa.String(length=255), nullable=False),
        sa.Column("normalized_mention", sa.String(length=255), nullable=False),
        sa.Column("linear_user_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "confirmed_by_actor_id",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "last_verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_id",
            "linear_team_id",
            "normalized_mention",
            "status",
            name="uq_identity_bindings_actor_team_mention_status",
        ),
    )
    op.create_index(
        "ix_identity_bindings_lookup",
        "identity_bindings",
        ["actor_id", "linear_team_id", "normalized_mention", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_identity_bindings_lookup",
        table_name="identity_bindings",
    )
    op.drop_table("identity_bindings")

    op.drop_index(
        op.f("ix_action_candidate_revisions_readiness"),
        table_name="action_candidate_revisions",
    )
    op.drop_index(
        op.f("ix_action_candidate_revisions_candidate_id"),
        table_name="action_candidate_revisions",
    )
    op.drop_table("action_candidate_revisions")

    for column in reversed(
        (
            "session_id",
            "lineage_root_id",
            "content_status",
            "execution_status",
            "superseded_by_candidate_id",
            "derived_from_candidate_id",
        )
    ):
        op.drop_index(
            op.f(f"ix_action_candidates_{column}"),
            table_name="action_candidates",
        )
    op.drop_table("action_candidates")

    op.drop_index(op.f("ix_meeting_marks_status"), table_name="meeting_marks")
    op.drop_index(op.f("ix_meeting_marks_kind"), table_name="meeting_marks")
    op.drop_index(
        op.f("ix_meeting_marks_session_id"),
        table_name="meeting_marks",
    )
    op.drop_table("meeting_marks")

    op.drop_index(
        op.f("ix_meeting_state_heads_status"),
        table_name="meeting_state_heads",
    )
    op.drop_table("meeting_state_heads")

    op.drop_index(
        op.f("ix_meeting_projection_offsets_session_id"),
        table_name="meeting_projection_offsets",
    )
    op.drop_table("meeting_projection_offsets")
