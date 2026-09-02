"""Enforce unique identities for Package documents without a language.

Revision ID: 20260811_0018
Revises: 20260810_0017
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260811_0018"
down_revision: str | None = "20260810_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    package_documents = sa.table(
        "package_documents",
        sa.column("package_id", sa.String()),
        sa.column("document_kind", sa.String()),
        sa.column("language", sa.String()),
    )
    duplicates = connection.execute(
        sa.select(
            package_documents.c.package_id,
            package_documents.c.document_kind,
            sa.func.count().label("document_count"),
        )
        .where(package_documents.c.language.is_(None))
        .group_by(
            package_documents.c.package_id,
            package_documents.c.document_kind,
        )
        .having(sa.func.count() > 1)
    ).mappings().all()
    if duplicates:
        identities = ", ".join(
            f"{row['package_id']}:{row['document_kind']}"
            for row in duplicates
        )
        raise RuntimeError(
            "Cannot enforce Package document identity because duplicate "
            f"language-less documents exist: {identities}"
        )

    op.create_index(
        "uq_package_documents_identity_no_language",
        "package_documents",
        ["package_id", "document_kind"],
        unique=True,
        sqlite_where=sa.text("language IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_package_documents_identity_no_language",
        table_name="package_documents",
    )
