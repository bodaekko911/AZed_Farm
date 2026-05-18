from typing import Optional

from pydantic import BaseModel


class ExpenseCategoryCreate(BaseModel):
    name: str
    account_code: Optional[str] = None
    description: Optional[str] = None
    unit_price: Optional[float] = None
    unit_name: Optional[str] = None
    carbon_factor_key: Optional[str] = None


class ExpenseCategoryUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    unit_price: Optional[float] = None
    unit_name: Optional[str] = None
    carbon_factor_key: Optional[str] = None


class ExpenseCreate(BaseModel):
    category_id: int
    expense_date: str
    amount: float
    payment_method: str = "cash"
    vendor: Optional[str] = None
    description: Optional[str] = None
    farm_id: Optional[int] = None
    animal_group_id: Optional[int] = None     # new — primary UI link
    consumption: Optional[float] = None        # quantity in the category's unit
    unit_price_used: Optional[float] = None    # snapshot at time of entry (optional override)


class ExpenseUpdate(BaseModel):
    category_id: Optional[int] = None
    expense_date: Optional[str] = None
    amount: Optional[float] = None
    payment_method: Optional[str] = None
    vendor: Optional[str] = None
    description: Optional[str] = None
    farm_id: Optional[int] = None
    animal_group_id: Optional[int] = None     # new — primary UI link
    consumption: Optional[float] = None
    unit_price_used: Optional[float] = None