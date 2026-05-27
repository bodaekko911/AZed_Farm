"""Drying Batch service.

Manages multi-day stateful processing batches (drying, fermenting, curing, etc).
Distinct from the one-shot ProductionBatch — drying batches span real wall-clock
time and move stock only at terminal transitions:

  • start_batch     — deducts input stock immediately
  • complete_batch  — credits output stock and computes actual yield %
  • cancel_batch    — refunds input stock
  • log_spoilage    — deducts spoiled stock at log time (mid-batch)
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.log import record
from app.models.drying import (
    DryingBatch,
    DryingBatchInput,
    DryingBatchOutput,
    DryingBatchSpoilage,
)
from app.models.inventory import StockMove
from app.models.product import Product
from app.schemas.drying import (
    DryingBatchCancelRequest,
    DryingBatchCompleteRequest,
    DryingBatchSpoilageCreate,
    DryingBatchStartCreate,
)

# Units treated as weight for yield % calculation
WEIGHT_UNITS = {"gram", "g", "kg", "ml", "l", "liter", "ltr", "litre"}

VALID_SPOILAGE_REASONS = {"mold", "pest", "weather", "other"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_weight_unit(unit: str) -> bool:
    return (unit or "").lower() in WEIGHT_UNITS


async def _load_product_or_404(db: AsyncSession, product_id: int) -> Product:
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail=f"Product not found: {product_id}")
    return product


async def _load_batch_or_404(
    db: AsyncSession,
    batch_id: int,
    *,
    with_inputs: bool = False,
    with_outputs: bool = False,
    with_spoilage: bool = False,
) -> DryingBatch:
    stmt = select(DryingBatch).where(DryingBatch.id == batch_id)
    options = []
    if with_inputs:
        options.append(selectinload(DryingBatch.inputs).selectinload(DryingBatchInput.product))
    if with_outputs:
        options.append(selectinload(DryingBatch.outputs).selectinload(DryingBatchOutput.product))
    if with_spoilage:
        options.append(selectinload(DryingBatch.spoilage).selectinload(DryingBatchSpoilage.product))
    if options:
        stmt = stmt.options(*options)

    result = await db.execute(stmt)
    batch = result.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=404, detail=f"Drying batch not found: {batch_id}")
    return batch


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

async def next_batch_number(db: AsyncSession) -> str:
    """Returns the next DRY-NNNN batch number."""
    result = await db.execute(select(func.max(DryingBatch.id)))
    max_id = result.scalar() or 0
    return f"DRY-{str(max_id + 1).zfill(4)}"


async def start_batch(
    db: AsyncSession,
    data: DryingBatchStartCreate,
    current_user,
) -> DryingBatch:
    """Start a new drying batch.

    1. Verify sufficient stock for each input product.
    2. Generate batch_number.
    3. Insert DryingBatch (status=in_progress).
    4. For each input: insert DryingBatchInput, deduct stock, insert StockMove.
    5. Log activity.
    6. Commit and return.
    """
    # Pre-validate stock for all inputs
    input_products = []
    for item in data.inputs:
        product = await _load_product_or_404(db, item.product_id)
        if float(product.stock) < item.qty:
            raise HTTPException(
                status_code=400,
                detail=f"Not enough stock for '{product.name}'. "
                       f"Available: {float(product.stock)}, requested: {item.qty}",
            )
        input_products.append((item, product))

    batch_number = await next_batch_number(db)

    batch = DryingBatch(
        batch_number=batch_number,
        status="in_progress",
        started_by_id=current_user.id,
        expected_yield_pct=data.expected_yield_pct,
        notes=data.notes,
    )
    db.add(batch)
    await db.flush()  # get batch.id

    for item, product in input_products:
        db.add(DryingBatchInput(
            batch_id=batch.id,
            product_id=product.id,
            qty=item.qty,
        ))
        product.stock = float(product.stock) - item.qty
        db.add(StockMove(
            product_id=product.id,
            type="out",
            qty=-item.qty,
            ref_type="drying_batch",
            ref_id=batch.id,
            note=f"Input to {batch_number}",
            user_id=current_user.id,
        ))

    record(
        db,
        "Drying",
        "start_batch",
        f"Batch {batch_number} started — {len(data.inputs)} inputs",
        user=current_user,
        ref_type="drying_batch",
        ref_id=batch.id,
    )

    await db.commit()
    await db.refresh(batch)
    return batch


async def complete_batch(
    db: AsyncSession,
    batch_id: int,
    data: DryingBatchCompleteRequest,
    current_user,
) -> DryingBatch:
    """Complete a drying batch.

    1. Load batch + inputs. Validate status=in_progress.
    2. For each output: create DryingBatchOutput, add to product.stock, StockMove.
    3. Compute actual_yield_pct from weight-unit products only.
    4. Update batch fields and optionally append completion notes.
    5. Log, commit, return.
    """
    batch = await _load_batch_or_404(db, batch_id, with_inputs=True)

    if batch.status != "in_progress":
        raise HTTPException(status_code=400, detail="Batch is not in progress")

    output_products = []
    for item in data.outputs:
        product = await _load_product_or_404(db, item.product_id)
        output_products.append((item, product))

    for item, product in output_products:
        db.add(DryingBatchOutput(
            batch_id=batch.id,
            product_id=product.id,
            qty=item.qty,
        ))
        product.stock = float(product.stock) + item.qty
        db.add(StockMove(
            product_id=product.id,
            type="in",
            qty=item.qty,
            ref_type="drying_batch",
            ref_id=batch.id,
            note=f"Output from {batch.batch_number}",
            user_id=current_user.id,
        ))

    # Compute actual_yield_pct from weight-unit products
    total_in_kg = sum(
        float(inp.qty)
        for inp in batch.inputs
        if _is_weight_unit(inp.product.unit if inp.product else "")
    )
    total_out_kg = sum(
        float(item.qty)
        for item, product in output_products
        if _is_weight_unit(product.unit)
    )
    if total_in_kg > 0:
        actual_yield_pct = round(total_out_kg / total_in_kg * 100, 2)
    else:
        actual_yield_pct = None

    batch.status = "completed"
    batch.completed_at = func.now()
    batch.completed_by_id = current_user.id
    batch.actual_yield_pct = actual_yield_pct

    if data.notes:
        batch.notes = f"{batch.notes or ''}\n\n[completion] {data.notes}".strip()

    record(
        db,
        "Drying",
        "complete_batch",
        f"Batch {batch.batch_number} completed — {len(data.outputs)} outputs, "
        f"yield {actual_yield_pct}%",
        user=current_user,
        ref_type="drying_batch",
        ref_id=batch.id,
    )

    await db.commit()
    await db.refresh(batch)
    return batch


async def cancel_batch(
    db: AsyncSession,
    batch_id: int,
    data: DryingBatchCancelRequest,
    current_user,
) -> DryingBatch:
    """Cancel an in-progress drying batch.

    Refunds all input stock back to inventory and marks batch as cancelled.
    """
    batch = await _load_batch_or_404(db, batch_id, with_inputs=True)

    if batch.status != "in_progress":
        raise HTTPException(status_code=400, detail="Batch is not in progress")

    for inp in batch.inputs:
        product = await _load_product_or_404(db, inp.product_id)
        product.stock = float(product.stock) + float(inp.qty)
        db.add(StockMove(
            product_id=product.id,
            type="in",
            qty=float(inp.qty),
            ref_type="drying_batch",
            ref_id=batch.id,
            note=f"Cancelled {batch.batch_number} — input refunded",
            user_id=current_user.id,
        ))

    batch.status = "cancelled"
    batch.cancelled_at = func.now()

    if data.reason:
        batch.notes = f"{batch.notes or ''}\n\n[cancelled] {data.reason}".strip()

    record(
        db,
        "Drying",
        "cancel_batch",
        f"Batch {batch.batch_number} cancelled",
        user=current_user,
        ref_type="drying_batch",
        ref_id=batch.id,
    )

    await db.commit()
    await db.refresh(batch)
    return batch


async def log_spoilage(
    db: AsyncSession,
    batch_id: int,
    data: DryingBatchSpoilageCreate,
    current_user,
) -> DryingBatchSpoilage:
    """Log a spoilage event on an in-progress batch.

    Deducts the spoiled qty from product.stock immediately (material removed and
    discarded). Inserts a StockMove with ref_type="drying_batch_spoilage".
    """
    batch = await _load_batch_or_404(db, batch_id)

    if batch.status != "in_progress":
        raise HTTPException(status_code=400, detail="Batch is not in progress")

    if data.reason not in VALID_SPOILAGE_REASONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid spoilage reason '{data.reason}'. "
                   f"Must be one of: {sorted(VALID_SPOILAGE_REASONS)}",
        )

    product = await _load_product_or_404(db, data.product_id)

    spoilage = DryingBatchSpoilage(
        batch_id=batch.id,
        product_id=product.id,
        qty=data.qty,
        reason=data.reason,
        detail=data.detail,
        logged_by_id=current_user.id,
    )
    db.add(spoilage)
    await db.flush()  # get spoilage.id

    product.stock = float(product.stock) - data.qty
    db.add(StockMove(
        product_id=product.id,
        type="out",
        qty=-data.qty,
        ref_type="drying_batch_spoilage",
        ref_id=spoilage.id,
        note=f"Spoilage in {batch.batch_number} ({data.reason})",
        user_id=current_user.id,
    ))

    record(
        db,
        "Drying",
        "log_spoilage",
        f"Spoilage logged in {batch.batch_number}: {data.qty} of product {product.name} ({data.reason})",
        user=current_user,
        ref_type="drying_batch",
        ref_id=batch.id,
    )

    await db.commit()
    await db.refresh(spoilage)
    return spoilage


async def list_batches(
    db: AsyncSession,
    *,
    status: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> list[DryingBatch]:
    """Return drying batches with inputs/outputs eagerly loaded."""
    stmt = (
        select(DryingBatch)
        .options(
            selectinload(DryingBatch.inputs).selectinload(DryingBatchInput.product),
            selectinload(DryingBatch.outputs).selectinload(DryingBatchOutput.product),
            selectinload(DryingBatch.spoilage).selectinload(DryingBatchSpoilage.product),
        )
        .order_by(DryingBatch.started_at.desc())
        .offset(skip)
        .limit(limit)
    )
    if status:
        stmt = stmt.where(DryingBatch.status == status)

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_batch(db: AsyncSession, batch_id: int) -> DryingBatch:
    """Return a single batch with all relations eagerly loaded. 404 if missing."""
    stmt = (
        select(DryingBatch)
        .where(DryingBatch.id == batch_id)
        .options(
            selectinload(DryingBatch.inputs).selectinload(DryingBatchInput.product),
            selectinload(DryingBatch.outputs).selectinload(DryingBatchOutput.product),
            selectinload(DryingBatch.spoilage).selectinload(DryingBatchSpoilage.product),
            selectinload(DryingBatch.started_by),
            selectinload(DryingBatch.completed_by),
        )
    )
    result = await db.execute(stmt)
    batch = result.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=404, detail=f"Drying batch not found: {batch_id}")
    return batch
