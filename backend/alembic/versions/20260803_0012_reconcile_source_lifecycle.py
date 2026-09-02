"""Reconcile terminal Worker-owned source lifecycle state.

Revision ID: 20260803_0012
Revises: 20260801_0011
Create Date: 2026-08-03
"""

from typing import Sequence

from alembic import op


revision: str = "20260803_0012"
down_revision: str | None = "20260801_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RECONCILIATION_DETAIL = (
    "Reconciled terminal non-HLS source lifecycle during migration."
)


def upgrade() -> None:
    # Before Worker-owned source cleanup was persisted, terminal microphone,
    # screen and file runs remained at starting/not_started. Their terminal
    # run state proves that the corresponding Worker audio loop has ended.
    op.execute(
        "UPDATE sessions "
        "SET source_status = 'stopped', "
        "source_ended_at = COALESCE(source_ended_at, ended_at, created_at), "
        "cleanup_status = 'completed', "
        f"cleanup_detail = '{_RECONCILIATION_DETAIL}' "
        "WHERE source_type <> 'hls' "
        "AND status IN ('completed', 'failed', 'cancelled') "
        "AND source_status IN ('starting', 'running', 'stopping') "
        "AND cleanup_status IN ('not_started', 'in_progress') "
        "AND cleanup_detail IS NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE sessions "
        "SET source_status = 'starting', source_ended_at = NULL, "
        "cleanup_status = 'not_started', cleanup_detail = NULL "
        "WHERE source_type <> 'hls' "
        f"AND cleanup_detail = '{_RECONCILIATION_DETAIL}'"
    )
