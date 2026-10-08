"""Unit cost saved on every sale and refund line.

Revision ID: 20261008_0048_sale_line_unit_cost
Revises: 20260831_0047_consignment_stock_count
Create Date: 2026-10-08

Margins were worked out with each product's cost as it is today, so a past
month's profit moved every time a cost was updated (and an old wrong cost
looked like an old loss). Each sale and refund line now keeps the product's
cost at the moment it was recorded:

  * invoice_items, b2b_invoice_items, consignment_sale_items  — at sale
  * retail_refund_items  — the cost of the sale being refunded
  * b2b_refund_items     — the product cost at refund time

Nullable: lines recorded before this, or sold while the product had no cost,
keep NULL and reports fall back to today's cost for them. Added defensively
(skipped if present) so it coexists with the runtime schema guard in
``app/app_factory.py``.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261008_0048_sale_line_unit_cost"
down_revision = "20260831_0047_consignment_stock_count"
branch_labels = None
depends_on = None

TABLES = (
    "invoice_items",
    "b2b_invoice_items",
    "consignment_sale_items",
    "retail_refund_items",
    "b2b_refund_items",
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    for table in TABLES:
        if table not in existing_tables:
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        if "unit_cost" not in columns:
            op.add_column(table, sa.Column("unit_cost", sa.Numeric(12, 3), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    for table in TABLES:
        if table in existing_tables and "unit_cost" in {c["name"] for c in inspector.get_columns(table)}:
            op.drop_column(table, "unit_cost")
