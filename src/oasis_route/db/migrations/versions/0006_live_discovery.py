"""live discovery: seed/frontier sources, rich offers, provider candidates, spend policy

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sources") as b:
        b.add_column(sa.Column("origin", sa.String(length=20), server_default="manual", nullable=False))
        b.add_column(sa.Column("depth", sa.Integer(), server_default="0", nullable=False))
        b.add_column(sa.Column("parent_id", sa.String(length=36), nullable=True))
        b.add_column(sa.Column("extractor", sa.String(length=40), nullable=True))
    with op.batch_alter_table("offers") as b:
        b.add_column(sa.Column("offer_class", sa.String(length=30), server_default="unknown", nullable=False))
        b.add_column(sa.Column("access_path", sa.String(length=30), server_default="api_key", nullable=False))
        b.add_column(sa.Column("models", sa.JSON(), server_default="[]", nullable=False))
        b.add_column(sa.Column("regions", sa.JSON(), server_default="[]", nullable=False))
        b.add_column(sa.Column("billing", sa.JSON(), server_default="{}", nullable=False))
        b.add_column(sa.Column("links", sa.JSON(), server_default="{}", nullable=False))
        b.add_column(sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("extractor_version", sa.String(length=20), nullable=True))
    with op.batch_alter_table("projects") as b:
        # Existing projects keep their current (unrestricted) behaviour; new projects default to free_only.
        b.add_column(
            sa.Column("spend_policy", sa.String(length=20), server_default="allow_paid", nullable=False)
        )
    with op.batch_alter_table("connections") as b:
        b.add_column(
            sa.Column("billing_mode", sa.String(length=20), server_default="unknown", nullable=False)
        )
    with op.batch_alter_table("models") as b:
        b.add_column(sa.Column("pricing", sa.JSON(), server_default="{}", nullable=False))
    op.create_table(
        "provider_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("via", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_provider_candidates_source_id_sources"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_candidates")),
        sa.UniqueConstraint("slug", name=op.f("uq_provider_candidates_slug")),
    )


def downgrade() -> None:
    op.drop_table("provider_candidates")
    with op.batch_alter_table("models") as b:
        b.drop_column("pricing")
    with op.batch_alter_table("connections") as b:
        b.drop_column("billing_mode")
    with op.batch_alter_table("projects") as b:
        b.drop_column("spend_policy")
    with op.batch_alter_table("offers") as b:
        for col in (
            "extractor_version",
            "last_observed_at",
            "first_observed_at",
            "links",
            "billing",
            "regions",
            "models",
            "access_path",
            "offer_class",
        ):
            b.drop_column(col)
    with op.batch_alter_table("sources") as b:
        for col in ("extractor", "parent_id", "depth", "origin"):
            b.drop_column(col)
