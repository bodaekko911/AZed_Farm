"""Monthly spending budget — and the month helpers the Sales targets page shares.

Both pages keep their numbers on the same ``monthly_plans`` row for the month; each saves only its own fields, and a
row is removed once nothing on it is set. Sales are counted the way the Sales dashboard counts them, so the pages agree:
paid POS invoices minus retail refunds, plus B2B revenue (credits to account 4000 from B2B and consignment journals).
Spending is every expense dated in the month, as on the Expenses page.
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.log import record
from app.core.time_utils import today_local, utc_bounds
from app.models.accounting import Account, Journal, JournalEntry
from app.models.budget import MonthlyPlan
from app.models.expense import Expense
from app.models.invoice import Invoice
from app.models.refund import RetailRefund

B2B_REVENUE_REFS = ["b2b", "b2b_invoice", "consignment_payment", "consignment"]   # same as the Sales dashboard
MAX_AMOUNT = Decimal("999999999999.99")
PLAN_FIELDS = ("budget", "sales_target", "sales_floor", "sales_stretch")


def month_range(year: int, month: int) -> tuple[date, date]:
    """First and last day of the month."""
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def parse_amount(value) -> Optional[Decimal]:
    """An amount typed on the page: empty means "not set"; anything else must be a number from 0 up."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        raise HTTPException(status_code=400, detail=f"'{value}' is not an amount")
    if not amount.is_finite() or amount < 0:
        raise HTTPException(status_code=400, detail="Amounts can't be negative")
    if amount > MAX_AMOUNT:
        raise HTTPException(status_code=400, detail="Amount is too large")
    return amount.quantize(Decimal("0.01"))


def check_year(year: int) -> None:
    if not 2000 <= year <= 2100:
        raise HTTPException(status_code=400, detail="Pick a year between 2000 and 2100")


def month_items(months: list[dict]) -> dict[int, dict]:
    """The rows the page sent, by month number."""
    out = {}
    for item in months:
        m = item.get("month")
        if not isinstance(m, int) or not 1 <= m <= 12:
            raise HTTPException(status_code=400, detail="Month must be 1–12")
        out[m] = item
    return out


async def sales_between(db: AsyncSession, start: date, end: date) -> float:
    s, e = utc_bounds(start, end)
    pos = (await db.execute(select(func.coalesce(func.sum(Invoice.total), 0))
                            .where(Invoice.created_at >= s, Invoice.created_at <= e, Invoice.status == "paid"))).scalar()
    refunds = (await db.execute(select(func.coalesce(func.sum(RetailRefund.total), 0))
                                .where(RetailRefund.created_at >= s, RetailRefund.created_at <= e))).scalar()
    b2b = (await db.execute(
        select(func.coalesce(func.sum(JournalEntry.credit), 0))
        .join(Journal, JournalEntry.journal_id == Journal.id)
        .join(Account, JournalEntry.account_id == Account.id)
        .where(Account.code == "4000", Journal.created_at >= s, Journal.created_at <= e,
               Journal.ref_type.in_(B2B_REVENUE_REFS))
    )).scalar()
    return max(0.0, float(pos or 0) - float(refunds or 0)) + float(b2b or 0)


async def spent_between(db: AsyncSession, start: date, end: date) -> float:
    r = await db.execute(select(func.coalesce(func.sum(Expense.amount), 0))
                         .where(Expense.expense_date >= start, Expense.expense_date <= end))
    return float(r.scalar() or 0)


def num(value) -> Optional[float]:
    return float(value) if value is not None else None


async def plans_for_year(db: AsyncSession, year: int) -> dict[int, MonthlyPlan]:
    rows = (await db.execute(select(MonthlyPlan).where(MonthlyPlan.month >= date(year, 1, 1),
                                                       MonthlyPlan.month <= date(year, 12, 1)))).scalars().all()
    return {p.month.month: p for p in rows}


async def plan_for(db: AsyncSession, month_start: date) -> Optional[MonthlyPlan]:
    return (await db.execute(select(MonthlyPlan).where(MonthlyPlan.month == month_start))).scalar_one_or_none()


async def store_months(db: AsyncSession, year: int, wanted: dict[int, dict], user, module: str, what: str) -> list[int]:
    """Write `wanted` ({month: {field: value}}) onto the year's rows; only those fields are touched."""
    existing = await plans_for_year(db, year)
    changed = []
    for m, fields in sorted(wanted.items()):
        plan = existing.get(m)
        if plan and all(getattr(plan, k) == v for k, v in fields.items()):
            continue
        if not plan:
            if all(v is None for v in fields.values()):
                continue
            plan = MonthlyPlan(month=date(year, m, 1))
            db.add(plan)
        for k, v in fields.items():
            setattr(plan, k, v)
        plan.updated_by_user_id = user.id
        if all(getattr(plan, k) is None for k in PLAN_FIELDS):
            await db.delete(plan)
        changed.append(m)
    if changed:
        names = ", ".join(calendar.month_abbr[m] for m in changed)
        record(db, module, "save_plan", f"Updated {what} for {names} {year}", user=user,
               ref_type="monthly_plan", ref_id=year)
        await db.commit()
    return changed


async def year_overview(db: AsyncSession, year: int) -> dict:
    """The 12 months of `year`: the budget and, for months that have started, what was spent."""
    today = today_local()
    plans = await plans_for_year(db, year)
    months = []
    for m in range(1, 13):
        start, end = month_range(year, m)
        plan = plans.get(m)
        months.append({
            "month": m,
            "budget": num(plan.budget) if plan else None,
            # Months still to come have nothing to compare yet.
            "spent": await spent_between(db, start, min(end, today)) if start <= today else None,
            "current": start <= today <= end,
        })
    return {"year": year, "months": months}


async def current_month(db: AsyncSession) -> dict:
    """This month's spending so far, with how far through the month we are — for the Sales dashboard strip."""
    today = today_local()
    start, end = month_range(today.year, today.month)
    plan = await plan_for(db, start)
    return {
        "year": today.year, "month": today.month, "day": today.day, "days_in_month": end.day,
        "budget": num(plan.budget) if plan else None,
        "spent": await spent_between(db, start, today),
    }


async def save_year(db: AsyncSession, year: int, months: list[dict], user) -> dict:
    """Set the budget of the months given; an empty one clears it."""
    check_year(year)
    wanted = {m: {"budget": parse_amount(item.get("budget"))} for m, item in month_items(months).items()}
    return {"ok": True, "changed": await store_months(db, year, wanted, user, "Budget", "budget")}
