"""Budget — a spending budget for each month, against what was actually spent.

Anyone with page_budget sees the numbers; setting them needs action_budget_manage. Sales targets have their own page
(routers/sales_targets.py).
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

router = APIRouter(prefix="/budget", tags=["Budget"],
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
<title>Budget — AZed Farm</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&family=Outfit:wght@400;600;800&family=Cairo:wght@400;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/plans.css">
<script src="/static/auth-guard.js"></script>
</head>
<body>
""" + render_app_header(current_user, "page_budget") + r"""
<div class="wrap">
    <div class="head">
        <div>
            <div class="title">Budget</div>
            <div class="sub">A spending budget for each month, against every expense dated in that month.</div>
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
            <thead><tr><th>Month</th><th>Budget</th><th>Spent</th><th>Used</th><th>Left</th></tr></thead>
            <tbody id="rows"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
        </table>
    </div>
    <div class="actions" id="actions" hidden>
        <span class="status" id="status"></span>
        <button class="btn" id="fill">Copy to the months after</button>
        <button class="btn primary" id="save" disabled>Save</button>
    </div>
</div>
<script src="/static/plans.js"></script>
<script src="/static/budget.js"></script>
</body>
</html>"""
