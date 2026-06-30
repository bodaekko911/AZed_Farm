"""Animal management — groups of animals and their feeding logs.

Stage 1 of the Animal Management module. Defines:
  • AnimalGroup  — a herd / flock / pen of animals on a farm
  • FeedingLog   — a record of feed consumed by a group

Stage 2 adds purchase-cost tracking on the group and a back-reference to
expenses (so general operating expenses can be allocated against a group
instead of, or in addition to, a farm).

Future stages will add: production logs (milk/eggs/weight), health logs,
FCR metrics, and dashboard integration. Those features will reuse these
tables; no breaking changes are anticipated.
"""
from sqlalchemy import Column, Integer, String, Numeric, DateTime, Date, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class AnimalGroup(Base):
    """A managed group of animals (e.g. 'Dairy Cows', 'Layer Chickens').

    Headcount can be edited directly; future stages may add structured
    intake/sale events that drive headcount automatically.

    Purchase cost can be entered either as a total for the whole group
    (purchase_cost) or as a per-head price (cost_per_head). The Analyze
    endpoint uses whichever is present (preferring total if both are set).
    """
    __tablename__ = "animal_groups"

    id             = Column(Integer, primary_key=True, index=True)
    name           = Column(String(150), nullable=False, index=True)
    animal_type    = Column(String(30), nullable=False, default="other")  # cattle|poultry|sheep|goats|other
    headcount      = Column(Integer, nullable=False, default=0)
    male_count     = Column(Integer, nullable=True)    # optional sex breakdown (does not drive headcount)
    female_count   = Column(Integer, nullable=True)    # optional sex breakdown (does not drive headcount)
    birth_date     = Column(Date, nullable=True)       # birth / hatch date of the cohort; drives auto age display
    farm_id        = Column(Integer, ForeignKey("farms.id"), nullable=True, index=True)
    status         = Column(String(20), nullable=False, default="active")  # active|sold|deceased|archived
    notes          = Column(Text, nullable=True)
    purchase_cost  = Column(Numeric(14, 2), nullable=True)   # total paid for the whole group (EGP)
    cost_per_head  = Column(Numeric(14, 2), nullable=True)   # optional per-animal price (EGP)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    archived_at    = Column(DateTime(timezone=True), nullable=True)

    farm     = relationship("Farm")
    feedings = relationship("FeedingLog", back_populates="group", cascade="all, delete-orphan")
    deaths   = relationship("MortalityLog", back_populates="group", cascade="all, delete-orphan")
    intakes  = relationship("AnimalIntakeLog", back_populates="group", cascade="all, delete-orphan")
    # Expenses tagged against this group (not a cascade delete — archiving a
    # group keeps history intact). The Expense side defines the FK column.
    expenses = relationship(
        "Expense",
        back_populates="animal_group",
        foreign_keys="Expense.animal_group_id",
    )


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


class MortalityLog(Base):
    """A recorded death (or batch death) within an animal group.

    Submitting one of these decrements the parent group's headcount by
    `count`. Deleting one restores the headcount.

    Cause values:
      illness | injury | age | predator | weather | birth | unknown | other
    """
    __tablename__ = "mortality_logs"

    id              = Column(Integer, primary_key=True, index=True)
    animal_group_id = Column(Integer, ForeignKey("animal_groups.id"), nullable=False, index=True)
    death_date      = Column(Date, nullable=False, index=True)
    count           = Column(Integer, nullable=False, default=1)
    cause           = Column(String(30), nullable=False, default="unknown")
    note            = Column(Text, nullable=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    group = relationship("AnimalGroup", back_populates="deaths")
    user  = relationship("User")


class AnimalIntakeLog(Base):
    """A recorded intake/receipt of animals into a group (purchase, birth, or
    transfer-in). Submitting one INCREMENTS the parent group's headcount by
    `count` — the inverse of MortalityLog. Deleting one decrements it again.

    The purchase cost can be booked as an animal expense; `expense_id` links to
    that expense so it can be reversed when the intake is undone.
    """
    __tablename__ = "animal_intake_logs"

    id              = Column(Integer, primary_key=True, index=True)
    animal_group_id = Column(Integer, ForeignKey("animal_groups.id"), nullable=False, index=True)
    intake_type     = Column(String(20), nullable=False, default="purchase")  # purchase|birth|transfer|other
    intake_date     = Column(Date, nullable=False, index=True)
    count           = Column(Integer, nullable=False, default=1)
    source          = Column(String(150), nullable=True)   # supplier / origin
    unit_cost       = Column(Numeric(14, 2), nullable=True) # per-head price (EGP)
    total_cost      = Column(Numeric(14, 2), nullable=True) # total purchase cost (EGP)
    note            = Column(Text, nullable=True)
    expense_id      = Column(Integer, ForeignKey("expenses.id"), nullable=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    group = relationship("AnimalGroup", back_populates="intakes")
    user  = relationship("User")