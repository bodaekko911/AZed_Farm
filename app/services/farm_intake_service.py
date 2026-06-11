from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.log import record
from app.models.carbon import CarbonEmissionFactor, CarbonLog
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.inventory import StockMove
from app.models.product import Product

# vehicle_type → carbon factor source_key (per-km, Scope 1)
VEHICLE_FACTOR_KEYS = {"van": "van_km", "truck": "truck_km"}


async def create_carbon_log_for_delivery(
    db: AsyncSession,
    delivery: FarmDelivery,
    user_id: int | None = None,
) -> bool:
    """Auto-create a transport CarbonLog for a farm delivery.

    Uses the delivery's vehicle_type to pick the per-km factor (van_km /
    truck_km) and distance_km as the quantity. Skips silently when distance
    is missing/zero or the factor is missing/inactive — same contract as the
    expense and spoilage auto-loggers. Returns True if a log was queued.
    """
    from decimal import Decimal

    distance = float(delivery.distance_km or 0)
    if distance <= 0:
        return False
    key = VEHICLE_FACTOR_KEYS.get((delivery.vehicle_type or "").strip().lower(), "van_km")
    result = await db.execute(
        select(CarbonEmissionFactor).where(
            CarbonEmissionFactor.source_key == key,
            CarbonEmissionFactor.is_active == True,
        )
    )
    factor = result.scalar_one_or_none()
    if not factor:
        return False
    qty = Decimal(str(distance))
    kg_co2e = (qty * factor.factor_kg_co2e_per_unit).quantize(Decimal("0.0001"))
    db.add(CarbonLog(
        factor_id=factor.id,
        farm_id=delivery.farm_id,
        user_id=user_id if user_id is not None else delivery.user_id,
        log_date=delivery.delivery_date,
        quantity=qty,
        kg_co2e=kg_co2e,
        ref_type="farm_delivery",
        ref_id=delivery.id,
        notes=f"Auto-logged from delivery {delivery.delivery_number} "
              f"({delivery.vehicle_type or 'van'}, {distance:g} km)",
    ))
    return True


async def resync_carbon_log_for_delivery(
    db: AsyncSession,
    delivery: FarmDelivery,
    user_id: int | None = None,
) -> None:
    """Delete this delivery's transport logs and recreate from current state.
    Used on delivery edit so distance/vehicle/date changes never leave stale
    emissions behind."""
    old_logs = await db.execute(
        select(CarbonLog).where(
            CarbonLog.ref_type == "farm_delivery",
            CarbonLog.ref_id == delivery.id,
        )
    )
    for _cl in old_logs.scalars().all():
        await db.delete(_cl)
    await create_carbon_log_for_delivery(db, delivery, user_id)


async def delete_carbon_log_for_delivery(db: AsyncSession, delivery_id: int) -> None:
    """Remove the auto-created transport logs when a delivery is deleted."""
    old_logs = await db.execute(
        select(CarbonLog).where(
            CarbonLog.ref_type == "farm_delivery",
            CarbonLog.ref_id == delivery_id,
        )
    )
    for _cl in old_logs.scalars().all():
        await db.delete(_cl)


async def create_farm_delivery(
    db: AsyncSession,
    *,
    farm: Farm,
    delivery_date: date,
    user_id: int | None,
    items: list[dict[str, Any]],
    received_by: str | None = None,
    quality_notes: str | None = None,
    notes: str | None = None,
    distance_km: float | None = None,
    vehicle_type: str | None = None,
    record_stock_movement: bool = True,
    activity_user=None,
) -> tuple[FarmDelivery, int]:
    if not items:
        raise ValueError("Delivery must have at least one item")

    max_id_result = await db.execute(select(func.max(FarmDelivery.id)))
    max_id = max_id_result.scalar() or 0
    number = f"FD-{str(max_id + 1).zfill(4)}"

    delivery = FarmDelivery(
        delivery_number=number,
        farm_id=farm.id,
        user_id=user_id,
        delivery_date=delivery_date,
        received_by=received_by,
        quality_notes=quality_notes,
        notes=notes,
        distance_km=distance_km if distance_km and distance_km > 0 else None,
        vehicle_type=(vehicle_type or "").strip().lower() or None,
    )
    db.add(delivery)
    await db.flush()

    # Transport emissions (carbon module) — skips silently when no distance.
    await create_carbon_log_for_delivery(db, delivery, user_id)

    stock_moves_created = 0
    for item in items:
        product_id = int(item["product_id"])
        qty = float(item["qty"])
        item_notes = item.get("notes")

        prod_result = await db.execute(select(Product).where(Product.id == product_id))
        product = prod_result.scalar_one_or_none()
        if not product:
            raise ValueError(f"Product not found: {product_id}")

        db.add(
            FarmDeliveryItem(
                delivery_id=delivery.id,
                product_id=product.id,
                qty=qty,
                unit=product.unit,
                notes=item_notes,
            )
        )

        if record_stock_movement:
            before = float(product.stock or 0)
            after = before + qty
            product.stock = after
            db.add(
                StockMove(
                    product_id=product.id,
                    type="in",
                    user_id=user_id,
                    qty=qty,
                    qty_before=before,
                    qty_after=after,
                    ref_type="farm_intake",
                    ref_id=delivery.id,
                    note=f"{farm.name} — {number}",
                )
            )
            stock_moves_created += 1

    stock_note = "with stock movement" if record_stock_movement else "without stock movement"
    record(
        db,
        "Farm",
        "create_delivery",
        f"Delivery {number} from {farm.name} — {len(items)} product(s) {stock_note}",
        user=activity_user,
        ref_type="farm_delivery",
        ref_id=delivery.id,
    )
    await db.flush()
    return delivery, stock_moves_created