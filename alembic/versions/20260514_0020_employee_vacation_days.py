"""add vacation_days_per_month to employees

Revision ID: 20260514_0019_emp_vacation
Revises: 20260513_0019_supplier_ap
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260514_0019_emp_vacation"
down_revision: Union[str, None] = "20260513_0019_supplier_ap"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _has_column("employees", "vacation_days_per_month"):
        op.add_column(
            "employees",
            sa.Column(
                "vacation_days_per_month",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    if _has_column("employees", "vacation_days_per_month"):
        op.drop_column("employees", "vacation_days_per_month")