"""Animals router — Stage 1 of the Animal Management module.

Provides:
  • CRUD for animal groups (herds/flocks/pens)
  • Feeding log entries (decreases stock at a chosen storage)
  • Two-tab HTML page (Groups + Feeding Log)

Stock side-effects when a feeding is logged:
  1. The chosen LocationStock row is decremented (per-storage)
  2. Product.stock is decremented (global total)
  3. A StockMove of type "out" is recorded for audit trail

Reversing a feeding (delete) re-credits both stock totals.
"""
from __future__ import annotations

from datetime import date as date_type, datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.log import record
from app.core.navigation import render_app_header
from app.core.permissions import require_permission
from app.database import get_async_session
from app.models.animal import AnimalGroup, FeedingLog, MortalityLog, AnimalIntakeLog
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm
from app.models.inventory import StockLocation, StockMove
from app.models.product import Product
from app.models.user import User
from app.services.location_inventory_service import (
    get_or_create_location_stock,
    quantize_qty,
)
from app.services.expense_service import (
    create_animal_intake_expense,
    reverse_animal_intake_expense,
)


router = APIRouter(
    prefix="/animals",
    tags=["Animals"],
    dependencies=[Depends(require_permission("page_animals"))],
)


# ── Schemas ──────────────────────────────────────────────────────────

class AnimalGroupIn(BaseModel):
    name:           str           = Field(..., min_length=1, max_length=150)
    animal_type:    str           = Field("other", max_length=30)
    headcount:      int           = Field(0, ge=0)
    farm_id:        Optional[int] = None
    notes:          Optional[str] = None
    purchase_cost:  Optional[float] = Field(None, ge=0)
    cost_per_head:  Optional[float] = Field(None, ge=0)


class AnimalGroupUpdate(BaseModel):
    name:           Optional[str] = Field(None, min_length=1, max_length=150)
    animal_type:    Optional[str] = Field(None, max_length=30)
    headcount:      Optional[int] = Field(None, ge=0)
    farm_id:        Optional[int] = None
    status:         Optional[str] = Field(None, max_length=20)
    notes:          Optional[str] = None
    purchase_cost:  Optional[float] = Field(None, ge=0)
    cost_per_head:  Optional[float] = Field(None, ge=0)


class FeedingCreate(BaseModel):
    animal_group_id: int       = Field(..., ge=1)
    product_id:      int       = Field(..., ge=1)
    location_id:     int       = Field(..., ge=1)
    qty:             float     = Field(..., gt=0)
    feed_date:       date_type
    note:            Optional[str] = None


# Valid mortality causes — frontend dropdown values must match these exactly.
VALID_CAUSES = {"illness", "injury", "age", "predator", "weather", "birth", "unknown", "other"}
VALID_INTAKE_TYPES = {"purchase", "birth", "transfer", "other"}


class MortalityCreate(BaseModel):
    animal_group_id: int       = Field(..., ge=1)
    death_date:      date_type
    count:           int       = Field(1, ge=1)
    cause:           str       = Field("unknown", max_length=30)
    note:            Optional[str] = None


class IntakeCreate(BaseModel):
    animal_group_id: Optional[int] = None          # receive into an existing group
    new_group_name:  Optional[str] = None          # ...or create a new group
    new_group_type:  Optional[str] = "other"
    farm_id:         Optional[int] = None           # farm for a newly-created group
    intake_type:     str           = Field("purchase", max_length=20)  # purchase|birth|transfer|other
    intake_date:     date_type
    count:           int           = Field(1, ge=1)
    source:          Optional[str] = None           # supplier / origin (or dam, for births)
    unit_cost:       Optional[float] = None          # per-head price (EGP)
    total_cost:      Optional[float] = None          # total cost (EGP) — wins over unit_cost
    note:            Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────

def _serialize_group(g: AnimalGroup) -> dict:
    purchase_cost = float(g.purchase_cost) if g.purchase_cost is not None else None
    cost_per_head = float(g.cost_per_head) if g.cost_per_head is not None else None
    head = int(g.headcount or 0)
    # Effective total purchase cost: prefer explicit total; otherwise derive
    # from per-head price × headcount.
    if purchase_cost is not None and purchase_cost > 0:
        effective_total = purchase_cost
    elif cost_per_head is not None and cost_per_head > 0:
        effective_total = round(cost_per_head * head, 2)
    else:
        effective_total = 0.0
    return {
        "id":           g.id,
        "name":         g.name,
        "animal_type":  g.animal_type or "other",
        "headcount":    head,
        "farm_id":      g.farm_id,
        "farm_name":    g.farm.name if g.farm else None,
        "status":       g.status or "active",
        "notes":        g.notes,
        "purchase_cost":          purchase_cost,
        "cost_per_head":          cost_per_head,
        "effective_purchase_cost": effective_total,
        "created_at":   g.created_at.isoformat() if g.created_at else None,
        "archived_at":  g.archived_at.isoformat() if g.archived_at else None,
    }


def _serialize_feeding(f: FeedingLog) -> dict:
    return {
        "id":              f.id,
        "animal_group_id": f.animal_group_id,
        "group_name":      f.group.name if f.group else None,
        "product_id":      f.product_id,
        "product_name":    f.product.name if f.product else None,
        "product_sku":     f.product.sku  if f.product else None,
        "location_id":     f.location_id,
        "location_name":   f.location.name if f.location else None,
        "qty":             float(f.qty or 0),
        "feed_date":       f.feed_date.isoformat() if f.feed_date else None,
        "note":            f.note,
        "user_id":         f.user_id,
        "user_name":       f.user.name if f.user else None,
        "created_at":      f.created_at.isoformat() if f.created_at else None,
    }


def _serialize_death(d: MortalityLog) -> dict:
    return {
        "id":              d.id,
        "animal_group_id": d.animal_group_id,
        "group_name":      d.group.name if d.group else None,
        "death_date":      d.death_date.isoformat() if d.death_date else None,
        "count":           int(d.count or 0),
        "cause":           d.cause or "unknown",
        "note":            d.note,
        "user_id":         d.user_id,
        "user_name":       d.user.name if d.user else None,
        "created_at":      d.created_at.isoformat() if d.created_at else None,
    }


def _serialize_intake(i: AnimalIntakeLog) -> dict:
    return {
        "id":              i.id,
        "animal_group_id": i.animal_group_id,
        "group_name":      i.group.name if i.group else None,
        "intake_type":     getattr(i, "intake_type", None) or "purchase",
        "intake_date":     i.intake_date.isoformat() if i.intake_date else None,
        "count":           int(i.count or 0),
        "source":          i.source,
        "unit_cost":       float(i.unit_cost) if i.unit_cost is not None else None,
        "total_cost":      float(i.total_cost) if i.total_cost is not None else None,
        "note":            i.note,
        "expense_id":      i.expense_id,
        "user_id":         i.user_id,
        "user_name":       i.user.name if i.user else None,
        "created_at":      i.created_at.isoformat() if i.created_at else None,
    }


# ── Animal Groups API ────────────────────────────────────────────────

@router.get("/api/groups")
async def list_groups(
    include_archived: bool = Query(False),
    db: AsyncSession = Depends(get_async_session),
):
    stmt = select(AnimalGroup).options(selectinload(AnimalGroup.farm))
    if not include_archived:
        stmt = stmt.where(AnimalGroup.status != "archived")
    stmt = stmt.order_by(AnimalGroup.name.asc())
    result = await db.execute(stmt)
    groups = result.scalars().all()
    return {"items": [_serialize_group(g) for g in groups]}


@router.post(
    "/api/groups",
    status_code=201,
    dependencies=[Depends(require_permission("action_animals_create"))],
)
async def create_group(
    data: AnimalGroupIn,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_permission("action_animals_create")),
):
    if data.farm_id is not None:
        farm = (await db.execute(select(Farm).where(Farm.id == data.farm_id))).scalar_one_or_none()
        if farm is None:
            raise HTTPException(status_code=400, detail="Farm not found")
    group = AnimalGroup(
        name=data.name.strip(),
        animal_type=data.animal_type or "other",
        headcount=data.headcount,
        farm_id=data.farm_id,
        notes=(data.notes or "").strip() or None,
        purchase_cost=(Decimal(str(data.purchase_cost)) if data.purchase_cost is not None else None),
        cost_per_head=(Decimal(str(data.cost_per_head)) if data.cost_per_head is not None else None),
        status="active",
    )
    db.add(group)
    await db.flush()
    await db.refresh(group, attribute_names=["farm"])
    record(db, "Animals", "create_group", f"Created group: {group.name}",
           user=current_user, ref_type="animal_group", ref_id=group.id)
    await db.commit()
    return _serialize_group(group)


@router.put(
    "/api/groups/{group_id}",
    dependencies=[Depends(require_permission("action_animals_edit"))],
)
async def update_group(
    group_id: int,
    data: AnimalGroupUpdate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_permission("action_animals_edit")),
):
    group = (
        await db.execute(
            select(AnimalGroup)
            .options(selectinload(AnimalGroup.farm))
            .where(AnimalGroup.id == group_id)
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")

    if data.name is not None:
        group.name = data.name.strip()
    if data.animal_type is not None:
        group.animal_type = data.animal_type
    if data.headcount is not None:
        group.headcount = data.headcount
    if data.farm_id is not None:
        if data.farm_id == 0:
            group.farm_id = None
        else:
            farm = (await db.execute(select(Farm).where(Farm.id == data.farm_id))).scalar_one_or_none()
            if farm is None:
                raise HTTPException(status_code=400, detail="Farm not found")
            group.farm_id = data.farm_id
    if data.status is not None:
        if data.status not in {"active", "sold", "deceased", "archived"}:
            raise HTTPException(status_code=400, detail="Invalid status")
        group.status = data.status
        if data.status == "archived":
            group.archived_at = datetime.now(timezone.utc)
    if data.notes is not None:
        group.notes = data.notes.strip() or None
    if data.purchase_cost is not None:
        # 0 / negative clears the field
        group.purchase_cost = (
            Decimal(str(data.purchase_cost)) if data.purchase_cost > 0 else None
        )
    if data.cost_per_head is not None:
        group.cost_per_head = (
            Decimal(str(data.cost_per_head)) if data.cost_per_head > 0 else None
        )

    await db.flush()
    await db.refresh(group, attribute_names=["farm"])
    record(db, "Animals", "update_group", f"Updated group: {group.name}",
           user=current_user, ref_type="animal_group", ref_id=group.id)
    await db.commit()
    return _serialize_group(group)


@router.delete(
    "/api/groups/{group_id}",
    dependencies=[Depends(require_permission("action_animals_delete"))],
)
async def archive_group(
    group_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_permission("action_animals_delete")),
):
    """Soft-delete: marks the group archived (preserves feeding history)."""
    group = (await db.execute(select(AnimalGroup).where(AnimalGroup.id == group_id))).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    group.status = "archived"
    group.archived_at = datetime.now(timezone.utc)
    record(db, "Animals", "archive_group", f"Archived group: {group.name}",
           user=current_user, ref_type="animal_group", ref_id=group.id)
    await db.commit()
    return {"ok": True}


# ── Feeding Log API ─────────────────────────────────────────────────

@router.get("/api/feedings")
async def list_feedings(
    limit: int = Query(100, ge=1, le=500),
    group_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(FeedingLog)
        .options(
            selectinload(FeedingLog.group),
            selectinload(FeedingLog.product),
            selectinload(FeedingLog.location),
            selectinload(FeedingLog.user),
        )
        .order_by(FeedingLog.feed_date.desc(), FeedingLog.id.desc())
        .limit(limit)
    )
    if group_id:
        stmt = stmt.where(FeedingLog.animal_group_id == group_id)
    result = await db.execute(stmt)
    return {"items": [_serialize_feeding(f) for f in result.scalars().all()]}


@router.post(
    "/api/feedings",
    status_code=201,
    dependencies=[Depends(require_permission("action_animal_feeding_create"))],
)
async def create_feeding(
    data: FeedingCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_permission("action_animal_feeding_create")),
):
    # Validate group
    group = (await db.execute(select(AnimalGroup).where(AnimalGroup.id == data.animal_group_id))).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=400, detail="Animal group not found")
    if group.status == "archived":
        raise HTTPException(status_code=400, detail="Cannot log feeding for archived group")

    # Validate product
    product = (await db.execute(select(Product).where(Product.id == data.product_id))).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=400, detail="Product not found")

    # Validate location
    location = (await db.execute(
        select(StockLocation).where(
            StockLocation.id == data.location_id,
            StockLocation.is_active.is_(True),
        )
    )).scalar_one_or_none()
    if location is None:
        raise HTTPException(status_code=400, detail="Storage not found or inactive")

    qty = Decimal(str(data.qty))

    # Check source stock — both per-location and global product
    loc_stock = await get_or_create_location_stock(
        db, location_id=location.id, product_id=product.id
    )
    available_at_location = Decimal(str(loc_stock.qty or 0))
    if available_at_location < qty:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Insufficient stock at {location.name}: "
                f"have {float(available_at_location):.3f}, need {float(qty):.3f}"
            ),
        )

    qty_before = Decimal(str(product.stock or 0))
    qty_after  = qty_before - qty

    # 1. Decrement per-location stock
    loc_stock.qty = quantize_qty(available_at_location - qty)
    # 2. Decrement global product stock
    product.stock = qty_after

    # 3. Create feeding log
    feeding = FeedingLog(
        animal_group_id=group.id,
        product_id=product.id,
        location_id=location.id,
        qty=qty,
        feed_date=data.feed_date,
        note=(data.note or "").strip() or None,
        user_id=current_user.id,
    )
    db.add(feeding)
    await db.flush()

    # 4. Stock movement for audit trail
    move = StockMove(
        product_id=product.id,
        type="out",
        qty=qty,
        qty_before=qty_before,
        qty_after=qty_after,
        ref_type="feeding",
        ref_id=feeding.id,
        note=f"Fed {group.name} from {location.name}",
        user_id=current_user.id,
    )
    db.add(move)

    record(db, "Animals", "create_feeding",
           f"Fed {group.name}: {float(qty)} of {product.name} from {location.name}",
           user=current_user, ref_type="feeding_log", ref_id=feeding.id)
    await db.commit()
    await db.refresh(feeding, attribute_names=["group", "product", "location", "user"])
    return _serialize_feeding(feeding)


@router.delete(
    "/api/feedings/{feeding_id}",
    dependencies=[Depends(require_permission("action_animal_feeding_delete"))],
)
async def delete_feeding(
    feeding_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_permission("action_animal_feeding_delete")),
):
    """Reverses a feeding log entry: re-credits both per-location and
    global stock, then deletes the row.
    """
    feeding = (
        await db.execute(
            select(FeedingLog)
            .options(
                selectinload(FeedingLog.product),
                selectinload(FeedingLog.location),
                selectinload(FeedingLog.group),
            )
            .where(FeedingLog.id == feeding_id)
        )
    ).scalar_one_or_none()
    if feeding is None:
        raise HTTPException(status_code=404, detail="Feeding entry not found")

    qty = Decimal(str(feeding.qty or 0))
    product = feeding.product
    if product is not None:
        product.stock = Decimal(str(product.stock or 0)) + qty
    # Re-credit per-location stock
    loc_stock = await get_or_create_location_stock(
        db, location_id=feeding.location_id, product_id=feeding.product_id
    )
    loc_stock.qty = quantize_qty(Decimal(str(loc_stock.qty or 0)) + qty)

    # Audit movement
    qty_after = Decimal(str(product.stock or 0)) if product else Decimal(0)
    move = StockMove(
        product_id=feeding.product_id,
        type="in",
        qty=qty,
        qty_before=qty_after - qty,
        qty_after=qty_after,
        ref_type="feeding_reverse",
        ref_id=feeding.id,
        note=f"Reversed feeding #{feeding.id}",
        user_id=current_user.id,
    )
    db.add(move)

    record(db, "Animals", "delete_feeding",
           f"Reversed feeding #{feeding.id} ({float(qty)} units restored)",
           user=current_user, ref_type="feeding_log", ref_id=feeding.id)
    await db.delete(feeding)
    await db.commit()
    return {"ok": True}


# ── Mortality Log API ───────────────────────────────────────────────

@router.get("/api/deaths")
async def list_deaths(
    limit: int = Query(100, ge=1, le=500),
    group_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(MortalityLog)
        .options(
            selectinload(MortalityLog.group),
            selectinload(MortalityLog.user),
        )
        .order_by(MortalityLog.death_date.desc(), MortalityLog.id.desc())
        .limit(limit)
    )
    if group_id:
        stmt = stmt.where(MortalityLog.animal_group_id == group_id)
    result = await db.execute(stmt)
    return {"items": [_serialize_death(d) for d in result.scalars().all()]}


@router.post(
    "/api/deaths",
    status_code=201,
    dependencies=[Depends(require_permission("action_animal_mortality_create"))],
)
async def create_death(
    data: MortalityCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_permission("action_animal_mortality_create")),
):
    # Validate group
    group = (await db.execute(select(AnimalGroup).where(AnimalGroup.id == data.animal_group_id))).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=400, detail="Animal group not found")
    if group.status == "archived":
        raise HTTPException(status_code=400, detail="Cannot log death for archived group")

    # Validate cause
    cause = (data.cause or "unknown").strip().lower()
    if cause not in VALID_CAUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid cause '{cause}'. Must be one of: {', '.join(sorted(VALID_CAUSES))}",
        )

    # Validate count doesn't exceed current headcount
    current_head = int(group.headcount or 0)
    if data.count > current_head:
        raise HTTPException(
            status_code=400,
            detail=f"Count ({data.count}) exceeds current headcount ({current_head}) for {group.name}",
        )

    # If cause is "other", note must be provided so the death isn't a mystery
    note = (data.note or "").strip() or None
    if cause == "other" and not note:
        raise HTTPException(
            status_code=400,
            detail="A note is required when cause is 'other' — describe what happened.",
        )

    # Create the record and adjust headcount
    death = MortalityLog(
        animal_group_id=group.id,
        death_date=data.death_date,
        count=data.count,
        cause=cause,
        note=note,
        user_id=current_user.id,
    )
    db.add(death)
    group.headcount = current_head - data.count
    await db.flush()
    await db.refresh(death, attribute_names=["group", "user"])

    record(db, "Animals", "create_mortality",
           f"Logged {data.count} death(s) in {group.name} (cause: {cause})",
           user=current_user, ref_type="mortality_log", ref_id=death.id)
    await db.commit()
    return _serialize_death(death)


@router.delete(
    "/api/deaths/{death_id}",
    dependencies=[Depends(require_permission("action_animal_mortality_delete"))],
)
async def delete_death(
    death_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_permission("action_animal_mortality_delete")),
):
    """Reverses a mortality entry: restores the group's headcount."""
    death = (
        await db.execute(
            select(MortalityLog)
            .options(selectinload(MortalityLog.group))
            .where(MortalityLog.id == death_id)
        )
    ).scalar_one_or_none()
    if death is None:
        raise HTTPException(status_code=404, detail="Mortality entry not found")

    count = int(death.count or 0)
    group_name = death.group.name if death.group else "(unknown group)"

    # Restore headcount
    if death.group is not None:
        death.group.headcount = int(death.group.headcount or 0) + count

    record(db, "Animals", "delete_mortality",
           f"Reversed mortality entry #{death.id} for {group_name} (restored {count})",
           user=current_user, ref_type="mortality_log", ref_id=death.id)
    await db.delete(death)
    await db.commit()
    return {"ok": True}


# ── Animal Intake (Receive) API ──────────────────────────────────────

@router.get("/api/intakes")
async def list_intakes(
    limit: int = Query(100, ge=1, le=500),
    group_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(AnimalIntakeLog)
        .options(
            selectinload(AnimalIntakeLog.group),
            selectinload(AnimalIntakeLog.user),
        )
        .order_by(AnimalIntakeLog.intake_date.desc(), AnimalIntakeLog.id.desc())
        .limit(limit)
    )
    if group_id:
        stmt = stmt.where(AnimalIntakeLog.animal_group_id == group_id)
    result = await db.execute(stmt)
    return {"items": [_serialize_intake(i) for i in result.scalars().all()]}


@router.post(
    "/api/intakes",
    status_code=201,
    dependencies=[Depends(require_permission("action_animals_create"))],
)
async def create_intake(
    data: IntakeCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_permission("action_animals_create")),
):
    """Receive animals into a group (existing or newly created): increments the
    group's headcount and, if a cost is given, books a Livestock Purchase
    expense tagged to the group/farm."""
    # Resolve the target group — an existing one, or create a new one.
    if data.animal_group_id:
        group = (
            await db.execute(select(AnimalGroup).where(AnimalGroup.id == data.animal_group_id))
        ).scalar_one_or_none()
        if group is None:
            raise HTTPException(status_code=400, detail="Animal group not found")
        if group.status == "archived":
            raise HTTPException(status_code=400, detail="Cannot receive into an archived group")
    else:
        name = (data.new_group_name or "").strip()
        if not name:
            raise HTTPException(
                status_code=400,
                detail="Choose an existing group or enter a new group name",
            )
        if data.farm_id is not None:
            farm = (await db.execute(select(Farm).where(Farm.id == data.farm_id))).scalar_one_or_none()
            if farm is None:
                raise HTTPException(status_code=400, detail="Farm not found")
        group = AnimalGroup(
            name=name,
            animal_type=(data.new_group_type or "other"),
            headcount=0,
            farm_id=data.farm_id,
            status="active",
        )
        db.add(group)
        await db.flush()

    if data.count < 1:
        raise HTTPException(status_code=400, detail="Count must be at least 1")

    intake_type = (data.intake_type or "purchase").strip().lower()
    if intake_type not in VALID_INTAKE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid type '{intake_type}'. Must be one of: {', '.join(sorted(VALID_INTAKE_TYPES))}",
        )

    # Resolve cost: an explicit total wins; otherwise unit_cost × count.
    total = None
    if data.total_cost is not None and float(data.total_cost) > 0:
        total = round(float(data.total_cost), 2)
    elif data.unit_cost is not None and float(data.unit_cost) > 0:
        total = round(float(data.unit_cost) * data.count, 2)

    # Increment headcount by the received count.
    group.headcount = int(group.headcount or 0) + data.count

    # Optionally book the purchase as a Livestock Purchase expense.
    expense = None
    if total and total > 0:
        expense = await create_animal_intake_expense(
            db,
            group=group,
            intake_date=data.intake_date,
            amount=total,
            supplier=data.source,
            count=data.count,
            current_user=current_user,
        )

    intake = AnimalIntakeLog(
        animal_group_id=group.id,
        intake_type=intake_type,
        intake_date=data.intake_date,
        count=data.count,
        source=(data.source or "").strip() or None,
        unit_cost=(Decimal(str(data.unit_cost)) if data.unit_cost is not None else None),
        total_cost=(Decimal(str(total)) if total is not None else None),
        note=(data.note or "").strip() or None,
        expense_id=expense.id if expense else None,
        user_id=current_user.id,
    )
    db.add(intake)
    await db.flush()
    await db.refresh(intake, attribute_names=["group", "user"])
    record(
        db, "Animals", "create_intake",
        f"Received {data.count} animal(s) into {group.name}"
        + (f" (cost {total:.2f})" if total else ""),
        user=current_user, ref_type="animal_intake", ref_id=intake.id,
    )
    await db.commit()
    return _serialize_intake(intake)


@router.delete(
    "/api/intakes/{intake_id}",
    dependencies=[Depends(require_permission("action_animals_delete"))],
)
async def delete_intake(
    intake_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_permission("action_animals_delete")),
):
    """Reverses an intake: removes the received count from the group's headcount
    and reverses the linked Livestock Purchase expense (if any)."""
    intake = (
        await db.execute(
            select(AnimalIntakeLog)
            .options(selectinload(AnimalIntakeLog.group))
            .where(AnimalIntakeLog.id == intake_id)
        )
    ).scalar_one_or_none()
    if intake is None:
        raise HTTPException(status_code=404, detail="Intake entry not found")

    count = int(intake.count or 0)
    group_name = intake.group.name if intake.group else "(unknown group)"
    if intake.group is not None:
        intake.group.headcount = max(0, int(intake.group.headcount or 0) - count)

    if intake.expense_id:
        await reverse_animal_intake_expense(db, intake.expense_id, current_user)

    record(
        db, "Animals", "delete_intake",
        f"Reversed intake #{intake.id} for {group_name} (removed {count})",
        user=current_user, ref_type="animal_intake", ref_id=intake.id,
    )
    await db.delete(intake)
    await db.commit()
    return {"ok": True}


# ── Convenience: list farms + products for the UI dropdowns ─────────

@router.get("/api/farms")
async def list_farms_for_picker(db: AsyncSession = Depends(get_async_session)):
    rows = (await db.execute(
        select(Farm).where(Farm.is_active == 1).order_by(Farm.name.asc())
    )).scalars().all()
    return {"items": [{"id": f.id, "name": f.name} for f in rows]}


@router.get("/api/locations")
async def list_locations_for_picker(db: AsyncSession = Depends(get_async_session)):
    """Active storage locations for the animals feeding form.

    Proxied here so users with page_animals don't also need page_inventory
    just to load the location picker when recording a feeding event.
    """
    rows = (await db.execute(
        select(StockLocation)
        .where(StockLocation.is_active == True)
        .order_by(StockLocation.name)
    )).scalars().all()
    return {"items": [{"id": loc.id, "name": loc.name, "code": loc.code or ""} for loc in rows]}


@router.get("/api/products")
async def list_products_for_picker(
    q: str = "",
    limit: int = 2000,
    db: AsyncSession = Depends(get_async_session),
):
    """Stock-tracked products (feed) for the animals feeding form picker.

    Proxied here so users with page_animals don't also need page_inventory.
    Returns only the fields the picker actually needs.
    """
    from app.models.product import Product
    from app.core.product_types import stock_tracked_product_condition
    from sqlalchemy import or_ as _or
    stmt = select(Product).where(
        _or(Product.is_active.is_(True), Product.is_active.is_(None)),
        stock_tracked_product_condition(Product),
    )
    if q:
        stmt = stmt.where(Product.name.ilike(f"%{q}%") | Product.sku.ilike(f"%{q}%"))
    rows = (await db.execute(stmt.order_by(Product.name).limit(limit))).scalars().all()
    return {
        "items": [
            {
                "id":    p.id,
                "sku":   p.sku,
                "name":  p.name,
                "unit":  p.unit,
                "stock": float(p.stock or 0),
            }
            for p in rows
        ],
    }


@router.get("/api/location-stock")
async def location_stock_for_picker(
    product_id: int,
    location_id: int,
    db: AsyncSession = Depends(get_async_session),
):
    """Per-location stock lookup for the animals feeding form hint.

    Proxied here so users with page_animals don't also need page_inventory
    when checking available feed quantity at the selected storage.
    """
    from app.models.inventory import LocationStock
    row = (await db.execute(
        select(LocationStock).where(
            LocationStock.product_id == product_id,
            LocationStock.location_id == location_id,
        )
    )).scalar_one_or_none()
    qty = float(row.qty or 0) if row else 0.0
    return {
        "items": [
            {
                "product_id": product_id,
                "locations": [{"location_id": location_id, "qty": qty}],
            }
        ],
    }


# ── Cost Analyze API ────────────────────────────────────────────────

@router.get(
    "/api/analyze",
    dependencies=[Depends(require_permission("action_animals_analyze"))],
)
async def analyze_animals(
    db: AsyncSession = Depends(get_async_session),
):
    """Combined cost analysis across ALL animal groups.

    Components of the total cost:
      1. Purchase cost  — Σ over all groups (group.purchase_cost,
                          or cost_per_head × headcount if total is blank)
      2. Feed cost      — Σ over all feeding_logs of qty × product.cost
      3. Other expenses — Σ over expenses where is_animal_expense=True
                          OR animal_group_id IS NOT NULL  (covers both
                          manual "Animals" tagging AND auto-payroll
                          expenses linked to a specific group)

    Cost-per-head = Total ÷ sum of all groups' headcount.
    """
    # ── Groups (for purchase cost + per-group summary) ──
    groups = (
        await db.execute(
            select(AnimalGroup)
            .options(selectinload(AnimalGroup.farm))
            .where(AnimalGroup.status != "archived")
            .order_by(AnimalGroup.name.asc())
        )
    ).scalars().all()

    total_purchase = Decimal("0")
    total_head = 0
    groups_summary: list[dict] = []
    for g in groups:
        head = int(g.headcount or 0)
        total_head += head
        purchase_cost = Decimal(str(g.purchase_cost or 0))
        cost_per_head = Decimal(str(g.cost_per_head or 0))
        if purchase_cost > 0:
            effective = purchase_cost
            src = "total"
        elif cost_per_head > 0:
            effective = (cost_per_head * Decimal(head)).quantize(Decimal("0.01"))
            src = "per_head"
        else:
            effective = Decimal("0")
            src = "none"
        total_purchase += effective
        groups_summary.append({
            "id": g.id,
            "name": g.name,
            "animal_type": g.animal_type or "other",
            "headcount": head,
            "farm_name": g.farm.name if g.farm else None,
            "purchase_cost": float(effective),
            "purchase_source": src,
        })

    # ── Feed cost (qty × product.cost) across all groups ──
    feedings = (
        await db.execute(
            select(FeedingLog)
            .options(
                selectinload(FeedingLog.product),
                selectinload(FeedingLog.group),
            )
            .order_by(FeedingLog.feed_date.asc())
        )
    ).scalars().all()

    total_feed = Decimal("0")
    feed_qty_total = Decimal("0")
    feed_breakdown: dict[int, dict] = {}
    feed_by_group: dict[int, Decimal] = {}
    for f in feedings:
        qty = Decimal(str(f.qty or 0))
        unit_cost = Decimal(str(f.product.cost or 0)) if f.product else Decimal("0")
        line_cost = (qty * unit_cost).quantize(Decimal("0.01"))
        total_feed += line_cost
        feed_qty_total += qty
        feed_by_group[f.animal_group_id] = feed_by_group.get(f.animal_group_id, Decimal("0")) + line_cost
        pid = f.product_id
        if pid not in feed_breakdown:
            feed_breakdown[pid] = {
                "product_id":   pid,
                "product_name": f.product.name if f.product else f"#{pid}",
                "product_sku":  f.product.sku  if f.product else "",
                "unit":         f.product.unit if f.product else "",
                "total_qty":    Decimal("0"),
                "unit_cost":    float(unit_cost),
                "total_cost":   Decimal("0"),
                "entries":      0,
            }
        feed_breakdown[pid]["total_qty"]  += qty
        feed_breakdown[pid]["total_cost"] += line_cost
        feed_breakdown[pid]["entries"]    += 1

    feed_lines = sorted(
        (
            {
                "product_id":   info["product_id"],
                "product_name": info["product_name"],
                "product_sku":  info["product_sku"],
                "unit":         info["unit"],
                "total_qty":    float(info["total_qty"]),
                "unit_cost":    info["unit_cost"],
                "total_cost":   float(info["total_cost"]),
                "entries":      info["entries"],
            }
            for info in feed_breakdown.values()
        ),
        key=lambda r: r["total_cost"],
        reverse=True,
    )

    # Merge feed cost into per-group summary
    for row in groups_summary:
        row["feed_cost"] = float(feed_by_group.get(row["id"], Decimal("0")))

    # ── Other expenses tagged for Animals (manual or via payroll group) ──
    expenses = (
        await db.execute(
            select(Expense)
            .options(selectinload(Expense.category))
            .where(
                or_(
                    Expense.is_animal_expense == True,
                    Expense.animal_group_id.isnot(None),
                )
            )
            .order_by(Expense.expense_date.desc(), Expense.id.desc())
        )
    ).scalars().all()

    total_expense = Decimal("0")
    expense_by_cat: dict[str, dict] = {}
    expense_lines: list[dict] = []
    for e in expenses:
        amt = Decimal(str(e.amount or 0))
        total_expense += amt
        cat_name = e.category.name if e.category else "Other"
        if cat_name not in expense_by_cat:
            expense_by_cat[cat_name] = {"name": cat_name, "amount": Decimal("0"), "count": 0}
        expense_by_cat[cat_name]["amount"] += amt
        expense_by_cat[cat_name]["count"]  += 1
        expense_lines.append({
            "id":           e.id,
            "ref_number":   e.ref_number,
            "category":     cat_name,
            "expense_date": e.expense_date.isoformat() if e.expense_date else None,
            "amount":       float(amt),
            "vendor":       e.vendor or "",
            "description":  e.description or "",
            "source":       "payroll" if e.animal_group_id else "manual",
        })

    expense_categories = sorted(
        (
            {"name": v["name"], "amount": float(v["amount"]), "count": v["count"]}
            for v in expense_by_cat.values()
        ),
        key=lambda r: r["amount"],
        reverse=True,
    )

    # ── Totals ──
    total_cost = (total_purchase + total_feed + total_expense).quantize(Decimal("0.01"))
    per_head = (total_cost / Decimal(total_head)).quantize(Decimal("0.01")) if total_head > 0 else Decimal("0")

    def _pct(part: Decimal) -> float:
        if total_cost <= 0:
            return 0.0
        return round(float(part / total_cost * 100), 1)

    # Sort groups summary by total cost contribution (purchase + feed) desc
    for row in groups_summary:
        row["subtotal"] = round(row["purchase_cost"] + row["feed_cost"], 2)
    groups_summary.sort(key=lambda r: r["subtotal"], reverse=True)

    return {
        "totals": {
            "purchase_cost":  float(total_purchase),
            "feed_cost":      float(total_feed),
            "expense_cost":   float(total_expense),
            "total_cost":     float(total_cost),
            "cost_per_head":  float(per_head),
            "headcount":      total_head,
            "group_count":    len(groups),
        },
        "shares": {
            "purchase_pct": _pct(total_purchase),
            "feed_pct":     _pct(total_feed),
            "expense_pct":  _pct(total_expense),
        },
        "feedings": {
            "total_qty":   float(feed_qty_total),
            "entry_count": len(feedings),
            "by_product":  feed_lines,
        },
        "expenses": {
            "entry_count":  len(expenses),
            "by_category":  expense_categories,
            "items":        expense_lines[:50],   # cap detail list to most recent 50
        },
        "groups": groups_summary,
    }


# ── HTML Page ───────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def animals_page(current_user: User = Depends(require_permission("page_animals"))):
    header = render_app_header(current_user, active_permission="page_animals")
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Animals — AZed Farm</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {{
    --bg: #060810; --surface: #0a0d18; --card: #0f1424; --card2: #151c30;
    --border: rgba(255,255,255,0.06); --border2: rgba(255,255,255,0.11);
    --green: #00ff9d; --blue: #4d9fff; --amber: #ffb547; --danger: #ff4d6d;
    --text: #f0f4ff; --sub: #8899bb; --muted: #445066;
    --sans: 'Outfit',-apple-system,sans-serif; --mono: 'JetBrains Mono',monospace;
    --r: 10px;
}}
body[data-theme="light"], body.light {{
    --bg:#f4f5ef; --surface:#fbfaf5; --card:#fff; --card2:#f5f6f0;
    --border:rgba(0,0,0,0.06); --border2:rgba(0,0,0,0.11);
    --text:#1a1e14; --sub:#5a6051; --muted:#a0a596;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;font-size:14px}}
.container{{max-width:1400px;margin:0 auto;padding:24px}}
.page-head{{margin-bottom:20px}}
.page-title{{font-size:24px;font-weight:800;margin-bottom:4px}}
.page-sub{{color:var(--muted);font-size:13px}}
.stats-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}}
.stat-card{{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px}}
.stat-label{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;font-weight:700}}
.stat-value{{font-size:22px;font-weight:800;margin-top:4px;font-family:var(--mono)}}
.tabs{{display:flex;gap:6px;margin-bottom:16px;border-bottom:1px solid var(--border)}}
.tab{{padding:10px 18px;background:none;border:none;color:var(--sub);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}}
.tab:hover{{color:var(--text)}}
.tab.active{{color:var(--green);border-bottom-color:var(--green)}}
.toolbar{{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}}
.search-box{{flex:1;min-width:200px;position:relative}}
.search-box svg{{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--muted)}}
.search-box input{{width:100%;padding:10px 12px 10px 36px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-family:var(--sans);font-size:13px}}
.btn{{padding:10px 16px;background:var(--card);border:1px solid var(--border2);border-radius:var(--r);color:var(--text);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;transition:.15s}}
.btn:hover{{border-color:var(--green)}}
.btn-green{{background:var(--green);border-color:var(--green);color:#0a0c08}}
.btn-green:hover{{filter:brightness(1.1)}}
.btn-outline{{background:transparent}}
.btn-danger{{color:var(--danger);border-color:transparent}}
.btn-danger:hover{{border-color:var(--danger)}}
.table-wrap{{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;overflow-x:auto}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;padding:12px 14px;background:var(--card2);font-size:11px;text-transform:uppercase;letter-spacing:1px;font-weight:700;color:var(--muted);border-bottom:1px solid var(--border)}}
td{{padding:12px 14px;font-size:13px;border-bottom:1px solid var(--border)}}
tbody tr:last-child td{{border-bottom:none}}
tbody tr:hover{{background:rgba(255,255,255,0.02)}}
.badge{{display:inline-block;padding:3px 9px;border-radius:6px;font-size:11px;font-weight:700;font-family:var(--mono)}}
.badge.active{{background:rgba(0,255,157,.12);color:var(--green)}}
.badge.sold{{background:rgba(77,159,255,.12);color:var(--blue)}}
.badge.deceased{{background:rgba(255,77,109,.12);color:var(--danger)}}
.badge.archived{{background:rgba(255,255,255,.04);color:var(--muted)}}
.modal-bg{{position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.7);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center}}
.modal-bg.open{{display:flex}}
.modal{{background:var(--card);border:1px solid var(--border2);border-radius:16px;padding:28px;width:500px;max-width:95vw;max-height:92vh;overflow-y:auto;animation:in .2s ease}}
@keyframes in{{from{{opacity:0;transform:scale(.95)}}to{{opacity:1;transform:scale(1)}}}}
.modal-title{{font-size:18px;font-weight:800;margin-bottom:6px}}
.modal-sub{{font-size:13px;color:var(--muted);margin-bottom:20px}}
.fld{{display:flex;flex-direction:column;gap:6px;margin-bottom:14px}}
.fld label{{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted)}}
.fld input,.fld select,.fld textarea{{background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;width:100%;resize:vertical}}
.fld textarea{{min-height:60px}}
.modal-actions{{display:flex;gap:10px;margin-top:6px;justify-content:flex-end}}
.toast{{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--card);border:1px solid var(--border2);padding:12px 18px;border-radius:var(--r);font-size:13px;opacity:0;transition:.2s;z-index:1000;pointer-events:none}}
.toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}
.empty{{text-align:center;color:var(--muted);padding:50px;font-size:13px}}
.stock-hint{{font-size:11px;color:var(--muted);margin-top:4px}}
.product-picker{{position:relative}}
.product-picker-list{{position:absolute;left:0;right:0;top:calc(100% + 4px);z-index:600;max-height:260px;overflow-y:auto;background:var(--card);border:1px solid var(--border2);border-radius:10px;padding:4px;box-shadow:0 18px 40px rgba(0,0,0,.4);display:none}}
.product-picker-list.open{{display:block}}
.product-picker-item{{padding:9px 12px;cursor:pointer;font-size:13px;border-radius:7px;display:flex;align-items:center;gap:10px;color:var(--text)}}
.product-picker-item:hover,.product-picker-item.highlighted{{background:rgba(77,159,255,.14);color:var(--blue)}}
.product-picker-item .ppi-sku{{font-family:var(--mono);font-size:11px;color:var(--muted);margin-left:auto;white-space:nowrap}}
.product-picker-empty{{padding:14px;text-align:center;font-size:12px;color:var(--muted)}}
/* ── Analyze tab ── */
.an-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:16px}}
.an-card{{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px}}
.an-card .an-label{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;font-weight:700;margin-bottom:6px}}
.an-card .an-value{{font-size:22px;font-weight:800;font-family:var(--mono);color:var(--text)}}
.an-card .an-hint{{font-size:11px;color:var(--muted);margin-top:6px}}
.an-bar{{display:flex;height:14px;border-radius:7px;overflow:hidden;background:var(--card2);margin:14px 0 18px}}
.an-bar span{{display:block;height:100%}}
.an-bar .seg-purchase{{background:var(--blue)}}
.an-bar .seg-feed{{background:var(--green)}}
.an-bar .seg-expense{{background:var(--amber)}}
.an-legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--sub);margin-bottom:18px}}
.an-legend span i{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:middle}}
.an-section-title{{font-size:13px;font-weight:700;letter-spacing:.5px;color:var(--sub);text-transform:uppercase;margin:18px 0 10px}}
</style>
</head>
<body>
{header}

<div class="container">
    <div class="page-head">
        <div class="page-title">Animals</div>
        <div class="page-sub">Manage animal groups and track feed consumption.</div>
    </div>

    <div class="stats-row">
        <div class="stat-card">
            <div class="stat-label">Active Groups</div>
            <div class="stat-value" id="stat-groups">—</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Total Headcount</div>
            <div class="stat-value" id="stat-heads">—</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Feedings Today</div>
            <div class="stat-value" id="stat-today">—</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Deaths This Month</div>
            <div class="stat-value" id="stat-deaths">—</div>
        </div>
    </div>

    <div class="tabs">
        <button class="tab active" id="tab-groups"   onclick="switchTab('groups')">Animal Groups</button>
        <button class="tab"        id="tab-feedings" onclick="switchTab('feedings')">Feeding Log</button>
        <button class="tab"        id="tab-deaths"   onclick="switchTab('deaths')">Mortality Log</button>
        <button class="tab"        id="tab-receive"  onclick="switchTab('receive')">Receive</button>
        <button class="tab"        id="tab-analyze"  onclick="switchTab('analyze')">Analyze</button>
    </div>

    <!-- GROUPS -->
    <div id="groups-section">
        <div class="toolbar">
            <div class="search-box">
                <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                    <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
                </svg>
                <input id="g-search" placeholder="Search groups…" oninput="renderGroups()">
            </div>
            <button class="btn btn-green" onclick="openGroupModal()">+ Add Group</button>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr>
                    <th>Name</th><th>Type</th><th>Headcount</th><th>Farm</th>
                    <th>Status</th><th>Notes</th><th></th>
                </tr></thead>
                <tbody id="groups-body">
                    <tr><td colspan="7" class="empty">Loading…</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <!-- FEEDINGS -->
    <div id="feedings-section" style="display:none">
        <div class="toolbar">
            <div class="search-box">
                <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                    <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
                </svg>
                <input id="f-search" placeholder="Search by group, product, or note…" oninput="renderFeedings()">
            </div>
            <button class="btn btn-green" onclick="openFeedingModal()">+ Log Feeding</button>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr>
                    <th>Date</th><th>Group</th><th>Product</th>
                    <th>Storage</th><th>Qty</th><th>Note</th><th>By</th><th></th>
                </tr></thead>
                <tbody id="feedings-body">
                    <tr><td colspan="8" class="empty">Loading…</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <!-- DEATHS -->
    <div id="deaths-section" style="display:none">
        <div class="toolbar">
            <div class="search-box">
                <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                    <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
                </svg>
                <input id="d-search" placeholder="Search by group, cause, or note…" oninput="renderDeaths()">
            </div>
            <button class="btn btn-green" onclick="openDeathModal()">+ Log Death</button>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr>
                    <th>Date</th><th>Group</th><th>Count</th><th>Cause</th>
                    <th>Note</th><th>By</th><th></th>
                </tr></thead>
                <tbody id="deaths-body">
                    <tr><td colspan="7" class="empty">Loading…</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <!-- RECEIVE -->
    <div id="receive-section" style="display:none">
        <div class="toolbar">
            <div class="search-box">
                <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                    <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
                </svg>
                <input id="r-search" placeholder="Search by group, supplier, or note…" oninput="renderIntakes()">
            </div>
            <button class="btn btn-green" onclick="openReceiveModal()">+ Receive Animals</button>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr>
                    <th>Date</th><th>Group</th><th>Type</th><th>Count</th><th>Supplier</th>
                    <th>Cost</th><th>Note</th><th>By</th><th></th>
                </tr></thead>
                <tbody id="receive-body">
                    <tr><td colspan="9" class="empty">Loading…</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <!-- ANALYZE -->
    <div id="analyze-section" style="display:none">
        <div class="toolbar">
            <div style="flex:1;color:var(--sub);font-size:13px;line-height:1.5">
                <b style="color:var(--text)">Combined Animal Cost Analysis</b><br>
                <span style="color:var(--muted);font-size:12px">Across all active animal groups — purchase, feed, expenses tagged "Animals", and salaries of employees assigned to a group.</span>
            </div>
            <button class="btn btn-green" onclick="loadAnalysis()">↻ Refresh</button>
        </div>
        <div id="analyze-body">
            <div class="empty" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:50px">
                Loading analysis…
            </div>
        </div>
    </div>
</div>

<!-- GROUP MODAL -->
<div class="modal-bg" id="group-modal">
    <div class="modal">
        <div class="modal-title" id="gm-title">Add Group</div>
        <div class="modal-sub">A group is a herd, flock, or pen of animals.</div>
        <input type="hidden" id="gm-id" value="">
        <div class="fld">
            <label>Name</label>
            <input type="text" id="gm-name" placeholder="e.g. Dairy Cows" maxlength="150">
        </div>
        <div class="fld">
            <label>Type</label>
            <select id="gm-type">
                <option value="cattle">Cattle</option>
                <option value="poultry">Poultry</option>
                <option value="sheep">Sheep</option>
                <option value="goats">Goats</option>
                <option value="other" selected>Other</option>
            </select>
        </div>
        <div class="fld">
            <label>Headcount</label>
            <input type="number" id="gm-head" min="0" step="1" placeholder="0">
        </div>
        <div class="fld">
            <label>Purchase Cost — Total (EGP, optional)</label>
            <input type="number" id="gm-purchase-cost" min="0" step="0.01" placeholder="e.g. 50000">
            <div class="stock-hint">Total amount paid for the whole group. Leave blank if you use per-head pricing.</div>
        </div>
        <div class="fld">
            <label>Cost per Head (EGP, optional)</label>
            <input type="number" id="gm-cost-per-head" min="0" step="0.01" placeholder="e.g. 5000">
            <div class="stock-hint">Per-animal price. Used only when total purchase cost is blank.</div>
        </div>
        <div class="fld">
            <label>Farm (optional)</label>
            <select id="gm-farm"><option value="">— None —</option></select>
        </div>
        <div class="fld" id="gm-status-wrap" style="display:none">
            <label>Status</label>
            <select id="gm-status">
                <option value="active">Active</option>
                <option value="sold">Sold</option>
                <option value="deceased">Deceased</option>
            </select>
        </div>
        <div class="fld">
            <label>Notes (optional)</label>
            <textarea id="gm-notes" placeholder="Any details about this group"></textarea>
        </div>
        <div class="modal-actions">
            <button class="btn btn-outline" onclick="closeGroupModal()">Cancel</button>
            <button class="btn btn-green" id="gm-save" onclick="saveGroup()">Save</button>
        </div>
    </div>
</div>

<!-- FEEDING MODAL -->
<div class="modal-bg" id="feeding-modal">
    <div class="modal">
        <div class="modal-title">Log Feeding</div>
        <div class="modal-sub">Records consumption and decreases stock at the chosen storage.</div>
        <div class="fld">
            <label>Date</label>
            <input type="date" id="fm-date">
        </div>
        <div class="fld">
            <label>Animal Group</label>
            <select id="fm-group"><option value="">— Choose group —</option></select>
        </div>
        <div class="fld product-picker">
            <label>Product (feed)</label>
            <input type="hidden" id="fm-product" value="">
            <input type="text" id="fm-product-search"
                   placeholder="Type product name or SKU…"
                   autocomplete="off"
                   oninput="onProdInput()"
                   onfocus="onProdFocus()"
                   onkeydown="onProdKey(event)">
            <div class="product-picker-list" id="fm-product-list"></div>
        </div>
        <div class="fld">
            <label>Source Storage</label>
            <select id="fm-loc"><option value="">— Choose storage —</option></select>
            <div id="fm-stock-hint" class="stock-hint"></div>
        </div>
        <div class="fld">
            <label>Quantity</label>
            <input type="number" id="fm-qty" min="0.001" step="0.001" placeholder="e.g. 25">
        </div>
        <div class="fld">
            <label>Note (optional)</label>
            <input type="text" id="fm-note" maxlength="200" placeholder="e.g. Morning feed">
        </div>
        <div class="modal-actions">
            <button class="btn btn-outline" onclick="closeFeedingModal()">Cancel</button>
            <button class="btn btn-green" id="fm-save" onclick="saveFeeding()">Log</button>
        </div>
    </div>
</div>

<!-- DEATH MODAL -->
<div class="modal-bg" id="death-modal">
    <div class="modal">
        <div class="modal-title">Log Death</div>
        <div class="modal-sub">Records a death and reduces the group's headcount.</div>
        <div class="fld">
            <label>Date</label>
            <input type="date" id="dm-date">
        </div>
        <div class="fld">
            <label>Animal Group</label>
            <select id="dm-group"><option value="">— Choose group —</option></select>
            <div id="dm-head-hint" class="stock-hint"></div>
        </div>
        <div class="fld">
            <label>Count (how many died)</label>
            <input type="number" id="dm-count" min="1" step="1" value="1">
        </div>
        <div class="fld">
            <label>Cause</label>
            <select id="dm-cause">
                <option value="unknown">Unknown</option>
                <option value="illness">Illness / disease</option>
                <option value="injury">Injury</option>
                <option value="age">Old age</option>
                <option value="predator">Predator</option>
                <option value="weather">Weather / heat / cold</option>
                <option value="birth">Birth complications</option>
                <option value="other">Other (describe in note)</option>
            </select>
        </div>
        <div class="fld">
            <label>Note <span id="dm-note-req" style="color:var(--muted)">(optional)</span></label>
            <textarea id="dm-note" maxlength="500" placeholder="e.g. Found in pen this morning, no obvious symptoms"></textarea>
        </div>
        <div class="modal-actions">
            <button class="btn btn-outline" onclick="closeDeathModal()">Cancel</button>
            <button class="btn btn-green" id="dm-save" onclick="saveDeath()">Log</button>
        </div>
    </div>
</div>

<div class="modal-bg" id="receive-modal">
    <div class="modal">
        <div class="modal-title">Receive Animals</div>
        <div class="modal-sub">Adds animals to a group's headcount and (optionally) books the purchase cost.</div>
        <div class="fld">
            <label>Type</label>
            <select id="rm-type" onchange="onReceiveTypeChange()">
                <option value="purchase">Purchase (bought in)</option>
                <option value="birth">Birth (born on farm)</option>
                <option value="transfer">Transfer in</option>
                <option value="other">Other</option>
            </select>
        </div>
        <div class="fld">
            <label>Date</label>
            <input type="date" id="rm-date">
        </div>
        <div class="fld">
            <label>Animal Group</label>
            <select id="rm-group" onchange="onReceiveGroupChange()">
                <option value="">— Choose group —</option>
                <option value="__new__">+ Create a new group…</option>
            </select>
            <div id="rm-head-hint" class="stock-hint"></div>
        </div>
        <div id="rm-new-group-wrap" style="display:none">
            <div class="fld">
                <label>New Group Name</label>
                <input type="text" id="rm-new-name" maxlength="150" placeholder="e.g. Spring lambs 2026">
            </div>
            <div class="fld">
                <label>Type</label>
                <select id="rm-new-type">
                    <option value="cattle">Cattle</option>
                    <option value="poultry">Poultry</option>
                    <option value="sheep">Sheep</option>
                    <option value="goats">Goats</option>
                    <option value="other" selected>Other</option>
                </select>
            </div>
            <div class="fld">
                <label>Farm <span style="color:var(--muted)">(optional)</span></label>
                <select id="rm-new-farm"><option value="">— No farm —</option></select>
            </div>
        </div>
        <div class="fld">
            <label>Count (how many received)</label>
            <input type="number" id="rm-count" min="1" step="1" value="1" oninput="updateReceiveCostHint()">
        </div>
        <div class="fld">
            <label>Supplier / Source <span id="rm-source-label" style="color:var(--muted)">(optional)</span></label>
            <input type="text" id="rm-source" maxlength="150" placeholder="e.g. Nile Valley Livestock">
        </div>
        <div id="rm-cost-wrap">
            <div class="fld">
                <label>Cost per head <span style="color:var(--muted)">(optional, EGP)</span></label>
                <input type="number" id="rm-unit" min="0" step="0.01" placeholder="0.00" oninput="updateReceiveCostHint()">
            </div>
            <div class="fld">
                <label>Total cost <span style="color:var(--muted)">(optional, EGP — overrides per-head)</span></label>
                <input type="number" id="rm-total" min="0" step="0.01" placeholder="0.00" oninput="updateReceiveCostHint()">
                <div id="rm-cost-hint" class="stock-hint"></div>
            </div>
        </div>
        <div class="fld">
            <label>Note <span style="color:var(--muted)">(optional)</span></label>
            <textarea id="rm-note" maxlength="500" placeholder="e.g. Vaccinated on arrival"></textarea>
        </div>
        <div class="modal-actions">
            <button class="btn btn-outline" onclick="closeReceiveModal()">Cancel</button>
            <button class="btn btn-green" id="rm-save" onclick="saveReceive()">Receive</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script src="/static/auth-guard.js"></script>
<script>
/* ── STATE ── */
let _groups = [];
let _feedings = [];
let _deaths = [];
let _intakes = [];
let _farms = [];
let _products = [];
let _locations = [];

function esc(s){{ return String(s ?? "").replace(/[&<>"]/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}}[c])); }}

function showToast(msg){{
    const t = document.getElementById("toast");
    t.innerText = msg;
    t.classList.add("show");
    setTimeout(()=>t.classList.remove("show"), 3000);
}}

/* ── TABS ── */
function switchTab(tab){{
    document.getElementById("tab-groups").classList.toggle("active",   tab==="groups");
    document.getElementById("tab-feedings").classList.toggle("active", tab==="feedings");
    document.getElementById("tab-deaths").classList.toggle("active",   tab==="deaths");
    document.getElementById("tab-receive").classList.toggle("active",  tab==="receive");
    document.getElementById("tab-analyze").classList.toggle("active",  tab==="analyze");
    document.getElementById("groups-section").style.display   = tab==="groups"   ? "" : "none";
    document.getElementById("feedings-section").style.display = tab==="feedings" ? "" : "none";
    document.getElementById("deaths-section").style.display   = tab==="deaths"   ? "" : "none";
    document.getElementById("receive-section").style.display  = tab==="receive"  ? "" : "none";
    document.getElementById("analyze-section").style.display  = tab==="analyze"  ? "" : "none";
    if (tab==="feedings") loadFeedings();
    if (tab==="deaths")   loadDeaths();
    if (tab==="receive")  loadIntakes();
    if (tab==="analyze")  initAnalyze();
}}

/* ── INIT ── */
async function init(){{
    await Promise.all([loadGroups(), loadFarmsAndLocations(), loadProducts(), loadDeaths({{silent:true}})]);
}}

async function loadFarmsAndLocations(){{
    try {{
        const [fr, lr] = await Promise.all([
            fetch("/animals/api/farms"),
            fetch("/animals/api/locations"),
        ]);
        if (fr.ok) {{
            const d = await fr.json();
            _farms = (d && d.items) ? d.items : [];
        }}
        if (lr.ok) {{
            const d = await lr.json();
            _locations = (d && d.items) ? d.items : (Array.isArray(d) ? d : []);
        }}
    }} catch(_) {{}}
}}

async function loadProducts(){{
    try {{
        const r = await fetch("/animals/api/products?limit=2000");
        if (r.ok) {{
            const d = await r.json();
            _products = (d && d.items) ? d.items : [];
        }}
    }} catch(_) {{}}
}}

async function loadGroups(){{
    try {{
        const r = await fetch("/animals/api/groups");
        if (!r.ok) {{
            document.getElementById("groups-body").innerHTML = `<tr><td colspan="7" class="empty">Could not load.</td></tr>`;
            return;
        }}
        const d = await r.json();
        _groups = (d && d.items) ? d.items : [];
        updateStats();
        renderGroups();
    }} catch(_) {{
        document.getElementById("groups-body").innerHTML = `<tr><td colspan="7" class="empty">Error.</td></tr>`;
    }}
}}

async function loadFeedings(){{
    try {{
        const r = await fetch("/animals/api/feedings?limit=200");
        if (!r.ok) {{
            document.getElementById("feedings-body").innerHTML = `<tr><td colspan="8" class="empty">Could not load.</td></tr>`;
            return;
        }}
        const d = await r.json();
        _feedings = (d && d.items) ? d.items : [];
        updateStats();
        renderFeedings();
    }} catch(_) {{
        document.getElementById("feedings-body").innerHTML = `<tr><td colspan="8" class="empty">Error.</td></tr>`;
    }}
}}

async function loadDeaths(opts){{
    const silent = opts && opts.silent;
    try {{
        const r = await fetch("/animals/api/deaths?limit=200");
        if (!r.ok) {{
            if (!silent) document.getElementById("deaths-body").innerHTML = `<tr><td colspan="7" class="empty">Could not load.</td></tr>`;
            return;
        }}
        const d = await r.json();
        _deaths = (d && d.items) ? d.items : [];
        updateStats();
        if (!silent) renderDeaths();
    }} catch(_) {{
        if (!silent) document.getElementById("deaths-body").innerHTML = `<tr><td colspan="7" class="empty">Error.</td></tr>`;
    }}
}}

const CAUSE_LABELS = {{
    illness:  "Illness / disease",
    injury:   "Injury",
    age:      "Old age",
    predator: "Predator",
    weather:  "Weather",
    birth:    "Birth complications",
    unknown:  "Unknown",
    other:    "Other",
}};

function renderDeaths(){{
    const q = (document.getElementById("d-search").value || "").toLowerCase().trim();
    const tbody = document.getElementById("deaths-body");
    const filtered = q
        ? _deaths.filter(d => (d.group_name||"").toLowerCase().includes(q)
                           || (d.cause||"").toLowerCase().includes(q)
                           || (d.note||"").toLowerCase().includes(q))
        : _deaths;
    if (!filtered.length) {{
        tbody.innerHTML = `<tr><td colspan="7" class="empty">No deaths recorded. Click "+ Log Death" to add one.</td></tr>`;
        return;
    }}
    tbody.innerHTML = filtered.map(d => `
        <tr>
            <td style="color:var(--sub)">${{esc(d.death_date||"")}}</td>
            <td><b>${{esc(d.group_name||"")}}</b></td>
            <td style="font-family:var(--mono)">${{d.count||0}}</td>
            <td style="color:var(--sub)">${{esc(CAUSE_LABELS[d.cause] || d.cause || "—")}}</td>
            <td style="color:var(--sub);max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{esc(d.note||"")}}">${{esc(d.note||"—")}}</td>
            <td style="color:var(--muted)">${{esc(d.user_name||"—")}}</td>
            <td style="text-align:right">
                <button class="btn btn-danger" onclick="deleteDeath(${{d.id}})">Delete</button>
            </td>
        </tr>
    `).join("");
}}

/* ── DEATH MODAL ── */
function openDeathModal(){{
    document.getElementById("dm-date").value = new Date().toISOString().slice(0,10);
    const sel = document.getElementById("dm-group");
    sel.innerHTML = `<option value="">— Choose group —</option>` +
        _groups.filter(g => g.status === "active")
               .map(g => `<option value="${{g.id}}" data-head="${{g.headcount||0}}">${{esc(g.name)}} (${{g.headcount||0}} head)</option>`)
               .join("");
    document.getElementById("dm-count").value = "1";
    document.getElementById("dm-cause").value = "unknown";
    document.getElementById("dm-note").value = "";
    document.getElementById("dm-head-hint").textContent = "";
    updateDeathNoteRequirement();
    sel.onchange = function(){{
        const opt = sel.options[sel.selectedIndex];
        const head = opt ? parseInt(opt.dataset.head, 10) || 0 : 0;
        document.getElementById("dm-head-hint").textContent = head ? `Current headcount: ${{head}}` : "";
    }};
    document.getElementById("dm-cause").onchange = updateDeathNoteRequirement;
    document.getElementById("death-modal").classList.add("open");
}}

function closeDeathModal(){{
    document.getElementById("death-modal").classList.remove("open");
}}

function updateDeathNoteRequirement(){{
    const cause = document.getElementById("dm-cause").value;
    const req   = document.getElementById("dm-note-req");
    if (cause === "other") {{
        req.textContent = "(required for 'other')";
        req.style.color = "var(--danger)";
    }} else {{
        req.textContent = "(optional)";
        req.style.color = "var(--muted)";
    }}
}}

async function saveDeath(){{
    const animal_group_id = parseInt(document.getElementById("dm-group").value, 10) || 0;
    const death_date      = document.getElementById("dm-date").value;
    const count           = parseInt(document.getElementById("dm-count").value, 10) || 0;
    const cause           = document.getElementById("dm-cause").value;
    const note            = document.getElementById("dm-note").value.trim() || null;

    if (!animal_group_id) {{ showToast("Pick a group"); return; }}
    if (!death_date) {{ showToast("Pick a date"); return; }}
    if (count < 1) {{ showToast("Count must be at least 1"); return; }}
    if (cause === "other" && !note) {{ showToast("A note is required for 'other' cause"); return; }}

    const btn = document.getElementById("dm-save");
    btn.disabled = true;
    try {{
        const r = await fetch("/animals/api/deaths", {{
            method: "POST", headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({{animal_group_id, death_date, count, cause, note}}),
        }});
        if (!r.ok) {{
            const err = await r.json().catch(()=>({{}}));
            showToast(err.detail || "Could not log death");
        }} else {{
            showToast("Death logged");
            closeDeathModal();
            loadDeaths();
            loadGroups();  // headcount changed
        }}
    }} finally {{
        btn.disabled = false;
    }}
}}

async function deleteDeath(id){{
    if (!confirm("Reverse this mortality entry? Headcount will be restored.")) return;
    const r = await fetch(`/animals/api/deaths/${{id}}`, {{method: "DELETE"}});
    if (!r.ok) {{
        const err = await r.json().catch(()=>({{}}));
        showToast(err.detail || "Could not delete");
    }} else {{
        showToast("Entry reversed");
        loadDeaths();
        loadGroups();
    }}
}}

/* ── RECEIVE (INTAKE) ── */
async function loadIntakes(opts){{
    const silent = opts && opts.silent;
    try {{
        const r = await fetch("/animals/api/intakes?limit=200");
        if (!r.ok) {{
            if (!silent) document.getElementById("receive-body").innerHTML = `<tr><td colspan="9" class="empty">Could not load.</td></tr>`;
            return;
        }}
        const d = await r.json();
        _intakes = (d && d.items) ? d.items : [];
        if (!silent) renderIntakes();
    }} catch(_) {{
        if (!silent) document.getElementById("receive-body").innerHTML = `<tr><td colspan="9" class="empty">Error.</td></tr>`;
    }}
}}

const INTAKE_TYPE_LABELS = {{
    purchase: "Purchase",
    birth:    "Birth",
    transfer: "Transfer in",
    other:    "Other",
}};

function renderIntakes(){{
    const q = (document.getElementById("r-search").value || "").toLowerCase().trim();
    const tbody = document.getElementById("receive-body");
    const filtered = q
        ? _intakes.filter(i => (i.group_name||"").toLowerCase().includes(q)
                            || (i.source||"").toLowerCase().includes(q)
                            || (i.intake_type||"").toLowerCase().includes(q)
                            || (i.note||"").toLowerCase().includes(q))
        : _intakes;
    if (!filtered.length) {{
        tbody.innerHTML = `<tr><td colspan="9" class="empty">No animals received yet. Click "+ Receive Animals" to add a batch.</td></tr>`;
        return;
    }}
    tbody.innerHTML = filtered.map(i => `
        <tr>
            <td style="color:var(--sub)">${{esc(i.intake_date||"")}}</td>
            <td><b>${{esc(i.group_name||"")}}</b></td>
            <td style="color:var(--sub)">${{esc(INTAKE_TYPE_LABELS[i.intake_type] || i.intake_type || "—")}}</td>
            <td style="font-family:var(--mono)">${{i.count||0}}</td>
            <td style="color:var(--sub)">${{esc(i.source||"—")}}</td>
            <td style="font-family:var(--mono);color:var(--sub)">${{i.total_cost!=null ? Number(i.total_cost).toLocaleString(undefined,{{minimumFractionDigits:2}})+" EGP" : "—"}}</td>
            <td style="color:var(--sub);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{esc(i.note||"")}}">${{esc(i.note||"—")}}</td>
            <td style="color:var(--muted)">${{esc(i.user_name||"—")}}</td>
            <td style="text-align:right">
                <button class="btn btn-danger" onclick="deleteIntake(${{i.id}})">Undo</button>
            </td>
        </tr>
    `).join("");
}}

function onReceiveGroupChange(){{
    const sel = document.getElementById("rm-group");
    const isNew = sel.value === "__new__";
    document.getElementById("rm-new-group-wrap").style.display = isNew ? "" : "none";
    const opt = sel.options[sel.selectedIndex];
    const head = (opt && opt.dataset && opt.dataset.head) ? parseInt(opt.dataset.head,10)||0 : 0;
    document.getElementById("rm-head-hint").textContent = (!isNew && sel.value) ? `Current headcount: ${{head}}` : "";
}}

function updateReceiveCostHint(){{
    const count = parseInt(document.getElementById("rm-count").value,10) || 0;
    const unit  = parseFloat(document.getElementById("rm-unit").value) || 0;
    const total = parseFloat(document.getElementById("rm-total").value) || 0;
    const hint  = document.getElementById("rm-cost-hint");
    let effective = total > 0 ? total : (unit > 0 ? unit * count : 0);
    hint.textContent = effective > 0
        ? `Will book a Livestock Purchase expense of ${{effective.toLocaleString(undefined,{{minimumFractionDigits:2}})}} EGP`
        : "No cost will be recorded (headcount only)";
}}

function openReceiveModal(){{
    document.getElementById("rm-date").value = new Date().toISOString().slice(0,10);
    const sel = document.getElementById("rm-group");
    sel.innerHTML = `<option value="">— Choose group —</option>` +
        `<option value="__new__">+ Create a new group…</option>` +
        _groups.filter(g => g.status === "active")
               .map(g => `<option value="${{g.id}}" data-head="${{g.headcount||0}}">${{esc(g.name)}} (${{g.headcount||0}} head)</option>`)
               .join("");
    // Farm options for a new group (reuse the farm list loaded for feedings).
    const farmSel = document.getElementById("rm-new-farm");
    farmSel.innerHTML = `<option value="">— No farm —</option>` +
        (_farms||[]).map(f => `<option value="${{f.id}}">${{esc(f.name)}}</option>`).join("");
    document.getElementById("rm-new-group-wrap").style.display = "none";
    document.getElementById("rm-new-name").value = "";
    document.getElementById("rm-new-type").value = "other";
    document.getElementById("rm-count").value = "1";
    document.getElementById("rm-source").value = "";
    document.getElementById("rm-unit").value = "";
    document.getElementById("rm-total").value = "";
    document.getElementById("rm-note").value = "";
    document.getElementById("rm-head-hint").textContent = "";
    document.getElementById("rm-type").value = "purchase";
    onReceiveTypeChange();
    updateReceiveCostHint();
    document.getElementById("receive-modal").classList.add("open");
}}

function onReceiveTypeChange(){{
    const type = document.getElementById("rm-type").value;
    const isBirth = type === "birth";
    // Births have no purchase cost — hide the cost fields and clear them.
    document.getElementById("rm-cost-wrap").style.display = isBirth ? "none" : "";
    if (isBirth) {{
        document.getElementById("rm-unit").value = "";
        document.getElementById("rm-total").value = "";
    }}
    document.getElementById("rm-source-label").textContent =
        isBirth ? "(optional — e.g. dam / line)" : "(optional)";
    updateReceiveCostHint();
}}

function closeReceiveModal(){{
    document.getElementById("receive-modal").classList.remove("open");
}}

async function saveReceive(){{
    const sel = document.getElementById("rm-group");
    const intake_type = document.getElementById("rm-type").value;
    const intake_date = document.getElementById("rm-date").value;
    const count = parseInt(document.getElementById("rm-count").value,10) || 0;
    const source = document.getElementById("rm-source").value.trim() || null;
    const isBirth = intake_type === "birth";
    const unitRaw  = parseFloat(document.getElementById("rm-unit").value);
    const totalRaw = parseFloat(document.getElementById("rm-total").value);
    const unit_cost  = (isBirth || isNaN(unitRaw))  ? null : unitRaw;
    const total_cost = (isBirth || isNaN(totalRaw)) ? null : totalRaw;
    const note = document.getElementById("rm-note").value.trim() || null;

    if (!intake_date) {{ showToast("Pick a date"); return; }}
    if (count < 1) {{ showToast("Count must be at least 1"); return; }}

    const payload = {{intake_type, intake_date, count, source, unit_cost, total_cost, note}};
    if (sel.value === "__new__") {{
        const name = document.getElementById("rm-new-name").value.trim();
        if (!name) {{ showToast("Enter a name for the new group"); return; }}
        payload.new_group_name = name;
        payload.new_group_type = document.getElementById("rm-new-type").value;
        const farm = document.getElementById("rm-new-farm").value;
        payload.farm_id = farm ? parseInt(farm,10) : null;
    }} else if (sel.value) {{
        payload.animal_group_id = parseInt(sel.value,10);
    }} else {{
        showToast("Choose a group or create a new one"); return;
    }}

    const btn = document.getElementById("rm-save");
    btn.disabled = true;
    try {{
        const r = await fetch("/animals/api/intakes", {{
            method: "POST", headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify(payload),
        }});
        if (!r.ok) {{
            const err = await r.json().catch(()=>({{}}));
            showToast(err.detail || "Could not receive animals");
        }} else {{
            showToast("Animals received");
            closeReceiveModal();
            loadIntakes();
            loadGroups();  // headcount changed
        }}
    }} finally {{
        btn.disabled = false;
    }}
}}

async function deleteIntake(id){{
    if (!confirm("Undo this receipt? Headcount will be reduced and the purchase expense reversed.")) return;
    const r = await fetch(`/animals/api/intakes/${{id}}`, {{method: "DELETE"}});
    if (!r.ok) {{
        const err = await r.json().catch(()=>({{}}));
        showToast(err.detail || "Could not undo");
    }} else {{
        showToast("Receipt undone");
        loadIntakes();
        loadGroups();
    }}
}}

function updateStats(){{
    const activeGroups = _groups.filter(g => g.status === "active");
    document.getElementById("stat-groups").textContent = activeGroups.length;
    document.getElementById("stat-heads").textContent = activeGroups.reduce((s,g)=>s+(g.headcount||0), 0);
    const today = new Date().toISOString().slice(0,10);
    const todayCount = _feedings.filter(f => f.feed_date === today).length;
    document.getElementById("stat-today").textContent = todayCount;
    // Deaths this month
    const ym = today.slice(0,7);  // "2026-05"
    const deathsThisMonth = _deaths
        .filter(d => (d.death_date||"").startsWith(ym))
        .reduce((s,d) => s + (d.count||0), 0);
    const elDeaths = document.getElementById("stat-deaths");
    if (elDeaths) elDeaths.textContent = deathsThisMonth;
}}

function renderGroups(){{
    const q = (document.getElementById("g-search").value || "").toLowerCase().trim();
    const tbody = document.getElementById("groups-body");
    const filtered = q
        ? _groups.filter(g => (g.name||"").toLowerCase().includes(q) || (g.animal_type||"").toLowerCase().includes(q) || (g.farm_name||"").toLowerCase().includes(q))
        : _groups;
    if (!filtered.length) {{
        tbody.innerHTML = `<tr><td colspan="7" class="empty">No groups. Click "+ Add Group" to create one.</td></tr>`;
        return;
    }}
    tbody.innerHTML = filtered.map(g => `
        <tr>
            <td><b>${{esc(g.name)}}</b></td>
            <td style="color:var(--sub)">${{esc(g.animal_type)}}</td>
            <td style="font-family:var(--mono)">${{g.headcount||0}}</td>
            <td style="color:var(--sub)">${{esc(g.farm_name||"—")}}</td>
            <td><span class="badge ${{g.status}}">${{esc(g.status)}}</span></td>
            <td style="color:var(--sub);max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{esc(g.notes||"")}}">${{esc(g.notes||"—")}}</td>
            <td style="text-align:right">
                <button class="btn btn-outline" onclick="openGroupModal(${{g.id}})">Edit</button>
                ${{g.status !== "archived" ? `<button class="btn btn-danger" onclick="archiveGroup(${{g.id}}, '${{esc(g.name).replace(/'/g, "&#39;")}}')">Archive</button>` : ""}}
            </td>
        </tr>
    `).join("");
}}

function renderFeedings(){{
    const q = (document.getElementById("f-search").value || "").toLowerCase().trim();
    const tbody = document.getElementById("feedings-body");
    const filtered = q
        ? _feedings.filter(f => (f.group_name||"").toLowerCase().includes(q) || (f.product_name||"").toLowerCase().includes(q) || (f.note||"").toLowerCase().includes(q))
        : _feedings;
    if (!filtered.length) {{
        tbody.innerHTML = `<tr><td colspan="8" class="empty">No feedings yet. Click "+ Log Feeding" to record one.</td></tr>`;
        return;
    }}
    tbody.innerHTML = filtered.map(f => `
        <tr>
            <td style="color:var(--sub)">${{esc(f.feed_date||"")}}</td>
            <td><b>${{esc(f.group_name||"")}}</b></td>
            <td>
                <div>${{esc(f.product_name||"")}}</div>
                <div style="font-family:var(--mono);font-size:11px;color:var(--muted)">${{esc(f.product_sku||"")}}</div>
            </td>
            <td style="color:var(--sub)">${{esc(f.location_name||"")}}</td>
            <td style="font-family:var(--mono)">${{parseFloat(f.qty).toFixed(3)}}</td>
            <td style="color:var(--sub);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{esc(f.note||"")}}">${{esc(f.note||"—")}}</td>
            <td style="color:var(--muted)">${{esc(f.user_name||"—")}}</td>
            <td style="text-align:right">
                <button class="btn btn-danger" onclick="deleteFeeding(${{f.id}})">Delete</button>
            </td>
        </tr>
    `).join("");
}}

/* ── GROUP MODAL ── */
function openGroupModal(id){{
    const isEdit = !!id;
    document.getElementById("gm-title").textContent = isEdit ? "Edit Group" : "Add Group";
    document.getElementById("gm-id").value = id || "";
    const farmSel = document.getElementById("gm-farm");
    farmSel.innerHTML = `<option value="">— None —</option>` +
        _farms.map(f => `<option value="${{f.id}}">${{esc(f.name)}}</option>`).join("");
    const statusWrap = document.getElementById("gm-status-wrap");
    if (isEdit) {{
        const g = _groups.find(x => x.id === id);
        if (g) {{
            document.getElementById("gm-name").value = g.name || "";
            document.getElementById("gm-type").value = g.animal_type || "other";
            document.getElementById("gm-head").value = g.headcount || 0;
            document.getElementById("gm-purchase-cost").value = g.purchase_cost != null ? g.purchase_cost : "";
            document.getElementById("gm-cost-per-head").value = g.cost_per_head != null ? g.cost_per_head : "";
            document.getElementById("gm-farm").value = g.farm_id || "";
            document.getElementById("gm-status").value = g.status === "archived" ? "active" : (g.status || "active");
            document.getElementById("gm-notes").value = g.notes || "";
            statusWrap.style.display = "";
        }}
    }} else {{
        document.getElementById("gm-name").value = "";
        document.getElementById("gm-type").value = "other";
        document.getElementById("gm-head").value = "";
        document.getElementById("gm-purchase-cost").value = "";
        document.getElementById("gm-cost-per-head").value = "";
        document.getElementById("gm-farm").value = "";
        document.getElementById("gm-notes").value = "";
        statusWrap.style.display = "none";
    }}
    document.getElementById("group-modal").classList.add("open");
}}

function closeGroupModal(){{
    document.getElementById("group-modal").classList.remove("open");
}}

async function saveGroup(){{
    const id = document.getElementById("gm-id").value;
    const name = document.getElementById("gm-name").value.trim();
    if (!name) {{ showToast("Group name is required"); return; }}
    const purchaseRaw = document.getElementById("gm-purchase-cost").value;
    const perHeadRaw  = document.getElementById("gm-cost-per-head").value;
    const purchaseCost = purchaseRaw === "" ? null : (parseFloat(purchaseRaw) || 0);
    const perHeadCost  = perHeadRaw  === "" ? null : (parseFloat(perHeadRaw)  || 0);
    const payload = {{
        name: name,
        animal_type: document.getElementById("gm-type").value,
        headcount: parseInt(document.getElementById("gm-head").value, 10) || 0,
        farm_id: parseInt(document.getElementById("gm-farm").value, 10) || null,
        notes: document.getElementById("gm-notes").value.trim() || null,
        purchase_cost: purchaseCost,
        cost_per_head: perHeadCost,
    }};
    if (id) {{
        payload.status = document.getElementById("gm-status").value;
    }}
    const btn = document.getElementById("gm-save");
    btn.disabled = true;
    try {{
        const url    = id ? `/animals/api/groups/${{id}}` : `/animals/api/groups`;
        const method = id ? "PUT" : "POST";
        const r = await fetch(url, {{
            method, headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify(payload),
        }});
        if (!r.ok) {{
            const err = await r.json().catch(()=>({{}}));
            showToast(err.detail || "Could not save");
        }} else {{
            showToast(id ? "Group updated" : "Group created");
            closeGroupModal();
            loadGroups();
        }}
    }} finally {{
        btn.disabled = false;
    }}
}}

async function archiveGroup(id, name){{
    if (!confirm(`Archive group "${{name}}"? Feeding history will be kept.`)) return;
    const r = await fetch(`/animals/api/groups/${{id}}`, {{method: "DELETE"}});
    if (!r.ok) {{
        const err = await r.json().catch(()=>({{}}));
        showToast(err.detail || "Could not archive");
    }} else {{
        showToast("Group archived");
        loadGroups();
    }}
}}

/* ── FEEDING MODAL ── */
function openFeedingModal(){{
    document.getElementById("fm-date").value = new Date().toISOString().slice(0,10);
    document.getElementById("fm-group").innerHTML = `<option value="">— Choose group —</option>` +
        _groups.filter(g => g.status === "active").map(g => `<option value="${{g.id}}">${{esc(g.name)}}</option>`).join("");
    document.getElementById("fm-loc").innerHTML = `<option value="">— Choose storage —</option>` +
        _locations.filter(l => l.is_active !== false).map(l => `<option value="${{l.id}}">${{esc(l.name)}}</option>`).join("");
    document.getElementById("fm-product").value = "";
    document.getElementById("fm-product-search").value = "";
    document.getElementById("fm-qty").value = "";
    document.getElementById("fm-note").value = "";
    document.getElementById("fm-stock-hint").textContent = "";
    document.getElementById("fm-product-list").classList.remove("open");
    document.getElementById("feeding-modal").classList.add("open");
    document.getElementById("fm-loc").onchange = updateStockHint;
}}

function closeFeedingModal(){{
    document.getElementById("feeding-modal").classList.remove("open");
}}

let _highlight = 0;
let _filtered = [];

function renderPickerList(items){{
    const list = document.getElementById("fm-product-list");
    _filtered = items;
    _highlight = 0;
    if (!items.length) {{
        list.innerHTML = `<div class="product-picker-empty">No products match.</div>`;
        list.classList.add("open");
        return;
    }}
    list.innerHTML = items.slice(0, 50).map((p,i) => `
        <div class="product-picker-item${{i===0?' highlighted':''}}" data-id="${{p.id}}" onclick="pickProduct(${{p.id}})">
            <span>${{esc(p.name)}}</span>
            <span class="ppi-sku">${{esc(p.sku||"")}}</span>
        </div>
    `).join("");
    list.classList.add("open");
}}

function onProdInput(){{
    const q = document.getElementById("fm-product-search").value.trim().toLowerCase();
    document.getElementById("fm-product").value = "";
    if (!q) {{ renderPickerList(_products); return; }}
    const f = _products.filter(p => (p.name||"").toLowerCase().includes(q) || (p.sku||"").toLowerCase().includes(q));
    renderPickerList(f);
}}

function onProdFocus(){{ onProdInput(); }}

function pickProduct(id){{
    const p = _products.find(x => x.id === id);
    if (!p) return;
    document.getElementById("fm-product").value = String(p.id);
    document.getElementById("fm-product-search").value = `${{p.name}} (${{p.sku||""}})`;
    document.getElementById("fm-product-list").classList.remove("open");
    updateStockHint();
}}

function onProdKey(ev){{
    const list  = document.getElementById("fm-product-list");
    const items = list.querySelectorAll(".product-picker-item");
    if (ev.key === "ArrowDown") {{
        ev.preventDefault();
        if (!items.length) return;
        _highlight = Math.min(_highlight + 1, items.length - 1);
        items.forEach((el,i) => el.classList.toggle("highlighted", i === _highlight));
        items[_highlight].scrollIntoView({{block:"nearest"}});
    }} else if (ev.key === "ArrowUp") {{
        ev.preventDefault();
        if (!items.length) return;
        _highlight = Math.max(_highlight - 1, 0);
        items.forEach((el,i) => el.classList.toggle("highlighted", i === _highlight));
        items[_highlight].scrollIntoView({{block:"nearest"}});
    }} else if (ev.key === "Enter") {{
        ev.preventDefault();
        if (items.length && _filtered[_highlight]) pickProduct(_filtered[_highlight].id);
    }} else if (ev.key === "Escape") {{
        list.classList.remove("open");
    }}
}}

document.addEventListener("click", function(e){{
    if (!e.target.closest(".product-picker")) {{
        const l = document.getElementById("fm-product-list");
        if (l) l.classList.remove("open");
    }}
}});

async function updateStockHint(){{
    const pid = parseInt(document.getElementById("fm-product").value, 10) || 0;
    const lid = parseInt(document.getElementById("fm-loc").value, 10) || 0;
    const hint = document.getElementById("fm-stock-hint");
    if (!pid || !lid) {{ hint.textContent = ""; return; }}
    try {{
        const r = await fetch(`/animals/api/location-stock?product_id=${{pid}}&location_id=${{lid}}`);
        if (r.ok) {{
            const d = await r.json();
            const items = (d && d.items) ? d.items : [];
            let qty = 0;
            if (items.length && Array.isArray(items[0].locations)) {{
                const loc = items[0].locations.find(x => x.location_id === lid);
                if (loc) qty = parseFloat(loc.qty || 0);
            }}
            hint.textContent = `Available at this storage: ${{qty.toFixed(3)}}`;
        }}
    }} catch(_) {{}}
}}

async function saveFeeding(){{
    const animal_group_id = parseInt(document.getElementById("fm-group").value, 10) || 0;
    const product_id      = parseInt(document.getElementById("fm-product").value, 10) || 0;
    const location_id     = parseInt(document.getElementById("fm-loc").value, 10) || 0;
    const qty             = parseFloat(document.getElementById("fm-qty").value) || 0;
    const feed_date       = document.getElementById("fm-date").value;
    const note            = document.getElementById("fm-note").value.trim() || null;

    if (!animal_group_id) {{ showToast("Pick a group"); return; }}
    if (!product_id) {{ showToast("Pick a product"); return; }}
    if (!location_id) {{ showToast("Pick a storage"); return; }}
    if (qty <= 0) {{ showToast("Quantity must be > 0"); return; }}
    if (!feed_date) {{ showToast("Pick a date"); return; }}

    const btn = document.getElementById("fm-save");
    btn.disabled = true;
    try {{
        const r = await fetch("/animals/api/feedings", {{
            method: "POST", headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({{animal_group_id, product_id, location_id, qty, feed_date, note}}),
        }});
        if (!r.ok) {{
            const err = await r.json().catch(()=>({{}}));
            showToast(err.detail || "Could not log feeding");
        }} else {{
            showToast("Feeding logged");
            closeFeedingModal();
            loadFeedings();
            loadGroups();
            loadProducts();  // refresh product stock totals
        }}
    }} finally {{
        btn.disabled = false;
    }}
}}

async function deleteFeeding(id){{
    if (!confirm("Delete this feeding entry? Stock will be restored.")) return;
    const r = await fetch(`/animals/api/feedings/${{id}}`, {{method: "DELETE"}});
    if (!r.ok) {{
        const err = await r.json().catch(()=>({{}}));
        showToast(err.detail || "Could not delete");
    }} else {{
        showToast("Feeding reversed");
        loadFeedings();
        loadProducts();
    }}
}}

/* ── ANALYZE TAB (Combined Animals) ── */
function initAnalyze(){{
    // Single combined view — no picker. Load fresh data every time the tab is opened.
    loadAnalysis();
}}

function fmtMoney(n){{
    const v = Number(n || 0);
    return v.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
}}

function fmtQty(n){{
    const v = Number(n || 0);
    return v.toLocaleString(undefined, {{minimumFractionDigits: 0, maximumFractionDigits: 3}});
}}

async function loadAnalysis(){{
    const body = document.getElementById("analyze-body");
    body.innerHTML = `<div class="empty" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:30px">Crunching numbers…</div>`;
    try {{
        const r = await fetch(`/animals/api/analyze`);
        if (!r.ok){{
            const err = await r.json().catch(()=>({{}}));
            body.innerHTML = `<div class="empty" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:30px">${{esc(err.detail || "Could not load analysis.")}}</div>`;
            return;
        }}
        const data = await r.json();
        renderAnalysis(data);
    }} catch(e){{
        body.innerHTML = `<div class="empty" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:30px">Network error.</div>`;
    }}
}}

function renderAnalysis(d){{
    const body = document.getElementById("analyze-body");
    const t = d.totals || {{}};
    const s = d.shares || {{}};
    const f = d.feedings || {{by_product:[]}};
    const ex = d.expenses || {{by_category:[], items:[]}};
    const gs = d.groups || [];

    const cards = `
        <div class="an-grid">
            <div class="an-card">
                <div class="an-label">Total Cost</div>
                <div class="an-value" style="color:var(--green)">EGP ${{fmtMoney(t.total_cost)}}</div>
                <div class="an-hint">Across ${{t.group_count||0}} group(s)</div>
            </div>
            <div class="an-card">
                <div class="an-label">Cost / Head</div>
                <div class="an-value" style="color:var(--blue)">EGP ${{fmtMoney(t.cost_per_head)}}</div>
                <div class="an-hint">Across ${{t.headcount||0}} total head</div>
            </div>
            <div class="an-card">
                <div class="an-label">Purchase Cost</div>
                <div class="an-value">EGP ${{fmtMoney(t.purchase_cost)}}</div>
                <div class="an-hint">Sum of all groups' purchase prices</div>
            </div>
            <div class="an-card">
                <div class="an-label">Feed Cost</div>
                <div class="an-value">EGP ${{fmtMoney(t.feed_cost)}}</div>
                <div class="an-hint">${{f.entry_count||0}} feeding(s), ${{fmtQty(f.total_qty)}} units</div>
            </div>
            <div class="an-card">
                <div class="an-label">Other Expenses</div>
                <div class="an-value">EGP ${{fmtMoney(t.expense_cost)}}</div>
                <div class="an-hint">${{ex.entry_count||0}} expense entry(s) tagged "Animals"</div>
            </div>
        </div>
    `;

    const bar = `
        <div class="an-bar">
            <span class="seg-purchase" style="width:${{s.purchase_pct||0}}%"></span>
            <span class="seg-feed"     style="width:${{s.feed_pct||0}}%"></span>
            <span class="seg-expense"  style="width:${{s.expense_pct||0}}%"></span>
        </div>
        <div class="an-legend">
            <span><i style="background:var(--blue)"></i>Purchase ${{(s.purchase_pct||0)}}%</span>
            <span><i style="background:var(--green)"></i>Feed ${{(s.feed_pct||0)}}%</span>
            <span><i style="background:var(--amber)"></i>Expenses ${{(s.expense_pct||0)}}%</span>
        </div>
    `;

    const groupRows = gs.map(r => `
        <tr>
            <td><b>${{esc(r.name||"")}}</b><div style="font-size:11px;color:var(--muted)">${{esc(r.animal_type||"")}}${{r.farm_name ? " · " + esc(r.farm_name) : ""}}</div></td>
            <td style="font-family:var(--mono)">${{r.headcount||0}}</td>
            <td style="font-family:var(--mono)">EGP ${{fmtMoney(r.purchase_cost)}}</td>
            <td style="font-family:var(--mono)">EGP ${{fmtMoney(r.feed_cost)}}</td>
            <td style="font-family:var(--mono);color:var(--text);font-weight:700">EGP ${{fmtMoney(r.subtotal)}}</td>
        </tr>
    `).join("") || `<tr><td colspan="5" class="empty">No active groups.</td></tr>`;

    const feedRows = (f.by_product || []).map(r => `
        <tr>
            <td><b>${{esc(r.product_name||"")}}</b><div style="font-family:var(--mono);font-size:11px;color:var(--muted)">${{esc(r.product_sku||"")}}</div></td>
            <td style="font-family:var(--mono)">${{fmtQty(r.total_qty)}} ${{esc(r.unit||"")}}</td>
            <td style="font-family:var(--mono);color:var(--sub)">${{fmtMoney(r.unit_cost)}}</td>
            <td style="font-family:var(--mono)">EGP ${{fmtMoney(r.total_cost)}}</td>
            <td style="color:var(--muted);font-family:var(--mono)">${{r.entries||0}}</td>
        </tr>
    `).join("") || `<tr><td colspan="5" class="empty">No feedings logged yet.</td></tr>`;

    const expCatRows = (ex.by_category || []).map(c => `
        <tr>
            <td><b>${{esc(c.name||"")}}</b></td>
            <td style="color:var(--muted);font-family:var(--mono)">${{c.count||0}}</td>
            <td style="font-family:var(--mono)">EGP ${{fmtMoney(c.amount)}}</td>
        </tr>
    `).join("") || `<tr><td colspan="3" class="empty">No expenses tagged "Animals" yet.</td></tr>`;

    const expItemRows = (ex.items || []).slice(0, 50).map(e => {{
        const srcBadge = e.source === "payroll"
            ? `<span style="font-size:9px;padding:1px 6px;border-radius:8px;background:rgba(96,165,250,.12);color:var(--blue);font-weight:700;margin-left:4px">PAYROLL</span>`
            : "";
        return `
        <tr>
            <td style="color:var(--sub);font-family:var(--mono);font-size:11px">${{esc(e.ref_number||"")}}${{srcBadge}}</td>
            <td style="color:var(--sub)">${{esc(e.expense_date||"")}}</td>
            <td>${{esc(e.category||"")}}</td>
            <td style="color:var(--sub);max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{esc(e.description||"")}}">${{esc(e.vendor || e.description || "—")}}</td>
            <td style="font-family:var(--mono)">EGP ${{fmtMoney(e.amount)}}</td>
        </tr>
        `;
    }}).join("") || `<tr><td colspan="5" class="empty">No expense entries yet.</td></tr>`;

    body.innerHTML = `
        ${{cards}}
        ${{bar}}

        <div class="an-section-title">Per-Group Summary</div>
        <div class="table-wrap">
            <table>
                <thead><tr>
                    <th>Group</th><th>Head</th><th>Purchase</th><th>Feed</th><th>Subtotal</th>
                </tr></thead>
                <tbody>${{groupRows}}</tbody>
            </table>
        </div>

        <div class="an-section-title">Feed Cost by Product (All Groups)</div>
        <div class="table-wrap">
            <table>
                <thead><tr>
                    <th>Product</th><th>Total Qty</th><th>Unit Cost</th><th>Total Cost</th><th>Entries</th>
                </tr></thead>
                <tbody>${{feedRows}}</tbody>
            </table>
        </div>

        <div class="an-section-title">Other Expenses by Category</div>
        <div class="table-wrap">
            <table>
                <thead><tr>
                    <th>Category</th><th>Entries</th><th>Amount</th>
                </tr></thead>
                <tbody>${{expCatRows}}</tbody>
            </table>
        </div>

        <div class="an-section-title">Expense Detail (most recent 50)</div>
        <div class="table-wrap">
            <table>
                <thead><tr>
                    <th>Ref</th><th>Date</th><th>Category</th><th>Vendor / Note</th><th>Amount</th>
                </tr></thead>
                <tbody>${{expItemRows}}</tbody>
            </table>
        </div>
    `;
}}

init();
</script>
</body>
</html>"""