"""Add meeting plugin session activation, durable operations and intent hashes.

Revision ID: 20260831_0027
Revises: 20260828_0026
"""
import sqlalchemy as sa
from alembic import op

revision = "20260831_0027"
down_revision = "20260828_0026"
branch_labels = None
depends_on = None


def _owner_columns():
    return [
        sa.Column("plugin_id", sa.String(128), nullable=False),
        sa.Column("plugin_version", sa.String(64), nullable=False),
        sa.Column("media_session_id", sa.String(36), sa.ForeignKey("media_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("legacy_session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("authority_epoch", sa.Integer(), nullable=False),
    ]


def upgrade():
    op.create_table(
        "meeting_plugin_sessions",
        sa.Column("legacy_session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("media_session_id", sa.String(36), sa.ForeignKey("media_sessions.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("plugin_id", sa.String(128), nullable=False),
        sa.Column("plugin_version", sa.String(64), nullable=False),
        sa.Column("analysis_state", sa.String(16), nullable=False, server_default="inactive"),
        sa.Column("analysis_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("authority_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("activated_by", sa.String(255)),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("stopped_reason", sa.String(128)),
        sa.Column("terminal_frontier_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("analysis_epoch >= 0 AND authority_epoch >= 0", name="ck_meeting_plugin_session_epochs"),
        sa.CheckConstraint("analysis_state IN ('inactive','active','draining','completed')", name="ck_meeting_plugin_analysis_state"),
    )
    op.create_index("ix_meeting_plugin_sessions_analysis", "meeting_plugin_sessions", ["analysis_state", "plugin_id"])
    op.create_table(
        "assistant_action_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        *_owner_columns(),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('active','consumed','revoked')", name="ck_assistant_action_intent_status"),
    )
    op.create_index("ix_assistant_action_intents_scope", "assistant_action_intents", ["plugin_id", "media_session_id", "status"])
    op.create_index("ix_assistant_action_intents_expiry", "assistant_action_intents", ["expires_at"])
    op.create_table(
        "meeting_plugin_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        *_owner_columns(),
        sa.Column("intent_id", sa.String(36), sa.ForeignKey("assistant_action_intents.id", ondelete="SET NULL")),
        sa.Column("execution_id", sa.String(36), sa.ForeignKey("assistant_executions.id", ondelete="SET NULL")),
        sa.Column("client_request_id", sa.String(255), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("request_payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="accepted"),
        sa.Column("error_code", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("plugin_id", "plugin_version", "media_session_id", "client_request_id", name="uq_meeting_plugin_operations_request"),
        sa.CheckConstraint("authority_epoch >= 0", name="ck_meeting_plugin_operation_epoch"),
        sa.CheckConstraint("status IN ('accepted','running','completed','failed','cancelled')", name="ck_meeting_plugin_operation_status"),
    )
    op.create_index("ix_meeting_plugin_operations_status", "meeting_plugin_operations", ["status", "created_at"])


def downgrade():
    op.drop_table("meeting_plugin_operations")
    op.drop_table("assistant_action_intents")
    op.drop_table("meeting_plugin_sessions")
