"""Create plugin installation, runtime, capability, and audit persistence.

Revision ID: 20260827_0024
Revises: 20260827_0023
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260827_0024"
down_revision: str | None = "20260827_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plugin_publishers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("key_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("trusted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_plugin_publishers_name"),
        sa.UniqueConstraint(
            "key_fingerprint", name="uq_plugin_publishers_key_fingerprint"
        ),
    )
    op.create_index(
        op.f("ix_plugin_publishers_key_fingerprint"),
        "plugin_publishers",
        ["key_fingerprint"],
        unique=True,
    )

    op.create_table(
        "plugin_packages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("content_digest", sa.String(length=80), nullable=False),
        sa.Column("manifest_hash", sa.String(length=80), nullable=False),
        sa.Column("image_digest", sa.String(length=80), nullable=False),
        sa.Column("signature_status", sa.String(length=32), nullable=False),
        sa.Column("package_path", sa.Text(), nullable=False),
        sa.Column("publisher_id", sa.String(length=36), nullable=True),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["publisher_id"], ["plugin_publishers.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plugin_id", "version", name="uq_plugin_packages_id_version"
        ),
        sa.UniqueConstraint(
            "content_digest", name="uq_plugin_packages_content_digest"
        ),
    )
    op.create_index(
        op.f("ix_plugin_packages_publisher_id"),
        "plugin_packages",
        ["publisher_id"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_packages_plugin_installed",
        "plugin_packages",
        ["plugin_id", "installed_at"],
        unique=False,
    )

    op.create_table(
        "plugin_installations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("preferred_package_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["preferred_package_id"], ["plugin_packages.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plugin_id", name="uq_plugin_installations_plugin_id"),
    )
    op.create_index(
        "ix_plugin_installations_status",
        "plugin_installations",
        ["status", "updated_at"],
        unique=False,
    )

    op.create_table(
        "plugin_staging_tickets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("manifest_hash", sa.String(length=80), nullable=False),
        sa.Column("content_digest", sa.String(length=80), nullable=False),
        sa.Column("staged_path", sa.Text(), nullable=False),
        sa.Column("permission_request_json", sa.JSON(), nullable=False),
        sa.Column("signature_status", sa.String(length=32), nullable=False),
        sa.Column("publisher_name", sa.String(length=128), nullable=True),
        sa.Column("publisher_fingerprint", sa.String(length=80), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_plugin_staging_tickets_expiry",
        "plugin_staging_tickets",
        ["expires_at", "consumed_at"],
        unique=False,
    )

    op.create_table(
        "plugin_permissions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("plugin_version", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("permission_name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_id"], ["plugin_packages.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "package_id", "permission_name", name="uq_plugin_permissions_package_name"
        ),
    )
    op.create_index(
        "ix_plugin_permissions_plugin_version",
        "plugin_permissions",
        ["plugin_id", "plugin_version"],
        unique=False,
    )

    op.create_table(
        "plugin_runtime_health",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("plugin_version", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("crash_count", sa.Integer(), nullable=False),
        sa.Column("last_exit_code", sa.Integer(), nullable=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantine_reason", sa.String(length=500), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_id"], ["plugin_packages.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_id", name="uq_plugin_runtime_health_package"),
    )
    op.create_index(
        "ix_plugin_runtime_health_status",
        "plugin_runtime_health",
        ["status", "updated_at"],
        unique=False,
    )

    op.create_table(
        "plugin_session_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("plugin_version", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("media_session_id", sa.String(length=36), nullable=False),
        sa.Column("session_scope", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_delivered_sequence", sa.Integer(), nullable=False),
        sa.Column("last_acknowledged_sequence", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_id"], ["plugin_packages.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["media_session_id"], ["media_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "package_id",
            "media_session_id",
            name="uq_plugin_bindings_package_session",
        ),
        sa.UniqueConstraint(
            "session_scope", name="uq_plugin_bindings_session_scope"
        ),
    )
    op.create_index(
        "ix_plugin_bindings_state",
        "plugin_session_bindings",
        ["status", "updated_at"],
        unique=False,
    )

    op.create_table(
        "plugin_state_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("plugin_version", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("namespace", sa.String(length=192), nullable=False),
        sa.Column("item_key", sa.String(length=192), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_id"], ["plugin_packages.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "package_id", "namespace", "item_key", name="uq_plugin_state_package_key"
        ),
    )
    op.create_index(
        "ix_plugin_state_namespace",
        "plugin_state_items",
        ["plugin_id", "namespace"],
        unique=False,
    )

    op.create_table(
        "plugin_ui_views",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("plugin_version", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("media_session_id", sa.String(length=36), nullable=False),
        sa.Column("surface", sa.String(length=32), nullable=False),
        sa.Column("view_id", sa.String(length=128), nullable=False),
        sa.Column("view_version", sa.Integer(), nullable=False),
        sa.Column("view_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_id"], ["plugin_packages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["media_session_id"], ["media_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "package_id",
            "media_session_id",
            "surface",
            "view_id",
            name="uq_plugin_ui_views_identity",
        ),
    )
    op.create_index(
        "ix_plugin_ui_views_session_surface",
        "plugin_ui_views",
        ["media_session_id", "surface"],
        unique=False,
    )

    op.create_table(
        "plugin_capability_grants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("plugin_version", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("media_session_id", sa.String(length=36), nullable=True),
        sa.Column("capability", sa.String(length=160), nullable=False),
        sa.Column("effect", sa.String(length=32), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_id"], ["plugin_packages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["media_session_id"], ["media_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_plugin_grants_expiry",
        "plugin_capability_grants",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_grants_plugin_session",
        "plugin_capability_grants",
        ["plugin_id", "media_session_id"],
        unique=False,
    )

    op.create_table(
        "plugin_capability_invocations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("plugin_version", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("media_session_id", sa.String(length=36), nullable=True),
        sa.Column("capability", sa.String(length=160), nullable=False),
        sa.Column("effect", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "outcome_unknown", sa.Boolean(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["package_id"], ["plugin_packages.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["media_session_id"], ["media_sessions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plugin_id",
            "idempotency_key",
            name="uq_plugin_invocations_idempotency",
        ),
    )
    op.create_index(
        "ix_plugin_invocations_status_created",
        "plugin_capability_invocations",
        ["status", "created_at"],
        unique=False,
    )

    op.create_table(
        "plugin_audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("plugin_version", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("media_session_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_id"], ["plugin_packages.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["media_session_id"], ["media_sessions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_plugin_audit_time",
        "plugin_audit_events",
        ["plugin_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_audit_event_type",
        "plugin_audit_events",
        ["event_type", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_plugin_audit_event_type", table_name="plugin_audit_events")
    op.drop_index("ix_plugin_audit_time", table_name="plugin_audit_events")
    op.drop_table("plugin_audit_events")
    op.drop_index(
        "ix_plugin_invocations_status_created",
        table_name="plugin_capability_invocations",
    )
    op.drop_table("plugin_capability_invocations")
    op.drop_index(
        "ix_plugin_grants_plugin_session", table_name="plugin_capability_grants"
    )
    op.drop_index("ix_plugin_grants_expiry", table_name="plugin_capability_grants")
    op.drop_table("plugin_capability_grants")
    op.drop_index(
        "ix_plugin_ui_views_session_surface", table_name="plugin_ui_views"
    )
    op.drop_table("plugin_ui_views")
    op.drop_index("ix_plugin_state_namespace", table_name="plugin_state_items")
    op.drop_table("plugin_state_items")
    op.drop_index("ix_plugin_bindings_state", table_name="plugin_session_bindings")
    op.drop_table("plugin_session_bindings")
    op.drop_index("ix_plugin_runtime_health_status", table_name="plugin_runtime_health")
    op.drop_table("plugin_runtime_health")
    op.drop_index(
        "ix_plugin_permissions_plugin_version", table_name="plugin_permissions"
    )
    op.drop_table("plugin_permissions")
    op.drop_index(
        "ix_plugin_staging_tickets_expiry", table_name="plugin_staging_tickets"
    )
    op.drop_table("plugin_staging_tickets")
    op.drop_index("ix_plugin_installations_status", table_name="plugin_installations")
    op.drop_table("plugin_installations")
    op.drop_index(
        "ix_plugin_packages_plugin_installed", table_name="plugin_packages"
    )
    op.drop_index(op.f("ix_plugin_packages_publisher_id"), table_name="plugin_packages")
    op.drop_table("plugin_packages")
    op.drop_index(
        op.f("ix_plugin_publishers_key_fingerprint"),
        table_name="plugin_publishers",
    )
    op.drop_table("plugin_publishers")
