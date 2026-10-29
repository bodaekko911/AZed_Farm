"""Paid leave days on each payroll run.

Revision ID: 20261029_0066_paid_leave
Revises: 20261028_0065_sales_levels
Create Date: 2026-10-29

* ``payroll.paid_leave_days`` — days off in the month paid from the leave balance. They are already part of
  ``base_salary``; storing them lets the payroll list and payslip show the leave pay as its own line.
"""
from alembic import context, op
import sqlalchemy as sa

revision = "20261029_0066_paid_leave"
down_revision = "20261028_0065_sales_levels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("payroll"):
        return
    if "paid_leave_days" not in {c["name"] for c in inspector.get_columns("payroll")}:
        op.add_column("payroll", sa.Column("paid_leave_days", sa.Numeric(8, 2), nullable=False, server_default="0"))


def downgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("payroll"):
        return
    if "paid_leave_days" in {c["name"] for c in inspector.get_columns("payroll")}:
        op.drop_column("payroll", "paid_leave_days")
