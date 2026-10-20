"""Manual days-off credit adjustments for employees.

Revision ID: 20261024_0063_days_off_adj
Revises: 20261023_0062_fish_tanks
Create Date: 2026-10-24

* ``employee_days_off_adjustments`` — dated +/- corrections to an employee's
  days-off balance, entered from the HR Days Off tab.
"""
from alembic import context, op
import sqlalchemy as sa

revision = "20261024_0063_days_off_adj"
down_revision = "20261023_0062_fish_tanks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("employee_days_off_adjustments"):
        op.create_table(
            "employee_days_off_adjustments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
            sa.Column("adjustment_date", sa.Date(), nullable=False),
            sa.Column("days", sa.Numeric(8, 2), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_employee_days_off_adjustments_id", "employee_days_off_adjustments", ["id"])
        op.create_index("ix_employee_days_off_adjustments_employee_id", "employee_days_off_adjustments", ["employee_id"])


def downgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("employee_days_off_adjustments"):
        op.drop_index("ix_employee_days_off_adjustments_employee_id", table_name="employee_days_off_adjustments")
        op.drop_index("ix_employee_days_off_adjustments_id", table_name="employee_days_off_adjustments")
        op.drop_table("employee_days_off_adjustments")
