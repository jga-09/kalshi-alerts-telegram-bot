"""strategies: per-strategy signals/orders/paper trades, resting orders, user strategy config

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-12 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("signals") as batch:
        batch.add_column(sa.Column("strategy", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("entry_style", sa.String(length=8), server_default="take", nullable=False))
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("strategy", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("entry_style", sa.String(length=8), server_default="take", nullable=False))
        batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("paper_trades") as batch:
        batch.add_column(sa.Column("strategy", sa.String(length=16), nullable=True))
    with op.batch_alter_table("risk_settings") as batch:
        batch.add_column(sa.Column("strategy_config", JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("risk_settings") as batch:
        batch.drop_column("strategy_config")
    with op.batch_alter_table("paper_trades") as batch:
        batch.drop_column("strategy")
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("expires_at")
        batch.drop_column("entry_style")
        batch.drop_column("strategy")
    with op.batch_alter_table("signals") as batch:
        batch.drop_column("entry_style")
        batch.drop_column("strategy")
