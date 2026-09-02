"""Add Stage 4 export metadata and source event timing.

Revision ID: 20260722_0003
Revises: 20260722_0002
Create Date: 2026-07-22
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260722_0003"
down_revision: str | None = "20260722_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("asr_provider", sa.String(length=64)))
    op.add_column("sessions", sa.Column("asr_model", sa.String(length=128)))
    op.add_column("sessions", sa.Column("final_result_count", sa.Integer()))
    op.add_column("sessions", sa.Column("first_partial_latency_ms", sa.Float()))
    op.add_column("sessions", sa.Column("average_final_latency_ms", sa.Float()))
    op.add_column("sessions", sa.Column("provider_error_count", sa.Integer()))
    op.add_column("sessions", sa.Column("sent_audio_chunk_count", sa.Integer()))
    op.add_column("sessions", sa.Column("sent_audio_bytes", sa.Integer()))

    op.add_column("segments", sa.Column("received_at_ms", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE segments "
        "SET received_at_ms = CAST(strftime('%s', finalized_at) AS INTEGER) * 1000"
    )
    with op.batch_alter_table("segments") as batch_op:
        batch_op.alter_column(
            "received_at_ms",
            existing_type=sa.Integer(),
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("segments") as batch_op:
        batch_op.drop_column("received_at_ms")
    op.drop_column("sessions", "sent_audio_bytes")
    op.drop_column("sessions", "sent_audio_chunk_count")
    op.drop_column("sessions", "provider_error_count")
    op.drop_column("sessions", "average_final_latency_ms")
    op.drop_column("sessions", "first_partial_latency_ms")
    op.drop_column("sessions", "final_result_count")
    op.drop_column("sessions", "asr_model")
    op.drop_column("sessions", "asr_provider")
