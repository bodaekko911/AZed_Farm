"""add_carbon_footprint_module

Revision ID: 20260510_0018_carbon_footprint
Revises: eadb1eb64495
Create Date: 2026-05-10

Creates three tables:
  - carbon_emission_factors   (coefficient lookup)
  - carbon_logs               (individual emission events)
  - carbon_targets            (period reduction goals)

Also seeds default emission factors for transport, energy, waste,
and production so the module is usable immediately after migration.
"""

from alembic import op
import sqlalchemy as sa
from datetime import datetime

revision = "20260510_0018_carbon_footprint"
down_revision = "eadb1eb64495"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── carbon_emission_factors ───────────────────────────────────────
    op.create_table(
        "carbon_emission_factors",
        sa.Column("id",                      sa.Integer(),      primary_key=True, autoincrement=True),
        sa.Column("source_type",             sa.String(30),     nullable=False),
        sa.Column("source_key",              sa.String(60),     nullable=False, unique=True),
        sa.Column("label",                   sa.String(150),    nullable=False),
        sa.Column("factor_kg_co2e_per_unit", sa.Numeric(10, 4), nullable=False),
        sa.Column("unit",                    sa.String(30),     nullable=False),
        sa.Column("description",             sa.Text(),         nullable=True),
        sa.Column("is_active",               sa.Boolean(),      nullable=False, server_default="1"),
        sa.Column("created_at",              sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── carbon_logs ───────────────────────────────────────────────────
    op.create_table(
        "carbon_logs",
        sa.Column("id",          sa.Integer(),      primary_key=True, autoincrement=True),
        sa.Column("factor_id",   sa.Integer(),      sa.ForeignKey("carbon_emission_factors.id"), nullable=False),
        sa.Column("farm_id",     sa.Integer(),      sa.ForeignKey("farms.id"),  nullable=True),
        sa.Column("user_id",     sa.Integer(),      sa.ForeignKey("users.id"),  nullable=True),
        sa.Column("log_date",    sa.Date(),         nullable=False),
        sa.Column("quantity",    sa.Numeric(14, 3), nullable=False),
        sa.Column("kg_co2e",     sa.Numeric(14, 4), nullable=False),
        sa.Column("ref_type",    sa.String(40),     nullable=True),
        sa.Column("ref_id",      sa.Integer(),      nullable=True),
        sa.Column("notes",       sa.Text(),         nullable=True),
        sa.Column("created_at",  sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_carbon_logs_log_date",  "carbon_logs", ["log_date"])
    op.create_index("ix_carbon_logs_ref",       "carbon_logs", ["ref_type", "ref_id"])
    op.create_index("ix_carbon_logs_farm_id",   "carbon_logs", ["farm_id"])

    # ── carbon_targets ────────────────────────────────────────────────
    op.create_table(
        "carbon_targets",
        sa.Column("id",              sa.Integer(),      primary_key=True, autoincrement=True),
        sa.Column("label",           sa.String(150),    nullable=False),
        sa.Column("period_start",    sa.Date(),         nullable=False),
        sa.Column("period_end",      sa.Date(),         nullable=False),
        sa.Column("target_kg_co2e",  sa.Numeric(14, 2), nullable=False),
        sa.Column("notes",           sa.Text(),         nullable=True),
        sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── Seed default emission factors ─────────────────────────────────
    factors_table = sa.table(
        "carbon_emission_factors",
        sa.column("source_type", sa.String),
        sa.column("source_key",  sa.String),
        sa.column("label",       sa.String),
        sa.column("factor_kg_co2e_per_unit", sa.Numeric),
        sa.column("unit",        sa.String),
        sa.column("description", sa.String),
    )
    op.bulk_insert(factors_table, [
        # Transport
        {
            "source_type": "transport", "source_key": "truck_km",
            "label": "Truck transport",
            "factor_kg_co2e_per_unit": 0.2100,
            "unit": "km",
            "description": "Per km driven by a heavy goods truck. IPCC Tier 1.",
        },
        {
            "source_type": "transport", "source_key": "van_km",
            "label": "Van / pickup transport",
            "factor_kg_co2e_per_unit": 0.1400,
            "unit": "km",
            "description": "Per km driven by a light van or pickup.",
        },
        {
            "source_type": "transport", "source_key": "refrigerated_truck_km",
            "label": "Refrigerated truck transport",
            "factor_kg_co2e_per_unit": 0.2900,
            "unit": "km",
            "description": "Per km driven by a refrigerated (reefer) truck.",
        },
        # Energy
        {
            "source_type": "energy", "source_key": "electricity_kwh",
            "label": "Grid electricity",
            "factor_kg_co2e_per_unit": 0.4500,
            "unit": "kWh",
            "description": "Egyptian national grid average (IEA 2024 estimate).",
        },
        {
            "source_type": "energy", "source_key": "diesel_liter",
            "label": "Diesel fuel",
            "factor_kg_co2e_per_unit": 2.6800,
            "unit": "litre",
            "description": "Combustion of diesel in generators or farm machinery.",
        },
        {
            "source_type": "energy", "source_key": "lpg_liter",
            "label": "LPG (cooking / heating)",
            "factor_kg_co2e_per_unit": 1.5100,
            "unit": "litre",
            "description": "Liquefied petroleum gas combustion.",
        },
        {
            "source_type": "energy", "source_key": "natural_gas_m3",
            "label": "Natural gas",
            "factor_kg_co2e_per_unit": 2.0400,
            "unit": "m³",
            "description": "Natural gas combustion per cubic metre.",
        },
        # Waste & Spoilage
        {
            "source_type": "waste", "source_key": "organic_waste_kg",
            "label": "Organic waste (landfill)",
            "factor_kg_co2e_per_unit": 0.4500,
            "unit": "kg",
            "description": "Methane from organic waste decomposing in landfill.",
        },
        {
            "source_type": "waste", "source_key": "spoilage_kg",
            "label": "Crop / produce spoilage",
            "factor_kg_co2e_per_unit": 0.3800,
            "unit": "kg",
            "description": "Embodied emissions in spoiled or wasted produce.",
        },
        {
            "source_type": "waste", "source_key": "wastewater_m3",
            "label": "Wastewater discharge",
            "factor_kg_co2e_per_unit": 0.7080,
            "unit": "m³",
            "description": "Treatment emissions per cubic metre of wastewater.",
        },
        # Production / Processing
        {
            "source_type": "production", "source_key": "cold_storage_kwh",
            "label": "Cold storage electricity",
            "factor_kg_co2e_per_unit": 0.4500,
            "unit": "kWh",
            "description": "Electricity for refrigerated storage, same grid factor.",
        },
        {
            "source_type": "production", "source_key": "irrigation_kwh",
            "label": "Irrigation pump electricity",
            "factor_kg_co2e_per_unit": 0.4500,
            "unit": "kWh",
            "description": "Electric pump energy used for field irrigation.",
        },
        {
            "source_type": "production", "source_key": "fertiliser_kg",
            "label": "Synthetic fertiliser (N)",
            "factor_kg_co2e_per_unit": 6.7000,
            "unit": "kg",
            "description": "N₂O emissions from synthetic nitrogen fertiliser application.",
        },
    ])


def downgrade() -> None:
    op.drop_table("carbon_targets")
    op.drop_index("ix_carbon_logs_farm_id",  table_name="carbon_logs")
    op.drop_index("ix_carbon_logs_ref",      table_name="carbon_logs")
    op.drop_index("ix_carbon_logs_log_date", table_name="carbon_logs")
    op.drop_table("carbon_logs")
    op.drop_table("carbon_emission_factors")