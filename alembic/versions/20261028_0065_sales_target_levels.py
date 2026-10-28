"""Floor and stretch for the monthly sales target.

Revision ID: 20261028_0065_sales_levels
Revises: 20261028_0064_monthly_plans
Create Date: 2026-10-28

* ``monthly_plans.sales_floor`` / ``sales_stretch`` — the lower and upper levels around the month's sales target
  (filled in as 85% and 120% of it on the Sales targets page). Existing targets get those defaults.
"""
from alembic import context, op
import sqlalchemy as sa

revision = "20261028_0065_sales_levels"
down_revision = "20261028_0064_monthly_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("monthly_plans"):
        return
    columns = {c["name"] for c in inspector.get_columns("monthly_plans")}
    if "sales_floor" not in columns:
        op.add_column("monthly_plans", sa.Column("sales_floor", sa.Numeric(14, 2), nullable=True))
    if "sales_stretch" not in columns:
        op.add_column("monthly_plans", sa.Column("sales_stretch", sa.Numeric(14, 2), nullable=True))
    op.execute("UPDATE monthly_plans SET sales_floor = ROUND(sales_target * 0.85, 2) "
               "WHERE sales_target IS NOT NULL AND sales_floor IS NULL")
    op.execute("UPDATE monthly_plans SET sales_stretch = ROUND(sales_target * 1.20, 2) "
               "WHERE sales_target IS NOT NULL AND sales_stretch IS NULL")


def downgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("monthly_plans"):
        return
    columns = {c["name"] for c in inspector.get_columns("monthly_plans")}
    if "sales_stretch" in columns:
        op.drop_column("monthly_plans", "sales_stretch")
    if "sales_floor" in columns:
        op.drop_column("monthly_plans", "sales_floor")
