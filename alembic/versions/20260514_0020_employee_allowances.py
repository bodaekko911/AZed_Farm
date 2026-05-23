"""add food and transportation allowance to employees

Revision ID: 20260514_0020_emp_allowances
Revises: 20260514_0019_emp_vacation
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260514_0020_emp_allowances"
down_revision: Union[str, None] = "20260514_0019_emp_vacation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if context.is_offline_mode():
        return

    if not _has_column("employees", "food_allowance"):
        op.add_column(
            "employees",
            sa.Column(
                "food_allowance",
                sa.Numeric(12, 2),
                nullable=False,
                server_default="0",
            ),
        )
    if not _has_column("employees", "transportation_allowance"):
        op.add_column(
            "employees",
            sa.Column(
                "transportation_allowance",
                sa.Numeric(12, 2),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    if context.is_offline_mode():
        return

    if _has_column("employees", "transportation_allowance"):
        op.drop_column("employees", "transportation_allowance")
    if _has_column("employees", "food_allowance"):
        op.drop_column("employees", "food_allowance")