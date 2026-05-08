from pydantic import BaseModel, Field
from typing import Optional, List, Dict


class ClientCreate(BaseModel):
    name:           str = Field(..., min_length=1, max_length=200)
    contact_person: Optional[str] = Field(None, max_length=200)
    phone:          Optional[str] = Field(None, max_length=50)
    email:          Optional[str] = Field(None, max_length=150)
    address:        Optional[str] = Field(None, max_length=300)
    payment_terms:  str = Field("cash", min_length=1, max_length=50)
    discount_pct:   float = Field(0, ge=0, le=100)
    credit_limit:   float = Field(0, ge=0)
    notes:          Optional[str] = Field(None, max_length=500)


class ClientUpdate(BaseModel):
    name:           Optional[str] = Field(None, min_length=1, max_length=200)
    contact_person: Optional[str] = Field(None, max_length=200)
    phone:          Optional[str] = Field(None, max_length=50)
    email:          Optional[str] = Field(None, max_length=150)
    address:        Optional[str] = Field(None, max_length=300)
    payment_terms:  Optional[str] = Field(None, max_length=50)
    discount_pct:   Optional[float] = Field(None, ge=0, le=100)
    credit_limit:   Optional[float] = Field(None, ge=0)
    notes:          Optional[str] = Field(None, max_length=500)


class InvoiceItemCreate(BaseModel):
    product_id: int
    qty:        float = Field(..., gt=0)
    unit_price: float = Field(..., ge=0)


class InvoiceCreate(BaseModel):
    client_id:      int
    invoice_type:   Optional[str] = Field(None, max_length=50)
    payment_method: Optional[str] = Field(None, max_length=50)
    discount_pct:   float = Field(0, ge=0, le=100)
    notes:          Optional[str] = Field(None, max_length=500)
    items:          List[InvoiceItemCreate]


class PaymentRecord(BaseModel):
    amount: float = Field(..., gt=0)
    method: str = Field("transfer", min_length=1, max_length=50)


class RefundItemCreate(BaseModel):
    product_id: int
    qty:        float = Field(..., gt=0)
    unit_price: float = Field(..., ge=0)


class ClientRefundCreate(BaseModel):
    client_id: int
    notes:     Optional[str] = Field(None, max_length=500)
    items:     List[RefundItemCreate]


class TopProduct(BaseModel):
    product_id: int
    name: str
    total_qty: float

class PurchaseTrend(BaseModel):
    month: str
    volume: float

class ClientAnalysisRow(BaseModel):
    id: int
    name: str
    contact_person: str
    phone: str
    payment_terms: str
    invoice_count: int
    gross_sales: float
    refunds: float
    net_sales: float
    paid_amount: float
    outstanding: float
    average_invoice: float
    payment_rate: float
    credit_limit: float
    credit_used_pct: Optional[float]
    last_invoice: str
    days_since_last_invoice: Optional[int]
    risk_level: str
    # New analysis fields
    ltv: float
    total_outstanding: float
    average_order_value: float
    return_rate: float
    purchase_trends: List[PurchaseTrend]
    top_products: List[TopProduct]

class ClientAnalysisSummary(BaseModel):
    active_clients: int
    clients_with_sales: int
    gross_sales: float
    refunds: float
    net_sales: float
    paid_amount: float
    outstanding: float
    payment_rate: float
    at_risk_clients: int
    top_client: str
    top_client_net_sales: float

class TermsBreakdownRow(BaseModel):
    payment_terms: str
    clients: int
    gross_sales: float
    outstanding: float

class ClientAnalysisResponse(BaseModel):
    summary: ClientAnalysisSummary
    clients: List[ClientAnalysisRow]
    top_clients: List[ClientAnalysisRow]
    collection_watch: List[ClientAnalysisRow]
    terms_breakdown: List[TermsBreakdownRow]
