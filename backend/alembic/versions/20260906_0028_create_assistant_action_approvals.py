"""Store pending human approvals for scoped ActionGrant minting.

Revision ID: 20260906_0028
Revises: 20260831_0027
"""

from alembic import op
import sqlalchemy as sa


revision = "20260906_0028"
down_revision = "20260831_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_action_approvals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("capability", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=True),
        sa.Column("logical_action_key", sa.String(length=255), nullable=True),
        sa.Column("resource_scope_json", sa.JSON(), nullable=False),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("display_summary_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("grant_id", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["assistant_executions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["action_grants.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_assistant_action_approvals_execution_id",
        "assistant_action_approvals",
        ["execution_id"],
    )
    op.create_index(
        "ix_assistant_action_approvals_status",
        "assistant_action_approvals",
        ["status"],
    )
    op.create_index(
        "ix_assistant_action_approvals_expires_at",
        "assistant_action_approvals",
        ["expires_at"],
    )
    op.create_index(
        "ix_assistant_action_approvals_grant_id",
        "assistant_action_approvals",
        ["grant_id"],
    )
    op.create_index(
        "ix_assistant_action_approvals_session_status",
        "assistant_action_approvals",
        ["session_id", "status"],
    )
    op.create_index(
        "uq_assistant_action_approvals_pending_execution",
        "assistant_action_approvals",
        ["execution_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_assistant_action_approvals_pending_execution",
        table_name="assistant_action_approvals",
    )
    op.drop_index(
        "ix_assistant_action_approvals_session_status",
        table_name="assistant_action_approvals",
    )
    op.drop_index(
        "ix_assistant_action_approvals_grant_id",
        table_name="assistant_action_approvals",
    )
    op.drop_index(
        "ix_assistant_action_approvals_expires_at",
        table_name="assistant_action_approvals",
    )
    op.drop_index(
        "ix_assistant_action_approvals_status",
        table_name="assistant_action_approvals",
    )
    op.drop_index(
        "ix_assistant_action_approvals_execution_id",
        table_name="assistant_action_approvals",
    )
    op.drop_table("assistant_action_approvals")
