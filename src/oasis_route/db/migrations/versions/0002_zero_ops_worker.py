"""zero-operations worker: job observability, heartbeats, pause controls

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-17 10:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import oasis_route.db.models

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = oasis_route.db.models.UTCDateTime


def upgrade() -> None:
    with op.batch_alter_table("worker_leases") as b:
        b.add_column(sa.Column("next_run_at", TS(timezone=True), nullable=True))
        b.add_column(sa.Column("last_started_at", TS(timezone=True), nullable=True))
        b.add_column(sa.Column("last_success_at", TS(timezone=True), nullable=True))
        b.add_column(sa.Column("last_duration_ms", sa.Integer(), nullable=True))
        b.add_column(sa.Column("last_error", sa.String(length=300), nullable=True))
        b.add_column(sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False))
        b.add_column(sa.Column("run_count", sa.Integer(), server_default="0", nullable=False))
        b.add_column(sa.Column("paused", sa.Boolean(), server_default=sa.false(), nullable=False))
    with op.batch_alter_table("projects") as b:
        b.add_column(sa.Column("automation_paused", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_table(
        "worker_instances",
        sa.Column("holder", sa.String(length=100), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("started_at", TS(timezone=True), nullable=False),
        sa.Column("heartbeat_at", TS(timezone=True), nullable=False),
        sa.Column("stopped_at", TS(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("holder", name=op.f("pk_worker_instances")),
    )


def downgrade() -> None:
    op.drop_table("worker_instances")
    with op.batch_alter_table("projects") as b:
        b.drop_column("automation_paused")
    with op.batch_alter_table("worker_leases") as b:
        for c in (
            "paused",
            "run_count",
            "consecutive_failures",
            "last_error",
            "last_duration_ms",
            "last_success_at",
            "last_started_at",
            "next_run_at",
        ):
            b.drop_column(c)
