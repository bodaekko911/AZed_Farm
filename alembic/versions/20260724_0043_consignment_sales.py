"""Consignment sale records — sold items captured with each consignment payment.

Revision ID: 20260724_0043_consignment_sales
Revises: 20260703_0042_salary_days_basis
Create Date: 2026-07-24

Adds two tables so a consignment-client payment can record exactly which items
the client reported sold, for which month, with the payment amount reconciled
against the sum of the line items:

  • consignment_sales       — payment header (client, month, amount, journal)
  • consignment_sale_items  — line items (product, qty, unit_price, total)

This is a bookkeeping record only; it does not modify consignment quantities or
stock (that stays with the separate Settle flow). Added defensively (skipped if
present) so it coexists with the runtime schema guard in ``app/app_factory.py``.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260724_0043_consignment_sales"
down_revision = "20260703_0042_salary_days_basis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "consignment_sales" not in tables:
        op.create_table(
            "consignment_sales",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("client_id", sa.Integer(), sa.ForeignKey("b2b_clients.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("journal_id", sa.Integer(), sa.ForeignKey("journals.id"), nullable=True),
            sa.Column("month_label", sa.String(100), nullable=True),
            sa.Column("amount", sa.Numeric(14, 2), server_default="0"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_consignment_sales_id", "consignment_sales", ["id"])
        op.create_index("ix_consignment_sales_client_id", "consignment_sales", ["client_id"])

    if "consignment_sale_items" not in tables:
        op.create_table(
            "consignment_sale_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("sale_id", sa.Integer(), sa.ForeignKey("consignment_sales.id"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("qty", sa.Numeric(12, 3), nullable=False),
            sa.Column("unit_price", sa.Numeric(14, 2), nullable=False),
            sa.Column("total", sa.Numeric(14, 2), nullable=False),
        )
        op.create_index("ix_consignment_sale_items_id", "consignment_sale_items", ["id"])
        op.create_index("ix_consignment_sale_items_sale_id", "consignment_sale_items", ["sale_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "consignment_sale_items" in tables:
        op.drop_table("consignment_sale_items")
    if "consignment_sales" in tables:
        op.drop_table("consignment_sales")
