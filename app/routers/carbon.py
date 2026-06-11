"""
Carbon Footprint Router
=======================
Prefix  : /carbon/
Tag     : Carbon
Permission gate : page_carbon  (admin + manager by default)

Page routes
-----------
  GET  /carbon/           — dashboard: summary cards + log table
  GET  /carbon/log        — log a new emission (form page)
  GET  /carbon/factors    — manage emission factors

API routes
----------
  GET  /carbon/api/summary          — totals by source_type for a date range
  GET  /carbon/api/logs             — paginated log entries
  POST /carbon/api/logs             — create a manual log entry
  DELETE /carbon/api/logs/{id}      — delete a log entry
  GET  /carbon/api/factors          — all active emission factors
  POST /carbon/api/factors          — create a new factor
  PATCH /carbon/api/factors/{id}    — update a factor
  GET  /carbon/api/targets          — list period targets
  POST /carbon/api/targets          — create a target
  DELETE /carbon/api/targets/{id}   — delete a target
  POST /carbon/api/auto-log/farm-delivery/{delivery_id}
       — auto-calculate transport CO₂ for a farm delivery
"""

from __future__ import annotations

from datetime import date, timedelta
from html import escape
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.log import record
from app.core.navigation import render_app_header
from app.core.permissions import get_current_user, require_permission
from app.database import get_async_session
from app.models.carbon import CarbonEmissionFactor, CarbonLog, CarbonTarget
from app.models.farm import Farm, FarmDelivery
from app.models.user import User

router = APIRouter(
    prefix="/carbon",
    tags=["Carbon"],
    dependencies=[Depends(require_permission("page_carbon"))],
)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class LogCreate(BaseModel):
    factor_id: int
    log_date: str          # ISO date string "YYYY-MM-DD"
    quantity: float
    farm_id: Optional[int] = None
    ref_type: Optional[str] = None
    ref_id: Optional[int] = None
    notes: Optional[str] = None


class FactorCreate(BaseModel):
    source_type: str
    source_key: str
    label: str
    factor_kg_co2e_per_unit: float
    unit: str
    description: Optional[str] = None
    scope: Optional[int] = None                 # GHG Protocol: 1 | 2 | 3
    methodology_source: Optional[str] = None    # e.g. "DEFRA GHG Conversion Factors 2024"
    source_year: Optional[int] = None
    region: Optional[str] = None


class FactorUpdate(BaseModel):
    label: Optional[str] = None
    factor_kg_co2e_per_unit: Optional[float] = None
    unit: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    scope: Optional[int] = None
    methodology_source: Optional[str] = None
    source_year: Optional[int] = None
    region: Optional[str] = None


class TargetCreate(BaseModel):
    label: str
    period_start: str   # "YYYY-MM-DD"
    period_end: str
    target_kg_co2e: float
    notes: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

SOURCE_LABELS = {
    "transport": "Transport",
    "energy": "Energy",
    "waste": "Waste / Spoilage",
    "production": "Production",
}

SOURCE_ORDER = ["transport", "energy", "waste", "production"]

SOURCE_ICONS = {
    "transport": "🚚",
    "energy": "⚡",
    "waste": "♻️",
    "production": "🏭",
}


def _date_range_defaults(date_from: Optional[str], date_to: Optional[str]) -> tuple[date, date]:
    """Return (date, date) defaulting to current calendar month."""
    today = date.today()
    month_start = today.replace(day=1)

    try:
        d_from = date.fromisoformat(date_from) if date_from and date_from.strip() else month_start
    except ValueError:
        d_from = month_start

    try:
        d_to = date.fromisoformat(date_to) if date_to and date_to.strip() else today
    except ValueError:
        d_to = today

    if d_from > d_to:
        d_from, d_to = d_to, d_from

    return d_from, d_to


def _model_dump(model: BaseModel, **kwargs) -> dict:
    """Pydantic v1/v2 compatible model dumping."""
    if hasattr(model, "model_dump"):
        return model.model_dump(**kwargs)
    return model.dict(**kwargs)


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def _fmt_kg(value) -> str:
    kg = _as_float(value)
    if abs(kg) >= 1000:
        return f"{kg / 1000:,.2f} t"
    if abs(kg) >= 10:
        return f"{kg:,.1f} kg"
    return f"{kg:,.3f} kg"


def _fmt_number(value, digits: int = 1) -> str:
    return f"{_as_float(value):,.{digits}f}"


def _safe(value) -> str:
    return escape(str(value)) if value is not None else ""


def _source_label(source_type: str) -> str:
    return SOURCE_LABELS.get(source_type, str(source_type or "Other").replace("_", " ").title())


def _source_icon(source_type: str) -> str:
    return SOURCE_ICONS.get(source_type, "•")


def _badge_html(source_type: str) -> str:
    cls = _safe(source_type or "other")
    return f'<span class="carbon-badge carbon-badge-{cls}">{_safe(_source_label(source_type))}</span>'


def _period_label(d_from: date, d_to: date) -> str:
    if d_from == d_to:
        return d_from.strftime("%d %b %Y")
    if d_from.year == d_to.year:
        return f"{d_from.strftime('%d %b')} → {d_to.strftime('%d %b %Y')}"
    return f"{d_from.strftime('%d %b %Y')} → {d_to.strftime('%d %b %Y')}"


def _quick_link(label: str, start: date, end: date, active_from: date, active_to: date) -> str:
    active = start == active_from and end == active_to
    cls = "quick-chip active" if active else "quick-chip"
    return (
        f'<a class="{cls}" href="/carbon/?date_from={start.isoformat()}&date_to={end.isoformat()}">'
        f'{_safe(label)}</a>'
    )


async def _totals_by_source(db: AsyncSession, d_from: date, d_to: date) -> dict[str, float]:
    rows = await db.execute(
        select(
            CarbonEmissionFactor.source_type,
            func.sum(CarbonLog.kg_co2e).label("total"),
        )
        .join(CarbonEmissionFactor, CarbonLog.factor_id == CarbonEmissionFactor.id)
        .where(CarbonLog.log_date.between(d_from, d_to))
        .group_by(CarbonEmissionFactor.source_type)
    )
    return {r.source_type: _as_float(r.total) for r in rows}


async def _entry_count(db: AsyncSession, d_from: date, d_to: date) -> int:
    result = await db.execute(
        select(func.count(CarbonLog.id)).where(CarbonLog.log_date.between(d_from, d_to))
    )
    return int(result.scalar_one() or 0)


# ── Page: Dashboard ───────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def carbon_dashboard(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    d_from, d_to = _date_range_defaults(date_from, date_to)
    today = date.today()
    days_in_range = max((d_to - d_from).days + 1, 1)

    by_type = await _totals_by_source(db, d_from, d_to)
    grand_total = sum(by_type.values())
    entry_count = await _entry_count(db, d_from, d_to)
    daily_average = grand_total / days_in_range if days_in_range else 0

    previous_to = d_from - timedelta(days=1)
    previous_from = previous_to - timedelta(days=days_in_range - 1)
    previous_by_type = await _totals_by_source(db, previous_from, previous_to)
    previous_total = sum(previous_by_type.values())
    if previous_total > 0:
        delta_pct = round(((grand_total - previous_total) / previous_total) * 100, 1)
        delta_label = f"{delta_pct:+.1f}% vs previous {days_in_range} days"
        delta_class = "bad" if delta_pct > 0 else "good"
    elif grand_total > 0:
        delta_label = "New activity vs previous period"
        delta_class = "neutral"
    else:
        delta_label = "No emissions in this range"
        delta_class = "neutral"

    # Latest 50 log entries
    logs_q = await db.execute(
        select(CarbonLog)
        .options(
            selectinload(CarbonLog.factor),
            selectinload(CarbonLog.farm),
            selectinload(CarbonLog.user),
        )
        .where(CarbonLog.log_date.between(d_from, d_to))
        .order_by(CarbonLog.log_date.desc(), CarbonLog.id.desc())
        .limit(50)
    )
    logs = logs_q.scalars().all()

    # Active targets overlapping the range
    targets_q = await db.execute(
        select(CarbonTarget)
        .where(
            and_(
                CarbonTarget.period_start <= d_to,
                CarbonTarget.period_end >= d_from,
            )
        )
        .order_by(CarbonTarget.period_start)
    )
    targets = targets_q.scalars().all()

    nav_html = render_app_header(current_user, "page_carbon")

    all_source_types = [s for s in SOURCE_ORDER if s in by_type]
    all_source_types.extend(sorted(s for s in by_type if s not in all_source_types))
    for default_source in SOURCE_ORDER:
        if default_source not in all_source_types:
            all_source_types.append(default_source)

    leading_source = max(by_type.items(), key=lambda item: item[1], default=("none", 0))
    leading_source_label = _source_label(leading_source[0]) if leading_source[1] else "—"
    leading_source_value = _fmt_kg(leading_source[1]) if leading_source[1] else "No source yet"

    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    last_30_start = today - timedelta(days=29)
    quick_links = "".join(
        [
            _quick_link("This month", month_start, today, d_from, d_to),
            _quick_link("Last 30 days", last_30_start, today, d_from, d_to),
            _quick_link("Year to date", year_start, today, d_from, d_to),
        ]
    )

    transport_val = by_type.get("transport", 0)
    energy_val = by_type.get("energy", 0)
    waste_val = by_type.get("waste", 0)
    production_val = by_type.get("production", 0)

    breakdown_html = ""
    for source_type in all_source_types:
        value = by_type.get(source_type, 0)
        pct = round(value / grand_total * 100, 1) if grand_total else 0
        breakdown_html += f"""
        <div class="breakdown-row">
          <div class="breakdown-head">
            <span class="breakdown-name"><span class="breakdown-icon">{_source_icon(source_type)}</span>{_safe(_source_label(source_type))}</span>
            <span class="breakdown-value">{_fmt_kg(value)} · {pct}%</span>
          </div>
          <div class="breakdown-track"><div class="breakdown-fill source-{_safe(source_type)}" style="width:{pct}%"></div></div>
        </div>"""

    log_rows = ""
    for lg in logs:
        factor = lg.factor
        source_type = factor.source_type if factor else "other"
        farm_name = lg.farm.name if lg.farm else "—"
        user_name = lg.user.name if lg.user else "—"
        ref_label = lg.ref_type or "manual"
        notes = (lg.notes or "").strip()
        notes_cell = f'<div class="log-notes">{_safe(notes)}</div>' if notes else ""
        log_rows += f"""
        <tr>
          <td><span class="date-pill">{_safe(lg.log_date)}</span></td>
          <td>{_badge_html(source_type)}</td>
          <td>
            <strong>{_safe(factor.label if factor else "Unknown factor")}</strong>
            {notes_cell}
          </td>
          <td>{_safe(farm_name)}</td>
          <td class="num">{_fmt_number(lg.quantity, 2)} {_safe(factor.unit if factor else "unit")}</td>
          <td class="num co2"><strong>{_fmt_kg(lg.kg_co2e)}</strong></td>
          <td><span class="ref-pill" title="Logged by {_safe(user_name)}">{_safe(ref_label)}</span></td>
          <td class="actions-cell">
            <button class="btn-icon btn-danger"
                    hx-delete="/carbon/api/logs/{lg.id}"
                    hx-confirm="Delete this log entry?"
                    hx-target="closest tr"
                    hx-swap="outerHTML"
                    aria-label="Delete log entry">✕</button>
          </td>
        </tr>"""

    target_html = ""
    best_target_status = "No active target"
    for t in targets:
        target_value = _as_float(t.target_kg_co2e)
        raw_pct = round(grand_total / target_value * 100, 1) if target_value else 0
        visual_pct = min(100, raw_pct)
        remaining = target_value - grand_total
        bar_class = "progress-ok" if raw_pct < 80 else ("progress-warn" if raw_pct < 100 else "progress-over")
        status_text = f"{_fmt_kg(max(remaining, 0))} remaining" if remaining >= 0 else f"{_fmt_kg(abs(remaining))} over target"
        best_target_status = f"{raw_pct}% of target" if target_value else best_target_status
        target_html += f"""
        <div class="target-card">
          <div class="target-header">
            <div>
              <strong>{_safe(t.label)}</strong>
              <span class="target-dates">{_safe(t.period_start)} → { _safe(t.period_end) }</span>
            </div>
            <button class="target-delete" type="button" data-target-id="{t.id}" aria-label="Delete target">Delete</button>
          </div>
          <div class="progress-bar-track">
            <div class="progress-bar {bar_class}" style="width:{visual_pct}%"></div>
          </div>
          <div class="target-stats">
            <span>{_fmt_kg(grand_total)} used</span>
            <span>Target: {_fmt_kg(target_value)}</span>
            <span>{raw_pct}%</span>
            <span>{status_text}</span>
          </div>
          {f'<p class="target-notes">{_safe(t.notes)}</p>' if t.notes else ''}
        </div>"""

    target_section = (
        f'<div class="section-title-row"><h2>Reduction Targets</h2><span>{len(targets)} active in range</span></div>{target_html}'
        if target_html
        else """
        <div class="empty-card compact">
          <strong>No target for this period yet.</strong>
          <span>Add a kg CO₂e target to track reduction progress for the selected date range.</span>
        </div>
        """
    )

    log_empty = """
    <tr>
      <td colspan="8">
        <div class="empty-table-state">
          <strong>No emissions logged for this period.</strong>
          <span>Use “Log Emission” to record transport, electricity, diesel, waste, or production activity.</span>
        </div>
      </td>
    </tr>
    """

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Carbon Footprint | AZed ERP</title>
  <script src="/static/theme-init.js"></script>
  <link rel="stylesheet" href="/static/dashboard.css">
  <script src="https://unpkg.com/htmx.org@1.9.12" defer></script>
  <style>
    :root {{
      --carbon-line: var(--border, rgba(148, 163, 184, .24));
      --carbon-line-strong: var(--border-strong, rgba(148, 163, 184, .34));
      --carbon-card: color-mix(in srgb, var(--surface, #1E293B) 92%, transparent);
      --carbon-card-strong: color-mix(in srgb, var(--surface-raised, #334155) 86%, transparent);
      --carbon-muted: var(--text-muted, #94A3B8);
      --carbon-sub: var(--text-sub, #cbd5e1);
      --carbon-teal: var(--teal, #2dd4bf);
      --carbon-green: var(--green, #22c55e);
      --carbon-amber: var(--amber, #f59e0b);
      --carbon-red: var(--negative, #f87171);
      --carbon-purple: var(--purple, #a855f7);
    }}
    .main-content {{max-width:1320px;margin:0 auto;padding:30px 24px 46px;position:relative;z-index:2}}
    .page-header {{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:22px;flex-wrap:wrap}}
    .page-title {{margin:0;font-size:clamp(1.9rem,4vw,3rem);line-height:1.02;font-weight:950;letter-spacing:-.04em;color:var(--text)}}
    .page-subtitle {{margin:8px 0 0;color:var(--carbon-sub);font-size:.98rem;max-width:760px}}
    .page-actions {{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}}
    .btn {{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:40px;padding:0 14px;border-radius:12px;border:1px solid var(--carbon-line-strong);font-weight:850;font-size:.9rem;cursor:pointer;text-decoration:none;transition:transform .16s ease,border-color .16s ease,background .16s ease,color .16s ease}}
    .btn:hover {{transform:translateY(-1px);border-color:color-mix(in srgb,var(--accent) 48%,var(--carbon-line-strong))}}
    .btn-primary {{background:linear-gradient(135deg,var(--carbon-teal),var(--accent));color:#04151a;border-color:transparent;box-shadow:0 14px 32px color-mix(in srgb,var(--accent) 20%,transparent)}}
    .btn-secondary {{background:var(--carbon-card);color:var(--text)}}
    .hero-panel {{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(320px,.9fr);gap:18px;margin-bottom:20px}}
    .hero-card,.panel-card,.carbon-card,.target-card,.empty-card {{background:var(--carbon-card);border:1px solid var(--carbon-line);border-radius:18px;box-shadow:0 18px 46px rgba(0,0,0,.18);backdrop-filter:blur(14px)}}
    .hero-card {{padding:24px;overflow:hidden;position:relative}}
    .hero-card:after {{content:"";position:absolute;right:-70px;top:-100px;width:280px;height:280px;border-radius:50%;background:radial-gradient(circle,color-mix(in srgb,var(--carbon-teal) 28%,transparent),transparent 66%);pointer-events:none}}
    .hero-eyebrow {{display:flex;align-items:center;gap:8px;color:var(--carbon-teal);font-size:.8rem;font-weight:900;letter-spacing:.08em;text-transform:uppercase;margin-bottom:10px}}
    .hero-total {{font-size:clamp(2.4rem,6vw,4.8rem);font-weight:950;letter-spacing:-.07em;line-height:.96;color:var(--text);margin:0}}
    .hero-total span {{font-size:1rem;letter-spacing:0;color:var(--carbon-muted);font-weight:800;margin-left:8px}}
    .hero-meta {{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}}
    .meta-pill,.quick-chip,.ref-pill,.date-pill {{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--carbon-line);background:color-mix(in srgb,var(--surface) 76%,transparent);border-radius:999px;padding:7px 10px;color:var(--carbon-sub);font-size:.82rem;font-weight:750}}
    .meta-pill.good {{color:var(--carbon-green);border-color:color-mix(in srgb,var(--carbon-green) 36%,transparent)}}
    .meta-pill.bad {{color:var(--carbon-red);border-color:color-mix(in srgb,var(--carbon-red) 36%,transparent)}}
    .meta-pill.neutral {{color:var(--carbon-muted)}}
    .quick-panel {{padding:18px}}
    .quick-title {{font-weight:900;margin:0 0 12px;color:var(--text)}}
    .quick-links {{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}}
    .quick-chip {{padding:8px 11px;text-decoration:none}}
    .quick-chip.active {{background:var(--accent-soft);border-color:color-mix(in srgb,var(--accent) 42%,transparent);color:var(--accent)}}
    .filter-bar {{display:grid;grid-template-columns:1fr 1fr auto;gap:10px;align-items:end}}
    .filter-bar label,.field label {{display:block;font-size:.74rem;font-weight:900;text-transform:uppercase;letter-spacing:.05em;color:var(--carbon-muted);margin-bottom:5px}}
    .filter-bar input,.field input,.field select,.field textarea {{width:100%;min-height:40px;border:1px solid var(--carbon-line-strong);border-radius:12px;background:color-mix(in srgb,var(--surface) 82%,transparent);color:var(--text);padding:0 12px;font:inherit}}
    .carbon-grid {{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:18px}}
    .carbon-card {{padding:17px 18px;position:relative;overflow:hidden}}
    .carbon-card:before {{content:"";position:absolute;inset:0 0 auto;height:4px;background:var(--accent)}}
    .carbon-card.transport:before {{background:var(--carbon-amber)}}
    .carbon-card.energy:before {{background:var(--carbon-red)}}
    .carbon-card.waste:before {{background:var(--carbon-green)}}
    .carbon-card.prod:before {{background:var(--carbon-purple)}}
    .carbon-card .label {{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:.74rem;font-weight:900;text-transform:uppercase;letter-spacing:.07em;color:var(--carbon-muted);margin-bottom:8px}}
    .carbon-card .value {{font-size:1.75rem;font-weight:950;color:var(--text);letter-spacing:-.04em}}
    .carbon-card .unit {{font-size:.82rem;color:var(--carbon-muted);margin-top:2px}}
    .content-grid {{display:grid;grid-template-columns:minmax(0,.9fr) minmax(0,1.1fr);gap:18px;margin-bottom:18px}}
    .panel-card {{padding:18px}}
    .section-title-row {{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}}
    .section-title-row h2,.section-title {{margin:0;font-size:1.08rem;font-weight:950;color:var(--text)}}
    .section-title-row span {{color:var(--carbon-muted);font-size:.84rem;font-weight:700}}
    .insight-grid {{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}}
    .insight-card {{border:1px solid var(--carbon-line);border-radius:14px;padding:14px;background:color-mix(in srgb,var(--surface) 74%,transparent)}}
    .insight-label {{font-size:.72rem;font-weight:900;letter-spacing:.06em;text-transform:uppercase;color:var(--carbon-muted)}}
    .insight-value {{font-size:1rem;font-weight:950;color:var(--text);margin-top:4px}}
    .breakdown-list {{display:grid;gap:13px}}
    .breakdown-head {{display:flex;justify-content:space-between;gap:12px;margin-bottom:7px;color:var(--carbon-sub);font-weight:800;font-size:.88rem}}
    .breakdown-name {{display:flex;align-items:center;gap:8px}}
    .breakdown-value {{color:var(--carbon-muted);white-space:nowrap}}
    .breakdown-track,.progress-bar-track {{height:10px;background:color-mix(in srgb,var(--surface-raised) 62%,transparent);border-radius:999px;overflow:hidden;border:1px solid color-mix(in srgb,var(--carbon-line) 70%,transparent)}}
    .breakdown-fill,.progress-bar {{height:100%;border-radius:999px;transition:width .35s ease}}
    .source-transport {{background:var(--carbon-amber)}}
    .source-energy {{background:var(--carbon-red)}}
    .source-waste {{background:var(--carbon-green)}}
    .source-production {{background:var(--carbon-purple)}}
    .source-other {{background:var(--accent)}}
    .target-card {{padding:16px;margin-bottom:12px}}
    .target-header {{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:12px}}
    .target-header strong {{display:block;color:var(--text);font-size:.98rem}}
    .target-dates {{display:block;color:var(--carbon-muted);font-size:.8rem;margin-top:3px}}
    .target-delete {{border:1px solid var(--carbon-line);background:transparent;color:var(--carbon-muted);border-radius:999px;padding:6px 10px;cursor:pointer;font-weight:800;font-size:.76rem}}
    .target-delete:hover {{border-color:var(--carbon-red);color:var(--carbon-red)}}
    .progress-ok {{background:var(--carbon-teal)}}
    .progress-warn {{background:var(--carbon-amber)}}
    .progress-over {{background:var(--carbon-red)}}
    .target-stats {{display:flex;gap:12px;flex-wrap:wrap;font-size:.8rem;color:var(--carbon-muted);font-weight:750;margin-top:9px}}
    .target-notes {{margin:10px 0 0;color:var(--carbon-sub);font-size:.86rem}}
    .add-target-card {{padding:18px;margin-top:12px}}
    .add-target-toggle {{border:1px dashed color-mix(in srgb,var(--carbon-teal) 48%,var(--carbon-line));background:color-mix(in srgb,var(--carbon-teal) 9%,transparent);color:var(--carbon-teal);width:100%;min-height:42px;border-radius:14px;font-weight:900;cursor:pointer}}
    .target-form-grid {{display:grid;grid-template-columns:1.4fr 1fr 1fr 1fr auto;gap:10px;align-items:end;margin-top:12px}}
    .hidden {{display:none!important}}
    .table-card {{background:var(--carbon-card);border:1px solid var(--carbon-line);border-radius:18px;overflow:hidden;box-shadow:0 18px 46px rgba(0,0,0,.18)}}
    .table-toolbar {{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:16px 18px;border-bottom:1px solid var(--carbon-line)}}
    .table-toolbar h2 {{margin:0;font-size:1.08rem;font-weight:950}}
    .table-toolbar span {{color:var(--carbon-muted);font-size:.84rem;font-weight:700}}
    .table-wrap {{overflow:auto}}
    .data-table {{width:100%;border-collapse:separate;border-spacing:0;min-width:920px}}
    .data-table th,.data-table td {{padding:13px 14px;border-bottom:1px solid var(--carbon-line);vertical-align:top;text-align:left}}
    .data-table th {{font-size:.74rem;text-transform:uppercase;letter-spacing:.07em;color:var(--carbon-muted);background:color-mix(in srgb,var(--surface-raised) 48%,transparent);font-weight:950}}
    .data-table tr:last-child td {{border-bottom:0}}
    .data-table tbody tr:hover td {{background:color-mix(in srgb,var(--accent) 4%,transparent)}}
    .num {{text-align:right!important;white-space:nowrap}}
    .co2 {{color:var(--carbon-teal)}}
    .actions-cell {{width:52px;text-align:center!important}}
    .btn-icon {{border:1px solid var(--carbon-line);border-radius:10px;background:transparent;color:var(--carbon-muted);width:32px;height:32px;cursor:pointer;font-size:.86rem}}
    .btn-danger:hover {{border-color:var(--carbon-red);color:var(--carbon-red)}}
    .carbon-badge {{display:inline-flex;align-items:center;border-radius:999px;padding:5px 9px;font-size:.74rem;font-weight:900;text-transform:capitalize;border:1px solid transparent;white-space:nowrap}}
    .carbon-badge-transport {{background:color-mix(in srgb,var(--carbon-amber) 14%,transparent);color:var(--carbon-amber);border-color:color-mix(in srgb,var(--carbon-amber) 30%,transparent)}}
    .carbon-badge-energy {{background:color-mix(in srgb,var(--carbon-red) 14%,transparent);color:var(--carbon-red);border-color:color-mix(in srgb,var(--carbon-red) 30%,transparent)}}
    .carbon-badge-waste {{background:color-mix(in srgb,var(--carbon-green) 14%,transparent);color:var(--carbon-green);border-color:color-mix(in srgb,var(--carbon-green) 30%,transparent)}}
    .carbon-badge-production {{background:color-mix(in srgb,var(--carbon-purple) 14%,transparent);color:var(--carbon-purple);border-color:color-mix(in srgb,var(--carbon-purple) 30%,transparent)}}
    .log-notes {{color:var(--carbon-muted);font-size:.8rem;font-weight:600;margin-top:3px;max-width:360px}}
    .empty-card {{padding:18px;display:grid;gap:4px;color:var(--carbon-sub)}}
    .empty-card.compact {{margin-bottom:12px}}
    .empty-card strong,.empty-table-state strong {{color:var(--text)}}
    .empty-table-state {{display:grid;justify-items:center;text-align:center;gap:5px;color:var(--carbon-muted);padding:30px 16px}}
    @media(max-width:1080px){{.hero-panel,.content-grid{{grid-template-columns:1fr}}.carbon-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}.target-form-grid{{grid-template-columns:1fr 1fr}}}}
    @media(max-width:680px){{.main-content{{padding:22px 14px 36px}}.page-actions,.filter-bar,.carbon-grid,.insight-grid,.target-form-grid{{grid-template-columns:1fr;width:100%}}.filter-bar{{display:grid}}.carbon-grid{{display:grid}}.hero-meta{{display:grid}}.page-actions .btn{{width:100%}}}}
  </style>
</head>
<body>
{nav_html}
<main class="main-content">
  <div class="page-header">
    <div>
      <h1 class="page-title">Carbon Footprint</h1>
      <p class="page-subtitle">Track CO₂-equivalent emissions from transport, energy, production, and waste across your farm operations.</p>
    </div>
    <div class="page-actions">
      <a href="/carbon/log" class="btn btn-primary">+ Log Emission</a>
      <a href="/carbon/report?date_from={d_from.isoformat()}&date_to={d_to.isoformat()}" class="btn btn-secondary">📄 Sustainability Report</a>
      <a href="/carbon/factors" class="btn btn-secondary">Manage Factors</a>
    </div>
  </div>

  <section class="hero-panel" aria-label="Carbon overview">
    <div class="hero-card">
      <div class="hero-eyebrow">🌿 Carbon overview · {_safe(_period_label(d_from, d_to))}</div>
      <p class="hero-total">{_fmt_number(grand_total, 1)}<span>kg CO₂e</span></p>
      <div class="hero-meta">
        <span class="meta-pill {delta_class}">{_safe(delta_label)}</span>
        <span class="meta-pill">Daily avg: {_fmt_kg(daily_average)}</span>
        <span class="meta-pill">Entries: {entry_count}</span>
      </div>
    </div>
    <div class="quick-panel panel-card">
      <p class="quick-title">Date range</p>
      <div class="quick-links">{quick_links}</div>
      <form method="get" class="filter-bar">
        <div><label>From</label><input type="date" name="date_from" value="{d_from.isoformat()}"></div>
        <div><label>To</label><input type="date" name="date_to" value="{d_to.isoformat()}"></div>
        <button type="submit" class="btn btn-secondary">Apply</button>
      </form>
    </div>
  </section>

  <section class="carbon-grid" aria-label="Carbon category totals">
    <div class="carbon-card transport">
      <div class="label"><span>Transport</span><span>🚚</span></div>
      <div class="value">{_fmt_number(transport_val, 1)}</div>
      <div class="unit">kg CO₂e</div>
    </div>
    <div class="carbon-card energy">
      <div class="label"><span>Energy</span><span>⚡</span></div>
      <div class="value">{_fmt_number(energy_val, 1)}</div>
      <div class="unit">kg CO₂e</div>
    </div>
    <div class="carbon-card waste">
      <div class="label"><span>Waste / Spoilage</span><span>♻️</span></div>
      <div class="value">{_fmt_number(waste_val, 1)}</div>
      <div class="unit">kg CO₂e</div>
    </div>
    <div class="carbon-card prod">
      <div class="label"><span>Production</span><span>🏭</span></div>
      <div class="value">{_fmt_number(production_val, 1)}</div>
      <div class="unit">kg CO₂e</div>
    </div>
  </section>

  <section class="content-grid">
    <div class="panel-card">
      <div class="section-title-row"><h2>Category Breakdown</h2><span>{_fmt_kg(grand_total)} total</span></div>
      <div class="breakdown-list">{breakdown_html}</div>
    </div>
    <div class="panel-card">
      <div class="section-title-row"><h2>Operating Insights</h2><span>Selected range</span></div>
      <div class="insight-grid">
        <div class="insight-card"><div class="insight-label">Highest source</div><div class="insight-value">{_safe(leading_source_label)}</div></div>
        <div class="insight-card"><div class="insight-label">Highest source kg</div><div class="insight-value">{_safe(leading_source_value)}</div></div>
        <div class="insight-card"><div class="insight-label">Target status</div><div class="insight-value">{_safe(best_target_status)}</div></div>
      </div>
    </div>
  </section>

  <section class="panel-card" aria-label="Reduction targets">
    <div class="section-title-row"><h2>Reduction Targets</h2><span>Progress uses selected range total</span></div>
    {target_section}
    <div class="add-target-card">
      <button class="add-target-toggle" type="button" onclick="document.getElementById('targetForm').classList.toggle('hidden')">+ Add target</button>
      <form id="targetForm" class="target-form-grid hidden">
        <div class="field"><label>Label</label><input name="label" placeholder="e.g. May carbon budget" required></div>
        <div class="field"><label>Start</label><input type="date" name="period_start" value="{d_from.isoformat()}" required></div>
        <div class="field"><label>End</label><input type="date" name="period_end" value="{d_to.isoformat()}" required></div>
        <div class="field"><label>Target kg CO₂e</label><input type="number" name="target_kg_co2e" step="0.01" min="0.01" required></div>
        <button type="submit" class="btn btn-primary">Save</button>
      </form>
    </div>
  </section>

  <section class="table-card" aria-label="Emission logs">
    <div class="table-toolbar">
      <h2>Emission Logs</h2>
      <span>Showing latest {len(logs)} of {entry_count} entries</span>
    </div>
    <div class="table-wrap">
      <table class="data-table">
        <thead>
          <tr>
            <th>Date</th><th>Category</th><th>Source</th><th>Farm</th>
            <th class="num">Quantity</th><th class="num">CO₂e</th>
            <th>Ref</th><th></th>
          </tr>
        </thead>
        <tbody id="logBody">
          {log_rows if log_rows else log_empty}
        </tbody>
      </table>
    </div>
  </section>
</main>
<script>
  document.getElementById("targetForm").addEventListener("submit", async function (event) {{
    event.preventDefault();
    var form = event.currentTarget;
    var data = Object.fromEntries(new FormData(form).entries());
    data.target_kg_co2e = parseFloat(data.target_kg_co2e);

    var response = await fetch("/carbon/api/targets", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(data)
    }});

    if (response.ok) {{
      window.location.reload();
      return;
    }}

    var error = await response.json().catch(function () {{ return {{detail: "Unknown error"}}; }});
    alert("Could not save target: " + (error.detail || "Unknown error"));
  }});

  document.querySelectorAll(".target-delete").forEach(function (button) {{
    button.addEventListener("click", async function () {{
      if (!confirm("Delete this carbon target?")) return;
      var targetId = button.getAttribute("data-target-id");
      var response = await fetch("/carbon/api/targets/" + targetId, {{method: "DELETE"}});
      if (response.ok) {{
        window.location.reload();
        return;
      }}
      alert("Could not delete target.");
    }});
  }});
</script>
</body>
</html>"""
    return HTMLResponse(html)


# ── Page: Log emission form ───────────────────────────────────────────────────

@router.get("/log", response_class=HTMLResponse)
async def log_emission_page(
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    factors_q = await db.execute(
        select(CarbonEmissionFactor)
        .where(CarbonEmissionFactor.is_active == True)
        .order_by(CarbonEmissionFactor.source_type, CarbonEmissionFactor.label)
    )
    factors = factors_q.scalars().all()

    farms_q = await db.execute(select(Farm).where(Farm.is_active == 1).order_by(Farm.name))
    farms = farms_q.scalars().all()

    nav_html = render_app_header(current_user, "page_carbon")

    factor_options = ""
    current_group = ""
    for f in factors:
        if f.source_type != current_group:
            if current_group:
                factor_options += "</optgroup>"
            factor_options += f'<optgroup label="{_safe(f.source_type.title())}">'
            current_group = f.source_type
        factor_options += (
            f'<option value="{f.id}" data-unit="{_safe(f.unit)}" '
            f'data-factor="{float(f.factor_kg_co2e_per_unit)}">'
            f'{_safe(f.label)} ({_safe(f.unit)})</option>'
        )
    if current_group:
        factor_options += "</optgroup>"

    if not factor_options:
        factor_options = '<option value="" disabled selected>No active emission factors</option>'

    farm_options = "".join(f'<option value="{fm.id}">{_safe(fm.name)}</option>' for fm in farms)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Log Emission | Carbon | AZed ERP</title>
  <script src="/static/theme-init.js"></script>
  <link rel="stylesheet" href="/static/dashboard.css">
  <style>
    .main-content {{max-width:920px;margin:0 auto;padding:30px 24px 46px;position:relative;z-index:2}}
    .page-header {{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:22px;flex-wrap:wrap}}
    .page-title {{margin:0;font-size:clamp(1.8rem,4vw,2.6rem);line-height:1.05;font-weight:950;letter-spacing:-.04em;color:var(--text)}}
    .page-subtitle {{margin:8px 0 0;color:var(--text-sub);font-size:.98rem}}
    .btn {{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:40px;padding:0 14px;border-radius:12px;border:1px solid var(--border-strong);font-weight:850;font-size:.9rem;cursor:pointer;text-decoration:none}}
    .btn-primary {{background:linear-gradient(135deg,var(--teal,#2dd4bf),var(--accent));color:#04151a;border-color:transparent}}
    .btn-secondary {{background:var(--surface);color:var(--text)}}
    .form-card{{max-width:620px;background:color-mix(in srgb,var(--surface) 92%,transparent);border:1px solid var(--border);border-radius:18px;padding:28px 28px 24px;box-shadow:0 18px 46px rgba(0,0,0,.18)}}
    .field{{margin-bottom:18px}}
    .field label{{display:block;margin-bottom:6px;font-size:.78rem;font-weight:900;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted)}}
    .field input,.field select,.field textarea{{width:100%;min-height:42px;border:1px solid var(--border-strong);border-radius:12px;background:var(--bg);color:var(--text);padding:0 12px;font:inherit}}
    .field textarea{{padding:10px 12px;min-height:86px;resize:vertical}}
    .co2-preview{{padding:13px 16px;background:color-mix(in srgb,var(--teal,#2dd4bf) 12%,transparent);border:1px solid color-mix(in srgb,var(--teal,#2dd4bf) 30%,transparent);border-radius:12px;font-weight:900;color:var(--teal,#2dd4bf);margin-bottom:18px;font-size:1.05rem}}
  </style>
</head>
<body>
{nav_html}
<main class="main-content">
  <div class="page-header">
    <div>
      <h1 class="page-title">Log Emission</h1>
      <p class="page-subtitle">Record a CO₂-equivalent emission event</p>
    </div>
    <a href="/carbon/" class="btn btn-secondary">← Back</a>
  </div>

  <div class="form-card">
    <form id="logForm">
      <div class="field">
        <label>Emission Source</label>
        <select id="factorSel" name="factor_id" required onchange="updatePreview()">{factor_options}</select>
      </div>
      <div class="field">
        <label>Date</label>
        <input type="date" name="log_date" value="{date.today().isoformat()}" required>
      </div>
      <div class="field">
        <label>Quantity (<span id="unitLabel">unit</span>)</label>
        <input type="number" id="qtyInp" name="quantity" step="0.001" min="0.001"
               required oninput="updatePreview()">
      </div>
      <div class="co2-preview" id="co2Preview">CO₂e: — kg</div>
      <div class="field">
        <label>Farm (optional)</label>
        <select name="farm_id">
          <option value="">— None —</option>
          {farm_options}
        </select>
      </div>
      <div class="field">
        <label>Notes</label>
        <textarea name="notes" placeholder="e.g. Diesel for irrigation pump, Field 3"></textarea>
      </div>
      <button type="submit" class="btn btn-primary" style="width:100%">Save Entry</button>
    </form>
  </div>
</main>
<script>
  function updatePreview() {{
    var sel = document.getElementById("factorSel");
    var opt = sel.options[sel.selectedIndex];
    if (!opt) return;
    var unit = opt.dataset.unit || "unit";
    var factor = parseFloat(opt.dataset.factor) || 0;
    var qty = parseFloat(document.getElementById("qtyInp").value) || 0;
    document.getElementById("unitLabel").textContent = unit;
    var co2 = (qty * factor).toFixed(3);
    document.getElementById("co2Preview").textContent = "CO₂e: " + co2 + " kg";
  }}
  updatePreview();

  document.getElementById("logForm").addEventListener("submit", async function(e) {{
    e.preventDefault();
    var fd = new FormData(this);
    var body = Object.fromEntries(fd.entries());
    body.factor_id = parseInt(body.factor_id, 10);
    body.quantity = parseFloat(body.quantity);
    if (body.farm_id === "") delete body.farm_id;
    else body.farm_id = parseInt(body.farm_id, 10);

    var r = await fetch("/carbon/api/logs", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(body)
    }});
    if (r.ok) {{ window.location.href = "/carbon/"; }}
    else {{
      var err = await r.json().catch(function () {{ return {{detail:"Unknown error"}}; }});
      alert("Error: " + (err.detail || "Unknown error"));
    }}
  }});
</script>
</body>
</html>"""
    return HTMLResponse(html)


# ── Page: Factors management ──────────────────────────────────────────────────

@router.get("/factors", response_class=HTMLResponse)
async def factors_page(
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    factors_q = await db.execute(
        select(CarbonEmissionFactor).order_by(CarbonEmissionFactor.source_type, CarbonEmissionFactor.label)
    )
    factors = factors_q.scalars().all()

    nav_html = render_app_header(current_user, "page_carbon")

    rows = ""
    for f in factors:
        active_badge = '<span style="color:var(--teal,#2dd4bf)">Active</span>' if f.is_active else '<span style="color:var(--text-muted)">Inactive</span>'
        scope_html = f'<span class="scope-chip scope-{int(f.scope)}">Scope {int(f.scope)}</span>' if f.scope else '<span style="color:var(--text-muted)">—</span>'
        method_bits = []
        if f.methodology_source:
            method_bits.append(_safe(f.methodology_source))
        meta = " · ".join(x for x in [str(f.source_year) if f.source_year else "", _safe(f.region) if f.region else ""] if x)
        method_html = (
            f'<div style="font-size:.82rem;line-height:1.35">{method_bits[0]}'
            + (f'<div style="color:var(--text-muted);font-size:.74rem;margin-top:2px">{meta}</div>' if meta else "")
            + "</div>"
        ) if method_bits else '<span style="color:var(--text-muted)">—</span>'
        rows += f"""
        <tr>
          <td>{_badge_html(f.source_type)}</td>
          <td>{_safe(f.source_key)}</td>
          <td>{_safe(f.label)}</td>
          <td class="num">{float(f.factor_kg_co2e_per_unit)}</td>
          <td>{_safe(f.unit)}</td>
          <td>{scope_html}</td>
          <td>{method_html}</td>
          <td>{active_badge}</td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:28px">No emission factors found.</td></tr>'

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Emission Factors | Carbon | AZed ERP</title>
  <script src="/static/theme-init.js"></script>
  <link rel="stylesheet" href="/static/dashboard.css">
  <style>
    .main-content {{max-width:1120px;margin:0 auto;padding:30px 24px 46px;position:relative;z-index:2}}
    .page-header {{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:22px;flex-wrap:wrap}}
    .page-title {{margin:0;font-size:clamp(1.8rem,4vw,2.6rem);line-height:1.05;font-weight:950;letter-spacing:-.04em;color:var(--text)}}
    .page-subtitle {{margin:8px 0 0;color:var(--text-sub);font-size:.98rem}}
    .btn {{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:40px;padding:0 14px;border-radius:12px;border:1px solid var(--border-strong);font-weight:850;font-size:.9rem;cursor:pointer;text-decoration:none}}
    .btn-secondary {{background:var(--surface);color:var(--text)}}
    .table-wrap {{overflow:auto;background:color-mix(in srgb,var(--surface) 92%,transparent);border:1px solid var(--border);border-radius:18px;box-shadow:0 18px 46px rgba(0,0,0,.18)}}
    .data-table {{width:100%;border-collapse:separate;border-spacing:0;min-width:760px}}
    .data-table th,.data-table td {{padding:13px 14px;border-bottom:1px solid var(--border);text-align:left}}
    .data-table th {{font-size:.74rem;text-transform:uppercase;letter-spacing:.07em;color:var(--text-muted);background:color-mix(in srgb,var(--surface-raised) 48%,transparent);font-weight:950}}
    .data-table tr:last-child td {{border-bottom:0}}
    .num{{text-align:right}}
    .carbon-badge {{display:inline-flex;align-items:center;border-radius:999px;padding:5px 9px;font-size:.74rem;font-weight:900;text-transform:capitalize;border:1px solid transparent;white-space:nowrap}}
    .carbon-badge-transport {{background:color-mix(in srgb,var(--amber,#f59e0b) 14%,transparent);color:var(--amber,#f59e0b);border-color:color-mix(in srgb,var(--amber,#f59e0b) 30%,transparent)}}
    .carbon-badge-energy {{background:color-mix(in srgb,var(--negative,#f87171) 14%,transparent);color:var(--negative,#f87171);border-color:color-mix(in srgb,var(--negative,#f87171) 30%,transparent)}}
    .carbon-badge-waste {{background:color-mix(in srgb,var(--green,#22c55e) 14%,transparent);color:var(--green,#22c55e);border-color:color-mix(in srgb,var(--green,#22c55e) 30%,transparent)}}
    .carbon-badge-production {{background:color-mix(in srgb,var(--purple,#a855f7) 14%,transparent);color:var(--purple,#a855f7);border-color:color-mix(in srgb,var(--purple,#a855f7) 30%,transparent)}}
    .scope-chip {{display:inline-flex;align-items:center;border-radius:999px;padding:4px 9px;font-size:.72rem;font-weight:900;white-space:nowrap;border:1px solid transparent}}
    .scope-1 {{background:color-mix(in srgb,var(--negative,#f87171) 14%,transparent);color:var(--negative,#f87171);border-color:color-mix(in srgb,var(--negative,#f87171) 30%,transparent)}}
    .scope-2 {{background:color-mix(in srgb,var(--amber,#f59e0b) 14%,transparent);color:var(--amber,#f59e0b);border-color:color-mix(in srgb,var(--amber,#f59e0b) 30%,transparent)}}
    .scope-3 {{background:color-mix(in srgb,var(--blue,#4d9fff) 14%,transparent);color:var(--blue,#4d9fff);border-color:color-mix(in srgb,var(--blue,#4d9fff) 30%,transparent)}}
  </style>
</head>
<body>
{nav_html}
<main class="main-content">
  <div class="page-header">
    <div>
      <h1 class="page-title">Emission Factors</h1>
      <p class="page-subtitle">CO₂e coefficients used to calculate emissions</p>
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-secondary" id="backfill-btn" onclick="backfillAutoLogs()">↻ Backfill from history</button>
      <a href="/carbon/" class="btn btn-secondary">← Back</a>
    </div>
  </div>
  <div class="table-wrap">
    <table class="data-table">
      <thead>
        <tr><th>Category</th><th>Key</th><th>Label</th><th class="num">Factor</th><th>Unit</th><th>Scope</th><th>Methodology</th><th>Status</th></tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</main>
<script>
async function backfillAutoLogs(){{
    const btn = document.getElementById("backfill-btn");
    btn.disabled = true;
    try {{
        // 1. Dry run — find out what would be created
        let res = await fetch("/carbon/api/backfill-auto-logs?dry_run=true", {{method:"POST"}});
        let d = await res.json();
        if (d.detail) {{ alert(d.detail); return; }}
        const total = (d.expenses_logged || 0) + (d.spoilage_logged || 0);
        if (total === 0) {{
            alert("Nothing to backfill — all mapped historical records already have carbon logs."
                + (d.skipped ? " (" + d.skipped + " records skipped: no matching active factor or non-mass unit.)" : ""));
            return;
        }}
        // 2. Confirm with real counts, then run for real
        if (!confirm("This will create " + d.expenses_logged + " carbon log(s) from past expenses and "
                   + d.spoilage_logged + " from past spoilage records."
                   + (d.skipped ? " " + d.skipped + " record(s) will be skipped." : "")
                   + " Existing logs are never modified. Proceed?")) return;
        res = await fetch("/carbon/api/backfill-auto-logs?dry_run=false", {{method:"POST"}});
        d = await res.json();
        if (d.detail) {{ alert(d.detail); return; }}
        alert("Done — created " + ((d.expenses_logged||0) + (d.spoilage_logged||0)) + " carbon log(s).");
        window.location.href = "/carbon/";
    }} catch(e) {{
        alert("Backfill failed — check the server logs.");
    }} finally {{
        btn.disabled = false;
    }}
}}
</script>
</body>
</html>"""
    return HTMLResponse(html)


# ── Farm Sustainability Report (printable) ──────────────────────────────────

@router.get("/report", response_class=HTMLResponse)
async def sustainability_report(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """Print-first sustainability report: totals, GHG Protocol scope breakdown,
    category breakdown, emission intensity vs production output, monthly trend,
    target progress, and a methodology appendix listing every active factor
    with its provenance."""
    from app.models.production import ProductionBatch, BatchOutput

    d_from, d_to = _date_range_defaults(date_from, date_to)
    days_in_range = max((d_to - d_from).days + 1, 1)

    # ── Core aggregates ──
    by_type = await _totals_by_source(db, d_from, d_to)
    grand_total = sum(by_type.values())
    entry_count = await _entry_count(db, d_from, d_to)
    daily_average = grand_total / days_in_range if days_in_range else 0.0

    previous_to = d_from - timedelta(days=1)
    previous_from = previous_to - timedelta(days=days_in_range - 1)
    previous_total = sum((await _totals_by_source(db, previous_from, previous_to)).values())
    if previous_total > 0:
        delta_pct = ((grand_total - previous_total) / previous_total) * 100
        delta_text = f"{delta_pct:+.1f}% vs previous {days_in_range} days"
    else:
        delta_text = "no data for previous period"

    # ── Scope breakdown (GHG Protocol) ──
    scope_q = await db.execute(
        select(CarbonEmissionFactor.scope, func.coalesce(func.sum(CarbonLog.kg_co2e), 0))
        .join(CarbonLog, CarbonLog.factor_id == CarbonEmissionFactor.id)
        .where(CarbonLog.log_date >= d_from, CarbonLog.log_date <= d_to)
        .group_by(CarbonEmissionFactor.scope)
    )
    scope_totals = {row[0]: float(row[1] or 0) for row in scope_q.all()}
    SCOPE_DESCRIPTIONS = {
        1: "Direct emissions — fuel burned in owned equipment and vehicles",
        2: "Indirect emissions — purchased electricity",
        3: "Value-chain emissions — waste disposal and other indirect sources",
    }

    # ── Emission intensity: kg CO₂e per kg of farm produce ──
    # Primary denominator = FARM INTAKE: every kilogram the farm produces
    # enters AZed as a farm delivery, whether it is later sold fresh or
    # processed, so intake is the upstream total that avoids double counting.
    # Production batch output is kept as a secondary KPI for the processed
    # subset. Both are mass-aware: only kg/g units count, grams converted to
    # kg (same guard as the spoilage auto-logger) — piece/bunch products are
    # excluded rather than silently summed as kilograms.
    from app.models.farm import FarmDelivery as _FD, FarmDeliveryItem as _FDI
    from app.models.product import Product as _Product
    from app.routers.production import _MASS_UNITS_G, _MASS_UNITS_KG

    def _mass_kg(rows) -> float:
        """Sum quantities as kilograms: kg as-is, g ÷1000, and piece/box/pack
        units via the product's configured average weight per piece."""
        total = 0.0
        for _qty, _unit, _piece_kg in rows:
            u = (_unit or "").strip().lower()
            if u in _MASS_UNITS_KG:
                total += float(_qty or 0)
            elif u in _MASS_UNITS_G:
                total += float(_qty or 0) / 1000.0
            elif _piece_kg and float(_piece_kg) > 0:
                total += float(_qty or 0) * float(_piece_kg)
        return total

    intake_q = await db.execute(
        select(_FDI.qty, func.coalesce(_FDI.unit, _Product.unit), _Product.unit_weight_kg)
        .join(_FD, _FDI.delivery_id == _FD.id)
        .join(_Product, _FDI.product_id == _Product.id)
        .where(_FD.delivery_date >= d_from, _FD.delivery_date <= d_to)
    )
    farm_intake_kg = _mass_kg(intake_q.all())

    output_q = await db.execute(
        select(BatchOutput.qty, _Product.unit, _Product.unit_weight_kg)
        .join(ProductionBatch, BatchOutput.batch_id == ProductionBatch.id)
        .join(_Product, BatchOutput.product_id == _Product.id)
        .where(
            func.date(ProductionBatch.created_at) >= d_from,
            func.date(ProductionBatch.created_at) <= d_to,
            ProductionBatch.status == "completed",
        )
    )
    production_output_kg = _mass_kg(output_q.all())

    intensity = (grand_total / farm_intake_kg) if farm_intake_kg > 0 else None

    # ── Monthly trend ──
    month_expr = func.to_char(CarbonLog.log_date, "YYYY-MM")
    trend_q = await db.execute(
        select(month_expr, func.coalesce(func.sum(CarbonLog.kg_co2e), 0), func.count(CarbonLog.id))
        .where(CarbonLog.log_date >= d_from, CarbonLog.log_date <= d_to)
        .group_by(month_expr)
        .order_by(month_expr)
    )
    trend_rows = trend_q.all()

    # ── Targets overlapping the period ──
    targets_q = await db.execute(
        select(CarbonTarget)
        .where(CarbonTarget.period_start <= d_to, CarbonTarget.period_end >= d_from)
        .order_by(CarbonTarget.period_start)
    )
    targets = targets_q.scalars().all()

    # ── Active factors for the methodology appendix ──
    factors_q = await db.execute(
        select(CarbonEmissionFactor)
        .where(CarbonEmissionFactor.is_active == True)
        .order_by(CarbonEmissionFactor.scope.nulls_last(), CarbonEmissionFactor.source_type, CarbonEmissionFactor.label)
    )
    factors = factors_q.scalars().all()

    # ── HTML fragments ──
    scope_rows_html = ""
    for s in (1, 2, 3):
        val = scope_totals.get(s, 0.0)
        pct = (val / grand_total * 100) if grand_total > 0 else 0
        scope_rows_html += f"""
        <tr>
          <td><strong>Scope {s}</strong></td>
          <td>{SCOPE_DESCRIPTIONS[s]}</td>
          <td class="num">{_fmt_kg(val)}</td>
          <td class="num">{pct:.1f}%</td>
        </tr>"""
    unscoped = scope_totals.get(None, 0.0)
    if unscoped > 0:
        pct = (unscoped / grand_total * 100) if grand_total > 0 else 0
        scope_rows_html += f"""
        <tr>
          <td><strong>Unclassified</strong></td>
          <td>Logged against factors without a scope assignment</td>
          <td class="num">{_fmt_kg(unscoped)}</td>
          <td class="num">{pct:.1f}%</td>
        </tr>"""

    cat_rows_html = ""
    for stype in SOURCE_ORDER:
        val = by_type.get(stype, 0.0)
        pct = (val / grand_total * 100) if grand_total > 0 else 0
        cat_rows_html += f"""
        <tr>
          <td>{_source_label(stype)}</td>
          <td class="num">{_fmt_kg(val)}</td>
          <td class="num">{pct:.1f}%</td>
        </tr>"""

    trend_html = "".join(
        f'<tr><td>{row[0]}</td><td class="num">{_fmt_kg(float(row[1] or 0))}</td><td class="num">{int(row[2])}</td></tr>'
        for row in trend_rows
    ) or '<tr><td colspan="3" class="muted-cell">No emissions logged in this period.</td></tr>'

    targets_html = ""
    for t in targets:
        target_val = float(t.target_kg_co2e or 0)
        progress_pct = (grand_total / target_val * 100) if target_val > 0 else 0
        status = "On track" if progress_pct < 80 else ("Approaching limit" if progress_pct <= 100 else "Exceeded")
        targets_html += f"""
        <tr>
          <td>{_safe(t.label)}</td>
          <td>{t.period_start.isoformat()} → {t.period_end.isoformat()}</td>
          <td class="num">{_fmt_kg(target_val)}</td>
          <td class="num">{_fmt_kg(grand_total)}</td>
          <td class="num">{progress_pct:.0f}%</td>
          <td>{status}</td>
        </tr>"""
    if not targets_html:
        targets_html = '<tr><td colspan="6" class="muted-cell">No reduction targets defined for this period.</td></tr>'

    factor_rows_html = "".join(
        f"""<tr>
          <td>{f"Scope {int(f.scope)}" if f.scope else "—"}</td>
          <td>{_safe(f.label)}</td>
          <td class="num">{float(f.factor_kg_co2e_per_unit)}</td>
          <td>kg CO₂e / {_safe(f.unit)}</td>
          <td>{_safe(f.methodology_source) if f.methodology_source else "—"}</td>
          <td class="num">{f.source_year or "—"}</td>
          <td>{_safe(f.region) if f.region else "—"}</td>
        </tr>"""
        for f in factors
    ) or '<tr><td colspan="7" class="muted-cell">No active emission factors.</td></tr>'

    intensity_html = (
        f"<strong>{intensity:.3f}</strong> kg CO₂e per kg of farm produce"
        if intensity is not None
        else '<span class="muted">— (no farm intake recorded in this period)</span>'
    )

    generated = date.today().isoformat()
    period_label = _period_label(d_from, d_to)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Farm Sustainability Report | AZed ERP</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Segoe UI',Arial,sans-serif;background:#f4f4f2;color:#1c1c1c;font-size:13px;line-height:1.5}}
    .sheet{{max-width:900px;margin:0 auto;background:#fff;padding:42px 48px;min-height:100vh}}
    .toolbar{{max-width:900px;margin:0 auto;display:flex;justify-content:space-between;align-items:center;padding:14px 4px}}
    .toolbar a,.toolbar button{{font-size:13px;font-weight:700;padding:8px 16px;border-radius:8px;border:1px solid #c9c9c4;background:#fff;color:#1c1c1c;cursor:pointer;text-decoration:none}}
    .toolbar .print-btn{{background:#2a7a2a;border-color:#2a7a2a;color:#fff}}
    .rpt-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;border-bottom:3px solid #2a7a2a;padding-bottom:16px;margin-bottom:22px}}
    .rpt-head img{{height:90px;object-fit:contain}}
    .org-name{{font-size:16px;font-weight:900;color:#2a7a2a}}
    .org-meta{{font-size:10.5px;color:#666;margin-top:2px}}
    .rpt-title{{font-size:21px;font-weight:800;color:#2a7a2a;text-align:right}}
    .rpt-meta{{font-size:11px;color:#666;margin-top:4px;text-align:right}}
    h2{{font-size:14px;font-weight:800;color:#2a7a2a;margin:26px 0 8px;text-transform:uppercase;letter-spacing:.06em}}
    .kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:6px}}
    .kpi{{border:1px solid #e2e2dd;border-radius:10px;padding:12px 14px}}
    .kpi .lab{{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:#777;font-weight:700}}
    .kpi .val{{font-size:18px;font-weight:800;margin-top:4px}}
    .kpi .sub{{font-size:10.5px;color:#888;margin-top:2px}}
    table{{width:100%;border-collapse:collapse;margin-top:6px}}
    th,td{{padding:8px 10px;border-bottom:1px solid #e7e7e2;text-align:left;vertical-align:top}}
    th{{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:#777;background:#fafaf8}}
    .num{{text-align:right;font-variant-numeric:tabular-nums}}
    .muted,.muted-cell{{color:#999}}
    .muted-cell{{text-align:center;padding:18px}}
    .note{{font-size:11px;color:#666;background:#f7f7f4;border:1px solid #e7e7e2;border-radius:10px;padding:12px 14px;margin-top:8px;line-height:1.55}}
    .intensity-line{{font-size:15px;margin-top:6px}}
    @media print{{
      body{{background:#fff}}
      .toolbar{{display:none}}
      .sheet{{padding:0;max-width:none}}
      h2{{page-break-after:avoid}}
      table{{page-break-inside:auto}}
      tr{{page-break-inside:avoid}}
    }}
  </style>
  <script src="/static/theme-init.js"></script>
</head>
<body data-no-i18n>
  <div class="toolbar">
    <a href="/carbon/?date_from={d_from.isoformat()}&date_to={d_to.isoformat()}">← Back to Carbon Dashboard</a>
    <button class="print-btn" onclick="window.print()">🖨 Print / Save PDF</button>
  </div>
  <div class="sheet">
    <div class="rpt-head">
      <div style="display:flex;align-items:center;gap:14px">
        <img src="/static/Logo.png" alt="">
        <div>
          <div class="org-name">Habiba Organic Farm</div>
          <div class="org-meta">Commercial registry: 126278 &nbsp;|&nbsp; Tax ID: 560042604</div>
        </div>
      </div>
      <div>
        <div class="rpt-title">Farm Sustainability Report</div>
        <div class="rpt-meta">Period: {period_label}</div>
        <div class="rpt-meta">Generated: {generated} · AZed ERP Carbon Module</div>
      </div>
    </div>

    <h2>Summary</h2>
    <div class="kpis">
      <div class="kpi"><div class="lab">Total emissions</div><div class="val">{_fmt_kg(grand_total)}</div><div class="sub">kg CO₂e · {delta_text}</div></div>
      <div class="kpi"><div class="lab">Daily average</div><div class="val">{_fmt_kg(daily_average)}</div><div class="sub">kg CO₂e / day over {days_in_range} days</div></div>
      <div class="kpi"><div class="lab">Logged events</div><div class="val">{entry_count}</div><div class="sub">emission records in period</div></div>
      <div class="kpi"><div class="lab">Farm produce (intake)</div><div class="val">{_fmt_kg(farm_intake_kg)}</div><div class="sub">kg received from farms (incl. piece products via avg weight)</div></div>
      <div class="kpi"><div class="lab">Processed output</div><div class="val">{_fmt_kg(production_output_kg)}</div><div class="sub">kg of completed batch output (subset of intake)</div></div>
    </div>

    <h2>Emission Intensity</h2>
    <div class="intensity-line">{intensity_html}</div>
    <div class="note">Emission intensity divides total logged emissions by the farm's produce volume for the
    same period, measured as farm intake — every kilogram recorded as delivered from the farms, whether sold
    fresh or processed afterwards. Products measured by mass count directly (grams converted to kg); products
    sold by piece, bunch, box or pack count via their configured average weight per piece.
    Intake is used rather than sales or processing output because it is the upstream total: it covers fresh
    vegetable sales and avoids double-counting produce that also passes through processing. A falling
    intensity means the farm produces more per unit of emissions.</div>

    <h2>Emissions by GHG Protocol Scope</h2>
    <table>
      <thead><tr><th>Scope</th><th>Definition</th><th class="num">kg CO₂e</th><th class="num">Share</th></tr></thead>
      <tbody>{scope_rows_html}</tbody>
    </table>

    <h2>Emissions by Category</h2>
    <table>
      <thead><tr><th>Category</th><th class="num">kg CO₂e</th><th class="num">Share</th></tr></thead>
      <tbody>{cat_rows_html}</tbody>
    </table>

    <h2>Monthly Trend</h2>
    <table>
      <thead><tr><th>Month</th><th class="num">kg CO₂e</th><th class="num">Events</th></tr></thead>
      <tbody>{trend_html}</tbody>
    </table>

    <h2>Reduction Targets</h2>
    <table>
      <thead><tr><th>Target</th><th>Period</th><th class="num">Target kg CO₂e</th><th class="num">Actual (selected range)</th><th class="num">Used</th><th>Status</th></tr></thead>
      <tbody>{targets_html}</tbody>
    </table>

    <h2>Methodology</h2>
    <div class="note">
      Emissions are calculated as <strong>activity quantity × emission factor</strong> for each logged event,
      following the operational approach of the <strong>GHG Protocol Corporate Standard</strong> (Scope 1: direct
      fuel combustion; Scope 2: purchased electricity; Scope 3: value-chain sources such as waste disposal).
      Default factors are indicative values from public datasets (DEFRA GHG Conversion Factors; IFI Harmonised
      Grid Emission Factors for the Egypt national grid) and are reviewed and editable by the administrator.
      Each event is linked to an operational record in the ERP (delivery, production batch, expense, or spoilage
      entry), providing an auditable trail from reported totals back to source transactions.
    </div>
    <table>
      <thead><tr><th>Scope</th><th>Factor</th><th class="num">Value</th><th>Unit</th><th>Source</th><th class="num">Year</th><th>Region</th></tr></thead>
      <tbody>{factor_rows_html}</tbody>
    </table>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


# ── API: Backfill auto-logs from history ────────────────────────────────────

@router.post("/api/backfill-auto-logs", dependencies=[Depends(require_permission("action_carbon_factors"))])
async def backfill_auto_logs(
    dry_run: bool = True,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """Create carbon logs for historical records that predate their factor
    mapping: expenses whose category now has a carbon_factor_key, and
    spoilage records (organic waste).

    Only creates logs that are MISSING — records that already have a carbon
    log are never touched, and existing logs are never recalculated, so the
    reported history stays stable. Run with dry_run=true first to see counts.
    """
    from app.models.expense import Expense, ExpenseCategory
    from app.models.spoilage import SpoilageRecord
    from app.models.product import Product
    from app.models.farm import FarmDelivery
    from app.services.expense_service import _create_carbon_log_for_expense
    from app.services.farm_intake_service import create_carbon_log_for_delivery
    from app.routers.production import _create_carbon_log_for_spoilage

    # ── Expenses missing a carbon log ──
    logged_expense_ids = select(CarbonLog.ref_id).where(CarbonLog.ref_type == "expense")
    exp_q = await db.execute(
        select(Expense, ExpenseCategory)
        .join(ExpenseCategory, Expense.category_id == ExpenseCategory.id)
        .where(
            ExpenseCategory.carbon_factor_key.is_not(None),
            Expense.consumption.is_not(None),
            Expense.consumption > 0,
            Expense.id.not_in(logged_expense_ids),
        )
        .order_by(Expense.id)
    )
    expense_rows = exp_q.all()
    for expense, cat in expense_rows:
        await _create_carbon_log_for_expense(db, expense, cat, float(expense.consumption), current_user)

    # ── Spoilage records missing a carbon log ──
    logged_spoilage_ids = select(CarbonLog.ref_id).where(CarbonLog.ref_type == "spoilage")
    sp_q = await db.execute(
        select(SpoilageRecord, Product)
        .join(Product, SpoilageRecord.product_id == Product.id)
        .where(SpoilageRecord.id.not_in(logged_spoilage_ids))
        .order_by(SpoilageRecord.id)
    )
    spoilage_rows = sp_q.all()
    for spoilage_rec, product in spoilage_rows:
        await _create_carbon_log_for_spoilage(db, spoilage_rec, product, current_user)

    # ── Farm deliveries with a recorded distance but no transport log ──
    logged_delivery_ids = select(CarbonLog.ref_id).where(CarbonLog.ref_type == "farm_delivery")
    dl_q = await db.execute(
        select(FarmDelivery)
        .where(
            FarmDelivery.distance_km.is_not(None),
            FarmDelivery.distance_km > 0,
            FarmDelivery.id.not_in(logged_delivery_ids),
        )
        .order_by(FarmDelivery.id)
    )
    delivery_rows = dl_q.scalars().all()
    for delivery in delivery_rows:
        await create_carbon_log_for_delivery(db, delivery, current_user.id)

    # Count what the helpers actually queued (they skip silently when the
    # factor is missing/inactive or the unit isn't mass-based).
    created = [obj for obj in db.new if isinstance(obj, CarbonLog)]
    expenses_logged = sum(1 for c in created if c.ref_type == "expense")
    spoilage_logged = sum(1 for c in created if c.ref_type == "spoilage")
    deliveries_logged = sum(1 for c in created if c.ref_type == "farm_delivery")

    if dry_run:
        await db.rollback()
    else:
        record(db, "Carbon", "backfill_auto_logs",
               f"Backfilled carbon logs — {expenses_logged} from expenses, "
               f"{spoilage_logged} from spoilage, {deliveries_logged} from deliveries",
               user=current_user)
        await db.commit()

    return {
        "dry_run": dry_run,
        "expenses_scanned": len(expense_rows),
        "expenses_logged": expenses_logged,
        "spoilage_scanned": len(spoilage_rows),
        "spoilage_logged": spoilage_logged,
        "deliveries_scanned": len(delivery_rows),
        "deliveries_logged": deliveries_logged,
        "skipped": (len(expense_rows) - expenses_logged)
                 + (len(spoilage_rows) - spoilage_logged)
                 + (len(delivery_rows) - deliveries_logged),
    }


# ── API: Summary ──────────────────────────────────────────────────────────────

@router.get("/api/summary")
async def api_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_async_session),
):
    d_from, d_to = _date_range_defaults(date_from, date_to)
    rows = await db.execute(
        select(
            CarbonEmissionFactor.source_type,
            func.sum(CarbonLog.kg_co2e).label("total_kg_co2e"),
            func.count(CarbonLog.id).label("entry_count"),
        )
        .join(CarbonEmissionFactor, CarbonLog.factor_id == CarbonEmissionFactor.id)
        .where(CarbonLog.log_date.between(d_from, d_to))
        .group_by(CarbonEmissionFactor.source_type)
    )
    by_type = [
        {"source_type": r.source_type, "total_kg_co2e": _as_float(r.total_kg_co2e), "entry_count": r.entry_count}
        for r in rows
    ]
    grand = sum(r["total_kg_co2e"] for r in by_type)
    return {
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "grand_total_kg_co2e": grand,
        "by_source_type": by_type,
    }


# ── API: Logs ─────────────────────────────────────────────────────────────────

@router.get("/api/logs")
async def api_list_logs(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_async_session),
):
    d_from, d_to = _date_range_defaults(date_from, date_to)
    q = await db.execute(
        select(CarbonLog)
        .options(selectinload(CarbonLog.factor), selectinload(CarbonLog.farm))
        .where(CarbonLog.log_date.between(d_from, d_to))
        .order_by(CarbonLog.log_date.desc(), CarbonLog.id.desc())
        .limit(limit)
    )
    logs = q.scalars().all()
    return [
        {
            "id": lg.id,
            "log_date": lg.log_date.isoformat(),
            "factor": lg.factor.label,
            "source_type": lg.factor.source_type,
            "unit": lg.factor.unit,
            "quantity": _as_float(lg.quantity),
            "kg_co2e": _as_float(lg.kg_co2e),
            "farm": lg.farm.name if lg.farm else None,
            "ref_type": lg.ref_type,
            "ref_id": lg.ref_id,
            "notes": lg.notes,
        }
        for lg in logs
    ]


@router.post("/api/logs", status_code=201, dependencies=[Depends(require_permission("action_carbon_log"))])
async def api_create_log(
    payload: LogCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    factor = await db.get(CarbonEmissionFactor, payload.factor_id)
    if not factor:
        raise HTTPException(404, "Emission factor not found")
    if not factor.is_active:
        raise HTTPException(400, "Emission factor is inactive")
    if payload.quantity <= 0:
        raise HTTPException(400, "Quantity must be greater than zero")

    try:
        log_date = date.fromisoformat(payload.log_date)
    except ValueError:
        raise HTTPException(400, "Invalid log_date. Use YYYY-MM-DD") from None

    kg = float(factor.factor_kg_co2e_per_unit) * payload.quantity
    lg = CarbonLog(
        factor_id=payload.factor_id,
        farm_id=payload.farm_id,
        user_id=current_user.id,
        log_date=log_date,
        quantity=payload.quantity,
        kg_co2e=round(kg, 4),
        ref_type=payload.ref_type,
        ref_id=payload.ref_id,
        notes=payload.notes,
    )
    db.add(lg)
    await db.commit()
    await db.refresh(lg)
    record(db, "Carbon", "create_log", f"{payload.quantity} × {factor.label} = {kg:.4f} kg CO₂e", user=current_user, ref_type="carbon_log", ref_id=lg.id)
    await db.commit()
    return {"id": lg.id, "kg_co2e": _as_float(lg.kg_co2e)}


@router.delete("/api/logs/{log_id}", status_code=204, dependencies=[Depends(require_permission("action_carbon_log"))])
async def api_delete_log(
    log_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    lg = await db.get(CarbonLog, log_id)
    if not lg:
        raise HTTPException(404, "Log entry not found")
    await db.delete(lg)
    record(db, "Carbon", "delete_log", "Deleted carbon log entry", user=current_user, ref_type="carbon_log", ref_id=log_id)
    await db.commit()


# ── API: Factors ──────────────────────────────────────────────────────────────

@router.get("/api/factors")
async def api_list_factors(db: AsyncSession = Depends(get_async_session)):
    q = await db.execute(
        select(CarbonEmissionFactor)
        .where(CarbonEmissionFactor.is_active == True)
        .order_by(CarbonEmissionFactor.source_type, CarbonEmissionFactor.label)
    )
    return [
        {
            "id": f.id,
            "source_type": f.source_type,
            "source_key": f.source_key,
            "label": f.label,
            "factor_kg_co2e_per_unit": float(f.factor_kg_co2e_per_unit),
            "unit": f.unit,
            "description": f.description,
            "scope": f.scope,
            "methodology_source": f.methodology_source,
            "source_year": f.source_year,
            "region": f.region,
        }
        for f in q.scalars().all()
    ]


@router.post("/api/factors", status_code=201, dependencies=[Depends(require_permission("action_carbon_factors"))])
async def api_create_factor(
    payload: FactorCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    existing = await db.execute(
        select(CarbonEmissionFactor).where(CarbonEmissionFactor.source_key == payload.source_key)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"Factor with source_key '{payload.source_key}' already exists")
    f = CarbonEmissionFactor(**_model_dump(payload))
    db.add(f)
    await db.commit()
    await db.refresh(f)
    record(db, "Carbon", "create_factor", f"Created emission factor {f.source_key}", user=current_user, ref_type="carbon_factor", ref_id=f.id)
    await db.commit()
    return {"id": f.id}


@router.patch("/api/factors/{factor_id}", dependencies=[Depends(require_permission("action_carbon_factors"))])
async def api_update_factor(
    factor_id: int,
    payload: FactorUpdate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    f = await db.get(CarbonEmissionFactor, factor_id)
    if not f:
        raise HTTPException(404, "Factor not found")
    old_value = float(f.factor_kg_co2e_per_unit)
    for field, val in _model_dump(payload, exclude_none=True).items():
        setattr(f, field, val)

    # If the coefficient itself changed, recompute every log that references
    # this factor (kg_co2e is stored as quantity × factor at save time, so
    # dashboards/reports would otherwise keep showing totals based on the old
    # value forever). Quantities are untouched — only the derived kg CO₂e.
    recalculated = 0
    new_value = float(f.factor_kg_co2e_per_unit)
    if payload.factor_kg_co2e_per_unit is not None and new_value != old_value:
        result = await db.execute(
            text(
                "UPDATE carbon_logs "
                "   SET kg_co2e = ROUND(quantity * CAST(:new_factor AS NUMERIC), 4) "
                " WHERE factor_id = :fid"
            ),
            {"new_factor": new_value, "fid": factor_id},
        )
        recalculated = result.rowcount or 0

    detail = f"Updated emission factor {f.source_key}"
    if recalculated:
        detail += f" ({old_value} → {new_value}; recalculated {recalculated} logs)"
    record(db, "Carbon", "update_factor", detail, user=current_user, ref_type="carbon_factor", ref_id=f.id)
    await db.commit()
    return {"ok": True, "recalculated_logs": recalculated}


# ── API: Targets ──────────────────────────────────────────────────────────────

@router.get("/api/targets")
async def api_list_targets(db: AsyncSession = Depends(get_async_session)):
    q = await db.execute(select(CarbonTarget).order_by(CarbonTarget.period_start.desc()))
    return [
        {
            "id": t.id,
            "label": t.label,
            "period_start": t.period_start.isoformat(),
            "period_end": t.period_end.isoformat(),
            "target_kg_co2e": _as_float(t.target_kg_co2e),
            "notes": t.notes,
        }
        for t in q.scalars().all()
    ]


@router.post("/api/targets", status_code=201, dependencies=[Depends(require_permission("action_carbon_factors"))])
async def api_create_target(
    payload: TargetCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    try:
        period_start = date.fromisoformat(payload.period_start)
        period_end = date.fromisoformat(payload.period_end)
    except ValueError:
        raise HTTPException(400, "Invalid target period. Use YYYY-MM-DD") from None

    if period_start > period_end:
        raise HTTPException(400, "Target start date must be before end date")
    if payload.target_kg_co2e <= 0:
        raise HTTPException(400, "Target kg CO₂e must be greater than zero")

    t = CarbonTarget(
        label=payload.label,
        period_start=period_start,
        period_end=period_end,
        target_kg_co2e=payload.target_kg_co2e,
        notes=payload.notes,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    record(db, "Carbon", "create_target", f"Created carbon target {t.label}", user=current_user, ref_type="carbon_target", ref_id=t.id)
    await db.commit()
    return {"id": t.id}


@router.delete("/api/targets/{target_id}", status_code=204, dependencies=[Depends(require_permission("action_carbon_factors"))])
async def api_delete_target(
    target_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    t = await db.get(CarbonTarget, target_id)
    if not t:
        raise HTTPException(404, "Target not found")
    await db.delete(t)
    record(db, "Carbon", "delete_target", "Deleted carbon target", user=current_user, ref_type="carbon_target", ref_id=target_id)
    await db.commit()


# ── API: Auto-log from Farm Delivery ─────────────────────────────────────────

@router.post("/api/auto-log/farm-delivery/{delivery_id}", status_code=201, dependencies=[Depends(require_permission("action_carbon_log"))])
async def api_auto_log_farm_delivery(
    delivery_id: int,
    distance_km: float,
    vehicle_type: str = "truck",    # truck | van
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """
    Set transport details on a farm delivery and (re)generate its CarbonLog.
    Persists distance/vehicle onto the delivery itself and rebuilds the log,
    so calling this twice updates the emissions instead of duplicating them.
    The Farm Intake form is the primary path; this endpoint covers deliveries
    recorded before transport tracking existed.
    """
    from app.services.farm_intake_service import (
        VEHICLE_FACTOR_KEYS,
        resync_carbon_log_for_delivery,
    )

    if distance_km <= 0:
        raise HTTPException(400, "Distance must be greater than zero")
    vt = (vehicle_type or "").strip().lower()
    if vt not in VEHICLE_FACTOR_KEYS:
        raise HTTPException(400, f"Unknown vehicle type '{vehicle_type}' — use one of: {', '.join(VEHICLE_FACTOR_KEYS)}")

    delivery = await db.get(FarmDelivery, delivery_id)
    if not delivery:
        raise HTTPException(404, "Farm delivery not found")

    delivery.distance_km = distance_km
    delivery.vehicle_type = vt
    await resync_carbon_log_for_delivery(db, delivery, current_user.id)

    log_q = await db.execute(
        select(CarbonLog).where(
            CarbonLog.ref_type == "farm_delivery",
            CarbonLog.ref_id == delivery_id,
        )
    )
    lg = log_q.scalars().first()
    if not lg:
        raise HTTPException(404, f"No active emission factor found for vehicle type '{vt}'")

    record(db, "Carbon", "auto_log_farm_delivery",
           f"Logged transport for delivery {delivery.delivery_number}: "
           f"{distance_km:g} km by {vt} = {float(lg.kg_co2e):.4f} kg CO₂e",
           user=current_user, ref_type="farm_delivery", ref_id=delivery_id)
    await db.commit()
    await db.refresh(lg)
    return {"id": lg.id, "kg_co2e": _as_float(lg.kg_co2e), "delivery_number": delivery.delivery_number}