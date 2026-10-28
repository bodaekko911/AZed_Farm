from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, Numeric
from sqlalchemy.sql import func

from app.database import Base


class MonthlyPlan(Base):
    """The numbers set for one calendar month on the Budget and Sales targets pages.

    ``month`` is always the 1st of the month. ``budget`` is the spending limit,
    compared with every expense dated in that month; ``sales_target`` is compared
    with net sales (POS after refunds + B2B revenue), as on the Sales dashboard,
    with ``sales_floor`` and ``sales_stretch`` below and above it (85% / 120% of
    it unless changed). Any may be empty — the month then has no budget or no target.
    """
    __tablename__ = "monthly_plans"

    id                 = Column(Integer, primary_key=True, index=True)
    month              = Column(Date, nullable=False, unique=True, index=True)
    budget             = Column(Numeric(14, 2), nullable=True)
    sales_target       = Column(Numeric(14, 2), nullable=True)
    sales_floor        = Column(Numeric(14, 2), nullable=True)
    sales_stretch      = Column(Numeric(14, 2), nullable=True)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at         = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
