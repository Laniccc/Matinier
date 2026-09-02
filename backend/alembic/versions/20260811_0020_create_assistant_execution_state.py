"""Create the shared Assistant execution substrate.

Revision ID: 20260811_0020
Revises: 20260811_0019
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260811_0020"
down_revision: str | None = "20260811_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assistant_context_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("meeting_state_version", sa.Integer(), nullable=False),
        sa.Column("state_slice_json", sa.JSON(), nullable=False),
        sa.Column("source_frontier_json", sa.JSON(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("evidence_messages_json", sa.JSON(), nullable=False),
        sa.Column(
            "relevant_context_hash",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_assistant_context_snapshots_session_id"),
        "assistant_context_snapshots",
        ["session_id"],
        unique=False,
    )

    op.create_table(
        "action_grants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("resource_scope_json", sa.JSON(), nullable=False),
        sa.Column("candidate_ids_json", sa.JSON(), nullable=False),
        sa.Column("linear_team_id", sa.String(length=255), nullable=True),
        sa.Column("max_side_effects", sa.Integer(), nullable=False),
        sa.Column("used_side_effects", sa.Integer(), nullable=False),
        sa.Column(
            "unresolved_identity_policy",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("session_id", "expires_at", "status"):
        op.create_index(
            op.f(f"ix_action_grants_{column}"),
            "action_grants",
            [column],
            unique=False,
        )

    op.create_table(
        "assistant_executions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("profile", sa.String(length=32), nullable=False),
        sa.Column("root_execution_id", sa.String(length=36), nullable=False),
        sa.Column("parent_execution_id", sa.String(length=36), nullable=True),
        sa.Column("snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("grant_id", sa.String(length=36), nullable=True),
        sa.Column("client_request_id", sa.String(length=255), nullable=True),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("step_count", sa.Integer(), nullable=False),
        sa.Column("budget_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["action_grants.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_execution_id"],
            ["assistant_executions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["root_execution_id"],
            ["assistant_executions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["assistant_context_snapshots.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "client_request_id",
            name="uq_assistant_executions_session_client_request",
        ),
    )
    op.create_index(
        "ix_assistant_executions_session_status_profile",
        "assistant_executions",
        ["session_id", "status", "profile"],
        unique=False,
    )
    for column in (
        "root_execution_id",
        "parent_execution_id",
        "snapshot_id",
        "grant_id",
    ):
        op.create_index(
            op.f(f"ix_assistant_executions_{column}"),
            "assistant_executions",
            [column],
            unique=False,
        )

    op.create_table(
        "assistant_steps",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("decision_summary", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["assistant_executions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id",
            "sequence",
            name="uq_assistant_steps_execution_sequence",
        ),
    )
    op.create_index(
        op.f("ix_assistant_steps_execution_id"),
        "assistant_steps",
        ["execution_id"],
        unique=False,
    )

    op.create_table(
        "assistant_tool_calls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("step_id", sa.String(length=36), nullable=True),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("tool_version", sa.String(length=32), nullable=False),
        sa.Column("capability", sa.String(length=128), nullable=False),
        sa.Column("effect", sa.String(length=32), nullable=False),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("arguments_hash", sa.String(length=64), nullable=False),
        sa.Column("logical_action_key", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("external_reference_json", sa.JSON(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["assistant_executions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["assistant_steps.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_assistant_tool_calls_idempotency_key",
        ),
    )
    op.create_index(
        op.f("ix_assistant_tool_calls_execution_id"),
        "assistant_tool_calls",
        ["execution_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_tool_calls_status"),
        "assistant_tool_calls",
        ["status"],
        unique=False,
    )

    op.create_table(
        "assistant_observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("step_id", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("observation_json", sa.JSON(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["assistant_executions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["assistant_steps.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_assistant_observations_execution_id"),
        "assistant_observations",
        ["execution_id"],
        unique=False,
    )

    op.create_table(
        "assistant_handoffs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_execution_id", sa.String(length=36), nullable=False),
        sa.Column("target_execution_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("grant_id", sa.String(length=36), nullable=True),
        sa.Column("envelope_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["action_grants.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["assistant_context_snapshots.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_execution_id"],
            ["assistant_executions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_execution_id"],
            ["assistant_executions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_execution_id",
            name="uq_assistant_handoffs_source_execution",
        ),
        sa.UniqueConstraint(
            "target_execution_id",
            name="uq_assistant_handoffs_target_execution",
        ),
    )

    op.create_table(
        "assistant_subagent_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("planning_round", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("task_json", sa.JSON(), nullable=False),
        sa.Column("budget_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["assistant_executions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["assistant_context_snapshots.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id",
            "planning_round",
            "role",
            name="uq_assistant_subagent_runs_execution_round_role",
        ),
    )

    op.create_table(
        "external_action_claims",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("capability", sa.String(length=128), nullable=False),
        sa.Column("logical_action_key", sa.String(length=255), nullable=False),
        sa.Column("holder_execution_id", sa.String(length=36), nullable=False),
        sa.Column("tool_call_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("arguments_hash", sa.String(length=64), nullable=False),
        sa.Column("external_reference_json", sa.JSON(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["holder_execution_id"],
            ["assistant_executions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id"],
            ["assistant_tool_calls.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "capability",
            "logical_action_key",
            name="uq_external_action_claims_logical_action",
        ),
    )
    op.create_index(
        op.f("ix_external_action_claims_holder_execution_id"),
        "external_action_claims",
        ["holder_execution_id"],
        unique=False,
    )
    op.create_index(
        "ix_external_action_claims_status_lease",
        "external_action_claims",
        ["status", "lease_expires_at"],
        unique=False,
    )

    op.create_table(
        "assistant_client_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("client_operation_id", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["assistant_executions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id",
            "client_operation_id",
            name="uq_assistant_client_operations_execution_operation",
        ),
    )
    op.create_index(
        op.f("ix_assistant_client_operations_execution_id"),
        "assistant_client_operations",
        ["execution_id"],
        unique=False,
    )

    op.create_table(
        "assistant_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("root_execution_id", sa.String(length=36), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["assistant_executions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["root_execution_id"],
            ["assistant_executions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_assistant_events_session_cursor",
        "assistant_events",
        ["session_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_assistant_events_execution_cursor",
        "assistant_events",
        ["execution_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_events_execution_cursor",
        table_name="assistant_events",
    )
    op.drop_index(
        "ix_assistant_events_session_cursor",
        table_name="assistant_events",
    )
    op.drop_table("assistant_events")

    op.drop_index(
        op.f("ix_assistant_client_operations_execution_id"),
        table_name="assistant_client_operations",
    )
    op.drop_table("assistant_client_operations")

    op.drop_index(
        "ix_external_action_claims_status_lease",
        table_name="external_action_claims",
    )
    op.drop_index(
        op.f("ix_external_action_claims_holder_execution_id"),
        table_name="external_action_claims",
    )
    op.drop_table("external_action_claims")
    op.drop_table("assistant_subagent_runs")
    op.drop_table("assistant_handoffs")

    op.drop_index(
        op.f("ix_assistant_observations_execution_id"),
        table_name="assistant_observations",
    )
    op.drop_table("assistant_observations")

    op.drop_index(
        op.f("ix_assistant_tool_calls_status"),
        table_name="assistant_tool_calls",
    )
    op.drop_index(
        op.f("ix_assistant_tool_calls_execution_id"),
        table_name="assistant_tool_calls",
    )
    op.drop_table("assistant_tool_calls")

    op.drop_index(
        op.f("ix_assistant_steps_execution_id"),
        table_name="assistant_steps",
    )
    op.drop_table("assistant_steps")

    for column in reversed(
        (
            "root_execution_id",
            "parent_execution_id",
            "snapshot_id",
            "grant_id",
        )
    ):
        op.drop_index(
            op.f(f"ix_assistant_executions_{column}"),
            table_name="assistant_executions",
        )
    op.drop_index(
        "ix_assistant_executions_session_status_profile",
        table_name="assistant_executions",
    )
    op.drop_table("assistant_executions")

    for column in reversed(("session_id", "expires_at", "status")):
        op.drop_index(
            op.f(f"ix_action_grants_{column}"),
            table_name="action_grants",
        )
    op.drop_table("action_grants")

    op.drop_index(
        op.f("ix_assistant_context_snapshots_session_id"),
        table_name="assistant_context_snapshots",
    )
    op.drop_table("assistant_context_snapshots")
