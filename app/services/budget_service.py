"""Monthly budget and sales target — what was set, and how the month is going.

Sales are counted the way the Sales dashboard counts them, so the two pages agree: paid POS invoices minus retail
refunds, plus B2B revenue (credits to account 4000 from B2B and consignment journals). Spending is every expense
dated in the month, as on the Expenses page.
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


def _num(value) -> Optional[float]:
    return float(value) if value is not None else None


async def year_overview(db: AsyncSession, year: int) -> dict:
    """The 12 months of `year`: what was set and, for months that have started, what actually happened."""
    today = today_local()
    rows = (await db.execute(select(MonthlyPlan).where(MonthlyPlan.month >= date(year, 1, 1),
                                                       MonthlyPlan.month <= date(year, 12, 1)))).scalars().all()
    plans = {p.month.month: p for p in rows}
    months = []
    for m in range(1, 13):
        start, end = month_range(year, m)
        plan = plans.get(m)
        started = start <= today
        upto = min(end, today)
        months.append({
            "month": m,
            "budget": _num(plan.budget) if plan else None,
            "sales_target": _num(plan.sales_target) if plan else None,
            # Months still to come have nothing to compare yet.
            "sales": await sales_between(db, start, upto) if started else None,
            "spent": await spent_between(db, start, upto) if started else None,
            "current": start <= today <= end,
        })
    return {"year": year, "months": months}


async def current_month(db: AsyncSession) -> dict:
    """This month so far, with how far through the month we are — for the Sales dashboard strip."""
    today = today_local()
    start, end = month_range(today.year, today.month)
    plan = (await db.execute(select(MonthlyPlan).where(MonthlyPlan.month == start))).scalar_one_or_none()
    return {
        "year": today.year, "month": today.month,
        "day": today.day, "days_in_month": end.day,
        "budget": _num(plan.budget) if plan else None,
        "sales_target": _num(plan.sales_target) if plan else None,
        "sales": await sales_between(db, start, today),
        "spent": await spent_between(db, start, today),
    }


async def save_year(db: AsyncSession, year: int, months: list[dict], user) -> dict:
    """Set the budget and target of the months given; a month left with neither is cleared."""
    if not 2000 <= year <= 2100:
        raise HTTPException(status_code=400, detail="Pick a year between 2000 and 2100")
    wanted: dict[int, tuple[Optional[Decimal], Optional[Decimal]]] = {}
    for item in months:
        m = item.get("month")
        if not isinstance(m, int) or not 1 <= m <= 12:
            raise HTTPException(status_code=400, detail="Month must be 1–12")
        wanted[m] = (parse_amount(item.get("budget")), parse_amount(item.get("sales_target")))

    rows = (await db.execute(select(MonthlyPlan).where(MonthlyPlan.month >= date(year, 1, 1),
                                                       MonthlyPlan.month <= date(year, 12, 1)))).scalars().all()
    existing = {p.month.month: p for p in rows}
    changed = []
    for m, (budget, target) in sorted(wanted.items()):
        plan = existing.get(m)
        if plan and (plan.budget, plan.sales_target) == (budget, target):
            continue
        if budget is None and target is None:
            if plan:
                await db.delete(plan)
                changed.append(m)
            continue
        if not plan:
            plan = MonthlyPlan(month=date(year, m, 1))
            db.add(plan)
        plan.budget, plan.sales_target, plan.updated_by_user_id = budget, target, user.id
        changed.append(m)

    if changed:
        names = ", ".join(calendar.month_abbr[m] for m in changed)
        record(db, "Budget", "save_plan", f"Updated budget / sales target for {names} {year}", user=user,
               ref_type="monthly_plan", ref_id=year)
        await db.commit()
    return {"ok": True, "changed": changed}
