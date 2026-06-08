"""add paid_amount and days_off_credited to payroll

Supports partial salary payment (pay a specific cash amount) and converting the
unpaid remainder into paid days off credited to the employee's leave balance.

Revision ID: 20260602_0033_partial_pay
Revises: 20260527_0032_drying_multistage
Create Date: 2026-06-02
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260602_0033_partial_pay"
down_revision: Union[str, None] = "20260527_0032_drying_multistage"
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

    if not _has_column("payroll", "paid_amount"):
        op.add_column(
            "payroll",
            sa.Column("paid_amount", sa.Numeric(12, 2), nullable=True),
        )

    if not _has_column("payroll", "days_off_credited"):
        op.add_column(
            "payroll",
            sa.Column(
                "days_off_credited",
                sa.Numeric(8, 2),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    if context.is_offline_mode():
        return

    if _has_column("payroll", "days_off_credited"):
        op.drop_column("payroll", "days_off_credited")
    if _has_column("payroll", "paid_amount"):
        op.drop_column("payroll", "paid_amount")