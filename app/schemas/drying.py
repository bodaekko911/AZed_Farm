from pydantic import BaseModel, Field
from typing import Optional, List


class _StageItem(BaseModel):
    product_id: int
    qty:        float = Field(..., gt=0)


class DryingBatchStartCreate(BaseModel):
    inputs: List[_StageItem] = Field(..., min_length=1)
    label:  Optional[str]    = Field(None, max_length=80)
    notes:  Optional[str]    = Field(None, max_length=500)


class DryingBatchNextStageRequest(BaseModel):
    # The outputs of the previous (currently-open) stage
    prev_stage_outputs: List[_StageItem] = Field(..., min_length=1)
    # The inputs of the new stage being opened
    new_stage_inputs:   List[_StageItem] = Field(..., min_length=1)
    new_stage_label:    Optional[str]    = Field(None, max_length=80)
    new_stage_notes:    Optional[str]    = Field(None, max_length=500)
    prev_stage_notes:   Optional[str]    = Field(None, max_length=500)


class DryingBatchFinalizeRequest(BaseModel):
    # The final outputs of the last open stage
    final_outputs: List[_StageItem] = Field(..., min_length=1)
    notes:         Optional[str]    = Field(None, max_length=500)


class DryingBatchSpoilageCreate(BaseModel):
    product_id: int
    qty:        float = Field(..., gt=0)
    reason:     str   = Field(..., max_length=50)  # mold | pest | weather | other
    detail:     Optional[str] = Field(None, max_length=500)


class DryingBatchCancelRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)
