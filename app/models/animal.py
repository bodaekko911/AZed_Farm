"""Animal management — groups of animals and their feeding logs.

Stage 1 of the Animal Management module. Defines:
  • AnimalGroup  — a herd / flock / pen of animals on a farm
  • FeedingLog   — a record of feed consumed by a group

Future stages will add: production logs (milk/eggs/weight), health logs,
mortality tracking, FCR metrics, and dashboard integration. Those features
will reuse these tables; no breaking changes are anticipated.
"""
from sqlalchemy import Column, Integer, String, Numeric, DateTime, Date, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class AnimalGroup(Base):
    """A managed group of animals (e.g. 'Dairy Cows', 'Layer Chickens').

    Headcount can be edited directly; future stages may add structured
    intake/sale events that drive headcount automatically.
    """
    __tablename__ = "animal_groups"

    id           = Column(Integer, primary_key=True, index=True)
    name         = Column(String(150), nullable=False, index=True)
    animal_type  = Column(String(30), nullable=False, default="other")  # cattle|poultry|sheep|goats|other
    headcount    = Column(Integer, nullable=False, default=0)
    farm_id      = Column(Integer, ForeignKey("farms.id"), nullable=True, index=True)
    status       = Column(String(20), nullable=False, default="active")  # active|sold|deceased|archived
    notes        = Column(Text, nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())
    archived_at  = Column(DateTime(timezone=True), nullable=True)

    farm     = relationship("Farm")
    feedings = relationship("FeedingLog", back_populates="group", cascade="all, delete-orphan")


class FeedingLog(Base):
    """A single feeding event: a group consumed `qty` of `product` from
    a specific storage `location` on `feed_date`.

    Submitting one of these reduces both Product.stock and the per-location
    LocationStock row, and emits a StockMove for the inventory audit trail.
    """
    __tablename__ = "feeding_logs"

    id              = Column(Integer, primary_key=True, index=True)
    animal_group_id = Column(Integer, ForeignKey("animal_groups.id"), nullable=False, index=True)
    product_id      = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    location_id     = Column(Integer, ForeignKey("stock_locations.id"), nullable=False, index=True)
    qty             = Column(Numeric(14, 4), nullable=False)
    feed_date       = Column(Date, nullable=False)
    note            = Column(Text, nullable=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    group    = relationship("AnimalGroup", back_populates="feedings")
    product  = relationship("Product")
    location = relationship("StockLocation")
    user     = relationship("User")