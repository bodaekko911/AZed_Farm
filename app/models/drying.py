from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class DryingBatch(Base):
    """Multi-stage processing batch (drying, fermenting, curing, etc).

    Each batch progresses through N transformation stages. Stock moves
    happen at each stage transition: inputs deducted when stage is opened,
    outputs credited when stage is closed.
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
    notes           = Column(Text)

    started_by_id   = Column(Integer, ForeignKey("users.id"), nullable=False)
    completed_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    stages   = relationship("DryingBatchStage",    back_populates="batch",
                            order_by="DryingBatchStage.stage_number",
                            cascade="all, delete-orphan")
    spoilage = relationship("DryingBatchSpoilage", back_populates="batch",
                            cascade="all, delete-orphan")
    started_by   = relationship("User", foreign_keys=[started_by_id])
    completed_by = relationship("User", foreign_keys=[completed_by_id])


class DryingBatchStage(Base):
    """One transformation step within a drying batch."""
    __tablename__ = "drying_batch_stages"

    id           = Column(Integer, primary_key=True, index=True)
    batch_id     = Column(Integer, ForeignKey("drying_batches.id", ondelete="CASCADE"), nullable=False)
    stage_number = Column(Integer, nullable=False)
    label        = Column(String(80))
    notes        = Column(Text)
    logged_at    = Column(DateTime(timezone=True), nullable=False,
                          server_default=func.now())
    logged_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    total_input_qty      = Column(Numeric(12, 3))
    total_output_qty     = Column(Numeric(12, 3))
    stage_loss_pct       = Column(Numeric(5, 2))
    cumulative_yield_pct = Column(Numeric(5, 2))

    batch     = relationship("DryingBatch", back_populates="stages")
    logged_by = relationship("User")
    inputs    = relationship("DryingBatchStageInput",  back_populates="stage",
                             cascade="all, delete-orphan")
    outputs   = relationship("DryingBatchStageOutput", back_populates="stage",
                             cascade="all, delete-orphan")


class DryingBatchStageInput(Base):
    __tablename__ = "drying_batch_stage_inputs"

    id         = Column(Integer, primary_key=True, index=True)
    stage_id   = Column(Integer, ForeignKey("drying_batch_stages.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty        = Column(Numeric(12, 3), nullable=False)

    stage   = relationship("DryingBatchStage", back_populates="inputs")
    product = relationship("Product")


class DryingBatchStageOutput(Base):
    __tablename__ = "drying_batch_stage_outputs"

    id         = Column(Integer, primary_key=True, index=True)
    stage_id   = Column(Integer, ForeignKey("drying_batch_stages.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty        = Column(Numeric(12, 3), nullable=False)

    stage   = relationship("DryingBatchStage", back_populates="outputs")
    product = relationship("Product")


class DryingBatchSpoilage(Base):
    """Spoilage logged during a drying batch — distinct from process loss."""
    __tablename__ = "drying_batch_spoilage"

    id           = Column(Integer, primary_key=True, index=True)
    batch_id     = Column(Integer, ForeignKey("drying_batches.id", ondelete="CASCADE"), nullable=False)
    product_id   = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty          = Column(Numeric(12, 3), nullable=False)
    reason       = Column(String(50), nullable=False)
    detail       = Column(Text)
    logged_at    = Column(DateTime(timezone=True), nullable=False,
                          server_default=func.now())
    logged_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    batch     = relationship("DryingBatch", back_populates="spoilage")
    product   = relationship("Product")
    logged_by = relationship("User")
