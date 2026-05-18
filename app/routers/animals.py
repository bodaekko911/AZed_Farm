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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.log import record
from app.core.navigation import render_app_header
from app.core.permissions import require_permission
from app.database import get_async_session
from app.models.animal import AnimalGroup, FeedingLog, MortalityLog
from app.models.farm import Farm
from app.models.inventory import StockLocation, StockMove
from app.models.product import Product
from app.models.user import User
from app.services.location_inventory_service import (
    get_or_create_location_stock,
    quantize_qty,
)


router = APIRouter(
    prefix="/animals",
    tags=["Animals"],
    dependencies=[Depends(require_permission("page_animals"))],
)


# ── Schemas ──────────────────────────────────────────────────────────

class AnimalGroupIn(BaseModel):
    name:        str           = Field(..., min_length=1, max_length=150)
    animal_type: str           = Field("other", max_length=30)
    headcount:   int           = Field(0, ge=0)
    farm_id:     Optional[int] = None
    notes:       Optional[str] = None


class AnimalGroupUpdate(BaseModel):
    name:        Optional[str] = Field(None, min_length=1, max_length=150)
    animal_type: Optional[str] = Field(None, max_length=30)
    headcount:   Optional[int] = Field(None, ge=0)
    farm_id:     Optional[int] = None
    status:      Optional[str] = Field(None, max_length=20)
    notes:       Optional[str] = None


class FeedingCreate(BaseModel):
    animal_group_id: int       = Field(..., ge=1)
    product_id:      int       = Field(..., ge=1)
    location_id:     int       = Field(..., ge=1)
    qty:             float     = Field(..., gt=0)
    feed_date:       date_type
    note:            Optional[str] = None


# Valid mortality causes — frontend dropdown values must match these exactly.
VALID_CAUSES = {"illness", "injury", "age", "predator", "weather", "birth", "unknown", "other"}


class MortalityCreate(BaseModel):
    animal_group_id: int       = Field(..., ge=1)
    death_date:      date_type
    count:           int       = Field(1, ge=1)
    cause:           str       = Field("unknown", max_length=30)
    note:            Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────

def _serialize_group(g: AnimalGroup) -> dict:
    return {
        "id":           g.id,
        "name":         g.name,
        "animal_type":  g.animal_type or "other",
        "headcount":    int(g.headcount or 0),
        "farm_id":      g.farm_id,
        "farm_name":    g.farm.name if g.farm else None,
        "status":       g.status or "active",
        "notes":        g.notes,
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


# ── Convenience: list farms + products for the UI dropdowns ─────────

@router.get("/api/farms")
async def list_farms_for_picker(db: AsyncSession = Depends(get_async_session)):
    rows = (await db.execute(
        select(Farm).where(Farm.is_active == 1).order_by(Farm.name.asc())
    )).scalars().all()
    return {"items": [{"id": f.id, "name": f.name} for f in rows]}


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
<title>Animals — AZed ERP</title>
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

<div class="toast" id="toast"></div>

<script src="/static/auth-guard.js"></script>
<script>
/* ── STATE ── */
let _groups = [];
let _feedings = [];
let _deaths = [];
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
    document.getElementById("groups-section").style.display   = tab==="groups"   ? "" : "none";
    document.getElementById("feedings-section").style.display = tab==="feedings" ? "" : "none";
    document.getElementById("deaths-section").style.display   = tab==="deaths"   ? "" : "none";
    if (tab==="feedings") loadFeedings();
    if (tab==="deaths")   loadDeaths();
}}

/* ── INIT ── */
async function init(){{
    await Promise.all([loadGroups(), loadFarmsAndLocations(), loadProducts(), loadDeaths({{silent:true}})]);
}}

async function loadFarmsAndLocations(){{
    try {{
        const [fr, lr] = await Promise.all([
            fetch("/animals/api/farms"),
            fetch("/inventory/api/locations"),
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
        const r = await fetch("/inventory/api/stock?limit=2000");
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
            document.getElementById("gm-farm").value = g.farm_id || "";
            document.getElementById("gm-status").value = g.status === "archived" ? "active" : (g.status || "active");
            document.getElementById("gm-notes").value = g.notes || "";
            statusWrap.style.display = "";
        }}
    }} else {{
        document.getElementById("gm-name").value = "";
        document.getElementById("gm-type").value = "other";
        document.getElementById("gm-head").value = "";
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
    const payload = {{
        name: name,
        animal_type: document.getElementById("gm-type").value,
        headcount: parseInt(document.getElementById("gm-head").value, 10) || 0,
        farm_id: parseInt(document.getElementById("gm-farm").value, 10) || null,
        notes: document.getElementById("gm-notes").value.trim() || null,
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
        const r = await fetch(`/inventory/api/location-stock?product_id=${{pid}}&location_id=${{lid}}`);
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

init();
</script>
</body>
</html>"""