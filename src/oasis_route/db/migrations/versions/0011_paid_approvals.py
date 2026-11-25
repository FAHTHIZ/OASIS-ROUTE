"""single-use paid-route approvals (bounded-autonomy spend mode)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-25 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paid_approvals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("upstream_model", sa.String(length=200), nullable=False),
        sa.Column("max_cost_usd_micros", sa.BigInteger(), nullable=True),
        sa.Column("cost_status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by_request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_paid_approvals_project_id", "paid_approvals", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_paid_approvals_project_id", table_name="paid_approvals")
    op.drop_table("paid_approvals")
