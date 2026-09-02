"""Add optional realtime translation configuration and status.

Revision ID: 20260730_0007
Revises: 20260728_0006
Create Date: 2026-07-30
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0007"
down_revision: str | None = "20260728_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("target_language", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "translation_status",
            sa.String(length=32),
            nullable=False,
            server_default="disabled",
        ),
    )
    op.add_column(
        "sessions",
        sa.Column("translation_provider", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("translation_model", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("translation_error_code", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "translation_error_message",
            sa.String(length=1000),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("sessions", "translation_error_message")
    op.drop_column("sessions", "translation_error_code")
    op.drop_column("sessions", "translation_model")
    op.drop_column("sessions", "translation_provider")
    op.drop_column("sessions", "translation_status")
    op.drop_column("sessions", "target_language")
