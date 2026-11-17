"""bind redirect OAuth flows to the browser that started them

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("oauth_flows") as b:
        b.add_column(sa.Column("browser_binding_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("oauth_flows") as b:
        b.drop_column("browser_binding_hash")
