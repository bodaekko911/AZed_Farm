"""
Farm Dashboard router.

Provides:
  - GET /farm-dashboard            → HTML page
  - GET /farm-dashboard/summary    → JSON aggregate for the page

Pulls together:
  • Farm deliveries  (volume, estimated value, by farm, by crop)
  • Spoilage         (qty, estimated value, top reasons, spoilage rate %)
  • Expenses         (farm-tagged + total, by category)
  • Season analysis  (monthly trends, YoY deltas, leading farms / crops)
  • Brainstormed extras:
      - top farms by volume / value / deliveries
      - top crops by volume / value
      - spoilage rate per crop (spoilage qty ÷ delivered qty)
      - average cost per kg per crop (uses product.cost)
      - delivery cadence (deliveries / day in range)
      - weather snapshot (avg temp/rain across logged farms in range)
      - active farms count + dormant farms in range
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.log import logger
from app.core.permissions import require_permission
from app.core.security import get_current_user
from app.database import get_async_session
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem, WeatherLog
from app.models.product import Product
from app.models.spoilage import SpoilageRecord
from app.models.production import ProductionBatch
from app.models.user import User


router = APIRouter(
    tags=["Farm Dashboard"],
    dependencies=[Depends(require_permission("page_farm_dashboard"))],
)


# ── helpers ───────────────────────────────────────────────────────────

def _parse_range(range_param: str, start: Optional[str], end: Optional[str]) -> tuple[date, date, str]:
    today = date.today()
    if range_param == "today":
        return today, today, "Today"
    if range_param == "7d":
        return today - timedelta(days=6), today, "Last 7 days"
    if range_param == "30d":
        return today - timedelta(days=29), today, "Last 30 days"
    if range_param == "90d":
        return today - timedelta(days=89), today, "Last 90 days"
    if range_param == "mtd":
        return today.replace(day=1), today, "Month to date"
    if range_param == "qtd":
        q = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=q, day=1), today, "Quarter to date"
    if range_param in ("year", "ytd"):
        return today.replace(month=1, day=1), today, "Year to date"
    if range_param == "custom":
        try:
            s = date.fromisoformat(start) if start else today
            e = date.fromisoformat(end)   if end   else today
            if e < s:
                s, e = e, s
            return s, e, f"{s.isoformat()} → {e.isoformat()}"
        except Exception:
            return today.replace(day=1), today, "Month to date"
    return today.replace(day=1), today, "Month to date"


def _prev_window(s: date, e: date) -> tuple[date, date]:
    span = (e - s).days + 1
    return s - timedelta(days=span), s - timedelta(days=1)


def _pct_delta(curr: float, prev: float) -> Optional[float]:
    if prev == 0:
        return None if curr == 0 else 100.0
    return round(((curr - prev) / prev) * 100.0, 1)


# ── JSON summary ──────────────────────────────────────────────────────

@router.get("/farm-dashboard/summary")
async def farm_dashboard_summary(
    range_param: str = Query("30d", pattern="^(today|7d|30d|90d|mtd|qtd|year|ytd|custom)$", alias="range"),
    start: Optional[str] = Query(None),
    end:   Optional[str] = Query(None),
    db:    AsyncSession  = Depends(get_async_session),
    current_user: User   = Depends(get_current_user),
):
    s_date, e_date, range_label = _parse_range(range_param, start, end)
    p_start, p_end = _prev_window(s_date, e_date)

    _errors: list[dict] = []
    out: dict = {
        "range": {
            "key":   range_param,
            "label": range_label,
            "start": s_date.isoformat(),
            "end":   e_date.isoformat(),
        },
    }

    # ── farms registry ────────────────────────────────────────────────
    farms_by_id: dict[int, Farm] = {}
    try:
        r = await db.execute(select(Farm))
        farms_by_id = {f.id: f for f in r.scalars().all()}
        out["farms_total"]  = len(farms_by_id)
        out["farms_active"] = sum(1 for f in farms_by_id.values() if (f.is_active or 0))
    except Exception:
        logger.exception("farm_dashboard: farms lookup failed")
        _errors.append({"section": "farms", "reason": "query failed"})
        out["farms_total"] = out["farms_active"] = 0

    # ── products map (for cost / name lookups) ────────────────────────
    products_by_id: dict[int, Product] = {}
    try:
        r = await db.execute(select(Product))
        products_by_id = {p.id: p for p in r.scalars().all()}
    except Exception:
        logger.exception("farm_dashboard: products lookup failed")
        _errors.append({"section": "products", "reason": "query failed"})

    def _prod_cost(pid: Optional[int]) -> float:
        p = products_by_id.get(pid or -1)
        if not p:
            return 0.0
        return float(p.cost or 0)

    def _prod_name(pid: Optional[int]) -> str:
        p = products_by_id.get(pid or -1)
        return p.name if p else "—"

    # ── DELIVERIES ────────────────────────────────────────────────────
    deliveries_count = 0
    delivered_qty    = 0.0
    delivered_value  = 0.0
    by_farm: dict[int, dict] = {}
    by_crop: dict[int, dict] = {}
    deliveries_by_day: dict[str, dict] = {}
    active_days = 0
    try:
        rows = await db.execute(
            select(
                FarmDelivery.id,
                FarmDelivery.farm_id,
                FarmDelivery.delivery_date,
                FarmDeliveryItem.product_id,
                FarmDeliveryItem.qty,
            )
            .join(FarmDeliveryItem, FarmDeliveryItem.delivery_id == FarmDelivery.id, isouter=True)
            .where(FarmDelivery.delivery_date >= s_date, FarmDelivery.delivery_date <= e_date)
        )
        for r in rows.all():
            qty = float(r.qty or 0)
            val = qty * _prod_cost(r.product_id)
            delivered_qty   += qty
            delivered_value += val

            farm_id = int(r.farm_id) if r.farm_id else 0
            entry = by_farm.setdefault(farm_id, {
                "farm_id": farm_id,
                "farm":    farms_by_id.get(farm_id).name if farms_by_id.get(farm_id) else "Unknown farm",
                "deliveries": 0, "qty": 0.0, "value": 0.0,
            })
            entry["qty"]   += qty
            entry["value"] += val

            pid = int(r.product_id) if r.product_id else 0
            crop = by_crop.setdefault(pid, {
                "product_id": pid,
                "name":       _prod_name(pid),
                "qty": 0.0, "value": 0.0,
            })
            crop["qty"]   += qty
            crop["value"] += val

            day_key = r.delivery_date.isoformat() if r.delivery_date else "—"
            day = deliveries_by_day.setdefault(day_key, {"date": day_key, "qty": 0.0, "value": 0.0, "deliveries": 0})
            day["qty"]   += qty
            day["value"] += val

        farm_deliv_count = await db.execute(
            select(FarmDelivery.farm_id, func.count(FarmDelivery.id))
            .where(FarmDelivery.delivery_date >= s_date, FarmDelivery.delivery_date <= e_date)
            .group_by(FarmDelivery.farm_id)
        )
        for fid, cnt in farm_deliv_count.all():
            if fid in by_farm:
                by_farm[fid]["deliveries"] = int(cnt or 0)
            elif fid is not None:
                by_farm[int(fid)] = {
                    "farm_id":    int(fid),
                    "farm":       farms_by_id.get(int(fid)).name if farms_by_id.get(int(fid)) else "Unknown farm",
                    "deliveries": int(cnt or 0),
                    "qty": 0.0, "value": 0.0,
                }

        dates_in_range = await db.execute(
            select(func.count(func.distinct(FarmDelivery.delivery_date)))
            .where(FarmDelivery.delivery_date >= s_date, FarmDelivery.delivery_date <= e_date)
        )
        dc_total = await db.execute(
            select(func.count(FarmDelivery.id))
            .where(FarmDelivery.delivery_date >= s_date, FarmDelivery.delivery_date <= e_date)
        )
        deliveries_count = int(dc_total.scalar() or 0)
        active_days      = int(dates_in_range.scalar() or 0)
    except Exception:
        logger.exception("farm_dashboard: deliveries section failed")
        _errors.append({"section": "deliveries", "reason": "query failed"})

    # deliveries previous-window totals for delta
    prev_delivered_qty = prev_delivered_value = 0.0
    prev_deliveries_count = 0
    try:
        rows = await db.execute(
            select(FarmDeliveryItem.product_id, FarmDeliveryItem.qty)
            .join(FarmDelivery, FarmDeliveryItem.delivery_id == FarmDelivery.id)
            .where(FarmDelivery.delivery_date >= p_start, FarmDelivery.delivery_date <= p_end)
        )
        for r in rows.all():
            q = float(r.qty or 0)
            prev_delivered_qty   += q
            prev_delivered_value += q * _prod_cost(r.product_id)
        r = await db.execute(
            select(func.count(FarmDelivery.id))
            .where(FarmDelivery.delivery_date >= p_start, FarmDelivery.delivery_date <= p_end)
        )
        prev_deliveries_count = int(r.scalar() or 0)
    except Exception:
        logger.exception("farm_dashboard: previous-window deliveries failed")

    out["deliveries"] = {
        "count":        deliveries_count,
        "count_prev":   prev_deliveries_count,
        "count_delta":  _pct_delta(deliveries_count, prev_deliveries_count),
        "qty":          round(delivered_qty, 3),
        "qty_prev":     round(prev_delivered_qty, 3),
        "qty_delta":    _pct_delta(delivered_qty, prev_delivered_qty),
        "value":        round(delivered_value, 2),
        "value_prev":   round(prev_delivered_value, 2),
        "value_delta":  _pct_delta(delivered_value, prev_delivered_value),
        "active_days":  active_days,
        "by_day":       sorted(deliveries_by_day.values(), key=lambda d: d["date"]),
    }

    # leaderboards
    out["top_farms_by_value"] = sorted(
        [{**v, "qty": round(v["qty"], 3), "value": round(v["value"], 2)} for v in by_farm.values()],
        key=lambda d: d["value"], reverse=True,
    )[:10]
    out["top_farms_by_qty"] = sorted(
        [{**v, "qty": round(v["qty"], 3), "value": round(v["value"], 2)} for v in by_farm.values()],
        key=lambda d: d["qty"], reverse=True,
    )[:10]
    out["top_crops"] = sorted(
        [{**v, "qty": round(v["qty"], 3), "value": round(v["value"], 2)} for v in by_crop.values()],
        key=lambda d: d["value"], reverse=True,
    )[:10]

    # dormant farms = active farms with zero deliveries in range
    dormant: list[dict] = []
    try:
        for fid, f in farms_by_id.items():
            if not (f.is_active or 0):
                continue
            if by_farm.get(fid, {}).get("deliveries", 0) == 0:
                dormant.append({"farm_id": fid, "farm": f.name, "location": f.location or ""})
    except Exception:
        logger.exception("farm_dashboard: dormant farms calc failed")
    out["dormant_farms"] = dormant[:10]
    out["dormant_count"] = len(dormant)

    # ── SPOILAGE ──────────────────────────────────────────────────────
    spoilage_qty = 0.0
    spoilage_value = 0.0
    spoilage_count = 0
    spoilage_by_reason: dict[str, dict] = {}
    spoilage_by_crop:   dict[int, dict] = {}
    try:
        rows = await db.execute(
            select(SpoilageRecord.qty, SpoilageRecord.reason, SpoilageRecord.product_id)
            .where(SpoilageRecord.spoilage_date >= s_date, SpoilageRecord.spoilage_date <= e_date)
        )
        for r in rows.all():
            q = float(r.qty or 0)
            v = q * _prod_cost(r.product_id)
            spoilage_qty   += q
            spoilage_value += v
            spoilage_count += 1
            reason = (r.reason or "—").strip() or "—"
            sb = spoilage_by_reason.setdefault(reason, {"reason": reason, "qty": 0.0, "value": 0.0, "count": 0})
            sb["qty"]   += q
            sb["value"] += v
            sb["count"] += 1
            sc = spoilage_by_crop.setdefault(int(r.product_id or 0), {
                "product_id": int(r.product_id or 0),
                "name":       _prod_name(r.product_id),
                "qty": 0.0, "value": 0.0,
            })
            sc["qty"]   += q
            sc["value"] += v
    except Exception:
        logger.exception("farm_dashboard: spoilage section failed")
        _errors.append({"section": "spoilage", "reason": "query failed"})

    # prev window for spoilage delta
    prev_spoilage_qty = prev_spoilage_value = 0.0
    try:
        rows = await db.execute(
            select(SpoilageRecord.qty, SpoilageRecord.product_id)
            .where(SpoilageRecord.spoilage_date >= p_start, SpoilageRecord.spoilage_date <= p_end)
        )
        for r in rows.all():
            q = float(r.qty or 0)
            prev_spoilage_qty   += q
            prev_spoilage_value += q * _prod_cost(r.product_id)
    except Exception:
        logger.exception("farm_dashboard: previous spoilage failed")

    spoilage_rate_by_crop: list[dict] = []
    for pid, sc in spoilage_by_crop.items():
        delivered = float(by_crop.get(pid, {}).get("qty", 0.0))
        spoiled = float(sc["qty"])
        denom = delivered + spoiled
        rate = (spoiled / denom * 100.0) if denom > 0 else 0.0
        spoilage_rate_by_crop.append({
            "product_id": pid,
            "name":       sc["name"],
            "delivered":  round(delivered, 3),
            "spoiled":    round(spoiled, 3),
            "rate_pct":   round(rate, 2),
        })
    spoilage_rate_by_crop.sort(key=lambda d: d["rate_pct"], reverse=True)

    overall_rate = 0.0
    denom_total = delivered_qty + spoilage_qty
    if denom_total > 0:
        overall_rate = round(spoilage_qty / denom_total * 100.0, 2)

    out["spoilage"] = {
        "count":       spoilage_count,
        "qty":         round(spoilage_qty, 3),
        "qty_prev":    round(prev_spoilage_qty, 3),
        "qty_delta":   _pct_delta(spoilage_qty, prev_spoilage_qty),
        "value":       round(spoilage_value, 2),
        "value_prev":  round(prev_spoilage_value, 2),
        "value_delta": _pct_delta(spoilage_value, prev_spoilage_value),
        "rate_pct":    overall_rate,
        "top_reasons": sorted(
            [{"reason": v["reason"], "qty": round(v["qty"], 3), "value": round(v["value"], 2), "count": v["count"]}
             for v in spoilage_by_reason.values()],
            key=lambda d: d["value"], reverse=True,
        )[:10],
        "by_crop":     spoilage_rate_by_crop[:10],
    }

    # ── EXPENSES (farm-tagged + total) ────────────────────────────────
    farm_exp_total = total_exp = 0.0
    farm_exp_count = total_exp_count = 0
    exp_by_category: dict[str, dict] = {}
    exp_by_farm: dict[int, dict] = {}
    try:
        rows = await db.execute(
            select(Expense.amount, Expense.farm_id, ExpenseCategory.name)
            .join(ExpenseCategory, Expense.category_id == ExpenseCategory.id, isouter=True)
            .where(
                Expense.expense_date >= s_date,
                Expense.expense_date <= e_date,
                Expense.farm_id.isnot(None),
            )
        )
        for r in rows.all():
            amt = float(r.amount or 0)
            farm_exp_total += amt
            farm_exp_count += 1

            cat = r.name or "Uncategorised"
            ec = exp_by_category.setdefault(cat, {"category": cat, "amount": 0.0, "count": 0})
            ec["amount"] += amt
            ec["count"]  += 1

            fid = int(r.farm_id) if r.farm_id else 0
            ef = exp_by_farm.setdefault(fid, {
                "farm_id": fid,
                "farm":    farms_by_id.get(fid).name if farms_by_id.get(fid) else "Unknown farm",
                "amount":  0.0, "count": 0,
            })
            ef["amount"] += amt
            ef["count"]  += 1

        r = await db.execute(
            select(func.sum(Expense.amount), func.count(Expense.id))
            .where(Expense.expense_date >= s_date, Expense.expense_date <= e_date)
        )
        row = r.first()
        if row:
            total_exp       = float(row[0] or 0)
            total_exp_count = int(row[1] or 0)
    except Exception:
        logger.exception("farm_dashboard: expenses section failed")
        _errors.append({"section": "expenses", "reason": "query failed"})

    prev_farm_exp = 0.0
    try:
        r = await db.execute(
            select(func.sum(Expense.amount))
            .where(
                Expense.expense_date >= p_start,
                Expense.expense_date <= p_end,
                Expense.farm_id.isnot(None),
            )
        )
        prev_farm_exp = float(r.scalar() or 0)
    except Exception:
        logger.exception("farm_dashboard: previous farm expenses failed")

    farm_share_pct = 0.0
    if total_exp > 0:
        farm_share_pct = round(farm_exp_total / total_exp * 100.0, 1)

    out["expenses"] = {
        "farm_total":    round(farm_exp_total, 2),
        "farm_prev":     round(prev_farm_exp, 2),
        "farm_delta":    _pct_delta(farm_exp_total, prev_farm_exp),
        "farm_count":    farm_exp_count,
        "company_total": round(total_exp, 2),
        "company_count": total_exp_count,
        "farm_share_pct": farm_share_pct,
        "by_category":   sorted(
            [{**v, "amount": round(v["amount"], 2)} for v in exp_by_category.values()],
            key=lambda d: d["amount"], reverse=True,
        )[:10],
        "by_farm":       sorted(
            [{**v, "amount": round(v["amount"], 2)} for v in exp_by_farm.values()],
            key=lambda d: d["amount"], reverse=True,
        )[:10],
    }

    # ── NET CONTRIBUTION (per farm) ──────────────────────────────────
    contribution: list[dict] = []
    spoiled_by_farm: dict[int, float] = {}
    try:
        rows = await db.execute(
            select(SpoilageRecord.farm_id, SpoilageRecord.qty, SpoilageRecord.product_id)
            .where(SpoilageRecord.spoilage_date >= s_date, SpoilageRecord.spoilage_date <= e_date,
                   SpoilageRecord.farm_id.isnot(None))
        )
        for r in rows.all():
            fid = int(r.farm_id) if r.farm_id else 0
            spoiled_by_farm[fid] = spoiled_by_farm.get(fid, 0.0) + float(r.qty or 0) * _prod_cost(r.product_id)
    except Exception:
        logger.exception("farm_dashboard: per-farm spoilage failed")

    for fid, fbf in by_farm.items():
        deliv_val = float(fbf["value"])
        exp_val   = float(exp_by_farm.get(fid, {}).get("amount", 0.0))
        spoil_val = float(spoiled_by_farm.get(fid, 0.0))
        net = deliv_val - exp_val - spoil_val
        contribution.append({
            "farm_id":  fid,
            "farm":     fbf["farm"],
            "delivered_value": round(deliv_val, 2),
            "expenses":        round(exp_val, 2),
            "spoiled_value":   round(spoil_val, 2),
            "net":             round(net, 2),
        })
    contribution.sort(key=lambda d: d["net"], reverse=True)
    out["contribution"] = contribution[:10]

    # ── DELIVERY CADENCE ──────────────────────────────────────────────
    span_days = max(1, (e_date - s_date).days + 1)
    out["cadence"] = {
        "span_days":          span_days,
        "active_days":        active_days,
        "deliveries_per_day": round(deliveries_count / span_days, 2),
        "qty_per_day":        round(delivered_qty / span_days, 2),
        "active_day_ratio":   round(active_days / span_days * 100.0, 1),
    }

    # ── WEATHER SNAPSHOT ──────────────────────────────────────────────
    weather = {"avg_temp_min": None, "avg_temp_max": None, "avg_rain_mm": None,
               "avg_humidity": None, "logs": 0, "farms_logged": 0}
    try:
        r = await db.execute(
            select(
                func.avg(WeatherLog.temp_min),
                func.avg(WeatherLog.temp_max),
                func.avg(WeatherLog.rainfall_mm),
                func.avg(WeatherLog.humidity_pct),
                func.count(WeatherLog.id),
                func.count(func.distinct(WeatherLog.farm_id)),
            )
            .where(WeatherLog.log_date >= s_date, WeatherLog.log_date <= e_date)
        )
        row = r.first()
        if row:
            weather = {
                "avg_temp_min": float(row[0]) if row[0] is not None else None,
                "avg_temp_max": float(row[1]) if row[1] is not None else None,
                "avg_rain_mm":  float(row[2]) if row[2] is not None else None,
                "avg_humidity": float(row[3]) if row[3] is not None else None,
                "logs":         int(row[4] or 0),
                "farms_logged": int(row[5] or 0),
            }
    except Exception:
        logger.exception("farm_dashboard: weather snapshot failed")
        _errors.append({"section": "weather", "reason": "query failed"})
    out["weather"] = weather

    # ── PRODUCTION SIDE-CAR ───────────────────────────────────────────
    try:
        from datetime import datetime, time, timezone
        s_dt = datetime.combine(s_date, time.min, tzinfo=timezone.utc)
        e_dt = datetime.combine(e_date, time.max, tzinfo=timezone.utc)
        r = await db.execute(
            select(func.count(ProductionBatch.id))
            .where(ProductionBatch.created_at >= s_dt, ProductionBatch.created_at <= e_dt)
        )
        out["batches_count"] = int(r.scalar() or 0)
    except Exception:
        logger.exception("farm_dashboard: batches count failed")
        _errors.append({"section": "batches", "reason": "query failed"})
        out["batches_count"] = 0

    # ── SEASON ANALYSIS ──────────────────────────────────────────────
    season: list[dict] = []
    try:
        today = date.today()
        anchor = today.replace(day=1)
        for i in range(11, -1, -1):
            y = anchor.year
            m = anchor.month - i
            while m <= 0:
                m += 12
                y -= 1
            m_start = date(y, m, 1)
            if m == 12:
                m_end = date(y, 12, 31)
            else:
                m_end = date(y, m + 1, 1) - timedelta(days=1)

            qty_row = await db.execute(
                select(func.sum(FarmDeliveryItem.qty))
                .join(FarmDelivery, FarmDeliveryItem.delivery_id == FarmDelivery.id)
                .where(FarmDelivery.delivery_date >= m_start, FarmDelivery.delivery_date <= m_end)
            )
            q = float(qty_row.scalar() or 0)

            sp_row = await db.execute(
                select(func.sum(SpoilageRecord.qty))
                .where(SpoilageRecord.spoilage_date >= m_start, SpoilageRecord.spoilage_date <= m_end)
            )
            sp = float(sp_row.scalar() or 0)

            ex_row = await db.execute(
                select(func.sum(Expense.amount))
                .where(
                    Expense.expense_date >= m_start,
                    Expense.expense_date <= m_end,
                    Expense.farm_id.isnot(None),
                )
            )
            ex = float(ex_row.scalar() or 0)

            season.append({
                "month":     m_start.strftime("%Y-%m"),
                "label":     m_start.strftime("%b %y"),
                "qty":       round(q, 3),
                "spoilage":  round(sp, 3),
                "expenses":  round(ex, 2),
            })
    except Exception:
        logger.exception("farm_dashboard: season analysis failed")
        _errors.append({"section": "season", "reason": "query failed"})

    out["season"] = season

    try:
        if season:
            peak  = max(season, key=lambda d: d["qty"])
            quiet = min(season, key=lambda d: d["qty"])
            out["season_peaks"] = {
                "peak_month":  peak["label"]  if peak["qty"]  > 0 else None,
                "peak_qty":    peak["qty"],
                "quiet_month": quiet["label"] if quiet["qty"] == 0 or quiet["qty"] < peak["qty"] else None,
                "quiet_qty":   quiet["qty"],
            }
        else:
            out["season_peaks"] = {"peak_month": None, "peak_qty": 0, "quiet_month": None, "quiet_qty": 0}
    except Exception:
        out["season_peaks"] = {"peak_month": None, "peak_qty": 0, "quiet_month": None, "quiet_qty": 0}

    # ── INSIGHTS ──────────────────────────────────────────────────────
    insights: list[dict] = []
    try:
        if out["deliveries"]["qty_delta"] is not None and out["deliveries"]["qty_delta"] < -15:
            insights.append({
                "kind": "warn",
                "title": "Intake is dropping",
                "body":  f"Delivered quantity is down {abs(out['deliveries']['qty_delta'])}% vs the previous period.",
            })
        elif out["deliveries"]["qty_delta"] is not None and out["deliveries"]["qty_delta"] > 20:
            insights.append({
                "kind": "good",
                "title": "Strong intake growth",
                "body":  f"Delivered quantity is up {out['deliveries']['qty_delta']}% vs the previous period.",
            })

        if out["spoilage"]["rate_pct"] >= 10:
            insights.append({
                "kind": "warn",
                "title": "High spoilage rate",
                "body":  f"{out['spoilage']['rate_pct']}% of intake was spoiled — investigate the top reasons.",
            })

        if out["dormant_count"] > 0:
            insights.append({
                "kind": "info",
                "title": f"{out['dormant_count']} active farm(s) had no deliveries",
                "body":  "Active farms without any delivery in this period may need follow-up.",
            })

        if out["expenses"]["farm_share_pct"] >= 50:
            insights.append({
                "kind": "info",
                "title": "Farm costs dominate spend",
                "body":  f"{out['expenses']['farm_share_pct']}% of total company expenses in this window were farm-tagged.",
            })

        if out["spoilage"]["by_crop"]:
            worst = out["spoilage"]["by_crop"][0]
            if worst["rate_pct"] >= 15 and worst["spoiled"] > 0:
                insights.append({
                    "kind": "warn",
                    "title": f"{worst['name']} spoilage at {worst['rate_pct']}%",
                    "body":  f"{worst['spoiled']} units spoiled out of {worst['delivered'] + worst['spoiled']} handled.",
                })
    except Exception:
        logger.exception("farm_dashboard: insights compose failed")

    out["insights"] = insights
    out["_errors"]  = _errors

    return JSONResponse(
        content=out,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


# ── UI ────────────────────────────────────────────────────────────────

@router.get("/farm-dashboard", response_class=HTMLResponse)
def farm_dashboard_ui(current_user: User = Depends(get_current_user)):
    locale_dir = getattr(settings, "APP_LOCALE_DIR", "ltr")
    return f"""<!DOCTYPE html>
<html lang="en" dir="{locale_dir}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Farm Dashboard – AZed ERP</title>
<script src="/static/theme-init.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400&family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&family=Outfit:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dashboard.css">
<link rel="stylesheet" href="/static/farm_dashboard.css">
<script src="/static/theme.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js" defer></script>
<script src="/static/auth-guard.js" defer></script>
</head>
<body>
<div class="bg-layer">
  <div class="bg-orb"></div>
  <div class="bg-orb"></div>
  <div class="bg-orb"></div>
</div>
<div class="bg-grain"></div>
<div id="loading"><div class="spinner"></div></div>
<nav class="top-nav" aria-label="Primary">
  <a href="/home" class="logo navbar-brand">
    <img src="/static/ERP_logo.png" alt="AZed ERP" style="height: 100%; max-height: 48px; width: auto; object-fit: contain; margin: 0; padding: 0;">
  </a>
  <div class="nav-links">
    <a href="/dashboard" class="nav-link">Sales dashboard</a>
    <a href="/farm-dashboard" class="nav-link active">Farm dashboard</a>
    <a href="/pos" class="nav-link">POS</a>
    <a href="/b2b/" class="nav-link">B2B</a>
    <a href="/reports/" class="nav-link">Reports</a>
    <a href="/inventory/" class="nav-link">Inventory</a>
  </div>
  <div class="nav-actions">
    <button class="mode-btn app-theme-toggle" id="mode-btn" type="button" data-theme-toggle aria-label="Switch color theme" title="Switch color theme" aria-pressed="false">&#127769;</button>
    <div class="account-menu">
      <button class="user-pill" id="account-trigger" type="button" aria-haspopup="menu" aria-expanded="false">
        <div class="user-avatar" id="user-avatar">A</div>
        <span class="user-name" id="user-name">Admin</span>
        <span class="menu-caret">&#9662;</span>
      </button>
      <div class="account-dropdown" id="account-dropdown" role="menu">
        <div class="account-head">
          <div class="account-label">Signed in as</div>
          <div class="account-email" id="user-email">&#8212;</div>
        </div>
        <a href="/users/password" class="account-item" role="menuitem">Change Password</a>
        <button class="account-item danger" id="signout-btn" type="button" role="menuitem">Sign out</button>
      </div>
    </div>
  </div>
</nav>
<main class="page-shell">
  <header class="header-strip">
    <div>
      <h1 class="greeting" id="greeting">Farm operations</h1>
      <p class="date-display" id="date-display"></p>
    </div>
    <div class="header-controls">
      <div class="range-picker" role="group" aria-label="Choose date range">
        <button type="button" class="range-btn" data-range="today">Today</button>
        <button type="button" class="range-btn" data-range="7d">7d</button>
        <button type="button" class="range-btn active" data-range="30d">30d</button>
        <button type="button" class="range-btn" data-range="mtd">Month</button>
        <button type="button" class="range-btn" data-range="year">Year</button>
        <button type="button" class="range-btn" data-range="custom">Custom</button>
      </div>
      <span class="updated-pill" id="last-updated">Updated just now</span>
    </div>
  </header>

  <div id="error-banner" aria-live="polite"></div>

  <article class="card briefing-card" aria-label="Farm briefing">
    <p class="briefing-lead" id="briefing-lead">Loading farm briefing…</p>
    <p class="briefing-body" id="briefing-body"></p>
    <div class="briefing-actions" id="briefing-actions"></div>
  </article>

  <section class="numbers-grid" aria-label="Farm key numbers">
    <article class="card number-card" data-card="deliveries" aria-live="polite"></article>
    <article class="card number-card" data-card="intake_qty" aria-live="polite"></article>
    <article class="card number-card" data-card="intake_value" aria-live="polite"></article>
    <article class="card number-card" data-card="spoilage" aria-live="polite"></article>
    <article class="card number-card" data-card="farm_expenses" aria-live="polite"></article>
  </section>

  <section class="card chart-card" aria-label="Intake vs spoilage vs expenses by month">
    <div class="panel-head"><h2 id="chart-title">Season analysis — last 12 months</h2></div>
    <div class="chart-wrap"><canvas id="season-chart" aria-label="Season analysis chart"></canvas></div>
    <table class="sr-only" id="chart-table" aria-label="Season analysis table"></table>
  </section>

  <div class="panel-grid">
    <section class="card panel-card" aria-label="Top farms">
      <div class="panel-head">
        <h2 id="top-farms-title">Top farms</h2>
        <div class="panel-tabs" role="tablist" aria-label="Top farms mode">
          <button type="button" class="tab-btn active" data-farm-tab="value">By value</button>
          <button type="button" class="tab-btn" data-farm-tab="qty">By quantity</button>
        </div>
      </div>
      <div id="top-farms-list" class="panel-body"></div>
    </section>

    <section class="card panel-card" aria-label="Top crops">
      <div class="panel-head">
        <h2>Top crops</h2>
      </div>
      <div id="top-crops-list" class="panel-body"></div>
    </section>
  </div>

  <div class="panel-grid">
    <section class="card panel-card" aria-label="Spoilage breakdown">
      <div class="panel-head">
        <h2>Spoilage breakdown</h2>
        <div class="panel-tabs" role="tablist" aria-label="Spoilage view">
          <button type="button" class="tab-btn active" data-spoilage-tab="reasons">Top reasons</button>
          <button type="button" class="tab-btn" data-spoilage-tab="crops">Spoilage rate</button>
        </div>
      </div>
      <div id="spoilage-list" class="panel-body"></div>
    </section>

    <section class="card panel-card" aria-label="Farm expenses">
      <div class="panel-head">
        <h2>Farm expenses</h2>
        <div class="panel-tabs" role="tablist" aria-label="Expense view">
          <button type="button" class="tab-btn active" data-exp-tab="category">By category</button>
          <button type="button" class="tab-btn" data-exp-tab="farm">By farm</button>
        </div>
      </div>
      <div id="expenses-list" class="panel-body"></div>
    </section>
  </div>

  <div class="panel-grid">
    <section class="card panel-card" aria-label="Net contribution per farm">
      <div class="panel-head">
        <h2>Net contribution per farm</h2>
      </div>
      <div id="contribution-list" class="panel-body"></div>
    </section>

    <section class="card panel-card" aria-label="Operational signals">
      <div class="panel-head">
        <h2>Operational signals</h2>
      </div>
      <div id="signals-list" class="panel-body"></div>
    </section>
  </div>
</main>

<div id="custom-range-modal" class="range-modal hidden" role="dialog" aria-modal="true" aria-labelledby="crm-title">
  <div class="range-modal-card">
    <div class="range-modal-header">
      <h3 id="crm-title">Custom Range</h3>
      <button type="button" class="range-modal-close" id="range-modal-close" aria-label="Close">&#215;</button>
    </div>
    <div class="range-modal-body">
      <label class="range-field"><span>Start date</span><input id="custom-range-start" type="date"></label>
      <label class="range-field"><span>End date</span><input id="custom-range-end" type="date"></label>
      <p id="custom-range-error" class="range-error" hidden></p>
    </div>
    <div class="range-modal-actions">
      <button type="button" class="range-secondary-btn" id="range-cancel">Cancel</button>
      <button type="button" class="range-primary-btn" id="range-apply">Apply</button>
    </div>
  </div>
</div>

<script src="/static/farm_dashboard.js"></script>
</body>
</html>"""
