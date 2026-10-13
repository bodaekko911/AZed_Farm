"""B2B quotations (price offers) — what a hotel, restaurant or lodge is offered before it orders.

A quotation never touches stock, accounting or a client's balance; only converting an accepted one into an invoice
does, through the existing B2B invoice code.
"""
from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class B2BQuotation(Base):
    __tablename__ = "b2b_quotations"

    id = Column(Integer, primary_key=True, index=True)
    number = Column(String(20), unique=True, index=True)                 # QUO-00001
    client_id = Column(Integer, ForeignKey("b2b_clients.id"), nullable=True, index=True)
    # A prospect not yet in AZed (used when client_id is empty).
    prospect_name = Column(String(200), nullable=True)
    prospect_contact = Column(String(150), nullable=True)
    prospect_phone = Column(String(50), nullable=True)
    prospect_email = Column(String(150), nullable=True)
    prospect_area = Column(String(120), nullable=True)
    quote_date = Column(Date, nullable=False)
    valid_until = Column(Date, nullable=False)
    status = Column(String(12), nullable=False, default="draft", index=True)   # draft|sent|accepted|rejected|expired
    language = Column(String(2), nullable=False, default="en")                 # en | ar (the printed copy)
    delivery_area = Column(String(120), nullable=True)
    delivery_day = Column(String(40), nullable=True)
    delivery_fee = Column(Numeric(12, 2), nullable=False, default=0)
    payment_terms = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)
    terms = Column(Text, nullable=True)
    subtotal = Column(Numeric(14, 2), nullable=False, default=0)          # before line discounts
    discount_total = Column(Numeric(14, 2), nullable=False, default=0)
    total = Column(Numeric(14, 2), nullable=False, default=0)             # lines after discounts + delivery fee
    weekly_value = Column(Numeric(14, 2), nullable=False, default=0)
    monthly_value = Column(Numeric(14, 2), nullable=False, default=0)
    below_cost_approved_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    below_cost_approved_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    sent_to = Column(String(150), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decided_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    reject_reason = Column(String(300), nullable=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    client = relationship("B2BClient")
    items = relationship("B2BQuotationItem", back_populates="quotation", cascade="all, delete-orphan",
                         order_by="B2BQuotationItem.sort")


class B2BQuotationItem(Base):
    __tablename__ = "b2b_quotation_items"

    id = Column(Integer, primary_key=True, index=True)
    quotation_id = Column(Integer, ForeignKey("b2b_quotations.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty = Column(Numeric(12, 3), nullable=False)
    unit = Column(String(30), nullable=True)
    unit_price = Column(Numeric(14, 2), nullable=False)          # after the client's discount, before the line's
    line_discount_pct = Column(Numeric(6, 2), nullable=False, default=0)
    line_total = Column(Numeric(14, 2), nullable=False)
    frequency = Column(String(14), nullable=False, default="once")   # once | weekly | twice_weekly
    note = Column(String(200), nullable=True)
    unit_cost = Column(Numeric(12, 3), nullable=True)            # for the margin shown to staff only
    sort = Column(Integer, nullable=False, default=0)

    quotation = relationship("B2BQuotation", back_populates="items")
    product = relationship("Product")


class B2BQuotationSettings(Base):
    __tablename__ = "b2b_quotation_settings"

    id = Column(Integer, primary_key=True)
    terms_en = Column(Text, nullable=True)
    terms_ar = Column(Text, nullable=True)
    target_margin_pct = Column(Numeric(6, 2), nullable=False, default=30)
    validity_days = Column(Integer, nullable=False, default=14)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
