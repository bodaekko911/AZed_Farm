"""
Carbon Footprint Module — Models
=================================
Tracks CO₂-equivalent emissions across four source categories that map
directly to data already recorded in AZed:

  1. Farm Intake     — transport emissions from farm deliveries
  2. Production      — energy/fuel consumed per batch
  3. Inventory       — waste/spoilage disposal emissions
  4. Expenses        — utility-related emissions (electricity, fuel, etc.)

Each CarbonEmissionFactor row stores a kg CO₂e coefficient for one
activity type, so the whole system is configurable without code changes.
"""

from sqlalchemy import (
    Column, Integer, String, Numeric, DateTime, ForeignKey, Text, Date, Boolean
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class CarbonEmissionFactor(Base):
    """
    Lookup table — one row per emission source type.

    Examples
    --------
    source_type  source_key             label                         factor_kg_co2e_per_unit  unit
    -----------  ---------------------  ----------------------------  -----------------------  ------
    transport    truck_km               Truck transport (per km)      0.21                     km
    transport    van_km                 Van transport (per km)        0.14                     km
    energy       electricity_kwh        Grid electricity (per kWh)    0.45                     kWh
    energy       diesel_liter           Diesel fuel (per litre)       2.68                     litre
    energy       lpg_liter              LPG (per litre)               1.51                     litre
    waste        organic_waste_kg       Organic waste to landfill     0.45                     kg
    waste        spoilage_kg            Crop spoilage                 0.38                     kg
    production   processing_kwh         Processing energy (per kWh)   0.45                     kWh
    """
    __tablename__ = "carbon_emission_factors"

    id                   = Column(Integer, primary_key=True, index=True)
    source_type          = Column(String(30),  nullable=False)          # transport | energy | waste | production
    source_key           = Column(String(60),  nullable=False, unique=True)
    label                = Column(String(150), nullable=False)
    factor_kg_co2e_per_unit = Column(Numeric(10, 4), nullable=False)   # kg CO₂e per 1 unit
    unit                 = Column(String(30),  nullable=False)          # km | kWh | litre | kg
    description          = Column(Text, nullable=True)
    is_active            = Column(Boolean, default=True)
    # ── Methodology provenance (GHG Protocol alignment) ──
    scope                = Column(Integer, nullable=True)               # 1 = direct, 2 = purchased energy, 3 = value chain
    methodology_source   = Column(String(200), nullable=True)           # e.g. "DEFRA GHG Conversion Factors 2024"
    source_year          = Column(Integer, nullable=True)               # publication year of the factor
    region               = Column(String(80), nullable=True)            # e.g. "Egypt", "Global default"
    created_at           = Column(DateTime(timezone=True), server_default=func.now())

    logs = relationship("CarbonLog", back_populates="factor")


class CarbonLog(Base):
    """
    One emission event — tied to a real transaction in AZed.

    ref_type / ref_id link back to the source record:
      ref_type="farm_delivery"   ref_id=<FarmDelivery.id>
      ref_type="production_batch" ref_id=<ProductionBatch.id>
      ref_type="expense"         ref_id=<Expense.id>
      ref_type="spoilage"        ref_id=<Spoilage.id>
      ref_type="manual"          ref_id=NULL  (manually entered)
    """
    __tablename__ = "carbon_logs"

    id           = Column(Integer, primary_key=True, index=True)
    factor_id    = Column(Integer, ForeignKey("carbon_emission_factors.id"), nullable=False)
    farm_id      = Column(Integer, ForeignKey("farms.id"),  nullable=True)   # optional farm context
    user_id      = Column(Integer, ForeignKey("users.id"),  nullable=True)
    log_date     = Column(Date, nullable=False)
    quantity     = Column(Numeric(14, 3), nullable=False)                    # amount of the unit consumed
    kg_co2e      = Column(Numeric(14, 4), nullable=False)                    # quantity × factor (computed on save)
    ref_type     = Column(String(40), nullable=True)                         # source table
    ref_id       = Column(Integer, nullable=True)                            # FK to source row
    notes        = Column(Text, nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    factor = relationship("CarbonEmissionFactor", back_populates="logs")
    farm   = relationship("Farm")
    user   = relationship("User")


class CarbonTarget(Base):
    """
    Optional period targets so users can track progress against a goal.
    E.g. "Reduce to 5 000 kg CO₂e for Q3 2026".
    """
    __tablename__ = "carbon_targets"

    id           = Column(Integer, primary_key=True, index=True)
    label        = Column(String(150), nullable=False)
    period_start = Column(Date, nullable=False)
    period_end   = Column(Date, nullable=False)
    target_kg_co2e = Column(Numeric(14, 2), nullable=False)
    notes        = Column(Text, nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())