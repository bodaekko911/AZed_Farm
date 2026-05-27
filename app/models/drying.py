from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class DryingBatch(Base):
    """A multi-day processing batch (drying, fermenting, curing, etc).

    Distinct from ProductionBatch — drying batches are stateful and span
    real wall-clock time. Stock moves only at terminal transitions:
    inputs deduct on start; outputs credit on complete; cancel refunds inputs.
    """
    __tablename__ = "drying_batches"

    id              = Column(Integer, primary_key=True, index=True)
    batch_number    = Column(String(30), unique=True, index=True)
    # in_progress | completed | cancelled
    status          = Column(String(20), nullable=False, default="in_progress",
                             server_default="in_progress")

    started_at      = Column(DateTime(timezone=True), nullable=False,
                             server_default=func.now())
    completed_at    = Column(DateTime(timezone=True), nullable=True)
    cancelled_at    = Column(DateTime(timezone=True), nullable=True)

    expected_yield_pct = Column(Numeric(5, 2), nullable=True)
    actual_yield_pct   = Column(Numeric(5, 2), nullable=True)
    notes              = Column(Text)

    started_by_id   = Column(Integer, ForeignKey("users.id"), nullable=False)
    completed_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    inputs   = relationship("DryingBatchInput",    back_populates="batch",
                            cascade="all, delete-orphan")
    outputs  = relationship("DryingBatchOutput",   back_populates="batch",
                            cascade="all, delete-orphan")
    spoilage = relationship("DryingBatchSpoilage", back_populates="batch",
                            cascade="all, delete-orphan")
    started_by   = relationship("User", foreign_keys=[started_by_id])
    completed_by = relationship("User", foreign_keys=[completed_by_id])


class DryingBatchInput(Base):
    __tablename__ = "drying_batch_inputs"

    id         = Column(Integer, primary_key=True, index=True)
    batch_id   = Column(Integer, ForeignKey("drying_batches.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty        = Column(Numeric(12, 3), nullable=False)

    batch   = relationship("DryingBatch", back_populates="inputs")
    product = relationship("Product")


class DryingBatchOutput(Base):
    __tablename__ = "drying_batch_outputs"

    id         = Column(Integer, primary_key=True, index=True)
    batch_id   = Column(Integer, ForeignKey("drying_batches.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty        = Column(Numeric(12, 3), nullable=False)

    batch   = relationship("DryingBatch", back_populates="outputs")
    product = relationship("Product")


class DryingBatchSpoilage(Base):
    """Spoilage logged during a drying batch.

    Distinct from normal process loss (water evaporation). Captures specifically
    what went wrong: mold, pest, weather, other.
    """
    __tablename__ = "drying_batch_spoilage"

    id           = Column(Integer, primary_key=True, index=True)
    batch_id     = Column(Integer, ForeignKey("drying_batches.id"), nullable=False)
    product_id   = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty          = Column(Numeric(12, 3), nullable=False)
    # mold | pest | weather | other
    reason       = Column(String(50), nullable=False)
    detail       = Column(Text)
    logged_at    = Column(DateTime(timezone=True), nullable=False,
                          server_default=func.now())
    logged_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    batch     = relationship("DryingBatch", back_populates="spoilage")
    product   = relationship("Product")
    logged_by = relationship("User")
