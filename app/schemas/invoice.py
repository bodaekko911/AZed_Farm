from pydantic import BaseModel, Field
from typing import Optional, List


class InvoiceItemCreate(BaseModel):
    sku:           str = Field(..., min_length=1, max_length=80)
    name:          Optional[str] = Field(None, max_length=200)
    price:         Optional[float] = Field(None, ge=0)
    qty:           float = Field(..., gt=0)
    unit_price:    Optional[float] = None
    catalog_price: Optional[float] = None
    price_edited:  Optional[bool] = False


class InvoiceCreate(BaseModel):
    customer_id:      Optional[int] = None
    items:            List[InvoiceItemCreate]
    discount_percent: float = Field(0, ge=0, le=100)
    notes:            Optional[str] = Field(None, max_length=500)
    payment_method:   str = Field("cash", min_length=1, max_length=50)
    settle_later:     bool = False


class InvoiceCollectionRequest(BaseModel):
    payment_method: str = Field("cash", min_length=1, max_length=50)


class ConsignmentSaleItemIn(BaseModel):
    product_id: int
    qty:        float = Field(..., gt=0)
    unit_price: float = Field(..., ge=0)


class B2BPaymentRequest(BaseModel):
    amount: float = Field(..., gt=0)
    month_label: Optional[str] = Field(None, max_length=100)
    # Consignment client payments carry the items the client reported sold.
    # When present, the payment amount must equal the sum of (qty × unit_price).
    items: Optional[List[ConsignmentSaleItemIn]] = None
