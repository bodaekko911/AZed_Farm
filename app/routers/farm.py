from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import func, select
from typing import Optional, List
from pydantic import BaseModel
from datetime import date

from app.database import get_async_session
from app.core.log import record
from app.core.permissions import get_current_user, require_admin, require_permission
from app.core.navigation import render_app_header
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem, WeatherLog
from app.models.product import Product
from app.models.inventory import StockMove
from app.models.user import User
from app.services.farm_intake_service import create_farm_delivery

router = APIRouter(
    prefix="/farm",
    tags=["Farm"],
    dependencies=[Depends(require_permission("page_farm"))],
)


# ── Schemas ────────────────────────────────────────────
class DeliveryItemIn(BaseModel):
    product_id: int
    qty:        float
    notes:      Optional[str] = None

class DeliveryCreate(BaseModel):
    farm_id:       int
    delivery_date: str
    received_by:   Optional[str] = None
    quality_notes: Optional[str] = None
    notes:         Optional[str] = None
    items:         List[DeliveryItemIn]


# ── SEED FARMS ─────────────────────────────────────────
@router.post("/api/seed-farms")
async def seed_farms(db: AsyncSession = Depends(get_async_session), _: User = Depends(require_admin)):
    cnt_result = await db.execute(select(func.count()).select_from(Farm))
    count = cnt_result.scalar()
    if count > 0:
        return {"message": "Farms already exist"}
    db.add(Farm(name="Organic Farm",      location="Nuweiba, South Sinai"))
    db.add(Farm(name="Regenerative Farm", location="Nuweiba, South Sinai"))
    await db.commit()
    return {"message": "2 farms created"}


# ── FARM API ───────────────────────────────────────────
@router.get("/api/farms")
async def get_farms(db: AsyncSession = Depends(get_async_session)):
    result = await db.execute(
        select(Farm).where(Farm.is_active == 1).order_by(Farm.name)
        .options(selectinload(Farm.deliveries))
    )
    farms = result.scalars().all()
    return [
        {
            "id":             f.id,
            "name":           f.name,
            "location":       f.location or "—",
            "delivery_count": len(f.deliveries),
        }
        for f in farms
    ]

@router.post("/api/farms", dependencies=[Depends(require_permission("action_farm_create"))])
async def create_farm(name: str, location: str = "", notes: str = "", db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Farm name is required")
    result = await db.execute(select(Farm).where(Farm.name == name))
    existing = result.scalar_one_or_none()
    if existing:
        if existing.is_active == 0:
            raise HTTPException(
                status_code=400,
                detail=f"An archived farm named '{name}' already exists. An admin can restore it from the archive instead.",
            )
        raise HTTPException(status_code=400, detail="A farm with that name already exists")
    f = Farm(name=name, location=location.strip() or None, notes=notes.strip() or None)
    db.add(f)
    await db.commit()
    await db.refresh(f)
    try:
        record(db, "farm", "create", f"Created farm: {f.name}", user=current_user)
    except Exception:
        pass
    return {"id": f.id, "name": f.name, "location": f.location or ""}


@router.delete("/api/farms/{farm_id}")
async def delete_farm(
    farm_id: int,
    hard: bool = False,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    """
    Delete a farm. Admin only.

    Default behavior is a SOFT delete (sets is_active=0) so historical
    deliveries, weather logs, employees, animals, expenses and other records
    remain intact and auditable. The farm just disappears from active lists
    and dropdowns.

    Pass ?hard=true to permanently delete a farm. This only succeeds when no
    other record references it (deliveries, weather logs, expenses, animals,
    employees, spoilage, carbon, etc.). If anything references the farm we
    return 400 with a clear message — the admin can archive it instead.
    """
    result = await db.execute(select(Farm).where(Farm.id == farm_id))
    farm = result.scalar_one_or_none()
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")

    farm_name = farm.name

    # Always-safe counts: deliveries and weather logs (these are the headline ones)
    dlv_cnt = (await db.execute(
        select(func.count(FarmDelivery.id)).where(FarmDelivery.farm_id == farm_id)
    )).scalar() or 0
    wx_cnt = (await db.execute(
        select(func.count(WeatherLog.id)).where(WeatherLog.farm_id == farm_id)
    )).scalar() or 0

    if hard:
        # Try the hard delete; if anything else (expenses, animals, employees,
        # spoilage, carbon…) still references the farm we'll catch the FK error.
        try:
            record(db, "farm", "delete",
                   f"Hard-deleted farm: {farm_name}",
                   user=current_user, ref_type="farm", ref_id=farm_id)
        except Exception:
            pass
        try:
            await db.delete(farm)
            await db.commit()
        except Exception as e:
            await db.rollback()
            msg = str(e).lower()
            if "foreign key" in msg or "violates" in msg or "constraint" in msg:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cannot permanently delete '{farm_name}' — other "
                        f"records (deliveries, expenses, animals, employees, "
                        f"weather logs, …) still reference it. Archive it "
                        f"instead so history is preserved."
                    ),
                )
            # Some other unexpected error — surface it.
            raise HTTPException(status_code=500, detail=f"Delete failed: {e}")
        return {"ok": True, "mode": "hard", "name": farm_name}

    # Soft delete (default): hide from active lists, keep history.
    if farm.is_active == 0:
        raise HTTPException(status_code=400, detail="Farm is already archived")
    farm.is_active = 0
    try:
        record(db, "farm", "archive",
               f"Archived farm: {farm_name} ({dlv_cnt} deliveries, {wx_cnt} weather logs preserved)",
               user=current_user, ref_type="farm", ref_id=farm_id)
    except Exception:
        pass
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Archive failed: {e}")
    return {
        "ok": True,
        "mode": "soft",
        "name": farm_name,
        "preserved": {"deliveries": dlv_cnt, "weather_logs": wx_cnt},
    }


@router.get("/api/farms/archived")
async def get_archived_farms(
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    """List soft-deleted (archived) farms. Admin only."""
    result = await db.execute(
        select(Farm).where(Farm.is_active == 0).order_by(Farm.name)
    )
    farms = result.scalars().all()
    out = []
    for f in farms:
        dlv_cnt = (await db.execute(
            select(func.count(FarmDelivery.id)).where(FarmDelivery.farm_id == f.id)
        )).scalar() or 0
        wx_cnt = (await db.execute(
            select(func.count(WeatherLog.id)).where(WeatherLog.farm_id == f.id)
        )).scalar() or 0
        out.append({
            "id":             f.id,
            "name":           f.name,
            "location":       f.location or "—",
            "notes":          f.notes or "",
            "delivery_count": dlv_cnt,
            "weather_count":  wx_cnt,
            "created_at":     f.created_at.isoformat() if f.created_at else None,
        })
    return out


@router.post("/api/farms/{farm_id}/restore")
async def restore_farm(
    farm_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(require_admin),
):
    """Restore an archived farm (sets is_active=1). Admin only."""
    result = await db.execute(select(Farm).where(Farm.id == farm_id))
    farm = result.scalar_one_or_none()
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")
    if farm.is_active == 1:
        raise HTTPException(status_code=400, detail="Farm is already active")

    # If another ACTIVE farm now uses this name, block the restore.
    name_clash = await db.execute(
        select(Farm).where(Farm.name == farm.name, Farm.is_active == 1, Farm.id != farm.id)
    )
    if name_clash.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail=f"Cannot restore — an active farm already uses the name '{farm.name}'. Rename the active farm first.",
        )

    farm.is_active = 1
    try:
        record(db, "farm", "restore",
               f"Restored farm: {farm.name}",
               user=current_user, ref_type="farm", ref_id=farm_id)
    except Exception:
        pass
    await db.commit()
    return {"ok": True, "id": farm.id, "name": farm.name}


# ── DELIVERY API ───────────────────────────────────────
@router.get("/api/deliveries")
async def get_deliveries(farm_id: int = None, skip: int = 0, limit: int = 50, db: AsyncSession = Depends(get_async_session)):
    base_where = []
    if farm_id:
        base_where.append(FarmDelivery.farm_id == farm_id)
    cnt_result = await db.execute(select(func.count()).select_from(FarmDelivery).where(*base_where))
    total = cnt_result.scalar()
    stmt = (
        select(FarmDelivery)
        .options(
            selectinload(FarmDelivery.items).selectinload(FarmDeliveryItem.product),
            selectinload(FarmDelivery.farm),
        )
        .where(*base_where)
        .order_by(FarmDelivery.delivery_date.desc(), FarmDelivery.created_at.desc())
        .offset(skip).limit(limit)
    )
    result = await db.execute(stmt)
    deliveries = result.scalars().all()
    return {
        "total": total,
        "deliveries": [
            {
                "id":              d.id,
                "delivery_number": d.delivery_number,
                "farm":            d.farm.name if d.farm else "—",
                "farm_id":         d.farm_id,
                "delivery_date":   str(d.delivery_date),
                "received_by":     d.received_by or "—",
                "quality_notes":   d.quality_notes or "",
                "notes":           d.notes or "",
                "created_at":      d.created_at.strftime("%Y-%m-%d %H:%M") if d.created_at else "—",
                "items": [
                    {
                        "product":    i.product.name if i.product else "—",
                        "product_id": i.product_id,
                        "qty":        float(i.qty),
                        "unit":       i.unit or (i.product.unit if i.product else ""),
                        "notes":      i.notes or "",
                    }
                    for i in d.items
                ],
                "total_items": len(d.items),
                "total_qty":   sum(float(i.qty) for i in d.items),
            }
            for d in deliveries
        ],
    }

@router.post("/api/deliveries", dependencies=[Depends(require_permission("action_farm_delivery_create"))])
async def create_delivery(data: DeliveryCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(Farm).where(Farm.id == data.farm_id))
    farm = result.scalar_one_or_none()
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")
    if not data.items:
        raise HTTPException(status_code=400, detail="Delivery must have at least one item")
    try:
        delivery_date = date.fromisoformat(data.delivery_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid delivery date")

    try:
        delivery, _stock_moves_created = await create_farm_delivery(
            db,
            farm=farm,
            delivery_date=delivery_date,
            user_id=current_user.id,
            items=[
                {"product_id": item.product_id, "qty": item.qty, "notes": item.notes}
                for item in data.items
            ],
            received_by=data.received_by,
            quality_notes=data.quality_notes,
            notes=data.notes,
            record_stock_movement=True,
            activity_user=current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await db.commit()
    await db.refresh(delivery)
    return {"id": delivery.id, "delivery_number": delivery.delivery_number, "items_count": len(data.items)}

@router.put("/api/deliveries/{delivery_id}", dependencies=[Depends(require_permission("action_farm_delivery_update"))])
async def edit_delivery(delivery_id: int, data: DeliveryCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    del_result = await db.execute(
        select(FarmDelivery)
        .options(selectinload(FarmDelivery.items))
        .where(FarmDelivery.id == delivery_id)
    )
    delivery = del_result.scalar_one_or_none()
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")

    farm_result = await db.execute(select(Farm).where(Farm.id == data.farm_id))
    farm = farm_result.scalar_one_or_none()
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")

    # Reverse old stock moves
    for item in delivery.items:
        prod_result = await db.execute(select(Product).where(Product.id == item.product_id))
        product = prod_result.scalar_one_or_none()
        if product:
            before = float(product.stock)
            after  = before - float(item.qty)
            product.stock = after
            db.add(StockMove(
                product_id=product.id, type="out",
                user_id=current_user.id,
                qty=-float(item.qty), qty_before=before, qty_after=after,
                ref_type="farm_intake_reversal", ref_id=delivery.id,
                note=f"Edit reversal — {delivery.delivery_number}",
            ))
        await db.delete(item)

    # Update delivery header
    delivery.farm_id       = data.farm_id
    delivery.user_id       = current_user.id
    delivery.delivery_date = date.fromisoformat(data.delivery_date)
    delivery.received_by   = data.received_by
    delivery.quality_notes = data.quality_notes
    delivery.notes         = data.notes

    # Apply new items
    for item in data.items:
        prod_result2 = await db.execute(select(Product).where(Product.id == item.product_id))
        product = prod_result2.scalar_one_or_none()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product not found: {item.product_id}")
        before = float(product.stock)
        after  = before + item.qty
        product.stock = after
        db.add(FarmDeliveryItem(
            delivery_id=delivery.id,
            product_id=product.id,
            qty=item.qty,
            unit=product.unit,
            notes=item.notes,
        ))
        db.add(StockMove(
            product_id=product.id, type="in",
            user_id=current_user.id,
            qty=item.qty, qty_before=before, qty_after=after,
            ref_type="farm_intake", ref_id=delivery.id,
            note=f"{farm.name} — {delivery.delivery_number} (edited)",
        ))

    record(db, "Farm", "edit_delivery",
           f"Edited delivery {delivery.delivery_number}",
           user=current_user, ref_type="farm_delivery", ref_id=delivery.id)
    await db.commit()
    return {"ok": True, "delivery_number": delivery.delivery_number}

@router.delete("/api/deliveries/{delivery_id}", dependencies=[Depends(require_permission("action_farm_delivery_delete"))])
async def delete_delivery(delivery_id: int, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    del_result = await db.execute(
        select(FarmDelivery)
        .options(selectinload(FarmDelivery.items))
        .where(FarmDelivery.id == delivery_id)
    )
    delivery = del_result.scalar_one_or_none()
    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")
    for item in delivery.items:
        prod_result = await db.execute(select(Product).where(Product.id == item.product_id))
        product = prod_result.scalar_one_or_none()
        if product:
            before = float(product.stock)
            after  = before - float(item.qty)
            product.stock = after
            db.add(StockMove(
                product_id=product.id, type="out",
                qty=-float(item.qty), qty_before=before, qty_after=after,
                ref_type="farm_intake_reversal", ref_id=delivery.id,
                note=f"Deleted — {delivery.delivery_number}",
            ))
    record(db, "Farm", "delete_delivery",
           f"Deleted delivery {delivery.delivery_number} — stock reversed",
           ref_type="farm_delivery", ref_id=delivery_id)
    await db.delete(delivery)
    await db.commit()
    return {"ok": True}


# ── STATS API ──────────────────────────────────────────
@router.get("/api/stats")
async def get_stats(db: AsyncSession = Depends(get_async_session)):
    from datetime import datetime, timezone
    now         = datetime.now(timezone.utc)
    month_start = date(now.year, now.month, 1)
    r1 = await db.execute(select(func.count(Farm.id)).where(Farm.is_active == 1))
    r2 = await db.execute(select(func.count(FarmDelivery.id)))
    r3 = await db.execute(select(func.count(FarmDelivery.id)).where(FarmDelivery.delivery_date >= month_start))
    return {
        "total_farms":      r1.scalar() or 0,
        "total_deliveries": r2.scalar() or 0,
        "this_month":       r3.scalar() or 0,
    }

@router.get("/api/products-list")
async def products_list(db: AsyncSession = Depends(get_async_session)):
    result = await db.execute(select(Product).where(Product.is_active == True).order_by(Product.name))
    products = result.scalars().all()
    return [{"id": p.id, "name": p.name, "sku": p.sku, "stock": float(p.stock), "unit": p.unit} for p in products]


# ── Weather Log ────────────────────────────────────────
class WeatherLogIn(BaseModel):
    farm_id:      int
    log_date:     str            # YYYY-MM-DD
    temp_min:     Optional[float] = None
    temp_max:     Optional[float] = None
    rainfall_mm:  Optional[float] = None
    humidity_pct: Optional[float] = None
    notes:        Optional[str]  = None

@router.get("/api/weather-logs", dependencies=[Depends(require_permission("tab_farm_weather"))])
async def get_weather_logs(farm_id: Optional[int] = None, limit: int = 90, db: AsyncSession = Depends(get_async_session)):
    stmt = select(WeatherLog).options(selectinload(WeatherLog.farm))
    if farm_id:
        stmt = stmt.where(WeatherLog.farm_id == farm_id)
    stmt = stmt.order_by(WeatherLog.log_date.desc()).limit(limit)
    result = await db.execute(stmt)
    logs = result.scalars().all()
    return [
        {
            "id":           w.id,
            "farm_id":      w.farm_id,
            "farm_name":    w.farm.name if w.farm else "—",
            "log_date":     str(w.log_date),
            "temp_min":     float(w.temp_min)     if w.temp_min     is not None else None,
            "temp_max":     float(w.temp_max)     if w.temp_max     is not None else None,
            "rainfall_mm":  float(w.rainfall_mm)  if w.rainfall_mm  is not None else None,
            "humidity_pct": float(w.humidity_pct) if w.humidity_pct is not None else None,
            "notes":        w.notes or "",
        }
        for w in logs
    ]

@router.post("/api/weather-logs", dependencies=[Depends(require_permission("action_farm_weather_log"))])
async def create_weather_log(data: WeatherLogIn, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    farm_result = await db.execute(select(Farm).where(Farm.id == data.farm_id))
    farm = farm_result.scalar_one_or_none()
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")
    try:
        log_date = date.fromisoformat(data.log_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format — use YYYY-MM-DD")
    w = WeatherLog(
        farm_id=data.farm_id,
        log_date=log_date,
        temp_min=data.temp_min,
        temp_max=data.temp_max,
        rainfall_mm=data.rainfall_mm,
        humidity_pct=data.humidity_pct,
        notes=(data.notes or "").strip() or None,
    )
    db.add(w); await db.commit(); await db.refresh(w)
    return {"id": w.id, "log_date": str(w.log_date)}

@router.put("/api/weather-logs/{log_id}", dependencies=[Depends(require_permission("action_farm_weather_log"))])
async def update_weather_log(log_id: int, data: WeatherLogIn, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(WeatherLog).where(WeatherLog.id == log_id))
    w = result.scalar_one_or_none()
    if not w:
        raise HTTPException(status_code=404, detail="Log not found")
    try:
        w.log_date = date.fromisoformat(data.log_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")
    w.farm_id      = data.farm_id
    w.temp_min     = data.temp_min
    w.temp_max     = data.temp_max
    w.rainfall_mm  = data.rainfall_mm
    w.humidity_pct = data.humidity_pct
    w.notes        = (data.notes or "").strip() or None
    await db.commit()
    return {"ok": True}

@router.delete("/api/weather-logs/{log_id}", dependencies=[Depends(require_permission("action_farm_weather_log"))])
async def delete_weather_log(log_id: int, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(WeatherLog).where(WeatherLog.id == log_id))
    w = result.scalar_one_or_none()
    if not w:
        raise HTTPException(status_code=404, detail="Log not found")
    await db.delete(w); await db.commit()
    return {"ok": True}


# ── UI ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def farm_ui(current_user: User = Depends(require_permission("page_farm"))):
    return """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Farm Intake — AZed ERP</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{
    --bg:#060810;--surface:#0a0d18;--card:#0f1424;--card2:#151c30;
    --border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.11);
    --green:#00ff9d;--blue:#4d9fff;--purple:#a855f7;--orange:#fb923c;
    --danger:#ff4d6d;--warn:#ffb547;--teal:#2dd4bf;--lime:#84cc16;
    --text:#f0f4ff;--sub:#8899bb;--muted:#445066;
    --sans:'Outfit',sans-serif;--mono:'JetBrains Mono',monospace;--r:12px;
}
body.light{
    --bg:#f4f5ef;--surface:#f1f3eb;--card:#eceee6;--card2:#e4e6de;
    --border:rgba(0,0,0,0.08);--border2:rgba(0,0,0,0.14);
    --green:#0f8a43;
    --text:#1a1e14;--sub:#4a5040;--muted:#7b816f;
}
body.light nav{background:rgba(244,245,239,.92);}
body.light .nav-link:hover{background:rgba(0,0,0,.05);}
body.light tr:hover td{background:rgba(0,0,0,.03);}
.mode-btn{display:flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:10px;border:1px solid var(--border);background:var(--card);color:var(--sub);font-size:16px;cursor:pointer;transition:all .2s;font-family:var(--sans);}
.mode-btn:hover{border-color:var(--border2);transform:scale(1.06);}
.topbar-right{display:flex;align-items:center;gap:12px;}
.account-menu{position:relative;}
.user-pill{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--border);border-radius:40px;padding:7px 16px 7px 10px;cursor:pointer;transition:all .2s;}
.user-pill:hover,.user-pill.open{border-color:var(--border2);}
.user-avatar{width:28px;height:28px;background:linear-gradient(135deg,#7ecb6f,#d4a256);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#0a0c08;}
.user-name{font-size:13px;font-weight:500;color:var(--sub);}
.menu-caret{font-size:11px;color:var(--muted);}
.account-dropdown{position:absolute;right:0;top:calc(100% + 10px);min-width:220px;background:var(--card);border:1px solid var(--border2);border-radius:14px;padding:8px;box-shadow:0 24px 50px rgba(0,0,0,.35);display:none;z-index:500;}
.account-dropdown.open{display:block;}
.account-head{padding:10px 12px 8px;border-bottom:1px solid var(--border);margin-bottom:6px;}
.account-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;}
.account-email{font-size:12px;color:var(--sub);margin-top:4px;word-break:break-word;}
.account-item{width:100%;display:flex;align-items:center;gap:10px;padding:10px 12px;border:none;background:transparent;border-radius:10px;color:var(--sub);font-family:var(--sans);font-size:13px;text-decoration:none;cursor:pointer;text-align:left;}
.account-item:hover{background:var(--card2);color:var(--text);}
.account-item.danger:hover{color:#c97a7a;}
.logout-btn{background:transparent;border:1px solid var(--border);color:var(--muted);font-family:var(--sans);font-size:12px;font-weight:500;padding:8px 16px;border-radius:8px;cursor:pointer;transition:all .2s;letter-spacing:.3px;}
.logout-btn:hover{border-color:#c97a7a;color:#c97a7a;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh;font-size:14px;}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 700px 500px at 10% 20%,rgba(132,204,22,.04) 0%,transparent 70%),radial-gradient(ellipse 500px 600px at 90% 80%,rgba(0,255,157,.03) 0%,transparent 70%);pointer-events:none;z-index:0;}
body>*{position:relative;z-index:1;}
nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:8px;padding:0 24px;height:58px;background:rgba(10,13,24,.92);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);flex-wrap:wrap;}
.logo{font-size:17px;font-weight:900;background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-right:10px;text-decoration:none;display:flex;align-items:center;gap:8px;}
.nav-link{padding:7px 12px;border-radius:8px;color:var(--sub);font-size:12px;font-weight:600;text-decoration:none;transition:all .2s;white-space:nowrap;}
.nav-link:hover{background:rgba(255,255,255,.05);color:var(--text);}
.nav-link.active{background:rgba(132,204,22,.1);color:var(--lime);}
.nav-spacer{flex:1;}
.content{max-width:1300px;margin:0 auto;padding:28px 24px;display:flex;flex-direction:column;gap:20px;}
.page-title{font-size:24px;font-weight:800;letter-spacing:-.5px;}
.page-sub{color:var(--muted);font-size:13px;margin-top:3px;}
.farms-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;}
.farm-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:20px;position:relative;overflow:hidden;cursor:pointer;transition:border-color .2s,box-shadow .2s;}
.farm-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;}
.farm-card:nth-child(1)::before{background:linear-gradient(90deg,var(--green),var(--lime));}
.farm-card:nth-child(2)::before{background:linear-gradient(90deg,var(--teal),var(--blue));}
.farm-card:hover{border-color:var(--border2);box-shadow:0 8px 24px rgba(0,0,0,.3);}
.farm-name{font-size:17px;font-weight:800;margin-bottom:4px;}
.farm-loc{font-size:12px;color:var(--muted);margin-bottom:14px;}
.farm-stat{display:flex;justify-content:space-between;font-size:12px;padding:6px 0;border-top:1px solid var(--border);}
.farm-stat-label{color:var(--muted);}
.farm-stat-val{font-family:var(--mono);color:var(--green);font-weight:700;}
.farm-del-btn{position:absolute;top:10px;right:10px;width:26px;height:26px;border-radius:50%;border:1px solid var(--border);background:var(--card2);color:var(--muted);font-size:16px;font-weight:700;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;font-family:var(--sans);padding:0;z-index:2;}
.farm-del-btn:hover{border-color:var(--danger);color:var(--danger);background:rgba(255,77,109,.08);transform:scale(1.08);}
.arch-row{display:flex;align-items:center;gap:12px;padding:12px 14px;background:var(--card2);border:1px solid var(--border);border-radius:10px;flex-wrap:wrap;}
.arch-row .arch-info{flex:1;min-width:200px;}
.arch-row .arch-name{font-size:14px;font-weight:700;color:var(--text);}
.arch-row .arch-meta{font-size:11px;color:var(--muted);margin-top:3px;font-family:var(--mono);}
.arch-row .arch-actions{display:flex;gap:6px;}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px;display:flex;flex-direction:column;gap:6px;position:relative;overflow:hidden;}
.stat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;}
.stat-card.green::before{background:linear-gradient(90deg,var(--green),transparent);}
.stat-card.lime::before {background:linear-gradient(90deg,var(--lime),transparent);}
.stat-card.teal::before {background:linear-gradient(90deg,var(--teal),transparent);}
.stat-label{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);}
.stat-value{font-family:var(--mono);font-size:24px;font-weight:700;}
.stat-value.green{color:var(--green);}
.stat-value.lime {color:var(--lime);}
.stat-value.teal {color:var(--teal);}
.tabs{display:flex;gap:4px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:4px;}
.tab{padding:8px 18px;border-radius:9px;font-size:13px;font-weight:700;cursor:pointer;border:none;background:transparent;color:var(--muted);transition:all .2s;font-family:var(--sans);}
.tab.active{background:var(--card2);color:var(--text);}
.btn{display:flex;align-items:center;gap:7px;padding:10px 16px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;border:none;transition:all .2s;white-space:nowrap;}
.btn-lime{background:linear-gradient(135deg,var(--lime),var(--green));color:#0a1a00;}
.btn-lime:hover{filter:brightness(1.1);transform:translateY(-1px);}
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.filter-sel{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;}
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;}
table{width:100%;border-collapse:collapse;}
thead{background:var(--card2);}
th{text-align:left;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:12px 16px;}
td{padding:12px 16px;border-top:1px solid var(--border);color:var(--sub);font-size:13px;}
tr.expandable{cursor:pointer;}
tr.expandable:hover td{background:rgba(255,255,255,.02);}
td.name{color:var(--text);font-weight:600;}
.farm-badge{display:inline-flex;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700;}
.farm-organic    {background:rgba(132,204,22,.1);color:var(--lime);}
.farm-regenerative{background:rgba(45,212,191,.1);color:var(--teal);}
.delivery-detail{background:var(--card2);border-top:1px solid var(--border);padding:14px 16px;display:none;}
.delivery-detail.open{display:block;}
.detail-items{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px;}
.detail-item{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 12px;}
.detail-item-name{font-weight:600;font-size:13px;margin-bottom:4px;}
.detail-item-qty{font-family:var(--mono);color:var(--green);font-size:15px;font-weight:700;}
.detail-item-unit{color:var(--muted);font-size:11px;}
.detail-item-note{color:var(--muted);font-size:11px;margin-top:4px;font-style:italic;}
.action-btn{background:transparent;border:1px solid var(--border2);color:var(--sub);font-size:12px;font-weight:600;padding:5px 10px;border-radius:7px;cursor:pointer;transition:all .15s;font-family:var(--sans);}
.action-btn:hover      {border-color:var(--blue);  color:var(--blue);}
.action-btn.danger:hover{border-color:var(--danger);color:var(--danger);}
.pagination{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-top:1px solid var(--border);font-size:13px;color:var(--muted);}
.page-btns{display:flex;gap:6px;}
.page-btn{background:var(--card2);border:1px solid var(--border2);color:var(--sub);font-family:var(--sans);font-size:12px;padding:6px 12px;border-radius:7px;cursor:pointer;transition:all .15s;}
.page-btn:hover{border-color:var(--lime);color:var(--lime);}
.page-btn:disabled{opacity:.3;cursor:not-allowed;}
.modal-bg{position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.75);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;}
.modal-bg.open{display:flex;}
.modal{background:var(--card);border:1px solid var(--border2);border-radius:16px;padding:28px;width:660px;max-width:95vw;max-height:90vh;overflow-y:auto;animation:modalIn .2s ease;}
@keyframes modalIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
.modal-title{font-size:18px;font-weight:800;margin-bottom:4px;}
.modal-sub{font-size:13px;color:var(--muted);margin-bottom:20px;}
.fld{display:flex;flex-direction:column;gap:6px;margin-bottom:14px;}
.fld label{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);}
.fld input,.fld select,.fld textarea{background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;transition:border-color .2s;width:100%;}
.fld input:focus,.fld select:focus,.fld textarea:focus{border-color:rgba(132,204,22,.4);}
.fld textarea{resize:vertical;min-height:70px;}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
.fld.span2{grid-column:span 2;}
.modal-actions{display:flex;gap:10px;margin-top:8px;justify-content:flex-end;}
.btn-cancel{background:transparent;border:1px solid var(--border2);color:var(--sub);padding:10px 18px;border-radius:var(--r);font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;}
.btn-cancel:hover{border-color:var(--danger);color:var(--danger);}
.farm-selector{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px;}
.farm-opt{background:var(--card2);border:2px solid var(--border2);border-radius:10px;padding:14px;cursor:pointer;text-align:center;transition:all .2s;}
.farm-opt:hover{border-color:var(--lime);}
.farm-opt.selected{border-color:var(--lime);background:rgba(132,204,22,.08);}
.farm-opt.selected.regen{border-color:var(--teal);background:rgba(45,212,191,.08);}
.farm-opt-icon{font-size:22px;margin-bottom:6px;}
.farm-opt-name{font-size:13px;font-weight:700;color:var(--text);}
.farm-opt-loc{font-size:10px;color:var(--muted);margin-top:2px;}
.item-row{display:grid;grid-template-columns:1.5fr 90px 60px 1fr 30px;gap:8px;align-items:center;margin-bottom:8px;}
.item-row input,.item-row select{background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;width:100%;}
.item-row input:focus{border-color:rgba(132,204,22,.4);}
.unit-label{font-size:11px;color:var(--muted);font-family:var(--mono);text-align:center;}
.rm-btn{background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer;padding:0;transition:color .15s;}
.rm-btn:hover{color:var(--danger);}
.add-item-btn{border:1px dashed rgba(132,204,22,.3);color:var(--lime);font-family:var(--sans);font-size:13px;font-weight:600;padding:8px;border-radius:8px;cursor:pointer;width:100%;transition:all .2s;margin-bottom:14px;background:transparent;}
.add-item-btn:hover{background:rgba(132,204,22,.08);}
/* SEARCH DROPDOWN */
.prod-search-wrap{position:relative;}
.prod-search-input{background:var(--card2);border:1px solid var(--border2);border-radius:8px;padding:8px 10px;color:var(--text);font-family:var(--sans);font-size:13px;outline:none;width:100%;}
.prod-search-input:focus{border-color:rgba(132,204,22,.4);}
.prod-dropdown{display:none;position:absolute;top:100%;left:0;right:0;background:var(--card);border:1px solid var(--border2);border-radius:8px;z-index:200;max-height:200px;overflow-y:auto;margin-top:4px;box-shadow:0 8px 24px rgba(0,0,0,.5);}
.prod-option{padding:10px 12px;cursor:pointer;border-bottom:1px solid var(--border);font-size:13px;}
.prod-option:last-child{border-bottom:none;}
.prod-option:hover{background:rgba(255,255,255,.05);}
.prod-option-name{font-weight:600;color:var(--text);}
.prod-option-meta{font-size:11px;color:var(--muted);margin-top:2px;}
/* HISTORY */
.history-section{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:20px;margin-bottom:14px;}
.history-title{font-size:14px;font-weight:700;margin-bottom:16px;display:flex;align-items:center;gap:10px;}
.history-bar-row{display:flex;align-items:center;gap:12px;margin-bottom:10px;}
.history-bar-label{font-size:12px;color:var(--sub);width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.history-bar-track{flex:1;background:var(--card2);border-radius:4px;height:8px;overflow:hidden;}
.history-bar-fill{height:100%;border-radius:4px;transition:width .6s ease;}
.history-bar-val{font-family:var(--mono);font-size:12px;color:var(--green);width:60px;text-align:right;}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(16px);background:var(--card2);border:1px solid var(--border2);border-radius:var(--r);padding:12px 20px;font-size:13px;font-weight:600;color:var(--text);box-shadow:0 20px 50px rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;z-index:999;}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
::-webkit-scrollbar{width:4px;}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px;}
</style>
    <script src="/static/auth-guard.js"></script>
</head>
<body>
""" + render_app_header(current_user, "page_farm") + """

<div class="content">
    <div>
        <div class="page-title">🌾 Farm Intake</div>
        <div class="page-sub">Receive crops from your farms — stock updates automatically</div>
    </div>

    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:8px;">
        <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);">Your Farms</div>
        <div style="display:flex;align-items:center;gap:10px;">
            <button id="btn-view-archived" onclick="openArchivedModal()" style="display:none;background:transparent;border:1px solid var(--border2);color:var(--sub);font-family:var(--sans);font-size:12px;font-weight:600;padding:6px 12px;border-radius:7px;cursor:pointer;transition:all .15s;">🗄️ Archived</button>
            <button class="btn btn-lime" id="btn-add-farm" onclick="openAddFarmModal()" style="font-size:12px;padding:7px 14px;">+ Add Farm</button>
        </div>
    </div>
    <div class="farms-row" id="farms-row">
        <div style="color:var(--muted);padding:20px">Loading farms...</div>
    </div>

    <div class="stats-grid">
        <div class="stat-card green"><div class="stat-label">Total Farms</div><div class="stat-value green" id="stat-farms">—</div></div>
        <div class="stat-card lime"><div class="stat-label">Total Deliveries</div><div class="stat-value lime" id="stat-total">—</div></div>
        <div class="stat-card teal"><div class="stat-label">This Month</div><div class="stat-value teal" id="stat-month">—</div></div>
    </div>

    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
        <div class="tabs">
            <button class="tab active" id="tab-deliveries" onclick="switchTab('deliveries')">Deliveries</button>
            <button class="tab"        id="tab-history"    onclick="switchTab('history')">Farm History</button>
            <button class="tab"        id="tab-weather"    onclick="switchTab('weather')">Weather Log</button>
            <button class="tab"        id="tab-season"     onclick="switchTab('season')">Season Analysis</button>
        </div>
        <div id="tab-action-area">
            <button class="btn btn-lime" id="btn-add-delivery" onclick="openDeliveryModal()">+ Record Delivery</button>
        </div>
    </div>

    <!-- DELIVERIES -->
    <div id="section-deliveries">
        <div class="toolbar">
            <select class="filter-sel" id="farm-filter" onchange="loadDeliveries()">
                <option value="">All Farms</option>
            </select>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Delivery #</th>
                        <th>Farm</th>
                        <th>Date</th>
                        <th>Received By</th>
                        <th>Items</th>
                        <th>Total Qty</th>
                        <th>Quality Notes</th>
                        <th></th>
                    </tr>
                </thead>
                <tbody id="deliveries-body">
                    <tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px">Loading...</td></tr>
                </tbody>
            </table>
            <div class="pagination">
                <span id="page-info">—</span>
                <div class="page-btns">
                    <button class="page-btn" id="prev-btn" onclick="prevPage()">← Prev</button>
                    <button class="page-btn" id="next-btn" onclick="nextPage()">Next →</button>
                </div>
            </div>
        </div>
    </div>

    <!-- HISTORY -->
    <div id="section-history" style="display:none">
        <div id="history-content">
            <div style="color:var(--muted);padding:40px;text-align:center">Loading...</div>
        </div>
    </div>

    <!-- WEATHER LOG -->
    <div id="section-weather" style="display:none">
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:14px;">
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                <select class="filter-sel" id="weather-farm-filter" onchange="loadWeatherLogs()">
                    <option value="">All Farms</option>
                </select>
            </div>
            <button class="btn btn-lime" id="btn-add-weather" onclick="openWeatherModal()">+ Log Weather</button>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Farm</th>
                        <th>Temp Min</th>
                        <th>Temp Max</th>
                        <th>Rainfall (mm)</th>
                        <th>Humidity (%)</th>
                        <th>Notes</th>
                        <th></th>
                    </tr>
                </thead>
                <tbody id="weather-body">
                    <tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px">Loading...</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <!-- SEASON ANALYSIS -->
    <div id="section-season" style="display:none">
        <div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:20px;margin-bottom:14px;">
            <div style="font-size:14px;font-weight:700;margin-bottom:14px;">Seasonal Cost Allocation</div>
            <div class="form-row" style="align-items:flex-end;gap:12px;flex-wrap:wrap;">
                <div class="fld" style="min-width:180px">
                    <label>Farm</label>
                    <select class="filter-sel" id="season-farm" style="width:100%">
                        <option value="">Select farm...</option>
                    </select>
                </div>
                <div class="fld" style="min-width:140px">
                    <label>Season Start</label>
                    <input id="season-from" type="date" style="background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;width:100%">
                </div>
                <div class="fld" style="min-width:140px">
                    <label>Season End</label>
                    <input id="season-to" type="date" style="background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;width:100%">
                </div>
                <button class="btn btn-lime" onclick="loadSeasonAnalysis()">Analyze</button>
            </div>
            <div style="font-size:12px;color:var(--muted);margin-top:8px;">
                Tip: tag expenses to this farm on the <a href="/expenses/" style="color:var(--lime);text-decoration:none">Expenses page</a> for them to appear here.
            </div>
        </div>
        <div id="season-result" style="display:none">
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:14px;" id="season-summary-cards"></div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px;flex-wrap:wrap;">
                <div class="history-section" id="season-cost-breakdown">
                    <div class="history-title">Cost Breakdown by Category</div>
                </div>
                <div class="history-section" id="season-product-chart">
                    <div class="history-title">Harvest by Product (kg)</div>
                </div>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Product</th>
                            <th>Harvested</th>
                            <th>Share</th>
                            <th>Allocated Cost</th>
                            <th>Cost / Unit</th>
                            <th>Sale Price</th>
                            <th>Profit / Unit</th>
                            <th>Margin</th>
                        </tr>
                    </thead>
                    <tbody id="season-body"></tbody>
                </table>
            </div>
        </div>
        <div id="season-empty" style="color:var(--muted);text-align:center;padding:40px;display:none">Select a farm and date range, then click Analyze.</div>
    </div>
</div>

<!-- ADD FARM MODAL -->
<div class="modal-bg" id="add-farm-modal">
    <div class="modal" style="width:480px">
        <div class="modal-title">Add New Farm</div>
        <div class="modal-sub">The farm will immediately appear in all dropdowns, delivery forms, weather logs, and season analysis.</div>
        <div class="fld">
            <label>Farm Name *</label>
            <input id="af-name" placeholder="e.g. Desert Rose Farm" maxlength="150">
        </div>
        <div class="fld">
            <label>Location</label>
            <input id="af-location" placeholder="e.g. Nuweiba, South Sinai" maxlength="200">
        </div>
        <div class="fld">
            <label>Notes</label>
            <textarea id="af-notes" placeholder="Any notes about this farm (optional)"></textarea>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeAddFarmModal()">Cancel</button>
            <button class="btn btn-lime" id="af-save-btn" onclick="saveNewFarm()">✓ Create Farm</button>
        </div>
    </div>
</div>

<!-- ARCHIVED FARMS MODAL (admin only) -->
<div class="modal-bg" id="archived-farms-modal">
    <div class="modal" style="width:680px">
        <div class="modal-title">🗄️ Archived Farms</div>
        <div class="modal-sub">Soft-deleted farms. Restore brings them back to active lists. Permanent delete is only available for farms with no history.</div>
        <div id="archived-farms-list" style="display:flex;flex-direction:column;gap:10px;max-height:50vh;overflow-y:auto;">
            <div style="color:var(--muted);padding:14px;text-align:center;">Loading archived farms…</div>
        </div>
        <div class="modal-actions" style="margin-top:20px;">
            <button class="btn-cancel" onclick="closeArchivedModal()">Close</button>
        </div>
    </div>
</div>

<!-- DELETE FARM CONFIRMATION MODAL (admin only) -->
<div class="modal-bg" id="delete-farm-modal">
    <div class="modal" style="width:500px;border-color:rgba(255,77,109,.35);">
        <div class="modal-title" id="del-farm-title" style="color:var(--danger);">⚠️ Delete farm?</div>
        <div class="modal-sub" id="del-farm-sub">This action cannot be undone.</div>

        <div id="del-farm-warning" style="background:rgba(255,77,109,.08);border:1px solid rgba(255,77,109,.25);border-radius:10px;padding:12px 14px;margin-bottom:16px;font-size:13px;color:var(--text);line-height:1.5;"></div>

        <div class="fld">
            <label>Type the farm name to confirm: <strong id="del-farm-expected" style="color:var(--danger);font-family:var(--mono);"></strong></label>
            <input id="del-farm-input" autocomplete="off" spellcheck="false" placeholder="Type farm name exactly…">
            <div id="del-farm-mismatch" style="display:none;font-size:11px;color:var(--danger);margin-top:4px;">Name doesn't match — type it exactly as shown.</div>
        </div>

        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeDeleteFarmModal()">Cancel</button>
            <button id="del-farm-confirm-btn" onclick="confirmDeleteFarm()" disabled
                style="background:var(--danger);color:#fff;border:1px solid var(--danger);border-radius:10px;padding:10px 18px;font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;opacity:.4;transition:opacity .15s;">
                Delete farm
            </button>
        </div>
    </div>
</div>

<!-- WEATHER MODAL -->
<div class="modal-bg" id="weather-modal">
    <div class="modal" style="width:500px">
        <div class="modal-title" id="weather-modal-title">Log Weather</div>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:8px;">
            <div class="modal-sub" style="margin:0">Daily conditions for this farm</div>
            <button id="autofill-btn" onclick="autoFillWeather()" style="display:flex;align-items:center;gap:7px;background:linear-gradient(135deg,var(--teal),var(--blue));border:none;border-radius:10px;padding:8px 16px;font-family:var(--sans);font-size:12px;font-weight:700;color:#001a1a;cursor:pointer;transition:all .2s">
                <span id="autofill-icon">⛅</span> Auto-fill from location
            </button>
        </div>
        <div id="weather-fetch-status" style="display:none;font-size:12px;padding:8px 12px;border-radius:8px;margin-bottom:12px;border:1px solid var(--border2);color:var(--sub)"></div>
        <div class="form-row">
            <div class="fld">
                <label>Farm *</label>
                <select id="w-farm" style="background:var(--card2);border:1px solid var(--border2);border-radius:10px;padding:10px 12px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;width:100%"></select>
            </div>
            <div class="fld">
                <label>Date *</label>
                <input id="w-date" type="date">
            </div>
            <div class="fld">
                <label>Temp Min (°C)</label>
                <input id="w-tmin" type="number" step="0.1" placeholder="e.g. 18">
            </div>
            <div class="fld">
                <label>Temp Max (°C)</label>
                <input id="w-tmax" type="number" step="0.1" placeholder="e.g. 34">
            </div>
            <div class="fld">
                <label>Rainfall (mm)</label>
                <input id="w-rain" type="number" step="0.1" min="0" placeholder="e.g. 2.5">
            </div>
            <div class="fld">
                <label>Humidity (%)</label>
                <input id="w-hum" type="number" step="0.1" min="0" max="100" placeholder="e.g. 65">
            </div>
            <div class="fld span2">
                <label>Notes</label>
                <input id="w-notes" placeholder="e.g. Sandstorm in the afternoon">
            </div>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeWeatherModal()">Cancel</button>
            <button class="btn btn-lime" onclick="saveWeatherLog()">✓ Save</button>
        </div>
    </div>
</div>

<!-- DELIVERY MODAL -->
<div class="modal-bg" id="delivery-modal">
    <div class="modal">
        <div class="modal-title" id="modal-title">Record Farm Delivery</div>
        <div class="modal-sub">Stock will be updated automatically on save</div>

        <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Select Farm *</div>
        <div class="farm-selector" id="farm-selector"></div>

        <div class="form-row">
            <div class="fld">
                <label>Delivery Date *</label>
                <input id="d-date" type="date">
            </div>
            <div class="fld">
                <label>Received By</label>
                <input id="d-receiver" placeholder="Your name">
            </div>
            <div class="fld span2">
                <label>Quality Notes</label>
                <textarea id="d-quality" placeholder="e.g. Fresh, good condition. Some wilting on kale."></textarea>
            </div>
        </div>

        <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:8px">Products Received</div>
        <div style="display:grid;grid-template-columns:1.5fr 90px 60px 1fr 30px;gap:8px;margin-bottom:6px;">
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Product</span>
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Qty</span>
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Unit</span>
            <span style="font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px">Note</span>
            <span></span>
        </div>
        <div id="delivery-items"></div>
        <button class="add-item-btn" onclick="addDeliveryItem()">+ Add Product</button>

        <div class="fld">
            <label>General Notes</label>
            <input id="d-notes" placeholder="Any other notes about this delivery">
        </div>

        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeDeliveryModal()">Cancel</button>
            <button class="btn btn-lime" id="save-btn" onclick="saveDelivery()">✓ Save Delivery & Update Stock</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
  function setModeButton(isLight){
    const btn = document.getElementById("mode-btn");
    if(btn) btn.innerText = isLight ? "☀️" : "🌙";
}
function toggleMode(){
    const isLight = document.body.classList.toggle("light");
    localStorage.setItem("colorMode", isLight ? "light" : "dark");
    setModeButton(isLight);
}
function initializeColorMode(){
    const isLight = localStorage.getItem("colorMode") === "light";
    document.body.classList.toggle("light", isLight);
    setModeButton(isLight);
}
async function initUser() {
    try {
        const r = await fetch("/auth/me");
        if (!r.ok) { _redirectToLogin(); return; }
        const u = await r.json();
        const nameEl = document.getElementById("user-name");
        const avatarEl = document.getElementById("user-avatar");
        const emailEl = document.getElementById("user-email");
        if (nameEl) nameEl.innerText = u.name;
        if (avatarEl) avatarEl.innerText = u.name.charAt(0).toUpperCase();
        if (emailEl) emailEl.innerText = u.email;
        return u;
    } catch(e) { _redirectToLogin(); }
}
function toggleAccountMenu(event){
    event.stopPropagation();
    const trigger = document.getElementById("account-trigger");
    const dropdown = document.getElementById("account-dropdown");
    const open = dropdown.classList.toggle("open");
    trigger.classList.toggle("open", open);
    trigger.setAttribute("aria-expanded", open ? "true" : "false");
}
document.addEventListener("click", e => {
    const menu = document.getElementById("account-dropdown");
    const trigger = document.getElementById("account-trigger");
    if(!menu || !trigger) return;
    if(menu.contains(e.target) || trigger.contains(e.target)) return;
    menu.classList.remove("open");
    trigger.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
});
async function logout(){
    await fetch("/auth/logout", { method: "POST" });
    window.location.href = "/";
}
let currentUserRole = "";
let currentUserPermissions = new Set();
let isAdmin = false;

async function initUser() {
    try {
        const r = await fetch("/auth/me");
        if (!r.ok) { _redirectToLogin(); return; }
        const u = await r.json();
        document.getElementById("user-name").innerText = u.name;
        document.getElementById("user-avatar").innerText = u.name.charAt(0).toUpperCase();
        document.getElementById("user-email").innerText = u.email;
        
        currentUserRole = u.role || "";
        currentUserPermissions = new Set(
            (typeof u.permissions === "string" ? u.permissions.split(",") : (u.permissions || []))
                .map(v => String(v).trim())
                .filter(Boolean)
        );
        isAdmin = (currentUserRole === "admin");
        return u;
    } catch(e) { _redirectToLogin(); }
}

function hasPermission(permission, u){
    const role = u ? (u.role || "") : currentUserRole;
    const perms = u ? new Set(typeof u.permissions === "string" ? u.permissions.split(",").map(v => v.trim()).filter(Boolean) : (u.permissions || [])) : currentUserPermissions;
    return role === "admin" || perms.has(permission);
}

function configureFarmPermissions(u){
    const tabMap = [
        {id:"tab-deliveries", permission:"tab_farm_deliveries", section:"deliveries"},
        {id:"tab-history", permission:"tab_farm_history", section:"history"},
        {id:"tab-weather", permission:"tab_farm_weather", section:"weather"},
        {id:"tab-season", permission:"tab_farm_season", section:"season"},
    ];
    let firstAvailable = null;
    
    tabMap.forEach(conf => {
        let el = document.getElementById(conf.id);
        if(el) {
            if(!hasPermission(conf.permission, u)) {
                el.style.display = "none";
                document.getElementById("section-" + conf.section).style.display = "none";
            } else if(!firstAvailable) {
                firstAvailable = conf.section;
            }
        }
    });

    if(!hasPermission("action_farm_delivery_create", u)) document.getElementById("btn-add-delivery").style.display = "none";
    if(!hasPermission("action_farm_weather_log", u)) document.getElementById("btn-add-weather").style.display = "none";
    if(!hasPermission("action_farm_create", u)) { let b = document.getElementById("btn-add-farm"); if(b) b.style.display = "none"; }
    // Archived farms view is admin-only
    let archBtn = document.getElementById("btn-view-archived");
    if(archBtn) archBtn.style.display = isAdmin ? "inline-block" : "none";
    if(firstAvailable) switchTab(firstAvailable);
}

initializeColorMode();
initUser().then(u => { 
    if(u) {
        configureFarmPermissions(u);
        init(); 
    }
});
  let allProducts     = [];
let allFarms        = [];
let selectedFarmId  = null;
let editingDeliveryId = null;   // null = creating new, number = editing existing
let deliveryPage    = 0;
let pageSize        = 20;
let totalDeliveries = 0;

async function init(){
    await fetch("/farm/api/seed-farms", {method:"POST"});
    allProducts = await (await fetch("/farm/api/products-list")).json();
    allFarms    = await (await fetch("/farm/api/farms")).json();
    renderFarmCards();
    fillFarmFilter();
    fillWeatherFarmFilter();
    fillSeasonFarmSelect();
    await loadStats();
    await loadDeliveries();
    // Set default season dates (current month)
    let now = new Date();
    let y   = now.getFullYear(), m = String(now.getMonth()+1).padStart(2,"0");
    document.getElementById("season-from").value = `${y}-${m}-01`;
    document.getElementById("season-to").value   = now.toISOString().split("T")[0];
}

function fillWeatherFarmFilter(){
    document.getElementById("weather-farm-filter").innerHTML =
        `<option value="">All Farms</option>` +
        allFarms.map(f=>`<option value="${f.id}">${f.name}</option>`).join("");
    document.getElementById("w-farm").innerHTML =
        allFarms.map(f=>`<option value="${f.id}">${f.name}</option>`).join("");
}

function fillSeasonFarmSelect(){
    let allOption = allFarms.length > 1
        ? `<option value="both">All Farms (Combined)</option>`
        : "";
    document.getElementById("season-farm").innerHTML =
        `<option value="">Select farm...</option>` +
        allOption +
        allFarms.map(f=>`<option value="${f.id}">${f.name}</option>`).join("");
}

/* ── FARMS ── */
const FARM_ICONS   = ["🌿","♻️","🌱","🍃","🌾","🫘","🌻","🌺","🍀","🌳"];
const FARM_CLASSES = ["farm-organic","farm-regenerative","farm-organic","farm-regenerative","farm-organic","farm-regenerative","farm-organic","farm-regenerative","farm-organic","farm-regenerative"];
const FARM_OPT_CLS = ["","regen","","regen","","regen","","regen","","regen"];

function getFarmIcon(i){ return FARM_ICONS[i % FARM_ICONS.length]; }
function getFarmCardClass(i){ return FARM_CLASSES[i % FARM_CLASSES.length]; }
function getFarmOptClass(i){ return FARM_OPT_CLS[i % FARM_OPT_CLS.length]; }

function renderFarmCards(){
    document.getElementById("farms-row").innerHTML = allFarms.map((f,i)=>`
        <div class="farm-card" data-farm-id="${f.id}">
            ${isAdmin ? `<button type="button" class="farm-del-btn" data-farm-id="${f.id}" data-farm-deliveries="${f.delivery_count}" title="Delete farm (admin)">×</button>` : ``}
            <div class="farm-name">${getFarmIcon(i)} ${escapeHtml(f.name)}</div>
            <div class="farm-loc">${escapeHtml(f.location || "—")}</div>
            <div class="farm-stat">
                <span class="farm-stat-label">Total Deliveries</span>
                <span class="farm-stat-val">${f.delivery_count}</span>
            </div>
        </div>`).join("") || `<div style="color:var(--muted)">No farms found.</div>`;

    document.getElementById("farm-selector").innerHTML = allFarms.map((f,i)=>`
        <div class="farm-opt ${getFarmOptClass(i)}" id="farm-opt-${f.id}" onclick="selectFarm(${f.id})">
            <div class="farm-opt-icon">${getFarmIcon(i)}</div>
            <div class="farm-opt-name">${escapeHtml(f.name)}</div>
            <div class="farm-opt-loc">${escapeHtml(f.location || "—")}</div>
        </div>`).join("");

    // Install the click delegation handler exactly once; it survives re-renders.
    let row = document.getElementById("farms-row");
    if(row && !row._delegationBound){
        row._delegationBound = true;
        row.addEventListener("click", function(e){
            let delBtn = e.target.closest(".farm-del-btn");
            if(delBtn){
                e.stopPropagation();
                let id = parseInt(delBtn.dataset.farmId, 10);
                let dc = parseInt(delBtn.dataset.farmDeliveries, 10) || 0;
                let farm = allFarms.find(x => x.id === id);
                if(farm) openDeleteFarmModal(farm.id, farm.name, dc);
                return;
            }
            let card = e.target.closest(".farm-card");
            if(card){
                let id = parseInt(card.dataset.farmId, 10);
                if(!isNaN(id)) filterByFarm(id);
            }
        });
    }
}

function fillFarmFilter(){
    document.getElementById("farm-filter").innerHTML =
        `<option value="">All Farms</option>` +
        allFarms.map(f=>`<option value="${f.id}">${f.name}</option>`).join("");
}

function filterByFarm(id){
    document.getElementById("farm-filter").value = id;
    deliveryPage = 0;
    loadDeliveries();
    switchTab("deliveries");
}

function selectFarm(id){
    selectedFarmId = id;
    allFarms.forEach(f=>{
        let el = document.getElementById("farm-opt-"+f.id);
        if(el) el.classList.toggle("selected", f.id===id);
    });
}

/* ── STATS ── */
async function loadStats(){
    let d = await (await fetch("/farm/api/stats")).json();
    document.getElementById("stat-farms").innerText = d.total_farms;
    document.getElementById("stat-total").innerText = d.total_deliveries;
    document.getElementById("stat-month").innerText = d.this_month;
}

/* ── TABS ── */
function switchTab(tab){
    ["deliveries","history","weather","season"].forEach(t=>{
        document.getElementById("section-"+t).style.display = tab===t?"":"none";
        document.getElementById("tab-"+t).classList.toggle("active", tab===t);
    });
    document.getElementById("tab-action-area").style.display = tab==="deliveries"?"":"none";
    if(tab==="history") loadHistory();
    if(tab==="weather") loadWeatherLogs();
}

/* ── DELIVERIES TABLE ── */
async function loadDeliveries(){
    let farmId = document.getElementById("farm-filter").value;
    let url    = `/farm/api/deliveries?skip=${deliveryPage*pageSize}&limit=${pageSize}`;
    if(farmId) url += `&farm_id=${farmId}`;

    let data = await (await fetch(url)).json();
    totalDeliveries = data.total;

    document.getElementById("page-info").innerText =
        totalDeliveries === 0 ? "No deliveries" :
        `${deliveryPage*pageSize+1}–${Math.min((deliveryPage+1)*pageSize,totalDeliveries)} of ${totalDeliveries}`;
    document.getElementById("prev-btn").disabled = deliveryPage===0;
    document.getElementById("next-btn").disabled = (deliveryPage+1)*pageSize>=totalDeliveries;

    if(!data.deliveries.length){
        document.getElementById("deliveries-body").innerHTML=
            `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:60px">No deliveries yet. Click <b>+ Record Delivery</b>.</td></tr>`;
        return;
    }

    let html="";
    data.deliveries.forEach(d=>{
        let farmIdx = allFarms.findIndex(f=>f.id===d.farm_id);
        let farmCls = farmIdx%2===0?"farm-organic":"farm-regenerative";
        let adminBtns = `<div style="display:flex;gap:6px">`;
        if (hasPermission("action_farm_delivery_update")) adminBtns += `<button class="action-btn" onclick="event.stopPropagation();openEditDelivery(${d.id})">Edit</button>`;
        if (hasPermission("action_farm_delivery_delete")) adminBtns += `<button class="action-btn danger" onclick="event.stopPropagation();deleteDelivery(${d.id},'${d.delivery_number}')">Delete</button>`;
        adminBtns += `</div>`;

        html+=`
        <tr class="expandable" onclick="toggleDetail('det-${d.id}')">
            <td style="font-family:var(--mono);font-size:12px;color:var(--lime)">${d.delivery_number}</td>
            <td><span class="farm-badge ${farmCls}">${d.farm}</span></td>
            <td style="font-family:var(--mono);font-size:12px">${d.delivery_date}</td>
            <td style="font-size:12px">${d.received_by}</td>
            <td style="font-family:var(--mono);color:var(--blue)">${d.total_items}</td>
            <td style="font-family:var(--mono);color:var(--green);font-weight:700">${d.total_qty.toFixed(1)}</td>
            <td style="font-size:12px;color:var(--muted);max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${d.quality_notes||"—"}</td>
            <td>${adminBtns}</td>
        </tr>
        <tr><td colspan="8" style="padding:0;border:none">
            <div class="delivery-detail" id="det-${d.id}">
                ${d.quality_notes?`<div style="font-size:12px;color:var(--muted);margin-bottom:12px;padding:8px 12px;background:rgba(132,204,22,.05);border-radius:8px;border-left:3px solid var(--lime)">Quality: ${d.quality_notes}</div>`:""}
                <div class="detail-items">
                    ${d.items.map(item=>`
                        <div class="detail-item">
                            <div class="detail-item-name">${item.product}</div>
                            <div>
                                <span class="detail-item-qty">${item.qty.toFixed(2)}</span>
                                <span class="detail-item-unit"> ${item.unit}</span>
                            </div>
                            ${item.notes?`<div class="detail-item-note">${item.notes}</div>`:""}
                        </div>`).join("")}
                </div>
                ${d.notes?`<div style="font-size:12px;color:var(--muted);margin-top:10px">${d.notes}</div>`:""}
            </div>
        </td></tr>`;
    });
    document.getElementById("deliveries-body").innerHTML = html;
}

function toggleDetail(id){
    let el = document.getElementById(id);
    if(el) el.classList.toggle("open");
}

function prevPage(){ if(deliveryPage>0){ deliveryPage--; loadDeliveries(); } }
function nextPage(){ if((deliveryPage+1)*pageSize<totalDeliveries){ deliveryPage++; loadDeliveries(); } }

/* ── HISTORY ── */
async function loadHistory(){
    let all = await (await fetch("/farm/api/deliveries?limit=500")).json();
    let byFarm = {};
    allFarms.forEach(f=>{ byFarm[f.id]={name:f.name, deliveries:[], products:{}}; });
    all.deliveries.forEach(d=>{
        if(byFarm[d.farm_id]){
            byFarm[d.farm_id].deliveries.push(d);
            d.items.forEach(item=>{
                byFarm[d.farm_id].products[item.product] = (byFarm[d.farm_id].products[item.product]||0) + item.qty;
            });
        }
    });
    document.getElementById("history-content").innerHTML = Object.values(byFarm).map((farm,fi)=>{
        let products = Object.entries(farm.products).sort((a,b)=>b[1]-a[1]);
        let maxQty   = products.length ? products[0][1] : 1;
        let color    = fi%2===0?"var(--lime)":"var(--teal)";
        return `
        <div class="history-section">
            <div class="history-title">
                <span style="font-size:20px">${getFarmIcon(fi)}</span>
                <span>${farm.name}</span>
                <span style="font-size:12px;color:var(--muted);font-weight:400">${farm.deliveries.length} deliveries</span>
            </div>
            ${products.length===0
                ? `<div style="color:var(--muted);font-size:13px">No deliveries recorded yet.</div>`
                : products.map(([name,qty])=>`
                    <div class="history-bar-row">
                        <div class="history-bar-label">${name}</div>
                        <div class="history-bar-track">
                            <div class="history-bar-fill" style="width:${(qty/maxQty*100).toFixed(1)}%;background:linear-gradient(90deg,${color},var(--green))"></div>
                        </div>
                        <div class="history-bar-val">${qty.toFixed(1)}</div>
                    </div>`).join("")}
        </div>`;
    }).join("");
}

/* ── DELIVERY MODAL ── */
function openDeliveryModal(){
    // Reset to CREATE mode
    editingDeliveryId = null;
    document.getElementById("modal-title").innerText = "Record Farm Delivery";
    document.getElementById("save-btn").innerText    = "✓ Save Delivery & Update Stock";

    document.getElementById("delivery-items").innerHTML = "";
    document.getElementById("d-date").value     = new Date().toISOString().split("T")[0];
    document.getElementById("d-receiver").value = "";
    document.getElementById("d-quality").value  = "";
    document.getElementById("d-notes").value    = "";

    if(allFarms.length) selectFarm(allFarms[0].id);
    addDeliveryItem();
    document.getElementById("delivery-modal").classList.add("open");
}

function closeDeliveryModal(){
    editingDeliveryId = null;
    document.getElementById("modal-title").innerText = "Record Farm Delivery";
    document.getElementById("save-btn").innerText    = "✓ Save Delivery & Update Stock";
    document.getElementById("delivery-modal").classList.remove("open");
}

/* ── EDIT DELIVERY ── */
async function openEditDelivery(id){
    // Load this specific delivery
    let data = await (await fetch(`/farm/api/deliveries?limit=1000`)).json();
    let d    = data.deliveries.find(x=>x.id===id);
    if(!d){ showToast("Could not load delivery"); return; }

    // Switch to EDIT mode
    editingDeliveryId = id;
    document.getElementById("modal-title").innerText = `Edit Delivery — ${d.delivery_number}`;
    document.getElementById("save-btn").innerText    = "✓ Save Changes & Reverse/Reapply Stock";

    // Fill header fields
    selectFarm(d.farm_id);
    document.getElementById("d-date").value     = d.delivery_date;
    document.getElementById("d-receiver").value = d.received_by === "—" ? "" : d.received_by;
    document.getElementById("d-quality").value  = d.quality_notes;
    document.getElementById("d-notes").value    = d.notes;

    // Fill items
    document.getElementById("delivery-items").innerHTML = "";
    d.items.forEach(item=>{
        addDeliveryItem();
        let rows = document.querySelectorAll("#delivery-items .item-row");
        let row  = rows[rows.length - 1];
        // Set product
        row.dataset.productId = item.product_id;
        row.querySelector(".prod-search-input").value = item.product;
        row.querySelector(".unit-label").innerText    = item.unit;
        // Set qty
        row.querySelectorAll("input")[1].value = item.qty;
        // Set note
        if(row.querySelectorAll("input")[2]) row.querySelectorAll("input")[2].value = item.notes || "";
    });

    document.getElementById("delivery-modal").classList.add("open");
}

/* ── DELETE DELIVERY ── */
async function deleteDelivery(id, number){
    if(!confirm(`Delete ${number}? This will reverse all stock changes. This cannot be undone.`)) return;
    let res  = await fetch(`/farm/api/deliveries/${id}`, {method:"DELETE"});
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast(`${number} deleted — stock reversed ✓`);
    await refreshAll();
}

/* ── SAVE (create or edit) ── */
async function saveDelivery(){
    if(!selectedFarmId){ showToast("Select a farm first"); return; }

    let rows  = document.querySelectorAll("#delivery-items .item-row");
    let items = [];
    for(let row of rows){
        let product_id = parseInt(row.dataset.productId);
        let qty        = parseFloat(row.querySelectorAll("input")[1].value)||0;
        let notes      = row.querySelectorAll("input")[2] ? row.querySelectorAll("input")[2].value.trim()||null : null;
        if(!product_id){ showToast("Select a product for all rows"); return; }
        if(qty <= 0)   { showToast("Quantity must be greater than 0"); return; }
        items.push({product_id, qty, notes});
    }
    if(!items.length){ showToast("Add at least one product"); return; }

    let body = {
        farm_id:       selectedFarmId,
        delivery_date: document.getElementById("d-date").value,
        received_by:   document.getElementById("d-receiver").value.trim()||null,
        quality_notes: document.getElementById("d-quality").value.trim()||null,
        notes:         document.getElementById("d-notes").value.trim()||null,
        items,
    };

    // If editingDeliveryId is set → PUT (edit), otherwise → POST (create)
    let url    = editingDeliveryId ? `/farm/api/deliveries/${editingDeliveryId}` : "/farm/api/deliveries";
    let method = editingDeliveryId ? "PUT" : "POST";

    let res  = await fetch(url, {method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }

    closeDeliveryModal();
    let msg = editingDeliveryId
        ? `${data.delivery_number} updated ✓ — stock reversed & reapplied`
        : `${data.delivery_number} saved ✓ — ${data.items_count} products added to stock`;
    showToast(msg);
    await refreshAll();
}

async function refreshAll(){
    allProducts = await (await fetch("/farm/api/products-list")).json();
    allFarms    = await (await fetch("/farm/api/farms")).json();
    renderFarmCards();
    loadDeliveries();
    loadStats();
}

/* ── PRODUCT SEARCH IN ITEMS ── */
function addDeliveryItem(){
    let div = document.createElement("div");
    div.className  = "item-row";
    div.dataset.productId = "";
    div.innerHTML  = `
        <div class="prod-search-wrap">
            <input type="text" class="prod-search-input" placeholder="Search by name or SKU..." autocomplete="off">
            <div class="prod-dropdown"></div>
        </div>
        <input type="number" placeholder="0" min="0.001" step="any">
        <span class="unit-label">—</span>
        <input type="text" placeholder="e.g. fresh, grade A">
        <button class="rm-btn" onclick="this.closest('.item-row').remove()">×</button>
    `;

    let searchInput = div.querySelector(".prod-search-input");
    let dropdown    = div.querySelector(".prod-dropdown");

    searchInput.addEventListener("input", ()=> showDropdown(searchInput, dropdown, div));
    searchInput.addEventListener("focus", ()=> showDropdown(searchInput, dropdown, div));

    // Close dropdown when clicking outside
    document.addEventListener("click", function(e){
        if(!div.contains(e.target)) dropdown.style.display = "none";
    });

    document.getElementById("delivery-items").appendChild(div);
}

function showDropdown(input, dropdown, row){
    let q = input.value.toLowerCase();
    let matches = allProducts.filter(p=>
        p.name.toLowerCase().includes(q) || p.sku.toLowerCase().includes(q)
    ).slice(0,10);

    if(!matches.length){
        dropdown.innerHTML = `<div style="padding:10px 12px;color:var(--muted);font-size:13px">No products found</div>`;
        dropdown.style.display = "block";
        return;
    }

    dropdown.innerHTML = matches.map(p=>`
        <div class="prod-option" data-id="${p.id}" data-name="${p.name}" data-unit="${p.unit}" data-stock="${p.stock}">
            <div class="prod-option-name">${p.name}</div>
            <div class="prod-option-meta">
                <span style="font-family:var(--mono)">${p.sku}</span>
                &nbsp;·&nbsp;
                <span style="color:var(--green)">${p.stock.toFixed(0)} ${p.unit} in stock</span>
            </div>
        </div>`).join("");

    dropdown.querySelectorAll(".prod-option").forEach(opt=>{
        opt.addEventListener("click", ()=>{
            input.value          = opt.dataset.name;
            row.dataset.productId= opt.dataset.id;
            row.querySelector(".unit-label").innerText = opt.dataset.unit;
            dropdown.style.display = "none";
            row.querySelectorAll("input")[1].focus();
        });
    });

    dropdown.style.display = "block";
}

let toastTimer=null;
function showToast(msg){
    let t=document.getElementById("toast");
    t.innerText=msg; t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer=setTimeout(()=>t.classList.remove("show"),4000);
}

function getErrorMessage(source, fallback="Something went wrong"){
    if(source == null) return fallback;
    if(typeof source === "string") return source;
    if(Array.isArray(source)){
        let parts = source
            .map(item => getErrorMessage(item, ""))
            .filter(Boolean);
        return parts.length ? parts.join("; ") : fallback;
    }
    if(source instanceof Error){
        return source.message || fallback;
    }
    if(typeof source === "object"){
        if(source.detail != null) return getErrorMessage(source.detail, fallback);
        if(source.message != null) return getErrorMessage(source.message, fallback);
        if(source.error != null) return getErrorMessage(source.error, fallback);
        if(source.msg != null) return getErrorMessage(source.msg, fallback);
        if(source.loc && source.msg){
            return `${source.loc.join(" -> ")}: ${source.msg}`;
        }
    }
    return fallback;
}

document.getElementById("delivery-modal").addEventListener("click",function(e){
    if(e.target===this) closeDeliveryModal();
});
document.getElementById("weather-modal").addEventListener("click",function(e){
    if(e.target===this) closeWeatherModal();
});
document.getElementById("add-farm-modal").addEventListener("click",function(e){
    if(e.target===this) closeAddFarmModal();
});
document.getElementById("archived-farms-modal").addEventListener("click",function(e){
    if(e.target===this) closeArchivedModal();
});
document.getElementById("delete-farm-modal").addEventListener("click",function(e){
    if(e.target===this) closeDeleteFarmModal();
});
document.getElementById("del-farm-input").addEventListener("input", onDeleteFarmInput);
document.getElementById("del-farm-input").addEventListener("keydown", function(e){
    if(e.key === "Enter"){
        e.preventDefault();
        let btn = document.getElementById("del-farm-confirm-btn");
        if(!btn.disabled) confirmDeleteFarm();
    } else if(e.key === "Escape"){
        closeDeleteFarmModal();
    }
});

/* ── ADD FARM ── */
function openAddFarmModal(){
    document.getElementById("af-name").value     = "";
    document.getElementById("af-location").value = "";
    document.getElementById("af-notes").value    = "";
    document.getElementById("add-farm-modal").classList.add("open");
    setTimeout(()=>document.getElementById("af-name").focus(), 100);
}

function closeAddFarmModal(){
    document.getElementById("add-farm-modal").classList.remove("open");
}

async function saveNewFarm(){
    let name     = document.getElementById("af-name").value.trim();
    let location = document.getElementById("af-location").value.trim();
    let notes    = document.getElementById("af-notes").value.trim();
    if(!name){ showToast("Farm name is required"); document.getElementById("af-name").focus(); return; }

    let btn = document.getElementById("af-save-btn");
    btn.disabled = true;
    btn.innerText = "Saving…";

    try {
        let url = `/farm/api/farms?name=${encodeURIComponent(name)}&location=${encodeURIComponent(location)}&notes=${encodeURIComponent(notes)}`;
        let res  = await fetch(url, {method:"POST"});
        let data = await res.json();
        if(data.detail){ showToast("Error: "+data.detail); return; }

        closeAddFarmModal();
        showToast(`${name} created ✓ — it now appears everywhere`);

        // Reload farms then refresh everything
        allFarms = await (await fetch("/farm/api/farms")).json();
        renderFarmCards();
        fillFarmFilter();
        fillWeatherFarmFilter();
        fillSeasonFarmSelect();
        await loadStats();
    } catch(err) {
        showToast("Error: "+err.message);
    } finally {
        btn.disabled  = false;
        btn.innerText = "✓ Create Farm";
    }
}

/* ── DELETE FARM (admin only, type-to-confirm) ── */
let _pendingDelete = null;

function openDeleteFarmModal(id, name, deliveryCount){
    if(!isAdmin){ showToast("Admin access required"); return; }

    // The on-card × always ARCHIVES (soft delete). Permanent removal lives in
    // the Archived modal — that flow calls openHardDeleteFarmModal(id, name).
    _pendingDelete = { id, name, deliveryCount, hard: false };

    let title = "⚠️ Archive farm?";
    let sub   = "The farm will be hidden from active lists. All history (deliveries, weather logs, expenses, employees, animals) is preserved and an admin can restore it later from the 🗄️ Archived view.";
    let warningHtml = deliveryCount > 0
        ? `You are about to <strong>archive</strong> "<strong>${escapeHtml(name)}</strong>" — ${deliveryCount} delivery${deliveryCount === 1 ? "" : "ies"} on record. It will disappear from active lists but all history stays intact.`
        : `You are about to <strong>archive</strong> "<strong>${escapeHtml(name)}</strong>". It will disappear from active lists; any linked records (expenses, animals, employees) stay intact.`;
    let btnText = "Archive farm";

    document.getElementById("del-farm-title").innerText    = title;
    document.getElementById("del-farm-sub").innerText      = sub;
    document.getElementById("del-farm-warning").innerHTML  = warningHtml;
    document.getElementById("del-farm-expected").innerText = name;
    document.getElementById("del-farm-input").value        = "";
    document.getElementById("del-farm-mismatch").style.display = "none";
    let btn = document.getElementById("del-farm-confirm-btn");
    btn.innerText = btnText;
    btn.disabled  = true;
    btn.style.opacity = ".4";
    btn.style.cursor  = "not-allowed";

    document.getElementById("delete-farm-modal").classList.add("open");
    setTimeout(()=>document.getElementById("del-farm-input").focus(), 100);
}

function openHardDeleteFarmModal(id, name){
    if(!isAdmin){ showToast("Admin access required"); return; }
    _pendingDelete = { id, name, deliveryCount: 0, hard: true };

    document.getElementById("del-farm-title").innerText    = "⚠️ Delete farm permanently?";
    document.getElementById("del-farm-sub").innerText      = "This cannot be undone. If any other record still references the farm the delete will be blocked.";
    document.getElementById("del-farm-warning").innerHTML  = `You are about to <strong style="color:var(--danger)">permanently delete</strong> "<strong>${escapeHtml(name)}</strong>". This cannot be undone.`;
    document.getElementById("del-farm-expected").innerText = name;
    document.getElementById("del-farm-input").value        = "";
    document.getElementById("del-farm-mismatch").style.display = "none";
    let btn = document.getElementById("del-farm-confirm-btn");
    btn.innerText = "Delete forever";
    btn.disabled  = true;
    btn.style.opacity = ".4";
    btn.style.cursor  = "not-allowed";

    document.getElementById("delete-farm-modal").classList.add("open");
    setTimeout(()=>document.getElementById("del-farm-input").focus(), 100);
}

function closeDeleteFarmModal(){
    document.getElementById("delete-farm-modal").classList.remove("open");
    _pendingDelete = null;
}

function onDeleteFarmInput(){
    if(!_pendingDelete) return;
    let typed = document.getElementById("del-farm-input").value;
    let match = (typed === _pendingDelete.name);  // case-sensitive, exact
    let btn = document.getElementById("del-farm-confirm-btn");
    btn.disabled = !match;
    btn.style.opacity = match ? "1" : ".4";
    btn.style.cursor  = match ? "pointer" : "not-allowed";
    // Only show the mismatch hint if the user has typed something
    let mismatchEl = document.getElementById("del-farm-mismatch");
    mismatchEl.style.display = (typed.length > 0 && !match) ? "block" : "none";
}

async function confirmDeleteFarm(){
    if(!_pendingDelete) return;
    let typed = document.getElementById("del-farm-input").value;
    if(typed !== _pendingDelete.name){ onDeleteFarmInput(); return; }

    let { id, name, hard } = _pendingDelete;
    let btn = document.getElementById("del-farm-confirm-btn");
    let originalText = btn.innerText;
    btn.disabled = true;
    btn.innerText = "Working…";

    try {
        let url = `/farm/api/farms/${id}` + (hard ? "?hard=true" : "");
        let res  = await fetch(url, {method:"DELETE"});
        let data = await res.json().catch(()=>({}));
        if(!res.ok || data.detail){
            showToast("Error: " + (data.detail || `Failed (${res.status})`));
            btn.disabled = false;
            btn.innerText = originalText;
            return;
        }

        closeDeleteFarmModal();
        if(data.mode === "soft"){
            showToast(`${name} archived ✓ — history preserved`);
        } else {
            showToast(`${name} deleted ✓`);
        }

        // Refresh everything that lists farms
        allFarms = await (await fetch("/farm/api/farms")).json();
        renderFarmCards();
        fillFarmFilter();
        if(typeof fillWeatherFarmFilter === "function") fillWeatherFarmFilter();
        if(typeof fillSeasonFarmSelect === "function") fillSeasonFarmSelect();
        let curFilter = document.getElementById("farm-filter");
        if(curFilter && String(curFilter.value) === String(id)){
            curFilter.value = "";
            deliveryPage = 0;
            loadDeliveries();
        }
        // If the archived modal is open (the hard-delete flow), refresh its list too.
        let archModal = document.getElementById("archived-farms-modal");
        if(archModal && archModal.classList.contains("open")){
            loadArchivedFarms();
        }
        await loadStats();
    } catch(err) {
        showToast("Error: " + err.message);
        btn.disabled = false;
        btn.innerText = originalText;
    }
}

/* ── ARCHIVED FARMS (admin only) ── */
function escapeHtml(s){
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function openArchivedModal(){
    if(!isAdmin){ showToast("Admin access required"); return; }
    document.getElementById("archived-farms-modal").classList.add("open");
    loadArchivedFarms();
}

function closeArchivedModal(){
    document.getElementById("archived-farms-modal").classList.remove("open");
}

async function loadArchivedFarms(){
    let list = document.getElementById("archived-farms-list");
    list.innerHTML = `<div style="color:var(--muted);padding:14px;text-align:center;">Loading archived farms…</div>`;
    try {
        let res  = await fetch("/farm/api/farms/archived");
        if(!res.ok){
            let data = await res.json().catch(()=>({}));
            list.innerHTML = `<div style="color:var(--danger);padding:14px;text-align:center;">Error: ${escapeHtml(data.detail || ("Failed ("+res.status+")"))}</div>`;
            return;
        }
        let farms = await res.json();
        if(!farms.length){
            list.innerHTML = `<div style="color:var(--muted);padding:20px;text-align:center;">No archived farms.</div>`;
            return;
        }
        list.innerHTML = farms.map(f => {
            return `
                <div class="arch-row">
                    <div class="arch-info">
                        <div class="arch-name">🌾 ${escapeHtml(f.name)}</div>
                        <div class="arch-meta">${escapeHtml(f.location || "—")} • ${f.delivery_count} deliveries • ${f.weather_count} weather logs</div>
                    </div>
                    <div class="arch-actions">
                        <button type="button" class="action-btn" data-arch-action="restore" data-farm-id="${f.id}" data-farm-name="${escapeHtml(f.name)}">↺ Restore</button>
                        <button type="button" class="action-btn danger" data-arch-action="hard-delete" data-farm-id="${f.id}" data-farm-name="${escapeHtml(f.name)}" title="Permanently delete — only works if no other record references this farm">Delete forever</button>
                    </div>
                </div>
            `;
        }).join("");

        // Install click delegation once for the archived list.
        if(!list._delegationBound){
            list._delegationBound = true;
            list.addEventListener("click", function(e){
                let btn = e.target.closest("[data-arch-action]");
                if(!btn) return;
                let id     = parseInt(btn.dataset.farmId, 10);
                let name   = btn.dataset.farmName;
                let action = btn.dataset.archAction;
                if(action === "restore")    restoreFarm(id, name);
                if(action === "hard-delete") hardDeleteFarm(id, name);
            });
        }
    } catch(err){
        list.innerHTML = `<div style="color:var(--danger);padding:14px;text-align:center;">Error: ${escapeHtml(err.message)}</div>`;
    }
}

async function restoreFarm(id, name){
    if(!isAdmin){ showToast("Admin access required"); return; }
    if(!confirm(`Restore "${name}"?\n\nIt will reappear in all active farm lists and dropdowns.`)) return;
    try {
        let res  = await fetch(`/farm/api/farms/${id}/restore`, {method:"POST"});
        let data = await res.json();
        if(!res.ok || data.detail){
            showToast("Error: " + (data.detail || `Failed (${res.status})`));
            return;
        }
        showToast(`${name} restored ✓`);

        // Refresh the active farms list + dropdowns + stats, and the archived list inside the modal
        allFarms = await (await fetch("/farm/api/farms")).json();
        renderFarmCards();
        fillFarmFilter();
        if(typeof fillWeatherFarmFilter === "function") fillWeatherFarmFilter();
        if(typeof fillSeasonFarmSelect === "function") fillSeasonFarmSelect();
        await loadStats();
        loadArchivedFarms();
    } catch(err){
        showToast("Error: " + err.message);
    }
}

async function hardDeleteFarm(id, name){
    if(!isAdmin){ showToast("Admin access required"); return; }
    // Route through the type-to-confirm modal in HARD-delete mode.
    openHardDeleteFarmModal(id, name);
}

/* ── WEATHER LOG ── */
let editingWeatherId = null;

async function loadWeatherLogs(){
    let farmId = document.getElementById("weather-farm-filter").value;
    let url    = "/farm/api/weather-logs?limit=120" + (farmId ? `&farm_id=${farmId}` : "");
    let logs   = await (await fetch(url)).json();
    if(!logs.length){
        document.getElementById("weather-body").innerHTML =
            `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px">No weather logs yet. Click <b>+ Log Weather</b>.</td></tr>`;
        return;
    }
    document.getElementById("weather-body").innerHTML = logs.map(w=>`
        <tr>
            <td style="font-family:var(--mono);font-size:12px">${w.log_date}</td>
            <td>${w.farm_name}</td>
            <td style="font-family:var(--mono);color:var(--blue)">${w.temp_min!=null?w.temp_min+"°":"—"}</td>
            <td style="font-family:var(--mono);color:var(--orange)">${w.temp_max!=null?w.temp_max+"°":"—"}</td>
            <td style="font-family:var(--mono);color:var(--teal)">${w.rainfall_mm!=null?w.rainfall_mm+" mm":"—"}</td>
            <td style="font-family:var(--mono);color:var(--purple)">${w.humidity_pct!=null?w.humidity_pct+"%":"—"}</td>
            <td style="font-size:12px;color:var(--muted)">${w.notes||"—"}</td>
            <td>
                <div style="display:flex;gap:6px">
                    ${hasPermission('action_farm_weather_log') ? `<button class="action-btn" onclick="openEditWeather(${JSON.stringify(w).replace(/"/g,'&quot;')})">Edit</button>
                    <button class="action-btn danger" onclick="deleteWeatherLog(${w.id},'${w.log_date}')">Delete</button>` : `<span></span>`}
                </div>
            </td>
        </tr>`).join("");
}

function openWeatherModal(){
    editingWeatherId = null;
    document.getElementById("weather-modal-title").innerText = "Log Weather";
    document.getElementById("w-date").value  = new Date().toISOString().split("T")[0];
    document.getElementById("w-tmin").value  = "";
    document.getElementById("w-tmax").value  = "";
    document.getElementById("w-rain").value  = "";
    document.getElementById("w-hum").value   = "";
    document.getElementById("w-notes").value = "";
    if(allFarms.length) document.getElementById("w-farm").value = allFarms[0].id;
    document.getElementById("weather-modal").classList.add("open");
}

function openEditWeather(w){
    editingWeatherId = w.id;
    document.getElementById("weather-modal-title").innerText = "Edit Weather Log";
    document.getElementById("w-farm").value  = w.farm_id;
    document.getElementById("w-date").value  = w.log_date;
    document.getElementById("w-tmin").value  = w.temp_min  != null ? w.temp_min  : "";
    document.getElementById("w-tmax").value  = w.temp_max  != null ? w.temp_max  : "";
    document.getElementById("w-rain").value  = w.rainfall_mm != null ? w.rainfall_mm : "";
    document.getElementById("w-hum").value   = w.humidity_pct != null ? w.humidity_pct : "";
    document.getElementById("w-notes").value = w.notes || "";
    document.getElementById("weather-modal").classList.add("open");
}

function closeWeatherModal(){
    editingWeatherId = null;
    document.getElementById("weather-modal").classList.remove("open");
    setWeatherStatus("", false);
}

function setWeatherStatus(msg, loading){
    let el = document.getElementById("weather-fetch-status");
    if(!msg){ el.style.display = "none"; return; }
    el.style.display = "";
    el.innerText = msg;
    let btn = document.getElementById("autofill-btn");
    let icon = document.getElementById("autofill-icon");
    btn.disabled = loading;
    icon.innerText = loading ? "⏳" : "⛅";
}

async function autoFillWeather(){
    let farmId = parseInt(document.getElementById("w-farm").value);
    let farm   = allFarms.find(f => f.id === farmId);
    if(!farm){ showToast("Select a farm first"); return; }

    let location = farm.location && farm.location !== "—" ? farm.location : farm.name;
    // Normalize: "Nuweiba, South Sinai" → "Nuweiba+South+Sinai"
    let query = location.replace(/,\\s*/g, "+").replace(/\\s+/g, "+");
    setWeatherStatus(`Fetching weather for "${location}" from wttr.in…`, true);

    try {
        // wttr.in uses The Weather Channel data — free, no API key
        let res  = await __erpFetch(`https://wttr.in/${query}?format=j1`);
        if(!res.ok) throw new Error(`HTTP ${res.status}`);
        let wx   = await res.json();

        let cur  = wx.current_condition?.[0];
        let day  = wx.weather?.[0];   // today
        if(!cur || !day) throw new Error("Unexpected response format");

        let tmin = parseFloat(day.mintempC);
        let tmax = parseFloat(day.maxtempC);
        let hum  = parseFloat(cur.humidity);

        // Sum hourly precipMM for total rainfall today
        let rain = (day.hourly || []).reduce((s, h) => s + parseFloat(h.precipMM || 0), 0);

        document.getElementById("w-tmin").value = tmin.toFixed(1);
        document.getElementById("w-tmax").value = tmax.toFixed(1);
        document.getElementById("w-rain").value = rain.toFixed(1);
        document.getElementById("w-hum").value  = hum.toFixed(0);

        let desc = cur.weatherDesc?.[0]?.value || "";
        setWeatherStatus(`✓ ${desc} — ${tmin}°C / ${tmax}°C, ${rain.toFixed(1)} mm rain, ${hum}% humidity (wttr.in / The Weather Channel)`, false);
    } catch(err) {
        setWeatherStatus(`Could not fetch weather data: ${err.message}. Fill in manually.`, false);
    }
}

async function saveWeatherLog(){
    let body = {
        farm_id:      parseInt(document.getElementById("w-farm").value),
        log_date:     document.getElementById("w-date").value,
        temp_min:     document.getElementById("w-tmin").value  !== "" ? parseFloat(document.getElementById("w-tmin").value)  : null,
        temp_max:     document.getElementById("w-tmax").value  !== "" ? parseFloat(document.getElementById("w-tmax").value)  : null,
        rainfall_mm:  document.getElementById("w-rain").value  !== "" ? parseFloat(document.getElementById("w-rain").value)  : null,
        humidity_pct: document.getElementById("w-hum").value   !== "" ? parseFloat(document.getElementById("w-hum").value)   : null,
        notes:        document.getElementById("w-notes").value.trim() || null,
    };
    if(!body.log_date){ showToast("Date is required"); return; }
    let url    = editingWeatherId ? `/farm/api/weather-logs/${editingWeatherId}` : "/farm/api/weather-logs";
    let method = editingWeatherId ? "PUT" : "POST";
    let res    = await fetch(url, {method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    let data   = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeWeatherModal();
    showToast(editingWeatherId ? "Weather log updated ✓" : "Weather log saved ✓");
    loadWeatherLogs();
}

async function deleteWeatherLog(id, dateStr){
    if(!confirm(`Delete weather log for ${dateStr}?`)) return;
    let res  = await fetch(`/farm/api/weather-logs/${id}`, {method:"DELETE"});
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast("Deleted ✓");
    loadWeatherLogs();
}

/* ── SEASON ANALYSIS ── */
async function loadSeasonAnalysis(){
    let farmId   = document.getElementById("season-farm").value;
    let dateFrom = document.getElementById("season-from").value;
    let dateTo   = document.getElementById("season-to").value;
    if(!farmId)  { showToast("Select a farm first"); return; }
    if(!dateFrom || !dateTo){ showToast("Set a date range"); return; }
    if(dateFrom > dateTo){ showToast("Start date must be before end date"); return; }

    try{
        let res  = await fetch(`/expenses/api/cost-allocation?farm_id=${encodeURIComponent(farmId)}&date_from=${dateFrom}&date_to=${dateTo}`);
        let data = null;
        try{
            data = await res.json();
        }catch(_err){
            data = null;
        }
        if(!res.ok){
            showToast("Error: " + getErrorMessage(data, `Request failed (${res.status})`));
            return;
        }
        if(data && data.detail){
            showToast("Error: " + getErrorMessage(data));
            return;
        }
        const costCategories = Array.isArray(data.cost_by_category) ? data.cost_by_category : [];
        const products = Array.isArray(data.products) ? data.products : [];

        document.getElementById("season-empty").style.display  = "none";
        document.getElementById("season-result").style.display = "";

        // Summary cards
        document.getElementById("season-summary-cards").innerHTML = `
            <div class="stat-card" style="border-top:2px solid var(--blue)"><div class="stat-label">Scope</div><div class="stat-value" style="font-size:20px;color:var(--blue)">${data.farm_scope_label || data.farm_name}</div></div>
            <div class="stat-card green"><div class="stat-label">Total Farm Costs</div><div class="stat-value green" style="font-size:20px">${Number(data.total_cost || 0).toLocaleString(undefined,{minimumFractionDigits:2})} EGP</div></div>
            <div class="stat-card" style="border-top:2px solid var(--orange)"><div class="stat-label">Salary & Wages</div><div class="stat-value" style="font-size:20px;color:var(--orange)">${Number(data.salary_cost || 0).toLocaleString(undefined,{minimumFractionDigits:2})} EGP</div></div>
            <div class="stat-card" style="border-top:2px solid var(--warn)"><div class="stat-label">Labor Cost</div><div class="stat-value" style="font-size:20px;color:var(--warn)">${Number(data.labor_cost || 0).toLocaleString(undefined,{minimumFractionDigits:2})} EGP</div></div>
            <div class="stat-card lime"><div class="stat-label">Total Harvested</div><div class="stat-value lime" style="font-size:20px">${Number(data.total_qty || 0).toFixed(1)} units</div></div>
            <div class="stat-card teal"><div class="stat-label">Expenses Tagged</div><div class="stat-value teal" style="font-size:20px">${Number(data.expense_count || 0)}</div></div>
            <div class="stat-card" style="border-top:2px solid var(--orange)"><div class="stat-label">Deliveries</div><div class="stat-value" style="font-size:20px;color:var(--orange)">${Number(data.delivery_count || 0)}</div></div>
        `;

        // Cost breakdown chart
        let maxCost = costCategories.length ? costCategories[0].amount : 1;
        document.getElementById("season-cost-breakdown").innerHTML = `
            <div class="history-title">Cost Breakdown by Category</div>
            ${costCategories.length === 0
                ? `<div style="color:var(--muted);font-size:13px">No expenses tagged to ${data.farm_scope_label || data.farm_name} for this period.<br>Go to <a href="/expenses/" style="color:var(--lime)">Expenses</a> and tag expenses to the relevant farm.</div>`
                : costCategories.map(c=>`
                    <div class="history-bar-row">
                        <div class="history-bar-label">${c.name}</div>
                        <div class="history-bar-track"><div class="history-bar-fill" style="width:${(c.amount/maxCost*100).toFixed(1)}%;background:linear-gradient(90deg,var(--orange),var(--warn))"></div></div>
                        <div class="history-bar-val">${c.amount.toLocaleString(undefined,{minimumFractionDigits:0})}</div>
                    </div>`).join("")}
        `;

        // Product harvest chart
        let maxQty = products.length ? products[0].total_qty : 1;
        document.getElementById("season-product-chart").innerHTML = `
            <div class="history-title">Harvest by Product</div>
            ${products.length === 0
                ? `<div style="color:var(--muted);font-size:13px">No deliveries from ${data.farm_scope_label || data.farm_name} in this period.</div>`
                : products.map(p=>`
                    <div class="history-bar-row">
                        <div class="history-bar-label">${p.product_name}</div>
                        <div class="history-bar-track"><div class="history-bar-fill" style="width:${(p.total_qty/maxQty*100).toFixed(1)}%;background:linear-gradient(90deg,var(--lime),var(--green))"></div></div>
                        <div class="history-bar-val">${p.total_qty.toFixed(1)}</div>
                    </div>`).join("")}
        `;

        // Products table
        if(!products.length){
            document.getElementById("season-body").innerHTML =
                `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px">No deliveries recorded for ${data.farm_scope_label || data.farm_name} in this period.</td></tr>`;
        } else {
            document.getElementById("season-body").innerHTML = products.map(p=>{
                let marginColor = p.profit_margin_pct >= 30 ? "var(--green)" : p.profit_margin_pct >= 0 ? "var(--warn)" : "var(--danger)";
                let profitColor = p.profit_per_unit >= 0 ? "var(--green)" : "var(--danger)";
                return `<tr>
                    <td class="name">${p.product_name}</td>
                    <td style="font-family:var(--mono)">${p.total_qty.toFixed(2)} ${p.unit}</td>
                    <td style="font-family:var(--mono);color:var(--muted)">${p.share_pct}%</td>
                    <td style="font-family:var(--mono);color:var(--orange)">${p.allocated_cost.toLocaleString(undefined,{minimumFractionDigits:2})}</td>
                    <td style="font-family:var(--mono);color:var(--warn)">${p.cost_per_unit.toFixed(2)}</td>
                    <td style="font-family:var(--mono);color:var(--blue)">${p.sale_price.toFixed(2)}</td>
                    <td style="font-family:var(--mono);font-weight:700;color:${profitColor}">${p.profit_per_unit.toFixed(2)}</td>
                    <td style="font-family:var(--mono);font-weight:700;color:${marginColor}">${p.profit_margin_pct}%</td>
                </tr>`;
            }).join("");
        }
    }catch(err){
        showToast("Error: " + getErrorMessage(err, "Unable to analyze farm intake"));
    }
}

</script>
</body>
</html>
"""