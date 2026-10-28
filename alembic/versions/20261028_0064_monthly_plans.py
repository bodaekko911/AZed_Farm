"""Monthly budget and sales target.

Revision ID: 20261028_0064_monthly_plans
Revises: 20261024_0063_days_off_adj
Create Date: 2026-10-28

* ``monthly_plans`` — one row per calendar month: the spending budget and the
  sales target set on the Budget & targets page.
"""
from alembic import context, op
import sqlalchemy as sa

revision = "20261028_0064_monthly_plans"
down_revision = "20261024_0063_days_off_adj"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("monthly_plans"):
        op.create_table(
            "monthly_plans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("month", sa.Date(), nullable=False),
            sa.Column("budget", sa.Numeric(14, 2), nullable=True),
            sa.Column("sales_target", sa.Numeric(14, 2), nullable=True),
            sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_monthly_plans_id", "monthly_plans", ["id"])
        op.create_index("ix_monthly_plans_month", "monthly_plans", ["month"], unique=True)


def downgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("monthly_plans"):
        op.drop_index("ix_monthly_plans_month", table_name="monthly_plans")
        op.drop_index("ix_monthly_plans_id", table_name="monthly_plans")
        op.drop_table("monthly_plans")
