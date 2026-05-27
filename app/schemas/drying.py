from pydantic import BaseModel, Field
from typing import Optional, List


class DryingBatchInputItem(BaseModel):
    product_id: int
    qty:        float = Field(..., gt=0)


class DryingBatchStartCreate(BaseModel):
    inputs:             List[DryingBatchInputItem] = Field(..., min_length=1)
    expected_yield_pct: Optional[float] = Field(None, ge=0, le=100)
    notes:              Optional[str] = Field(None, max_length=500)


class DryingBatchOutputItem(BaseModel):
    product_id: int
    qty:        float = Field(..., gt=0)


class DryingBatchCompleteRequest(BaseModel):
    outputs: List[DryingBatchOutputItem] = Field(..., min_length=1)
    notes:   Optional[str] = Field(None, max_length=500)


class DryingBatchSpoilageCreate(BaseModel):
    product_id: int
    qty:        float = Field(..., gt=0)
    # mold | pest | weather | other
    reason:     str   = Field(..., max_length=50)
    detail:     Optional[str] = Field(None, max_length=500)


class DryingBatchCancelRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)
