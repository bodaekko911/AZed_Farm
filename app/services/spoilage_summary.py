"""
Spoilage summary — how much was lost, what it cost, and against what
====================================================================
Powers the figures on the spoilage screen and the spoilage report, from one
definition so the two agree.

  • quantity lost   — summed in kilograms where products convert, so a bunch
                      of herbs and a crate of tomatoes are not simply added
  • cost of it      — qty × the product's cost, the same cost basis batch
                      costing uses
  • loss rate       — spoiled kilograms against what the farm actually
                      delivered in the same window

Farm intake is the denominator rather than production output. Every kilogram
the farm produces enters as a delivery, whether it is later sold fresh or
processed, so intake is the upstream total — adding processed output on top
would count the same produce twice. The same reasoning the carbon report uses
for its intensity metric. Production output is returned alongside so a caller
can show the processed-only rate too.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.units import to_kilograms


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def _delivered_kg(db: AsyncSession, d_from: date, d_to: date) -> float:
    from app.models.farm import FarmDelivery, FarmDeliveryItem
    from app.models.product import Product

    rows = await db.execute(
        select(FarmDeliveryItem.qty, FarmDeliveryItem.unit, Product)
        .join(FarmDelivery, FarmDeliveryItem.delivery_id == FarmDelivery.id)
        .join(Product, FarmDeliveryItem.product_id == Product.id)
        .where(FarmDelivery.delivery_date >= d_from, FarmDelivery.delivery_date <= d_to)
    )
    total = 0.0
    for qty, unit, product in rows.all():
        kg = to_kilograms(qty, unit, product)
        if kg is not None:
            total += kg
    return total


async def _produced_kg(db: AsyncSession, d_from: date, d_to: date) -> float:
    from app.models.product import Product
    from app.models.production import BatchOutput, ProductionBatch

    rows = await db.execute(
        select(BatchOutput.qty, Product)
        .join(ProductionBatch, BatchOutput.batch_id == ProductionBatch.id)
        .join(Product, BatchOutput.product_id == Product.id)
        .where(
            func.date(ProductionBatch.created_at) >= d_from,
            func.date(ProductionBatch.created_at) <= d_to,
        )
    )
    total = 0.0
    for qty, product in rows.all():
        kg = to_kilograms(qty, None, product)
        if kg is not None:
            total += kg
    return total


async def spoilage_summary(db: AsyncSession, *, d_from: date, d_to: date) -> dict:
    from app.models.spoilage import SpoilageRecord

    result = await db.execute(
        select(SpoilageRecord)
        .options(selectinload(SpoilageRecord.product), selectinload(SpoilageRecord.farm))
        .where(
            SpoilageRecord.spoilage_date >= d_from,
            SpoilageRecord.spoilage_date <= d_to,
        )
    )
    records = result.scalars().all()

    by_product: dict[int, dict] = {}
    by_reason: dict[str, dict] = {}
    total_cost = 0.0
    total_kg = 0.0
    missing_mass: list[str] = []
    missing_cost: list[str] = []

    for record in records:
        product = record.product
        name = getattr(product, "name", None) or f"Product #{record.product_id}"
        qty = _num(record.qty)
        kg = to_kilograms(qty, None, product)
        unit_cost = _num(getattr(product, "cost", 0))
        cost = qty * unit_cost

        if kg is None:
            missing_mass.append(name)
        else:
            total_kg += kg
        if unit_cost <= 0 and qty > 0:
            missing_cost.append(name)
        total_cost += cost

        entry = by_product.setdefault(record.product_id, {
            "product_id": record.product_id,
            "product": name,
            "unit": getattr(product, "unit", None) or "",
            "qty": 0.0, "kg": 0.0, "cost": 0.0, "records": 0,
        })
        entry["qty"] += qty
        entry["kg"] += kg or 0.0
        entry["cost"] += cost
        entry["records"] += 1

        reason = (record.reason or "Unspecified").strip() or "Unspecified"
        bucket = by_reason.setdefault(reason, {"reason": reason, "qty": 0.0, "kg": 0.0,
                                               "cost": 0.0, "records": 0})
        bucket["qty"] += qty
        bucket["kg"] += kg or 0.0
        bucket["cost"] += cost
        bucket["records"] += 1

    products = sorted(by_product.values(), key=lambda r: r["cost"], reverse=True)
    reasons = sorted(by_reason.values(), key=lambda r: r["cost"], reverse=True)
    for row in products:
        row["qty"] = round(row["qty"], 3)
        row["kg"] = round(row["kg"], 3)
        row["cost"] = round(row["cost"], 2)
        row["cost_share_pct"] = round(row["cost"] / total_cost * 100, 1) if total_cost else 0.0
    for row in reasons:
        row["qty"] = round(row["qty"], 3)
        row["kg"] = round(row["kg"], 3)
        row["cost"] = round(row["cost"], 2)
        row["cost_share_pct"] = round(row["cost"] / total_cost * 100, 1) if total_cost else 0.0

    delivered_kg = await _delivered_kg(db, d_from, d_to)
    produced_kg = await _produced_kg(db, d_from, d_to)

    # Only meaningful when every spoiled item converts to kilograms — otherwise
    # the numerator is missing rows the denominator still contains.
    rate_usable = total_kg > 0 and delivered_kg > 0 and not missing_mass
    spoilage_pct = round(total_kg / delivered_kg * 100, 2) if rate_usable else None
    spoilage_pct_of_production = (
        round(total_kg / produced_kg * 100, 2)
        if total_kg > 0 and produced_kg > 0 and not missing_mass else None
    )

    return {
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "records": len(records),
        "total_qty_kg": round(total_kg, 3),
        "total_cost": round(total_cost, 2),
        "top_item": products[0] if products else None,
        "by_product": products,
        "by_reason": reasons,
        "delivered_kg": round(delivered_kg, 3),
        "produced_kg": round(produced_kg, 3),
        "spoilage_pct": spoilage_pct,
        "spoilage_pct_of_production": spoilage_pct_of_production,
        "products_missing_weight": sorted(set(missing_mass)),
        "products_missing_cost": sorted(set(missing_cost)),
        "cost_is_complete": not missing_cost,
    }
