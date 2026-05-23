"""add customer discount percentage

Revision ID: 20260413_0006
Revises: 20260413_0005
Create Date: 2026-04-13 20:40:00

Idempotent: on a fresh-from-zero database, `customers.discount_pct` is
already created by `0001_initial_schema`. On older databases that pre-date
that schema consolidation, this migration is what adds the column. The
existence check below makes the migration safe to apply in either case.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260413_0006"
down_revision = "20260413_0005"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(
        column["name"] == column_name
        for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    if not _column_exists("customers", "discount_pct"):
        op.add_column(
            "customers",
            sa.Column("discount_pct", sa.Numeric(precision=6, scale=2), nullable=True, server_default="0"),
        )
    op.execute("UPDATE customers SET discount_pct = 0 WHERE discount_pct IS NULL")
    op.alter_column("customers", "discount_pct", server_default=None)


def downgrade() -> None:
    # Defensive: only drop if the column exists. This avoids breaking
    # downgrade on databases where 0001_initial_schema (not this migration)
    # created the column.
    if _column_exists("customers", "discount_pct"):
        op.drop_column("customers", "discount_pct")