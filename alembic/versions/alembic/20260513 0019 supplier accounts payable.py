"""supplier accounts payable

Adds:
- product_receipts.supplier_id  (nullable FK → suppliers.id)
- product_receipts.amount_paid  (numeric, default 0)
- supplier_payments             (new table for payments against supplier balance)

Revision ID: 20260513_0019
Revises: a5ad9f786549
Create Date: 2026-05-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260513_0019"
down_revision: Union[str, None] = "a5ad9f786549"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = [c["name"] for c in insp.get_columns(table)]
    return column in cols


def _has_table(table: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return table in insp.get_table_names()


def upgrade() -> None:
    # 1. product_receipts.supplier_id
    if _has_table("product_receipts") and not _has_column("product_receipts", "supplier_id"):
        op.add_column(
            "product_receipts",
            sa.Column("supplier_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            "fk_product_receipts_supplier_id",
            "product_receipts",
            "suppliers",
            ["supplier_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            "ix_product_receipts_supplier_id",
            "product_receipts",
            ["supplier_id"],
        )

    # 2. product_receipts.amount_paid
    if _has_table("product_receipts") and not _has_column("product_receipts", "amount_paid"):
        op.add_column(
            "product_receipts",
            sa.Column(
                "amount_paid",
                sa.Numeric(12, 2),
                nullable=False,
                server_default="0",
            ),
        )

    # 3. supplier_payments
    if not _has_table("supplier_payments"):
        op.create_table(
            "supplier_payments",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column("ref_number", sa.String(30), nullable=False, unique=True, index=True),
            sa.Column(
                "supplier_id",
                sa.Integer(),
                sa.ForeignKey("suppliers.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=True,
            ),
            sa.Column("payment_date", sa.Date(), nullable=False),
            sa.Column("amount", sa.Numeric(14, 2), nullable=False),
            sa.Column(
                "payment_method",
                sa.String(20),
                nullable=False,
                server_default="cash",
            ),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "journal_id",
                sa.Integer(),
                sa.ForeignKey("journals.id"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    if _has_table("supplier_payments"):
        op.drop_table("supplier_payments")

    if _has_table("product_receipts"):
        if _has_column("product_receipts", "amount_paid"):
            op.drop_column("product_receipts", "amount_paid")
        if _has_column("product_receipts", "supplier_id"):
            try:
                op.drop_index("ix_product_receipts_supplier_id", table_name="product_receipts")
            except Exception:
                pass
            try:
                op.drop_constraint("fk_product_receipts_supplier_id", "product_receipts", type_="foreignkey")
            except Exception:
                pass
            op.drop_column("product_receipts", "supplier_id")