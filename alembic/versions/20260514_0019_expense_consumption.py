"""add consumption tracking to expense categories and expenses

Revision ID: 20260514_0022_expense_consumption
Revises: 20260514_0021_emp_allowance_advances
Create Date: 2026-05-14

Adds the consumption / unit / carbon-factor columns introduced for the
utilities tracking flow and the auto-carbon-log feature. Idempotent --
checks each column before adding so it is safe on a database that may
have been partially upgraded.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260514_0022_expense_consumption"
down_revision: Union[str, None] = "20260514_0021_emp_allowance_advances"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _has_column("expense_categories", "unit_price"):
        op.add_column(
            "expense_categories",
            sa.Column("unit_price", sa.Numeric(12, 4), nullable=True),
        )
    if not _has_column("expense_categories", "unit_name"):
        op.add_column(
            "expense_categories",
            sa.Column("unit_name", sa.String(20), nullable=True),
        )
    if not _has_column("expense_categories", "carbon_factor_key"):
        op.add_column(
            "expense_categories",
            sa.Column("carbon_factor_key", sa.String(60), nullable=True),
        )
    if not _has_column("expenses", "consumption"):
        op.add_column(
            "expenses",
            sa.Column("consumption", sa.Numeric(14, 4), nullable=True),
        )
    if not _has_column("expenses", "unit_price_used"):
        op.add_column(
            "expenses",
            sa.Column("unit_price_used", sa.Numeric(12, 4), nullable=True),
        )


def downgrade() -> None:
    if _has_column("expenses", "unit_price_used"):
        op.drop_column("expenses", "unit_price_used")
    if _has_column("expenses", "consumption"):
        op.drop_column("expenses", "consumption")
    if _has_column("expense_categories", "carbon_factor_key"):
        op.drop_column("expense_categories", "carbon_factor_key")
    if _has_column("expense_categories", "unit_name"):
        op.drop_column("expense_categories", "unit_name")
    if _has_column("expense_categories", "unit_price"):
        op.drop_column("expense_categories", "unit_price")