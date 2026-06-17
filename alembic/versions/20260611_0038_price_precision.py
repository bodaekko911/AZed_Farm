"""Widen per-unit price/cost columns to Numeric(12,3).

Allows 3-decimal unit prices (e.g. 0.065 EGP per gram). Only per-UNIT columns
are widened; line totals, subtotals, and cash amounts stay at 2 decimals since
they represent money actually paid.

Columns widened:
  products.price, products.cost
  invoice_items.unit_price
  refund_items.unit_price
  receipts.unit_cost
  supplier_order_items.unit_cost   (table name resolved at runtime)

Revision ID: 20260611_0038_price_precision
Revises: 20260611_0037_unit_weight
Create Date: 2026-06-13
"""
from alembic import op
import sqlalchemy as sa

revision = "20260611_0038_price_precision"
down_revision = "20260611_0037_unit_weight"
branch_labels = None
depends_on = None

# (table, column) pairs to widen. Tables that don't exist or columns already at
# (12,3) are skipped, so this is safe to run against a partially-migrated DB.
TARGETS = [
    ("products", "price"),
    ("products", "cost"),
    ("invoice_items", "unit_price"),
    ("retail_refund_items", "unit_price"),
    ("product_receipts", "unit_cost"),
    ("purchase_items", "unit_cost"),
]


def _alter(table, column, precision, scale):
    op.alter_column(
        table, column,
        type_=sa.Numeric(precision, scale),
        existing_nullable=True,
    )


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_tables = set(insp.get_table_names())
    for table, column in TARGETS:
        if table not in existing_tables:
            print(f"20260611_0038: table {table} missing — skipping {column}")
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if column not in cols:
            print(f"20260611_0038: column {table}.{column} missing — skipping")
            continue
        _alter(table, column, 12, 3)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_tables = set(insp.get_table_names())
    for table, column in TARGETS:
        if table in existing_tables:
            _alter(table, column, 12, 2)