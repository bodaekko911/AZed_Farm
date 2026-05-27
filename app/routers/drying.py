from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.database import get_async_session
from app.core.permissions import get_current_user, require_permission
from app.models.user import User
from app.schemas.drying import (
    DryingBatchStartCreate,
    DryingBatchCompleteRequest,
    DryingBatchSpoilageCreate,
    DryingBatchCancelRequest,
)
from app.services import drying_service

router = APIRouter(
    prefix="/drying",
    tags=["drying"],
    dependencies=[Depends(require_permission("page_drying"))],
)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _batch_to_dict(batch) -> dict:
    return {
        "id": batch.id,
        "batch_number": batch.batch_number,
        "status": batch.status,
        "started_at": batch.started_at.isoformat() if batch.started_at else None,
        "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
        "cancelled_at": batch.cancelled_at.isoformat() if batch.cancelled_at else None,
        "expected_yield_pct": float(batch.expected_yield_pct) if batch.expected_yield_pct is not None else None,
        "actual_yield_pct": float(batch.actual_yield_pct) if batch.actual_yield_pct is not None else None,
        "notes": batch.notes,
        "started_by_id": batch.started_by_id,
        "completed_by_id": batch.completed_by_id,
        "inputs": [
            {
                "id": inp.id,
                "product_id": inp.product_id,
                "product_name": inp.product.name if inp.product else None,
                "qty": float(inp.qty),
            }
            for inp in (batch.inputs or [])
        ],
        "outputs": [
            {
                "id": out.id,
                "product_id": out.product_id,
                "product_name": out.product.name if out.product else None,
                "qty": float(out.qty),
            }
            for out in (batch.outputs or [])
        ],
        "spoilage": [
            {
                "id": sp.id,
                "product_id": sp.product_id,
                "product_name": sp.product.name if sp.product else None,
                "qty": float(sp.qty),
                "reason": sp.reason,
                "detail": sp.detail,
                "logged_at": sp.logged_at.isoformat() if sp.logged_at else None,
                "logged_by_id": sp.logged_by_id,
            }
            for sp in (batch.spoilage or [])
        ],
    }


def _spoilage_to_dict(sp) -> dict:
    return {
        "id": sp.id,
        "batch_id": sp.batch_id,
        "product_id": sp.product_id,
        "qty": float(sp.qty),
        "reason": sp.reason,
        "detail": sp.detail,
        "logged_at": sp.logged_at.isoformat() if sp.logged_at else None,
        "logged_by_id": sp.logged_by_id,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/api/batches")
async def start_batch(
    data: DryingBatchStartCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_permission("action_drying_start_batch")),
):
    batch = await drying_service.start_batch(db, data, current_user)
    return _batch_to_dict(batch)


@router.get("/api/batches")
async def list_batches(
    status: Optional[str] = Query(None, description="Filter by status: in_progress, completed, cancelled"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    batches = await drying_service.list_batches(db, status=status, skip=skip, limit=limit)
    return [_batch_to_dict(b) for b in batches]


@router.get("/api/batches/{batch_id}")
async def get_batch(
    batch_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    batch = await drying_service.get_batch(db, batch_id)
    return _batch_to_dict(batch)


@router.post("/api/batches/{batch_id}/complete")
async def complete_batch(
    batch_id: int,
    data: DryingBatchCompleteRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_permission("action_drying_complete_batch")),
):
    batch = await drying_service.complete_batch(db, batch_id, data, current_user)
    return _batch_to_dict(batch)


@router.post("/api/batches/{batch_id}/cancel")
async def cancel_batch(
    batch_id: int,
    data: DryingBatchCancelRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_permission("action_drying_cancel_batch")),
):
    batch = await drying_service.cancel_batch(db, batch_id, data, current_user)
    return _batch_to_dict(batch)


@router.post("/api/batches/{batch_id}/spoilage")
async def log_spoilage(
    batch_id: int,
    data: DryingBatchSpoilageCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_permission("action_drying_log_spoilage")),
):
    spoilage = await drying_service.log_spoilage(db, batch_id, data, current_user)
    return _spoilage_to_dict(spoilage)
