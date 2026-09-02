"""Add Stage 6 session lifecycle error fields.

Revision ID: 20260728_0005
Revises: 20260723_0004
Create Date: 2026-07-28
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0005"
down_revision: str | None = "20260723_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("error_code", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("error_message", sa.String(length=1000), nullable=True),
    )
    op.execute(
        "UPDATE sessions SET status = 'transcribing' WHERE status = 'running'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE sessions SET status = 'running' WHERE status = 'transcribing'"
    )
    op.drop_column("sessions", "error_message")
    op.drop_column("sessions", "error_code")
