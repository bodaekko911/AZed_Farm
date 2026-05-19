"""Add is_animal_expense flag to expenses

Revision ID: 20260518_0028_is_animal_exp
Revises: 20260518_0027_emp_animal
Create Date: 2026-05-18

Adds expenses.is_animal_expense (boolean, default False). Set to True when
the user picks the "Animals" option in the Farm dropdown on the Expenses
page. These expenses are rolled up in the combined Animal Cost Analysis.

Idempotent: safe to re-run.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260518_0028_is_animal_exp"
down_revision: Union[str, None] = "20260518_0027_emp_animal"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _has_column("expenses", "is_animal_expense"):
        op.add_column(
            "expenses",
            sa.Column(
                "is_animal_expense",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    if _has_column("expenses", "is_animal_expense"):
        op.drop_column("expenses", "is_animal_expense")