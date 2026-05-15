"""add consumption tracking to expense categories and expenses

Revision ID: 20260514_0019_expense_consumption
Revises: 20260513_0019
Create Date: 2026-05-14

Adds the consumption / unit / carbon-factor columns introduced for the
utilities tracking flow and the auto-carbon-log feature. Skips columns
that already exist (idempotent), so it is safe to run on a database that
may have been partially upgraded by a previously broken migration file.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260514_0019_expense_consumption"
down_revision: Union[str, None] = "20260513_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # expense_categories.unit_price
    if not _has_column(inspector, "expense_categories", "unit_price"):
        op.add_column(
            "expense_categories",
            sa.Column("unit_price", sa.Numeric(12, 4), nullable=True),
        )

    # expense_categories.unit_name
    if not _has_column(inspector, "expense_categories", "unit_name"):
        op.add_column(
            "expense_categories",
            sa.Column("unit_name", sa.String(20), nullable=True),
        )

    # expense_categories.carbon_factor_key
    if not _has_column(inspector, "expense_categories", "carbon_factor_key"):
        op.add_column(
            "expense_categories",
            sa.Column("carbon_factor_key", sa.String(60), nullable=True),
        )

    # expenses.consumption
    if not _has_column(inspector, "expenses", "consumption"):
        op.add_column(
            "expenses",
            sa.Column("consumption", sa.Numeric(14, 4), nullable=True),
        )

    # expenses.unit_price_used
    if not _has_column(inspector, "expenses", "unit_price_used"):
        op.add_column(
            "expenses",
            sa.Column("unit_price_used", sa.Numeric(12, 4), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_column(inspector, "expenses", "unit_price_used"):
        op.drop_column("expenses", "unit_price_used")
    if _has_column(inspector, "expenses", "consumption"):
        op.drop_column("expenses", "consumption")
    if _has_column(inspector, "expense_categories", "carbon_factor_key"):
        op.drop_column("expense_categories", "carbon_factor_key")
    if _has_column(inspector, "expense_categories", "unit_name"):
        op.drop_column("expense_categories", "unit_name")
    if _has_column(inspector, "expense_categories", "unit_price"):
        op.drop_column("expense_categories", "unit_price")