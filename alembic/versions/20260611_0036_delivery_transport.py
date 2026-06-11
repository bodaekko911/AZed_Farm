"""add_delivery_transport_columns

Revision ID: 20260611_0036_dlv_transport
Revises: 20260609_0035_intake_type
Create Date: 2026-06-11

Adds transport provenance to farm deliveries so the carbon module can
auto-log Scope 1 transport emissions per delivery:
  - distance_km   one-way distance farm → site
  - vehicle_type  'van' | 'truck' (selects the per-km emission factor)
"""

from alembic import op
import sqlalchemy as sa

revision = "20260611_0036_dlv_transport"
down_revision = "20260609_0035_intake_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("farm_deliveries")}
    # Idempotent: the startup guard (ensure_delivery_transport_columns) may
    # have added these already on installs where this migration lagged.
    if "distance_km" not in existing:
        op.add_column("farm_deliveries", sa.Column("distance_km", sa.Numeric(8, 1), nullable=True))
    if "vehicle_type" not in existing:
        op.add_column("farm_deliveries", sa.Column("vehicle_type", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("farm_deliveries", "vehicle_type")
    op.drop_column("farm_deliveries", "distance_km")