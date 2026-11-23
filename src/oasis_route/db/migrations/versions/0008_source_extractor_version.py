"""source extractor version (re-extract stored sources after an extractor upgrade)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-23 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sources") as b:
        b.add_column(sa.Column("extractor_version", sa.String(length=40), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sources") as b:
        b.drop_column("extractor_version")
