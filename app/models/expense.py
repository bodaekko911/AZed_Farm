from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class ExpenseCategory(Base):
    """
    User-defined expense categories, e.g. Water, Electricity, Gas, Rent.
    Each maps to a dedicated expense ledger account (code 5xxx).
    """
    __tablename__ = "expense_categories"

    id           = Column(Integer, primary_key=True, index=True)
    name         = Column(String(100), nullable=False, unique=True)
    account_code = Column(String(20), nullable=False)   # e.g. "5001", "5002"
    description  = Column(String(255), nullable=True)
    is_active    = Column(String(1), default="1")        # "1" active, "0" archived
    unit_price   = Column(Numeric(12, 4), nullable=True)   # EGP per unit (e.g. price per m³ of water)
    unit_name    = Column(String(20),  nullable=True)      # "m³", "kWh", "litre"
    carbon_factor_key = Column(String(60), nullable=True)  # FK by string to CarbonEmissionFactor.source_key

    expenses = relationship("Expense", back_populates="category")


class Expense(Base):
    """
    A single expense transaction — one bill, one payment, one entry.
    Automatically posts a journal entry (Debit expense account, Credit cash/bank).

    Cost allocation:
      • `animal_group_id` links the expense to an animal group so the
        Animals → Analyze tab can roll it up into that group's total cost.
      • `farm_id` is retained for legacy cost-allocation reports and bulk
        Excel imports; the UI no longer sets it directly.
    """
    __tablename__ = "expenses"

    id              = Column(Integer, primary_key=True, index=True)
    ref_number      = Column(String(30), unique=True, index=True)  # EXP-00001
    category_id     = Column(Integer, ForeignKey("expense_categories.id"), nullable=False)
    user_id         = Column(Integer, ForeignKey("users.id"),              nullable=True)
    expense_date    = Column(Date, nullable=False)
    amount          = Column(Numeric(14, 2), nullable=False)
    payment_method  = Column(String(20), default="cash")  # cash | bank_transfer | card
    vendor          = Column(String(150), nullable=True)  # e.g. "Cairo Electric Co."
    description     = Column(Text, nullable=True)
    journal_id      = Column(Integer, ForeignKey("journals.id"), nullable=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    farm_id         = Column(Integer, ForeignKey("farms.id"), nullable=True)
    animal_group_id = Column(Integer, ForeignKey("animal_groups.id"), nullable=True, index=True)
    is_animal_expense = Column(Boolean, nullable=False, default=False, server_default="0")
    payroll_id      = Column(Integer, ForeignKey("payroll.id"), nullable=True, unique=True, index=True)
    consumption     = Column(Numeric(14, 4), nullable=True)        # quantity in the category's unit
    unit_price_used = Column(Numeric(12, 4), nullable=True)        # snapshot of unit price at time of entry

    category     = relationship("ExpenseCategory", back_populates="expenses")
    user         = relationship("User")
    journal      = relationship("Journal")
    farm         = relationship("Farm")
    payroll      = relationship("Payroll")
    animal_group = relationship(
        "AnimalGroup",
        back_populates="expenses",
        foreign_keys=[animal_group_id],
    )