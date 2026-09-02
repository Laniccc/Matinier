"""Freeze Meeting Mark evidence revisions and messages.

Revision ID: 20260811_0021
Revises: 20260811_0020
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260811_0021"
down_revision: str | None = "20260811_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "meeting_marks",
        sa.Column(
            "source_segment_revisions_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "meeting_marks",
        sa.Column(
            "evidence_messages_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("meeting_marks", "evidence_messages_json")
    op.drop_column("meeting_marks", "source_segment_revisions_json")
