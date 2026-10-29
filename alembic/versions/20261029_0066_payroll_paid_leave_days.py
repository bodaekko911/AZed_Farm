"""Paid leave days on each payroll run, and working days per month for the 'work_days' salary deal.

Revision ID: 20261029_0066_paid_leave
Revises: 20261028_0065_sales_levels
Create Date: 2026-10-29

* ``payroll.paid_leave_days`` — days paid from the days-off credit this month. They are already part of
  ``base_salary``; storing them lets the payroll list and payslip show the leave pay as its own line, and is what
  'work_days' employees spend from their credit.
* ``employees.work_days_per_month`` — the working days the salary pays on a 'work_days' deal (e.g. 23 on / 7 off).
"""
from alembic import context, op
import sqlalchemy as sa

revision = "20261029_0066_paid_leave"
down_revision = "20261028_0065_sales_levels"
branch_labels = None
depends_on = None

COLUMNS = [
    ("payroll", "paid_leave_days", sa.Numeric(8, 2)),
    ("employees", "work_days_per_month", sa.Integer()),
]


def upgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    for table, column, type_ in COLUMNS:
        if inspector.has_table(table) and column not in {c["name"] for c in inspector.get_columns(table)}:
            op.add_column(table, sa.Column(column, type_, nullable=False, server_default="0"))


def downgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    for table, column, _type in COLUMNS:
        if inspector.has_table(table) and column in {c["name"] for c in inspector.get_columns(table)}:
            op.drop_column(table, column)
