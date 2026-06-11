"""Add products.unit_weight_kg — average weight of one piece in kg.

Lets piece/bunch/box products be counted in the carbon module's mass-based
metrics (emission intensity per kg of farm produce, spoilage waste logging).

Revision ID: 20260611_0037_unit_weight
Revises: 20260611_0036_dlv_transport
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa

revision = "20260611_0037_unit_weight"
down_revision = "20260611_0036_dlv_transport"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: the self-healing startup guard
    # (app_factory.ensure_delivery_transport_columns) may already have added
    # this column when a previous `alembic upgrade head` failed at deploy.
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("products")}
    if "unit_weight_kg" in cols:
        print("20260611_0037_unit_weight: column already exists — skipping")
        return
    op.add_column("products", sa.Column("unit_weight_kg", sa.Numeric(8, 3), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "unit_weight_kg")