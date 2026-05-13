from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class ProductReceipt(Base):
    __tablename__ = "product_receipts"

    id           = Column(Integer, primary_key=True, index=True)
    ref_number   = Column(String(30), unique=True, index=True, nullable=False)
    product_id   = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=True)
    receive_date = Column(Date, nullable=False)
    qty          = Column(Numeric(12, 3), nullable=False)
    unit_cost    = Column(Numeric(12, 2), nullable=True)
    total_cost   = Column(Numeric(12, 2), nullable=True)
    supplier_ref = Column(String(150), nullable=True)
    supplier_id  = Column(Integer, ForeignKey("suppliers.id", ondelete="SET NULL"),
                          nullable=True, index=True)
    amount_paid  = Column(Numeric(12, 2), nullable=False, default=0, server_default="0")
    notes        = Column(Text, nullable=True)
    expense_id   = Column(Integer, ForeignKey("expenses.id"), nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    product  = relationship("Product")
    user     = relationship("User")
    expense  = relationship("Expense")
    supplier = relationship("Supplier")