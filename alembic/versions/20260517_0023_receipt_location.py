"""add location_id to product_receipts and seed default storage locations

Revision ID: 20260517_0023_receipt_location
Revises: 20260514_0022_exp_consumption
Create Date: 2026-05-17

Multi-storage support for the Receive Products flow.

1. Adds nullable `location_id` column to product_receipts so each receipt
   remembers which storage the stock went into.
2. Seeds the three default storage locations (Main Warehouse, Farm Storage,
   Cold Room) if they do not already exist.
3. Backfills existing LocationStock totals from products.stock for the
   Main Warehouse, so reports/inventory stay consistent.

Idempotent — checks before adding columns and inserting seed rows so it
is safe to run on a database that may already have partial state.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260517_0023_receipt_location"
down_revision: Union[str, None] = "20260514_0022_exp_consumption"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return table in insp.get_table_names()


DEFAULT_LOCATIONS = [
    {"name": "Main Warehouse", "code": "MAIN",  "location_type": "warehouse"},
    {"name": "Farm Storage",   "code": "FARM",  "location_type": "warehouse"},
    {"name": "Cold Room",      "code": "COLD",  "location_type": "warehouse"},
]


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Add location_id to product_receipts (nullable FK to stock_locations)
    if _table_exists("product_receipts") and not _has_column("product_receipts", "location_id"):
        op.add_column(
            "product_receipts",
            sa.Column("location_id", sa.Integer(), nullable=True),
        )
        # Foreign key + index (named so downgrade can drop them cleanly)
        op.create_foreign_key(
            "fk_product_receipts_location_id",
            "product_receipts",
            "stock_locations",
            ["location_id"],
            ["id"],
        )
        op.create_index(
            "ix_product_receipts_location_id",
            "product_receipts",
            ["location_id"],
            unique=False,
        )

    # 2. Seed default storage locations (only if missing)
    if _table_exists("stock_locations"):
        existing = bind.execute(
            sa.text("SELECT name FROM stock_locations")
        ).fetchall()
        existing_names = {row[0] for row in existing}

        for loc in DEFAULT_LOCATIONS:
            if loc["name"] not in existing_names:
                bind.execute(
                    sa.text(
                        "INSERT INTO stock_locations (name, code, location_type, is_active) "
                        "VALUES (:name, :code, :type, 1)"
                    ),
                    {
                        "name": loc["name"],
                        "code": loc["code"],
                        "type": loc["location_type"],
                    },
                )

    # 3. Backfill: for every product with stock > 0, ensure a LocationStock
    #    row exists in Main Warehouse with that quantity. Only inserts where
    #    no row currently exists (will NOT overwrite if you've already moved
    #    stock around via transfers).
    if _table_exists("location_stocks") and _table_exists("stock_locations") and _table_exists("products"):
        main_row = bind.execute(
            sa.text("SELECT id FROM stock_locations WHERE name = 'Main Warehouse' LIMIT 1")
        ).fetchone()
        if main_row:
            main_id = main_row[0]
            # Find products whose stock isn't yet represented in any location_stock row.
            unbacked = bind.execute(
                sa.text(
                    "SELECT p.id, p.stock FROM products p "
                    "WHERE p.stock IS NOT NULL AND p.stock > 0 "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM location_stocks ls WHERE ls.product_id = p.id"
                    "  )"
                )
            ).fetchall()
            for product_id, qty in unbacked:
                bind.execute(
                    sa.text(
                        "INSERT INTO location_stocks (location_id, product_id, qty) "
                        "VALUES (:loc, :pid, :qty)"
                    ),
                    {"loc": main_id, "pid": product_id, "qty": qty},
                )


def downgrade() -> None:
    # Drop FK + index + column. We do NOT remove seeded locations or
    # backfilled stock — those are user data once created.
    if _has_column("product_receipts", "location_id"):
        try:
            op.drop_index("ix_product_receipts_location_id", table_name="product_receipts")
        except Exception:
            pass
        try:
            op.drop_constraint(
                "fk_product_receipts_location_id",
                "product_receipts",
                type_="foreignkey",
            )
        except Exception:
            pass
        op.drop_column("product_receipts", "location_id")