"""connection account status (provider-reported free tier / credits)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("connections") as b:
        b.add_column(sa.Column("account_status", sa.JSON(), server_default="{}", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("connections") as b:
        b.drop_column("account_status")
