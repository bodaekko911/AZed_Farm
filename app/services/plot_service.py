"""Seeds, fertilizer and other inputs put into a greenhouse or field — and the fish tanks of sandponic beds.

One place for the stock side, used by the farm page and by the field bot's reviewed drafts: an input taken from
inventory comes out of a storage (LocationStock and Product.stock, with a StockMove), as with animal feeding; one bought
straight for the plot is only recorded with its cost. Nothing here commits — the caller does, once for all its lines.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select

from app.core.product_types import is_stock_tracked_product
from app.models.farm import FISH_LOG_TYPES, PLOT_INPUT_TYPES, FarmPlot, FishLog, FishTank, PlotInput
from app.models.inventory import LocationStock, StockLocation, StockMove
from app.models.product import Product
from app.services.location_inventory_service import get_or_create_location_stock, quantize_qty


async def fullest_location(db, product_id: int, qty: Decimal) -> Optional[StockLocation]:
    """The active storage holding the most of a product, if it holds at least ``qty`` — for a worker who didn't say
    which store it came from."""
    row = (await db.execute(
        select(StockLocation, LocationStock.qty)
        .join(LocationStock, LocationStock.location_id == StockLocation.id)
        .where(LocationStock.product_id == product_id, StockLocation.is_active.is_(True))
        .order_by(LocationStock.qty.desc()).limit(1))).first()
    if row is None or Decimal(str(row[1] or 0)) < qty:
        return None
    return row[0]


async def add_plot_input(db, *, plot: FarmPlot, input_type: str, qty, applied_on: date, user,
                         product_id: Optional[int] = None, location_id: Optional[int] = None,
                         item_name: Optional[str] = None, unit: Optional[str] = None, total_cost=None,
                         crop: Optional[str] = None, note: Optional[str] = None) -> PlotInput:
    if not plot.is_active:
        raise HTTPException(status_code=400, detail=f"{plot.name} is archived")
    if input_type not in PLOT_INPUT_TYPES:
        raise HTTPException(status_code=400, detail="Type must be seed, fertilizer, pesticide or other")
    qty = Decimal(str(qty))
    if qty <= 0:
        raise HTTPException(status_code=400, detail="The quantity must be above 0")
    entry = PlotInput(plot_id=plot.id, input_type=input_type, qty=qty, applied_on=applied_on,
                      crop=(crop or "").strip()[:150] or None, note=(note or "").strip() or None,
                      user_id=getattr(user, "id", None))

    if product_id:
        product, location, qty_before = await _take_from_stock(db, product_id, location_id, qty)
        entry.product_id = product.id
        entry.location_id = location.id
        entry.item_name = product.name
        entry.unit = product.unit
        # A cost given wins; otherwise the product's cost price.
        entry.total_cost = (Decimal(str(total_cost)) if total_cost is not None
                            else (Decimal(str(product.cost or 0)) * qty).quantize(Decimal("0.01")))
        db.add(entry)
        await db.flush()
        db.add(StockMove(
            product_id=product.id, type="out", qty=qty, qty_before=qty_before, qty_after=qty_before - qty,
            ref_type="plot_input", ref_id=entry.id,
            note=f"{input_type.capitalize()} for {plot.name} from {location.name}",
            user_id=getattr(user, "id", None),
        ))
    else:
        name = (item_name or "").strip()[:200]
        if not name:
            raise HTTPException(status_code=400, detail="Pick a product from inventory or type the item name")
        entry.item_name = name
        entry.unit = (unit or "").strip()[:30] or None
        entry.total_cost = Decimal(str(total_cost)) if total_cost is not None else None
        db.add(entry)
        await db.flush()
    return entry


async def reverse_plot_input(db, entry: PlotInput, user) -> None:
    """Undo an entry's stock side (if it came from inventory); the caller deletes the row."""
    if entry.product_id and entry.location_id:
        await _back_to_stock(db, entry.product_id, entry.location_id, entry.qty, user, "plot_input_reverse", entry.id,
                             f"Reversed {entry.input_type} entry #{entry.id}")


async def _take_from_stock(db, product_id: int, location_id: Optional[int], qty: Decimal):
    """Take ``qty`` of a product out of a storage (the one holding most of it when none is given). Returns the
    product, the storage and the product's total before — the caller adds the StockMove once it has its own id."""
    product = (await db.execute(select(Product).where(Product.id == product_id))).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if not is_stock_tracked_product(product):
        raise HTTPException(status_code=400, detail=f"{product.name} is a service — it has no stock to take from")
    if location_id:
        location = (await db.execute(select(StockLocation).where(
            StockLocation.id == location_id, StockLocation.is_active.is_(True)))).scalar_one_or_none()
        if not location:
            raise HTTPException(status_code=400, detail="Storage not found or inactive")
    else:
        location = await fullest_location(db, product.id, qty)
        if not location:
            raise HTTPException(status_code=400, detail=(
                f"No storage holds {float(qty):g} {product.unit or ''} of {product.name}".replace("  ", " ")))
    loc_stock = await get_or_create_location_stock(db, location_id=location.id, product_id=product.id)
    available = Decimal(str(loc_stock.qty or 0))
    if available < qty:
        raise HTTPException(status_code=400, detail=(
            f"Not enough {product.name} at {location.name}: have {float(available):g}, need {float(qty):g}"))
    qty_before = Decimal(str(product.stock or 0))
    loc_stock.qty = quantize_qty(available - qty)
    product.stock = qty_before - qty
    return product, location, qty_before


async def _back_to_stock(db, product_id: int, location_id: int, qty, user, ref_type: str, ref_id: int, note: str):
    product = (await db.execute(select(Product).where(Product.id == product_id))).scalar_one_or_none()
    if product is None:
        return
    qty = Decimal(str(qty or 0))
    qty_before = Decimal(str(product.stock or 0))
    product.stock = qty_before + qty
    loc_stock = await get_or_create_location_stock(db, location_id=location_id, product_id=product_id)
    loc_stock.qty = quantize_qty(Decimal(str(loc_stock.qty or 0)) + qty)
    db.add(StockMove(product_id=product.id, type="in", qty=qty, qty_before=qty_before, qty_after=qty_before + qty,
                     ref_type=ref_type, ref_id=ref_id, note=note, user_id=getattr(user, "id", None)))


# ── Fish tanks (sandponic) ───────────────────────────────────────────────────

async def add_fish_log(db, *, tank: FishTank, log_type: str, log_date: date, user, product_id: Optional[int] = None,
                       location_id: Optional[int] = None, qty=None, total_cost=None, fish_count: Optional[int] = None,
                       weight_kg=None, note: Optional[str] = None) -> FishLog:
    """Feed from stock, fish added, deaths or a harvest — the tank's count moves with the last three."""
    if not tank.is_active:
        raise HTTPException(status_code=400, detail=f"{tank.name} is archived")
    if log_type not in FISH_LOG_TYPES:
        raise HTTPException(status_code=400, detail="Type must be feed, stock_in, death or harvest")
    entry = FishLog(tank_id=tank.id, log_type=log_type, log_date=log_date, note=(note or "").strip() or None,
                    user_id=getattr(user, "id", None))
    if log_type == "feed":
        if not product_id:
            raise HTTPException(status_code=400, detail="Pick the feed product")
        qty = Decimal(str(qty or 0))
        if qty <= 0:
            raise HTTPException(status_code=400, detail="The quantity must be above 0")
        product, location, qty_before = await _take_from_stock(db, product_id, location_id, qty)
        entry.product_id, entry.location_id, entry.qty = product.id, location.id, qty
        entry.total_cost = (Decimal(str(total_cost)) if total_cost is not None
                            else (Decimal(str(product.cost or 0)) * qty).quantize(Decimal("0.01")))
        db.add(entry)
        await db.flush()
        db.add(StockMove(product_id=product.id, type="out", qty=qty, qty_before=qty_before, qty_after=qty_before - qty,
                         ref_type="fish_feed", ref_id=entry.id, note=f"Fish feed for {tank.name} from {location.name}",
                         user_id=getattr(user, "id", None)))
        return entry
    count = int(fish_count or 0)
    if count <= 0:
        raise HTTPException(status_code=400, detail="The number of fish must be above 0")
    if log_type != "stock_in" and count > (tank.fish_count or 0):
        raise HTTPException(status_code=400, detail=f"{tank.name} has only {tank.fish_count or 0} fish")
    entry.fish_count = count
    if log_type == "harvest" and weight_kg is not None:
        if float(weight_kg) < 0:
            raise HTTPException(status_code=400, detail="The weight can't be negative")
        entry.weight_kg = Decimal(str(weight_kg))
    tank.fish_count = (tank.fish_count or 0) + (count if log_type == "stock_in" else -count)
    db.add(entry)
    await db.flush()
    return entry


async def reverse_fish_log(db, entry: FishLog, tank: FishTank, user) -> None:
    """Undo a log: feed goes back to its storage, the fish count moves back. The caller deletes the row."""
    if entry.log_type == "feed":
        if entry.product_id and entry.location_id:
            await _back_to_stock(db, entry.product_id, entry.location_id, entry.qty, user, "fish_feed_reverse",
                                 entry.id, f"Reversed fish feed #{entry.id}")
        return
    count = int(entry.fish_count or 0)
    if entry.log_type == "stock_in":
        if count > (tank.fish_count or 0):
            raise HTTPException(status_code=400, detail=(
                f"{tank.name} has only {tank.fish_count or 0} fish now — delete the later deaths or harvests first"))
        tank.fish_count = (tank.fish_count or 0) - count
    else:
        tank.fish_count = (tank.fish_count or 0) + count
