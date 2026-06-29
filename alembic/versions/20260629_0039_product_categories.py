"""Add persisted product categories.

Revision ID: 20260629_0039_product_categories
Revises: 20260611_0038_price_precision
Create Date: 2026-06-29
"""
from alembic import op
import sqlalchemy as sa

revision = "20260629_0039_product_categories"
down_revision = "20260611_0038_price_precision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "product_categories" in inspector.get_table_names():
        return

    op.create_table(
        "product_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_product_categories_name"),
    )
    op.create_index("ix_product_categories_id", "product_categories", ["id"], unique=False)
    op.create_index("ix_product_categories_name", "product_categories", ["name"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "product_categories" not in inspector.get_table_names():
        return
    op.drop_index("ix_product_categories_name", table_name="product_categories")
    op.drop_index("ix_product_categories_id", table_name="product_categories")
    op.drop_table("product_categories")
