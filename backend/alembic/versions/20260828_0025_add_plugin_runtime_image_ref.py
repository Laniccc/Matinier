"""Persist the Docker image reference returned by verified image import.

Revision ID: 20260828_0025
Revises: 20260827_0024
Create Date: 2026-08-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260828_0025"
down_revision = "20260827_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plugin_packages",
        sa.Column("runtime_image_ref", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plugin_packages", "runtime_image_ref")
