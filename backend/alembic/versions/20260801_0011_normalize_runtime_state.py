"""Normalize Session and Translation runtime lifecycle states.

Revision ID: 20260801_0011
Revises: 20260801_0009
Create Date: 2026-08-03
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260801_0011"
down_revision: str | None = "20260801_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SESSION_STATE_MAP = {
    "created": "created",
    "room_ready": "starting",
    "replaying": "running",
    "transcribing": "running",
    "finalizing": "finalizing",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}
_TRANSLATION_STATE_MAP = {
    "disabled": "disabled",
    "pending": "starting",
    "starting": "starting",
    "translating": "running",
    "finalizing": "running",
    "completed": "completed",
    "failed": "failed",
}


def _assert_known_values(column: str, known: set[str]) -> None:
    connection = op.get_bind()
    values = set(
        connection.execute(
            sa.text(f"SELECT DISTINCT {column} FROM sessions")
        ).scalars()
    )
    unknown = sorted(value for value in values if value not in known)
    if unknown:
        raise RuntimeError(
            f"Cannot normalize unknown sessions.{column} values: {unknown}"
        )


def _apply_mapping(column: str, mapping: dict[str, str]) -> None:
    clauses = " ".join(
        f"WHEN '{old}' THEN '{new}'" for old, new in mapping.items()
    )
    op.execute(
        f"UPDATE sessions SET {column} = CASE {column} {clauses} END"
    )


def upgrade() -> None:
    _assert_known_values("status", set(_SESSION_STATE_MAP))
    _assert_known_values("translation_status", set(_TRANSLATION_STATE_MAP))

    op.add_column(
        "sessions",
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("failure_code", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("failure_detail", sa.String(length=1000), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "translation_ended_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE sessions SET failure_code = error_code, "
        "failure_detail = error_message WHERE error_code IS NOT NULL"
    )
    op.execute(
        "UPDATE sessions SET translation_ended_at = "
        "COALESCE(ended_at, source_ended_at, created_at) "
        "WHERE translation_status IN ('completed', 'failed')"
    )
    _apply_mapping("status", _SESSION_STATE_MAP)
    _apply_mapping("translation_status", _TRANSLATION_STATE_MAP)


def downgrade() -> None:
    op.execute(
        "UPDATE sessions SET error_code = COALESCE(error_code, failure_code), "
        "error_message = COALESCE(error_message, failure_detail)"
    )
    op.execute(
        "UPDATE sessions SET status = CASE status "
        "WHEN 'created' THEN 'created' "
        "WHEN 'starting' THEN 'room_ready' "
        "WHEN 'running' THEN 'transcribing' "
        "WHEN 'finalizing' THEN 'finalizing' "
        "WHEN 'completed' THEN 'completed' "
        "WHEN 'failed' THEN 'failed' "
        "WHEN 'cancelled' THEN 'cancelled' END"
    )
    op.execute(
        "UPDATE sessions SET translation_status = CASE translation_status "
        "WHEN 'disabled' THEN 'disabled' "
        "WHEN 'starting' THEN 'pending' "
        "WHEN 'running' THEN 'translating' "
        "WHEN 'completed' THEN 'completed' "
        "WHEN 'failed' THEN 'failed' END"
    )
    op.drop_column("sessions", "translation_ended_at")
    op.drop_column("sessions", "failure_detail")
    op.drop_column("sessions", "failure_code")
    op.drop_column("sessions", "stop_reason")
