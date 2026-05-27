"""Drying Batch service — multi-stage workflow.

Each batch progresses through N transformation stages:
  • start_batch      — creates DryingBatch + Stage 1 (open); deducts input stock
  • add_next_stage   — closes the open stage (writes outputs, metrics, credits stock);
                       opens Stage N+1 (deducts new input stock)
  • finalize_batch   — closes the open stage; marks batch completed
  • cancel_batch     — clawbacks closed outputs, refunds all inputs (reverse order)
  • log_spoilage     — deducts spoiled qty mid-batch

An "open stage" is one where total_output_qty IS NULL.
At any moment of an in_progress batch, exactly one stage is open.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.log import record
from app.models.drying import (
    DryingBatch,
    DryingBatchStage,
    DryingBatchStageInput,
    DryingBatchStageOutput,
    DryingBatchSpoilage,
)
from app.models.inventory import StockMove
from app.models.product import Product
from app.schemas.drying import (
    DryingBatchCancelRequest,
    DryingBatchFinalizeRequest,
    DryingBatchNextStageRequest,
    DryingBatchSpoilageCreate,
    DryingBatchStartCreate,
)

# Units treated as weight/volume for yield % calculation
WEIGHT_UNITS = ("gram", "g", "kg", "ml", "l", "liter", "ltr", "litre")

VALID_SPOILAGE_REASONS = {"mold", "pest", "weather", "other"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_weight_unit(unit: str) -> bool:
    return (unit or "").lower().strip() in WEIGHT_UNITS


def _compute_stage_metrics(
    stage_inputs: list,
    stage_outputs: list,
    stage_1_inputs: list,
) -> dict:
    """Compute per-stage and cumulative yield metrics.

    Returns a dict with keys:
      total_input_qty, total_output_qty, stage_loss_pct, cumulative_yield_pct
    All values may be None if any product is non-weight-unit.
    """
    # Check all inputs are weight units
    if not stage_inputs or any(
        not _is_weight_unit(getattr(inp.product, "unit", "") or "")
        for inp in stage_inputs
    ):
        return {
            "total_input_qty": None,
            "total_output_qty": None,
            "stage_loss_pct": None,
            "cumulative_yield_pct": None,
        }
    if not stage_outputs or any(
        not _is_weight_unit(getattr(out.product, "unit", "") or "")
        for out in stage_outputs
    ):
        return {
            "total_input_qty": None,
            "total_output_qty": None,
            "stage_loss_pct": None,
            "cumulative_yield_pct": None,
        }

    total_in  = sum(float(inp.qty) for inp in stage_inputs)
    total_out = sum(float(out.qty) for out in stage_outputs)

    stage_loss_pct = round((1 - total_out / total_in) * 100, 2) if total_in > 0 else None

    # Cumulative yield vs Stage 1 inputs
    stage1_weight = None
    if stage_1_inputs and all(
        _is_weight_unit(getattr(inp.product, "unit", "") or "")
        for inp in stage_1_inputs
    ):
        stage1_weight = sum(float(inp.qty) for inp in stage_1_inputs)

    cumulative_yield_pct = None
    if stage1_weight and stage1_weight > 0:
        cumulative_yield_pct = round(total_out / stage1_weight * 100, 2)

    return {
        "total_input_qty": round(total_in, 3),
        "total_output_qty": round(total_out, 3),
        "stage_loss_pct": stage_loss_pct,
        "cumulative_yield_pct": cumulative_yield_pct,
    }


def _find_open_stage(batch) -> DryingBatchStage:
    """Return the single open stage (total_output_qty IS NULL).

    Raises HTTPException(400) if not exactly one.
    """
    open_stages = [s for s in batch.stages if s.total_output_qty is None]
    if len(open_stages) == 0:
        raise HTTPException(
            status_code=400,
            detail=f"Batch {batch.batch_number} has no open stage — it may already be finalized.",
        )
    if len(open_stages) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"Batch {batch.batch_number} has {len(open_stages)} open stages — data inconsistency.",
        )
    return open_stages[0]


async def _close_open_stage(
    db: AsyncSession,
    batch,
    outputs_payload: list,
    stage_1_inputs: list,
    current_user,
    batch_number: str,
) -> None:
    """Find the open stage, write its outputs, credit stock, compute metrics."""
    open_stage = _find_open_stage(batch)

    for item in outputs_payload:
        product = await _load_product_or_404(db, item.product_id)
        before = float(product.stock)
        product.stock = before + float(item.qty)
        after = float(product.stock)
        db.add(DryingBatchStageOutput(
            stage_id=open_stage.id,
            product_id=product.id,
            qty=item.qty,
        ))
        db.add(StockMove(
            product_id=product.id,
            type="in",
            user_id=current_user.id,
            qty=float(item.qty),
            qty_before=before,
            qty_after=after,
            ref_type="drying_batch",
            ref_id=batch.id,
            note=f"Output from {batch_number} stage {open_stage.stage_number}",
        ))

    # Flush so the output rows have IDs, then reload stage with products attached
    await db.flush()

    # Re-read stage inputs/outputs with products for metrics (already in memory from eager load)
    metrics = _compute_stage_metrics(
        open_stage.inputs,
        open_stage.outputs,
        stage_1_inputs,
    )

    open_stage.total_input_qty      = metrics["total_input_qty"]
    open_stage.total_output_qty     = metrics["total_output_qty"] if metrics["total_output_qty"] is not None else sum(float(i.qty) for i in outputs_payload)
    open_stage.stage_loss_pct       = metrics["stage_loss_pct"]
    open_stage.cumulative_yield_pct = metrics["cumulative_yield_pct"]


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
    with_stages: bool = False,
) -> DryingBatch:
    stmt = select(DryingBatch).where(DryingBatch.id == batch_id)
    if with_stages:
        stmt = stmt.options(
            selectinload(DryingBatch.stages)
            .selectinload(DryingBatchStage.inputs)
            .selectinload(DryingBatchStageInput.product),
            selectinload(DryingBatch.stages)
            .selectinload(DryingBatchStage.outputs)
            .selectinload(DryingBatchStageOutput.product),
        )
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

    1. Validate stock for all inputs.
    2. Create DryingBatch (status=in_progress).
    3. Create Stage 1 (open — no outputs yet).
    4. Insert DryingBatchStageInput rows, deduct stock, insert StockMoves.
    """
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
        notes=data.notes,
    )
    db.add(batch)
    await db.flush()  # get batch.id

    stage = DryingBatchStage(
        batch_id=batch.id,
        stage_number=1,
        label=data.label,
        logged_by_id=current_user.id,
        # Metrics columns stay NULL — stage is open
    )
    db.add(stage)
    await db.flush()  # get stage.id

    for item, product in input_products:
        db.add(DryingBatchStageInput(
            stage_id=stage.id,
            product_id=product.id,
            qty=item.qty,
        ))
        before = float(product.stock)
        product.stock = before - float(item.qty)
        after = float(product.stock)
        db.add(StockMove(
            product_id=product.id,
            type="out",
            user_id=current_user.id,
            qty=float(item.qty),
            qty_before=before,
            qty_after=after,
            ref_type="drying_batch",
            ref_id=batch.id,
            note=f"Input to {batch_number} stage 1",
        ))

    record(
        db,
        "Drying",
        "start_batch",
        f"Batch {batch_number} started — {len(data.inputs)} inputs in stage 1",
        user=current_user,
        ref_type="drying_batch",
        ref_id=batch.id,
    )

    await db.commit()
    await db.refresh(batch)
    return batch


async def add_next_stage(
    db: AsyncSession,
    batch_id: int,
    data: DryingBatchNextStageRequest,
    current_user,
) -> DryingBatch:
    """Close the current open stage and open a new one.

    1. Load batch with all stage inputs/outputs.
    2. Validate status=in_progress.
    3. Close the open stage: write outputs, credit stock, compute metrics.
    4. Open Stage N+1: write new inputs, deduct stock.
    """
    batch = await _load_batch_or_404(db, batch_id, with_stages=True)

    if batch.status != "in_progress":
        raise HTTPException(status_code=400, detail="Batch is not in progress")

    stage_1_inputs = batch.stages[0].inputs if batch.stages else []
    open_stage = _find_open_stage(batch)

    if data.prev_stage_notes:
        open_stage.notes = data.prev_stage_notes

    await _close_open_stage(
        db, batch, data.prev_stage_outputs, stage_1_inputs, current_user, batch.batch_number
    )

    # Open next stage
    new_stage_number = open_stage.stage_number + 1
    new_stage = DryingBatchStage(
        batch_id=batch.id,
        stage_number=new_stage_number,
        label=data.new_stage_label,
        notes=data.new_stage_notes,
        logged_by_id=current_user.id,
        # Metrics NULL — stage open
    )
    db.add(new_stage)
    await db.flush()  # get new_stage.id

    new_input_products = []
    for item in data.new_stage_inputs:
        product = await _load_product_or_404(db, item.product_id)
        if float(product.stock) < item.qty:
            raise HTTPException(
                status_code=400,
                detail=f"Not enough stock for '{product.name}' in stage {new_stage_number}. "
                       f"Available: {float(product.stock)}, requested: {item.qty}",
            )
        new_input_products.append((item, product))

    for item, product in new_input_products:
        db.add(DryingBatchStageInput(
            stage_id=new_stage.id,
            product_id=product.id,
            qty=item.qty,
        ))
        before = float(product.stock)
        product.stock = before - float(item.qty)
        after = float(product.stock)
        db.add(StockMove(
            product_id=product.id,
            type="out",
            user_id=current_user.id,
            qty=float(item.qty),
            qty_before=before,
            qty_after=after,
            ref_type="drying_batch",
            ref_id=batch.id,
            note=f"Input to {batch.batch_number} stage {new_stage_number}",
        ))

    record(
        db,
        "Drying",
        "add_next_stage",
        f"Batch {batch.batch_number}: stage {open_stage.stage_number} closed, "
        f"stage {new_stage_number} opened",
        user=current_user,
        ref_type="drying_batch",
        ref_id=batch.id,
    )

    await db.commit()
    await db.refresh(batch)
    return batch


async def finalize_batch(
    db: AsyncSession,
    batch_id: int,
    data: DryingBatchFinalizeRequest,
    current_user,
) -> DryingBatch:
    """Finalize a drying batch — close the open stage and mark completed.

    1. Load batch with all stage data.
    2. Validate status=in_progress.
    3. Close the open stage: write final outputs, credit stock, compute metrics.
    4. Mark batch completed.
    """
    batch = await _load_batch_or_404(db, batch_id, with_stages=True)

    if batch.status != "in_progress":
        raise HTTPException(status_code=400, detail="Batch is not in progress")

    stage_1_inputs = batch.stages[0].inputs if batch.stages else []

    open_stage = _find_open_stage(batch)

    await _close_open_stage(
        db, batch, data.final_outputs, stage_1_inputs, current_user, batch.batch_number
    )

    batch.status = "completed"
    batch.completed_at = func.now()
    batch.completed_by_id = current_user.id

    if data.notes:
        batch.notes = f"{batch.notes or ''}\n\n[completion] {data.notes}".strip()

    record(
        db,
        "Drying",
        "finalize_batch",
        f"Batch {batch.batch_number} finalized after {len(batch.stages)} stage(s)",
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

    Iterates stages in REVERSE order:
      - For each closed stage (total_output_qty is not None): clawback outputs (deduct stock).
      - For each stage: refund inputs (credit stock).
    """
    batch = await _load_batch_or_404(db, batch_id, with_stages=True)

    if batch.status != "in_progress":
        raise HTTPException(status_code=400, detail="Batch is not in progress")

    for stage in reversed(batch.stages):
        # Clawback outputs of closed stages
        if stage.total_output_qty is not None:
            for out in stage.outputs:
                product = await _load_product_or_404(db, out.product_id)
                before = float(product.stock)
                product.stock = before - float(out.qty)
                after = float(product.stock)
                db.add(StockMove(
                    product_id=product.id,
                    type="out",
                    user_id=current_user.id,
                    qty=float(out.qty),
                    qty_before=before,
                    qty_after=after,
                    ref_type="drying_batch",
                    ref_id=batch.id,
                    note=f"Clawback: cancelled {batch.batch_number} stage {stage.stage_number} output",
                ))

        # Refund all inputs of this stage
        for inp in stage.inputs:
            product = await _load_product_or_404(db, inp.product_id)
            before = float(product.stock)
            product.stock = before + float(inp.qty)
            after = float(product.stock)
            db.add(StockMove(
                product_id=product.id,
                type="in",
                user_id=current_user.id,
                qty=float(inp.qty),
                qty_before=before,
                qty_after=after,
                ref_type="drying_batch",
                ref_id=batch.id,
                note=f"Refund: cancelled {batch.batch_number} stage {stage.stage_number} input",
            ))

    batch.status = "cancelled"
    batch.cancelled_at = func.now()

    if data.reason:
        batch.notes = f"{batch.notes or ''}\n\n[cancelled] {data.reason}".strip()

    record(
        db,
        "Drying",
        "cancel_batch",
        f"Batch {batch.batch_number} cancelled across {len(batch.stages)} stage(s)",
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

    Deducts the spoiled qty from product.stock immediately.
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

    before = float(product.stock)
    product.stock = before - float(data.qty)
    after = float(product.stock)
    db.add(StockMove(
        product_id=product.id,
        type="out",
        user_id=current_user.id,
        qty=float(data.qty),
        qty_before=before,
        qty_after=after,
        ref_type="drying_batch_spoilage",
        ref_id=spoilage.id,
        note=f"Spoilage in {batch.batch_number} ({data.reason})",
    ))

    record(
        db,
        "Drying",
        "log_spoilage",
        f"Spoilage logged in {batch.batch_number}: {data.qty} of {product.name} ({data.reason})",
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
    """Return drying batches with stages eagerly loaded."""
    stmt = (
        select(DryingBatch)
        .options(
            selectinload(DryingBatch.stages)
            .selectinload(DryingBatchStage.inputs)
            .selectinload(DryingBatchStageInput.product),
            selectinload(DryingBatch.stages)
            .selectinload(DryingBatchStage.outputs)
            .selectinload(DryingBatchStageOutput.product),
            selectinload(DryingBatch.spoilage),
            selectinload(DryingBatch.started_by),
            selectinload(DryingBatch.completed_by),
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
            selectinload(DryingBatch.stages)
            .selectinload(DryingBatchStage.inputs)
            .selectinload(DryingBatchStageInput.product),
            selectinload(DryingBatch.stages)
            .selectinload(DryingBatchStage.outputs)
            .selectinload(DryingBatchStageOutput.product),
            selectinload(DryingBatch.spoilage),
            selectinload(DryingBatch.started_by),
            selectinload(DryingBatch.completed_by),
        )
    )
    result = await db.execute(stmt)
    batch = result.scalar_one_or_none()
    if not batch:
        raise HTTPException(status_code=404, detail=f"Drying batch not found: {batch_id}")
    return batch
