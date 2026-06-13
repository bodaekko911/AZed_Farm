from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.database import get_async_session
from app.core.permissions import get_current_user, require_permission
from app.models.user import User
from app.schemas.drying import (
    DryingBatchStartCreate,
    DryingBatchNextStageRequest,
    DryingBatchFinalizeRequest,
    DryingBatchSpoilageCreate,
    DryingBatchCancelRequest,
    DryingBatchEditRequest,
)
from app.services import drying_service

router = APIRouter(
    prefix="/production/drying",
    tags=["drying"],
    dependencies=[Depends(require_permission("page_production"))],
)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _serialize_batch(batch) -> dict:
    stages = batch.stages or []

    # Find the last closed stage that has cumulative_yield_pct set
    final_yield_pct = None
    for s in reversed(stages):
        if s.total_output_qty is not None and s.cumulative_yield_pct is not None:
            final_yield_pct = float(s.cumulative_yield_pct)
            break

    # Find the currently open stage label (total_output_qty IS NULL)
    open_stages = [s for s in stages if s.total_output_qty is None]
    current_stage_label = open_stages[0].label if open_stages else None

    serialized_stages = []
    for s in stages:
        serialized_stages.append({
            "id": s.id,
            "stage_number": s.stage_number,
            "label": s.label,
            "notes": s.notes,
            "logged_at": s.logged_at.isoformat() if s.logged_at else None,
            "logged_by": s.logged_by.name if getattr(s, "logged_by", None) else None,
            "is_open": s.total_output_qty is None,
            "total_input_qty": float(s.total_input_qty) if s.total_input_qty is not None else None,
            "total_output_qty": float(s.total_output_qty) if s.total_output_qty is not None else None,
            "stage_loss_pct": float(s.stage_loss_pct) if s.stage_loss_pct is not None else None,
            "cumulative_yield_pct": float(s.cumulative_yield_pct) if s.cumulative_yield_pct is not None else None,
            "inputs": [
                {
                    "product_id": inp.product_id,
                    "product_name": inp.product.name if inp.product else None,
                    "qty": float(inp.qty),
                    "unit": inp.product.unit if inp.product else None,
                }
                for inp in (s.inputs or [])
            ],
            "outputs": [
                {
                    "product_id": out.product_id,
                    "product_name": out.product.name if out.product else None,
                    "qty": float(out.qty),
                    "unit": out.product.unit if out.product else None,
                }
                for out in (s.outputs or [])
            ],
        })

    return {
        "id": batch.id,
        "batch_number": batch.batch_number,
        "status": batch.status,
        "started_at": batch.started_at.isoformat() if batch.started_at else None,
        "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
        "cancelled_at": batch.cancelled_at.isoformat() if batch.cancelled_at else None,
        "notes": batch.notes,
        "started_by": batch.started_by.name if getattr(batch, "started_by", None) else None,
        "completed_by": batch.completed_by.name if getattr(batch, "completed_by", None) else None,
        "stages": serialized_stages,
        "spoilage": [
            {
                "id": sp.id,
                "product_id": sp.product_id,
                "product_name": sp.product.name if getattr(sp, "product", None) else None,
                "qty": float(sp.qty),
                "reason": sp.reason,
                "detail": sp.detail,
                "logged_at": sp.logged_at.isoformat() if sp.logged_at else None,
            }
            for sp in (batch.spoilage or [])
        ],
        # Convenience top-level summaries for table view
        "stage_count": len(stages),
        "current_stage_label": current_stage_label,
        "final_yield_pct": final_yield_pct,
        "spoilage_count": len(batch.spoilage or []),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/api/batches",
             dependencies=[Depends(require_permission("action_drying_start_batch"))])
async def start_drying_batch(
    data: DryingBatchStartCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    batch = await drying_service.start_batch(db, data, current_user)
    batch = await drying_service.get_batch(db, batch.id)
    return _serialize_batch(batch)


@router.get("/api/batches")
async def list_drying_batches(
    status: Optional[str] = Query(None, description="Filter by status: in_progress, completed, cancelled"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    batches = await drying_service.list_batches(db, status=status, skip=skip, limit=limit)
    return [_serialize_batch(b) for b in batches]


@router.get("/api/batches/{batch_id}")
async def get_drying_batch(
    batch_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    batch = await drying_service.get_batch(db, batch_id)
    return _serialize_batch(batch)


@router.post("/api/batches/{batch_id}/next-stage",
             dependencies=[Depends(require_permission("action_drying_start_batch"))])
async def add_drying_next_stage(
    batch_id: int,
    data: DryingBatchNextStageRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    batch = await drying_service.add_next_stage(db, batch_id, data, current_user)
    batch = await drying_service.get_batch(db, batch.id)
    return _serialize_batch(batch)


@router.post("/api/batches/{batch_id}/finalize",
             dependencies=[Depends(require_permission("action_drying_complete_batch"))])
async def finalize_drying_batch(
    batch_id: int,
    data: DryingBatchFinalizeRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    batch = await drying_service.finalize_batch(db, batch_id, data, current_user)
    batch = await drying_service.get_batch(db, batch.id)
    return _serialize_batch(batch)


@router.post("/api/batches/{batch_id}/cancel",
             dependencies=[Depends(require_permission("action_drying_cancel_batch"))])
async def cancel_drying_batch(
    batch_id: int,
    data: DryingBatchCancelRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    batch = await drying_service.cancel_batch(db, batch_id, data, current_user)
    batch = await drying_service.get_batch(db, batch.id)
    return _serialize_batch(batch)


@router.post("/api/batches/{batch_id}/edit",
             dependencies=[Depends(require_permission("action_drying_edit_batch"))])
async def edit_drying_batch(
    batch_id: int,
    data: DryingBatchEditRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    batch = await drying_service.edit_finalized_batch(db, batch_id, data, current_user)
    batch = await drying_service.get_batch(db, batch.id)
    return _serialize_batch(batch)


@router.post("/api/batches/{batch_id}/spoilage",
             dependencies=[Depends(require_permission("action_drying_log_spoilage"))])
async def log_drying_spoilage(
    batch_id: int,
    data: DryingBatchSpoilageCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    spoilage = await drying_service.log_spoilage(db, batch_id, data, current_user)
    return {
        "id": spoilage.id,
        "batch_id": spoilage.batch_id,
        "product_id": spoilage.product_id,
        "qty": float(spoilage.qty),
        "reason": spoilage.reason,
    }