"""Add independent source and cleanup lifecycle state.

Revision ID: 20260801_0009
Revises: 20260730_0008
Create Date: 2026-08-01
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260801_0009"
down_revision: str | None = "20260730_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "source_status",
            sa.String(length=32),
            nullable=False,
            server_default="stopped",
        ),
    )
    op.add_column(
        "sessions",
        sa.Column("source_ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "cleanup_status",
            sa.String(length=32),
            nullable=False,
            server_default="completed",
        ),
    )
    op.add_column(
        "sessions",
        sa.Column("cleanup_detail", sa.String(length=2000), nullable=True),
    )
    op.execute(
        "UPDATE sessions "
        "SET source_ended_at = ended_at "
        "WHERE status IN ('completed', 'failed', 'cancelled')"
    )
    op.execute(
        "UPDATE sessions "
        "SET source_status = 'lost', cleanup_status = 'failed', "
        "cleanup_detail = 'Source ownership was unavailable during lifecycle migration.' "
        "WHERE status NOT IN ('completed', 'failed', 'cancelled')"
    )


def downgrade() -> None:
    op.drop_column("sessions", "cleanup_detail")
    op.drop_column("sessions", "cleanup_status")
    op.drop_column("sessions", "source_ended_at")
    op.drop_column("sessions", "source_status")
