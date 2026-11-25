"""catalogue provenance on providers and models (imported OmniRoute catalogue)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-25 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("providers", "models"):
        with op.batch_alter_table(table) as b:
            b.add_column(sa.Column("catalog", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    for table in ("providers", "models"):
        with op.batch_alter_table(table) as b:
            b.drop_column("catalog")
