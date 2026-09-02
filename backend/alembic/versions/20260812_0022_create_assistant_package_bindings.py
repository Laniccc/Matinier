"""Bind Assistant executions to immutable transcript Packages.

Revision ID: 20260812_0022
Revises: 20260811_0021
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260812_0022"
down_revision: str | None = "20260811_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assistant_package_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("package_id", sa.String(length=36), nullable=False),
        sa.Column("package_version", sa.Integer(), nullable=False),
        sa.Column("package_content_hash", sa.String(length=64), nullable=False),
        sa.Column("segment_item_mapping_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["assistant_executions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["assistant_context_snapshots.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["result_packages.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id",
            name="uq_assistant_package_bindings_execution",
        ),
    )
    op.create_index(
        op.f("ix_assistant_package_bindings_snapshot_id"),
        "assistant_package_bindings",
        ["snapshot_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_package_bindings_package_id"),
        "assistant_package_bindings",
        ["package_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_assistant_package_bindings_package_id"),
        table_name="assistant_package_bindings",
    )
    op.drop_index(
        op.f("ix_assistant_package_bindings_snapshot_id"),
        table_name="assistant_package_bindings",
    )
    op.drop_table("assistant_package_bindings")
