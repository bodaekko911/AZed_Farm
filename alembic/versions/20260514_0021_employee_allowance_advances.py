"""add employee_allowance_advances table

Revision ID: 20260514_0021_emp_advances
Revises: 20260514_0020_emp_allowances
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260514_0021_emp_advances"
down_revision: Union[str, None] = "20260514_0020_emp_allowances"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return table in insp.get_table_names()


def upgrade() -> None:
    if context.is_offline_mode():
        return

    if not _has_table("employee_allowance_advances"):
        op.create_table(
            "employee_allowance_advances",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column(
                "employee_id",
                sa.Integer(),
                sa.ForeignKey("employees.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("advance_date", sa.Date(), nullable=False),
            sa.Column("amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column(
                "status",
                sa.String(20),
                nullable=False,
                server_default="open",
            ),
            sa.Column(
                "payroll_id",
                sa.Integer(),
                sa.ForeignKey("payroll.id"),
                nullable=True,
                index=True,
            ),
            sa.Column(
                "created_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    if context.is_offline_mode():
        return

    if _has_table("employee_allowance_advances"):
        op.drop_table("employee_allowance_advances")