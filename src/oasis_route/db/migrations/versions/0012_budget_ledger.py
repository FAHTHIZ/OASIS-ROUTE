"""USD budget ledger and per-request receipts

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-25 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "spend_budgets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("period", sa.String(length=20), nullable=False),
        sa.Column("reset_tz", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("limit_usd_micros", sa.BigInteger(), nullable=False),
        sa.Column("used_usd_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_usd_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("drift_usd_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "period"),
    )
    with op.batch_alter_table("reservations") as b:
        b.add_column(sa.Column("budget_usd_micros", sa.BigInteger(), nullable=False, server_default="0"))
        b.add_column(sa.Column("budget_ids", sa.JSON(), nullable=False, server_default="[]"))
    op.create_table(
        "request_receipts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("api_key_id", sa.String(length=36), nullable=True),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        sa.Column("spend_mode", sa.String(length=20), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=True),
        sa.Column("upstream_model", sa.String(length=200), nullable=True),
        sa.Column("connection_id", sa.String(length=36), nullable=True),
        sa.Column("cost_basis", sa.String(length=30), nullable=True),
        sa.Column("cost_status", sa.String(length=20), nullable=True),
        sa.Column("price_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("estimated_usd_micros", sa.BigInteger(), nullable=True),
        sa.Column("reserved_usd_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("actual_usd_micros", sa.BigInteger(), nullable=True),
        sa.Column("drift_usd_micros", sa.BigInteger(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("usage_source", sa.String(length=20), nullable=False, server_default="none"),
        sa.Column("streamed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("approval_id", sa.String(length=36), nullable=True),
        sa.Column("reservation_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("budgets_after", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "request_id"),
    )


def downgrade() -> None:
    op.drop_table("request_receipts")
    with op.batch_alter_table("reservations") as b:
        b.drop_column("budget_ids")
        b.drop_column("budget_usd_micros")
    op.drop_table("spend_budgets")
