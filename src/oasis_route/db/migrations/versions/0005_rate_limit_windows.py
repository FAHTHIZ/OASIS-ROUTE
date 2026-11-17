"""shared per-minute rate-limit counters for multi-replica deployments

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_windows",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("window", sa.BigInteger(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key", "window", name=op.f("pk_rate_limit_windows")),
    )


def downgrade() -> None:
    op.drop_table("rate_limit_windows")
