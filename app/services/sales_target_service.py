"""Monthly sales target, with a floor and a stretch.

Setting a target fills in its floor (85% of it) and stretch (120%) unless they are given; either can be changed, as
long as floor ≤ target ≤ stretch. Clearing the target clears all three. Sales are counted as on the Sales dashboard
(see budget_service.sales_between).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time_utils import today_local
from app.services.budget_service import (check_year, month_items, month_range, num, parse_amount, plan_for,
                                         plans_for_year, sales_between, store_months)

FLOOR_SHARE = Decimal("0.85")
STRETCH_SHARE = Decimal("1.20")


def levels(target: Optional[Decimal], floor: Optional[Decimal] = None,
           stretch: Optional[Decimal] = None) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    """(floor, target, stretch) to store: the defaults filled in, and checked against each other."""
    if target is None:
        return None, None, None
    cent = Decimal("0.01")
    floor = floor if floor is not None else (target * FLOOR_SHARE).quantize(cent)
    stretch = stretch if stretch is not None else (target * STRETCH_SHARE).quantize(cent)
    if not floor <= target <= stretch:
        raise HTTPException(status_code=400, detail="The floor can't be above the target, nor the stretch below it")
    return floor, target, stretch


def _month(plan, sales: Optional[float]) -> dict:
    return {
        "sales_floor": num(plan.sales_floor) if plan else None,
        "sales_target": num(plan.sales_target) if plan else None,
        "sales_stretch": num(plan.sales_stretch) if plan else None,
        "sales": sales,
    }


async def year_overview(db: AsyncSession, year: int) -> dict:
    """The 12 months of `year`: floor, target and stretch, and for months that have started, the sales."""
    today = today_local()
    plans = await plans_for_year(db, year)
    months = []
    for m in range(1, 13):
        start, end = month_range(year, m)
        # Months still to come have nothing to compare yet.
        sales = await sales_between(db, start, min(end, today)) if start <= today else None
        months.append({"month": m, **_month(plans.get(m), sales), "current": start <= today <= end})
    return {"year": year, "months": months, "floor_share": float(FLOOR_SHARE), "stretch_share": float(STRETCH_SHARE)}


async def current_month(db: AsyncSession) -> dict:
    """This month's sales so far against its levels — for the Sales dashboard strip."""
    today = today_local()
    start, end = month_range(today.year, today.month)
    plan = await plan_for(db, start)
    return {"year": today.year, "month": today.month, "day": today.day, "days_in_month": end.day,
            **_month(plan, await sales_between(db, start, today))}


async def save_year(db: AsyncSession, year: int, months: list[dict], user) -> dict:
    """Set the target (and its floor and stretch) of the months given; an empty target clears the month's three."""
    check_year(year)
    wanted = {}
    for m, item in month_items(months).items():
        floor, target, stretch = levels(parse_amount(item.get("sales_target")), parse_amount(item.get("sales_floor")),
                                        parse_amount(item.get("sales_stretch")))
        wanted[m] = {"sales_floor": floor, "sales_target": target, "sales_stretch": stretch}
    return {"ok": True, "changed": await store_months(db, year, wanted, user, "Sales targets", "sales target")}
