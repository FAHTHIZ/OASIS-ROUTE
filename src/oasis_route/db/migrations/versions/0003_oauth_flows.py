"""official provider OAuth (PKCE) connection flows

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import oasis_route.db.models

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = oasis_route.db.models.UTCDateTime


def upgrade() -> None:
    op.create_table(
        "oauth_flows",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("verifier_credential_id", sa.String(length=36), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=True),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("expires_at", TS(timezone=True), nullable=False),
        sa.Column("created_at", TS(timezone=True), nullable=False),
        sa.Column("completed_at", TS(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"]),
        sa.ForeignKeyConstraint(["verifier_credential_id"], ["credentials.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash"),
    )


def downgrade() -> None:
    op.drop_table("oauth_flows")
