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

from datetime import date, datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_async_session
from app.core.log import record
from app.core.navigation import render_app_header
from app.core.permissions import get_current_user, require_permission
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
    log_date:  str          # ISO date string "YYYY-MM-DD"
    quantity:  float
    farm_id:   Optional[int] = None
    ref_type:  Optional[str] = None
    ref_id:    Optional[int] = None
    notes:     Optional[str] = None


class FactorCreate(BaseModel):
    source_type:             str
    source_key:              str
    label:                   str
    factor_kg_co2e_per_unit: float
    unit:                    str
    description:             Optional[str] = None


class FactorUpdate(BaseModel):
    label:                   Optional[str]   = None
    factor_kg_co2e_per_unit: Optional[float] = None
    unit:                    Optional[str]   = None
    description:             Optional[str]   = None
    is_active:               Optional[bool]  = None


class TargetCreate(BaseModel):
    label:          str
    period_start:   str   # "YYYY-MM-DD"
    period_end:     str
    target_kg_co2e: float
    notes:          Optional[str] = None


# ── Helper ────────────────────────────────────────────────────────────────────

def _date_range_defaults(date_from: Optional[str], date_to: Optional[str]):
    """Return (date, date) defaulting to current calendar month."""
    today = date.today()
    d_from = date.fromisoformat(date_from) if date_from else today.replace(day=1)
    d_to   = date.fromisoformat(date_to)   if date_to   else today
    return d_from, d_to


# ── Page: Dashboard ───────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def carbon_dashboard(
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    db:        AsyncSession  = Depends(get_async_session),
    current_user: User       = Depends(get_current_user),
):
    d_from, d_to = _date_range_defaults(date_from, date_to)

    # Totals by source_type
    rows = await db.execute(
        select(
            CarbonEmissionFactor.source_type,
            func.sum(CarbonLog.kg_co2e).label("total"),
        )
        .join(CarbonEmissionFactor, CarbonLog.factor_id == CarbonEmissionFactor.id)
        .where(CarbonLog.log_date.between(d_from, d_to))
        .group_by(CarbonEmissionFactor.source_type)
    )
    by_type = {r.source_type: float(r.total or 0) for r in rows}

    grand_total = sum(by_type.values())

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
                CarbonTarget.period_end   >= d_from,
            )
        )
        .order_by(CarbonTarget.period_start)
    )
    targets = targets_q.scalars().all()

    nav_html = await render_app_header(current_user, active_permission="page_carbon", db=db)

    # Build log rows HTML
    log_rows = ""
    for lg in logs:
        farm_name  = lg.farm.name if lg.farm else "—"
        user_name  = lg.user.full_name if lg.user else "—"
        source_tag = f'<span class="badge badge-{lg.factor.source_type}">{lg.factor.source_type}</span>'
        log_rows += f"""
        <tr>
          <td>{lg.log_date}</td>
          <td>{source_tag}</td>
          <td>{lg.factor.label}</td>
          <td>{farm_name}</td>
          <td class="num">{float(lg.quantity):,.2f} {lg.factor.unit}</td>
          <td class="num co2"><strong>{float(lg.kg_co2e):,.3f}</strong> kg</td>
          <td>{lg.ref_type or "manual"}</td>
          <td>
            <button class="btn-icon btn-danger"
                    hx-delete="/carbon/api/logs/{lg.id}"
                    hx-confirm="Delete this log entry?"
                    hx-target="closest tr"
                    hx-swap="outerHTML">✕</button>
          </td>
        </tr>"""

    # Build target progress HTML
    target_html = ""
    for t in targets:
        pct = min(100, round(grand_total / float(t.target_kg_co2e) * 100, 1)) if t.target_kg_co2e else 0
        bar_class = "progress-ok" if pct < 80 else ("progress-warn" if pct < 100 else "progress-over")
        target_html += f"""
        <div class="target-card">
          <div class="target-header">
            <strong>{t.label}</strong>
            <span class="target-dates">{t.period_start} → {t.period_end}</span>
          </div>
          <div class="target-progress">
            <div class="progress-bar {bar_class}" style="width:{pct}%"></div>
          </div>
          <div class="target-stats">
            <span>{grand_total:,.1f} kg CO₂e used</span>
            <span>Target: {float(t.target_kg_co2e):,.0f} kg</span>
            <span>{pct}%</span>
          </div>
        </div>"""

    transport_val  = by_type.get("transport",  0)
    energy_val     = by_type.get("energy",     0)
    waste_val      = by_type.get("waste",      0)
    production_val = by_type.get("production", 0)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Carbon Footprint | AZed ERP</title>
  <link rel="stylesheet" href="/static/dashboard.css">
  <script src="https://unpkg.com/htmx.org@1.9.12" defer></script>
  <style>
    .carbon-grid {{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px}}
    .carbon-card {{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:18px 20px}}
    .carbon-card .label {{font-size:.78rem;font-weight:700;text-transform:uppercase;color:var(--text-muted);margin-bottom:6px}}
    .carbon-card .value {{font-size:1.85rem;font-weight:900;color:var(--text)}}
    .carbon-card .unit  {{font-size:.82rem;color:var(--text-muted);margin-top:2px}}
    .carbon-card.total  {{border-top:4px solid var(--teal)}}
    .carbon-card.transport {{border-top:4px solid var(--amber)}}
    .carbon-card.energy    {{border-top:4px solid var(--red)}}
    .carbon-card.waste     {{border-top:4px solid var(--green)}}
    .carbon-card.prod      {{border-top:4px solid #a78bfa}}
    .badge {{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:700;text-transform:capitalize}}
    .badge-transport{{background:color-mix(in srgb,var(--amber) 18%,transparent);color:var(--amber)}}
    .badge-energy   {{background:color-mix(in srgb,var(--red)   18%,transparent);color:var(--red)}}
    .badge-waste    {{background:color-mix(in srgb,var(--green)  18%,transparent);color:var(--green)}}
    .badge-production{{background:color-mix(in srgb,#a78bfa     18%,transparent);color:#a78bfa}}
    .num{{text-align:right}}
    .co2{{color:var(--teal)}}
    .target-card{{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:12px}}
    .target-header{{display:flex;justify-content:space-between;margin-bottom:10px;font-size:.9rem}}
    .target-dates{{color:var(--text-muted);font-size:.82rem}}
    .progress-bar-track{{height:10px;background:var(--surface-strong);border-radius:999px;overflow:hidden;margin-bottom:8px}}
    .progress-bar{{height:100%;border-radius:999px;transition:width .4s ease}}
    .progress-ok  {{background:var(--teal)}}
    .progress-warn{{background:var(--amber)}}
    .progress-over{{background:var(--red)}}
    .target-stats{{display:flex;gap:16px;font-size:.8rem;color:var(--text-muted)}}
    .filter-bar{{display:flex;gap:10px;align-items:flex-end;margin-bottom:20px;flex-wrap:wrap}}
    .filter-bar label{{font-size:.78rem;font-weight:700;text-transform:uppercase;color:var(--text-muted)}}
    .filter-bar input{{min-height:38px;border:1px solid var(--line-strong);border-radius:6px;background:var(--surface);color:var(--text);padding:0 10px;font:inherit}}
    .section-title{{font-size:1.05rem;font-weight:800;margin:28px 0 12px}}
    .btn-icon{{border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--text-muted);width:28px;height:28px;cursor:pointer;font-size:.85rem}}
    .btn-danger:hover{{border-color:var(--red);color:var(--red)}}
    @media(max-width:900px){{.carbon-grid{{grid-template-columns:repeat(2,1fr)}}}}
    @media(max-width:560px){{.carbon-grid{{grid-template-columns:1fr}}}}
  </style>
</head>
<body>
{nav_html}
<main class="main-content">
  <div class="page-header">
    <div>
      <h1 class="page-title">Carbon Footprint</h1>
      <p class="page-subtitle">Track CO₂-equivalent emissions from farm operations</p>
    </div>
    <div style="display:flex;gap:8px">
      <a href="/carbon/log" class="btn btn-primary">+ Log Emission</a>
      <a href="/carbon/factors" class="btn btn-secondary">Manage Factors</a>
    </div>
  </div>

  <!-- Date filter -->
  <form method="get" class="filter-bar">
    <div><label>From</label><br>
      <input type="date" name="date_from" value="{d_from.isoformat()}"></div>
    <div><label>To</label><br>
      <input type="date" name="date_to" value="{d_to.isoformat()}"></div>
    <button type="submit" class="btn btn-secondary" style="align-self:flex-end">Apply</button>
  </form>

  <!-- Summary cards -->
  <div class="carbon-grid">
    <div class="carbon-card total">
      <div class="label">Total Emissions</div>
      <div class="value">{grand_total:,.1f}</div>
      <div class="unit">kg CO₂e</div>
    </div>
    <div class="carbon-card transport">
      <div class="label">Transport</div>
      <div class="value">{transport_val:,.1f}</div>
      <div class="unit">kg CO₂e</div>
    </div>
    <div class="carbon-card energy">
      <div class="label">Energy</div>
      <div class="value">{energy_val:,.1f}</div>
      <div class="unit">kg CO₂e</div>
    </div>
    <div class="carbon-card waste">
      <div class="label">Waste / Spoilage</div>
      <div class="value">{waste_val:,.1f}</div>
      <div class="unit">kg CO₂e</div>
    </div>
  </div>

  <!-- Targets -->
  {'<div class="section-title">Reduction Targets</div>' + target_html if target_html else ""}
  <div style="margin-bottom:6px">
    <a href="#" onclick="document.getElementById('targetForm').classList.toggle('hidden');return false"
       style="font-size:.85rem;color:var(--teal)">+ Add target</a>
  </div>
  <div id="targetForm" class="hidden" style="background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:18px;margin-bottom:20px">
    <form hx-post="/carbon/api/targets" hx-swap="none" onsubmit="location.reload()" style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end">
      <div><label style="font-size:.78rem;font-weight:700;display:block;margin-bottom:4px">Label</label>
        <input name="label" required style="min-height:36px;border:1px solid var(--line-strong);border-radius:6px;background:var(--bg);color:var(--text);padding:0 10px;font:inherit"></div>
      <div><label style="font-size:.78rem;font-weight:700;display:block;margin-bottom:4px">Start</label>
        <input type="date" name="period_start" required style="min-height:36px;border:1px solid var(--line-strong);border-radius:6px;background:var(--bg);color:var(--text);padding:0 10px;font:inherit"></div>
      <div><label style="font-size:.78rem;font-weight:700;display:block;margin-bottom:4px">End</label>
        <input type="date" name="period_end" required style="min-height:36px;border:1px solid var(--line-strong);border-radius:6px;background:var(--bg);color:var(--text);padding:0 10px;font:inherit"></div>
      <div><label style="font-size:.78rem;font-weight:700;display:block;margin-bottom:4px">Target kg CO₂e</label>
        <input type="number" name="target_kg_co2e" step="0.01" required style="width:140px;min-height:36px;border:1px solid var(--line-strong);border-radius:6px;background:var(--bg);color:var(--text);padding:0 10px;font:inherit"></div>
      <button type="submit" class="btn btn-primary">Save Target</button>
    </form>
  </div>

  <!-- Log table -->
  <div class="section-title">Emission Logs</div>
  <div class="table-wrap">
    <table class="data-table">
      <thead>
        <tr>
          <th>Date</th><th>Category</th><th>Source</th><th>Farm</th>
          <th class="num">Quantity</th><th class="num">kg CO₂e</th>
          <th>Ref</th><th></th>
        </tr>
      </thead>
      <tbody id="logBody">
        {log_rows if log_rows else '<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:32px">No emissions logged for this period.</td></tr>'}
      </tbody>
    </table>
  </div>
</main>
<script>
  // Init theme from localStorage
  try {{
    var t = localStorage.getItem("colorMode") === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", t);
  }} catch(_) {{}}
  document.querySelectorAll(".hidden"){{}}
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

    nav_html = await render_app_header(current_user, active_permission="page_carbon", db=db)

    factor_options = ""
    current_group  = ""
    for f in factors:
        if f.source_type != current_group:
            if current_group:
                factor_options += "</optgroup>"
            factor_options += f'<optgroup label="{f.source_type.title()}">'
            current_group = f.source_type
        factor_options += f'<option value="{f.id}" data-unit="{f.unit}" data-factor="{float(f.factor_kg_co2e_per_unit)}">{f.label} ({f.unit})</option>'
    if current_group:
        factor_options += "</optgroup>"

    farm_options = "".join(f'<option value="{fm.id}">{fm.name}</option>' for fm in farms)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Log Emission | Carbon | AZed ERP</title>
  <link rel="stylesheet" href="/static/dashboard.css">
  <style>
    .form-card{{max-width:540px;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:28px 28px 24px}}
    .field{{margin-bottom:18px}}
    .field label{{display:block;margin-bottom:6px;font-size:.78rem;font-weight:700;text-transform:uppercase;color:var(--text-muted)}}
    .field input,.field select,.field textarea{{width:100%;min-height:42px;border:1px solid var(--line-strong);border-radius:7px;background:var(--bg);color:var(--text);padding:0 12px;font:inherit}}
    .field textarea{{padding:10px 12px;min-height:80px;resize:vertical}}
    .co2-preview{{padding:12px 16px;background:color-mix(in srgb,var(--teal) 12%,transparent);border:1px solid color-mix(in srgb,var(--teal) 30%,transparent);border-radius:7px;font-weight:700;color:var(--teal);margin-bottom:18px;font-size:1.05rem}}
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
  try {{ var t = localStorage.getItem("colorMode")==="light"?"light":"dark"; document.documentElement.setAttribute("data-theme",t); }} catch(_){{}}

  function updatePreview() {{
    var sel = document.getElementById("factorSel");
    var opt = sel.options[sel.selectedIndex];
    var unit   = opt.dataset.unit   || "unit";
    var factor = parseFloat(opt.dataset.factor) || 0;
    var qty    = parseFloat(document.getElementById("qtyInp").value) || 0;
    document.getElementById("unitLabel").textContent = unit;
    var co2 = (qty * factor).toFixed(3);
    document.getElementById("co2Preview").textContent = "CO₂e: " + co2 + " kg";
  }}
  updatePreview();

  document.getElementById("logForm").addEventListener("submit", async function(e) {{
    e.preventDefault();
    var fd = new FormData(this);
    var body = Object.fromEntries(fd.entries());
    body.quantity = parseFloat(body.quantity);
    if (body.farm_id === "") delete body.farm_id;
    var r = await fetch("/carbon/api/logs", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(body)
    }});
    if (r.ok) {{ window.location.href = "/carbon/"; }}
    else {{
      var err = await r.json();
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

    nav_html = await render_app_header(current_user, active_permission="page_carbon", db=db)

    rows = ""
    for f in factors:
        active_badge = '<span style="color:var(--teal)">Active</span>' if f.is_active else '<span style="color:var(--text-muted)">Inactive</span>'
        rows += f"""
        <tr>
          <td><span class="badge badge-{f.source_type}">{f.source_type}</span></td>
          <td>{f.source_key}</td>
          <td>{f.label}</td>
          <td class="num">{float(f.factor_kg_co2e_per_unit)}</td>
          <td>{f.unit}</td>
          <td>{active_badge}</td>
        </tr>"""

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Emission Factors | Carbon | AZed ERP</title>
  <link rel="stylesheet" href="/static/dashboard.css">
  <style>
    .badge {{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:700;text-transform:capitalize}}
    .badge-transport{{background:color-mix(in srgb,var(--amber) 18%,transparent);color:var(--amber)}}
    .badge-energy   {{background:color-mix(in srgb,var(--red)   18%,transparent);color:var(--red)}}
    .badge-waste    {{background:color-mix(in srgb,var(--green)  18%,transparent);color:var(--green)}}
    .badge-production{{background:color-mix(in srgb,#a78bfa     18%,transparent);color:#a78bfa}}
    .num{{text-align:right}}
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
    <a href="/carbon/" class="btn btn-secondary">← Back</a>
  </div>
  <div class="table-wrap">
    <table class="data-table">
      <thead>
        <tr><th>Category</th><th>Key</th><th>Label</th><th class="num">Factor</th><th>Unit</th><th>Status</th></tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</main>
<script>
  try {{ var t = localStorage.getItem("colorMode")==="light"?"light":"dark"; document.documentElement.setAttribute("data-theme",t); }} catch(_){{}}
</script>
</body>
</html>"""
    return HTMLResponse(html)


# ── API: Summary ──────────────────────────────────────────────────────────────

@router.get("/api/summary")
async def api_summary(
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
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
        {"source_type": r.source_type, "total_kg_co2e": float(r.total_kg_co2e or 0), "entry_count": r.entry_count}
        for r in rows
    ]
    grand = sum(r["total_kg_co2e"] for r in by_type)
    return {"date_from": d_from.isoformat(), "date_to": d_to.isoformat(),
            "grand_total_kg_co2e": grand, "by_source_type": by_type}


# ── API: Logs ─────────────────────────────────────────────────────────────────

@router.get("/api/logs")
async def api_list_logs(
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
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
            "id":          lg.id,
            "log_date":    lg.log_date.isoformat(),
            "factor":      lg.factor.label,
            "source_type": lg.factor.source_type,
            "unit":        lg.factor.unit,
            "quantity":    float(lg.quantity),
            "kg_co2e":     float(lg.kg_co2e),
            "farm":        lg.farm.name if lg.farm else None,
            "ref_type":    lg.ref_type,
            "ref_id":      lg.ref_id,
            "notes":       lg.notes,
        }
        for lg in logs
    ]


@router.post("/api/logs", status_code=201)
async def api_create_log(
    payload: LogCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    factor = await db.get(CarbonEmissionFactor, payload.factor_id)
    if not factor:
        raise HTTPException(404, "Emission factor not found")

    kg = float(factor.factor_kg_co2e_per_unit) * payload.quantity
    lg = CarbonLog(
        factor_id=payload.factor_id,
        farm_id=payload.farm_id,
        user_id=current_user.id,
        log_date=date.fromisoformat(payload.log_date),
        quantity=payload.quantity,
        kg_co2e=round(kg, 4),
        ref_type=payload.ref_type,
        ref_id=payload.ref_id,
        notes=payload.notes,
    )
    db.add(lg)
    await db.commit()
    await db.refresh(lg)
    await record(db, current_user.id, "carbon_log", "create", lg.id, f"{payload.quantity} × {factor.label} = {kg:.4f} kg CO₂e")
    return {"id": lg.id, "kg_co2e": float(lg.kg_co2e)}


@router.delete("/api/logs/{log_id}", status_code=204)
async def api_delete_log(
    log_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    lg = await db.get(CarbonLog, log_id)
    if not lg:
        raise HTTPException(404, "Log entry not found")
    await db.delete(lg)
    await db.commit()
    await record(db, current_user.id, "carbon_log", "delete", log_id, "Deleted carbon log entry")


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
            "id":          f.id,
            "source_type": f.source_type,
            "source_key":  f.source_key,
            "label":       f.label,
            "factor_kg_co2e_per_unit": float(f.factor_kg_co2e_per_unit),
            "unit":        f.unit,
            "description": f.description,
        }
        for f in q.scalars().all()
    ]


@router.post("/api/factors", status_code=201)
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
    f = CarbonEmissionFactor(**payload.dict())
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return {"id": f.id}


@router.patch("/api/factors/{factor_id}")
async def api_update_factor(
    factor_id: int,
    payload: FactorUpdate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    f = await db.get(CarbonEmissionFactor, factor_id)
    if not f:
        raise HTTPException(404, "Factor not found")
    for field, val in payload.dict(exclude_none=True).items():
        setattr(f, field, val)
    await db.commit()
    return {"ok": True}


# ── API: Targets ──────────────────────────────────────────────────────────────

@router.get("/api/targets")
async def api_list_targets(db: AsyncSession = Depends(get_async_session)):
    q = await db.execute(select(CarbonTarget).order_by(CarbonTarget.period_start.desc()))
    return [
        {
            "id":             t.id,
            "label":          t.label,
            "period_start":   t.period_start.isoformat(),
            "period_end":     t.period_end.isoformat(),
            "target_kg_co2e": float(t.target_kg_co2e),
            "notes":          t.notes,
        }
        for t in q.scalars().all()
    ]


@router.post("/api/targets", status_code=201)
async def api_create_target(
    payload: TargetCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    t = CarbonTarget(
        label=payload.label,
        period_start=date.fromisoformat(payload.period_start),
        period_end=date.fromisoformat(payload.period_end),
        target_kg_co2e=payload.target_kg_co2e,
        notes=payload.notes,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"id": t.id}


@router.delete("/api/targets/{target_id}", status_code=204)
async def api_delete_target(
    target_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    t = await db.get(CarbonTarget, target_id)
    if not t:
        raise HTTPException(404, "Target not found")
    await db.delete(t)
    await db.commit()


# ── API: Auto-log from Farm Delivery ─────────────────────────────────────────

@router.post("/api/auto-log/farm-delivery/{delivery_id}", status_code=201)
async def api_auto_log_farm_delivery(
    delivery_id: int,
    distance_km: float,
    vehicle_type: str = "truck",    # truck | van | refrigerated_truck
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """
    Auto-generate a transport CarbonLog from a farm delivery.
    Pass the one-way distance in km and the vehicle type.
    The system uses the correct emission factor and logs the result.
    """
    delivery = await db.get(FarmDelivery, delivery_id)
    if not delivery:
        raise HTTPException(404, "Farm delivery not found")

    source_key = f"{vehicle_type}_km"
    factor_q = await db.execute(
        select(CarbonEmissionFactor).where(CarbonEmissionFactor.source_key == source_key)
    )
    factor = factor_q.scalar_one_or_none()
    if not factor:
        raise HTTPException(404, f"No emission factor found for vehicle type '{vehicle_type}'")

    kg = float(factor.factor_kg_co2e_per_unit) * distance_km
    lg = CarbonLog(
        factor_id=factor.id,
        farm_id=delivery.farm_id,
        user_id=current_user.id,
        log_date=delivery.delivery_date,
        quantity=distance_km,
        kg_co2e=round(kg, 4),
        ref_type="farm_delivery",
        ref_id=delivery_id,
        notes=f"Auto-logged from delivery {delivery.delivery_number}",
    )
    db.add(lg)
    await db.commit()
    await db.refresh(lg)
    return {"id": lg.id, "kg_co2e": float(lg.kg_co2e), "factor_used": factor.label}