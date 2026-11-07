"""documents.reindexing: a re-index of a ready document keeps it searchable

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A plain ALTER TABLE ADD COLUMN (no table rebuild, so existing chunks are never touched).
    op.add_column(
        "documents",
        sa.Column("reindexing", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    # Not a batch rebuild: dropping and recreating `documents` while chunks/jobs rows point at it
    # fails (or cascades) with foreign keys on. SQLite >= 3.35 (our minimum) drops the column in
    # place.
    op.execute("ALTER TABLE documents DROP COLUMN reindexing")
