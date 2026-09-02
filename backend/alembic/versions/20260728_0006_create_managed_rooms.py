"""Create managed Rooms and allow reusable LiveKit room names.

Revision ID: 20260728_0006
Revises: 20260728_0005
Create Date: 2026-07-28
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0006"
down_revision: str | None = "20260728_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def upgrade() -> None:
    op.create_table(
        "managed_rooms",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("room_name", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_name"),
    )
    op.create_index(
        "ix_managed_rooms_room_name",
        "managed_rooms",
        ["room_name"],
        unique=True,
    )

    with op.batch_alter_table(
        "sessions",
        naming_convention=NAMING_CONVENTION,
        recreate="always",
    ) as batch_op:
        batch_op.drop_index("ix_sessions_room_name")
        batch_op.drop_constraint("uq_sessions_room_name", type_="unique")
        batch_op.add_column(sa.Column("room_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_sessions_room_id_managed_rooms",
            "managed_rooms",
            ["room_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_sessions_room_id", ["room_id"], unique=False)
        batch_op.create_index("ix_sessions_room_name", ["room_name"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table(
        "sessions",
        naming_convention=NAMING_CONVENTION,
        recreate="always",
    ) as batch_op:
        batch_op.drop_index("ix_sessions_room_name")
        batch_op.drop_index("ix_sessions_room_id")
        batch_op.drop_constraint(
            "fk_sessions_room_id_managed_rooms",
            type_="foreignkey",
        )
        batch_op.drop_column("room_id")
        batch_op.create_unique_constraint("uq_sessions_room_name", ["room_name"])
        batch_op.create_index("ix_sessions_room_name", ["room_name"], unique=True)

    op.drop_index("ix_managed_rooms_room_name", table_name="managed_rooms")
    op.drop_table("managed_rooms")
