"""Budget & targets — a spending budget and a sales target for each month, against what actually happened.

Anyone with page_budget sees the numbers; setting them needs action_budget_manage. The figures come from
budget_service, which counts sales the way the Sales dashboard does.
"""
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.navigation import render_app_header
from app.core.permissions import get_current_user, has_permission, require_permission
from app.core.time_utils import today_local
from app.database import get_async_session
from app.models.user import User
from app.services import budget_service

router = APIRouter(prefix="/budget", tags=["Budget & targets"],
                   dependencies=[Depends(require_permission("page_budget"))])


class YearIn(BaseModel):
    months: list[dict]


@router.get("/api/year")
async def year(year: Optional[int] = None, db: AsyncSession = Depends(get_async_session),
               user: User = Depends(get_current_user)):
    out = await budget_service.year_overview(db, year or today_local().year)
    out["can_manage"] = has_permission(user, "action_budget_manage")
    return out


@router.get("/api/current")
async def current(db: AsyncSession = Depends(get_async_session)):
    return await budget_service.current_month(db)


@router.put("/api/year/{year}", dependencies=[Depends(require_permission("action_budget_manage"))])
async def save(year: int, data: YearIn, db: AsyncSession = Depends(get_async_session),
               user: User = Depends(get_current_user)):
    return await budget_service.save_year(db, year, data.months, user)


@router.get("/", response_class=HTMLResponse)
def page(current_user: User = Depends(get_current_user)):
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Budget &amp; targets — AZed Farm</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&family=Outfit:wght@400;600;800&family=Cairo:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#08090c;--card:#0f1311;--card2:#151a17;--border:rgba(255,255,255,.07);--border2:rgba(255,255,255,.13);
      --green:#7ecb6f;--lime:#84cc16;--blue:#6a9fd4;--warn:#e0a84a;--danger:#e06c75;--text:#e8eae0;--sub:#a3aa98;--muted:#6b7363;
      --sans:'DM Sans','Cairo',sans-serif;--mono:'DM Mono',monospace}
body.light{--bg:#f4f5ef;--card:#ffffff;--card2:#eef0e8;--border:rgba(0,0,0,.08);--border2:rgba(0,0,0,.14);
      --green:#0f8a43;--warn:#b7791f;--danger:#c0392b;--text:#1a1e14;--sub:#4a5040;--muted:#8a9080}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh}
.wrap{max-width:1100px;margin:0 auto;padding:28px 16px 80px}
.head{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap}
.title{font-family:'Outfit',sans-serif;font-size:26px;font-weight:800}
.sub{color:var(--sub);font-size:13px;margin-top:4px;line-height:1.5;max-width:640px}
.year{display:flex;align-items:center;gap:6px}
.year button{background:var(--card);border:1px solid var(--border2);color:var(--text);border-radius:8px;width:32px;height:32px;cursor:pointer;font-size:15px}
.year button:hover{border-color:var(--lime)}
.year span{font-family:var(--mono);font-size:16px;min-width:52px;text-align:center}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin:22px 0}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px 18px}
.card-label{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted)}
.card-big{font-family:var(--mono);font-size:22px;margin:6px 0 2px}
.card-big small{font-size:13px;color:var(--sub)}
.card-note{font-size:12.5px;color:var(--sub);margin-top:8px;line-height:1.45}
.bar{position:relative;height:8px;border-radius:6px;background:var(--card2);margin-top:10px;overflow:visible}
.bar i{position:absolute;inset:0 auto 0 0;border-radius:6px;background:var(--green);max-width:100%}
.bar.warn i{background:var(--warn)}.bar.over i{background:var(--danger)}
.bar b{position:absolute;top:-4px;width:2px;height:16px;background:var(--text);opacity:.55}
.ok{color:var(--green)}.warn{color:var(--warn)}.bad{color:var(--danger)}
.table-card{padding:0;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:760px}
th,td{padding:9px 12px;border-bottom:1px solid var(--border);text-align:end;white-space:nowrap}
th:first-child,td:first-child{text-align:start}
th{color:var(--muted);font-weight:500;font-size:12px}
th.grp{text-align:center;color:var(--sub);border-bottom:none;padding-bottom:0}
td.num{font-family:var(--mono)}
tr.now td{background:color-mix(in srgb,var(--lime) 7%,transparent)}
tr.total td{font-weight:600;border-bottom:none}
td.future{color:var(--muted)}
input.amt{width:120px;background:var(--card2);border:1px solid var(--border2);border-radius:7px;color:var(--text);
          padding:5px 8px;font-family:var(--mono);font-size:13px;text-align:end}
input.amt:focus{outline:none;border-color:var(--lime)}
input.amt.changed{border-color:var(--warn)}
.actions{display:flex;gap:10px;align-items:center;justify-content:flex-end;flex-wrap:wrap;margin-top:14px}
.btn{background:var(--card);border:1px solid var(--border2);color:var(--text);border-radius:9px;padding:8px 14px;font-family:var(--sans);font-size:13px;cursor:pointer}
.btn:hover{border-color:var(--lime)}
.btn.primary{background:var(--green);border-color:var(--green);color:#0a0c08;font-weight:600}
.btn:disabled{opacity:.5;cursor:default}
.status{font-size:12.5px;color:var(--sub)}
.empty{color:var(--muted);font-size:13px;padding:20px}
</style>
<script src="/static/auth-guard.js"></script>
</head>
<body>
""" + render_app_header(current_user, "page_budget") + r"""
<div class="wrap">
    <div class="head">
        <div>
            <div class="title">Budget &amp; targets</div>
            <div class="sub">A spending budget and a sales target for each month, against what actually happened.
                Sales are counted as on the Sales dashboard (POS after refunds + B2B); spending is every expense dated in the month.</div>
        </div>
        <div class="year">
            <button id="prev-year" aria-label="Previous year">&lsaquo;</button>
            <span id="year-label">—</span>
            <button id="next-year" aria-label="Next year">&rsaquo;</button>
        </div>
    </div>

    <div class="cards" id="cards"></div>

    <div class="card table-card">
        <table>
            <thead>
                <tr><th></th><th class="grp" colspan="3">Sales</th><th class="grp" colspan="3">Spending</th></tr>
                <tr><th>Month</th><th>Target</th><th>Actual</th><th>Reached</th><th>Budget</th><th>Spent</th><th>Left</th></tr>
            </thead>
            <tbody id="rows"><tr><td colspan="7" class="empty">Loading…</td></tr></tbody>
        </table>
    </div>
    <div class="actions" id="actions" hidden>
        <span class="status" id="status"></span>
        <button class="btn" id="fill">Copy this month's numbers to the rest of the year</button>
        <button class="btn primary" id="save" disabled>Save</button>
    </div>
</div>
<script src="/static/budget.js"></script>
</body>
</html>"""
