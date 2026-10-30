"""Treasury — the bank account and the cash box: balances, money in / money out, and a statement.

Anyone with page_treasury sees the numbers; entering an opening balance or a transfer between the cash box and the
bank needs action_treasury_manage. The logic lives in services/treasury_service.py.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.navigation import render_app_header
from app.core.permissions import get_current_user, has_permission, require_permission
from app.database import get_async_session
from app.models.user import User
from app.services import treasury_service

router = APIRouter(prefix="/treasury", tags=["Treasury"],
                   dependencies=[Depends(require_permission("page_treasury"))])


class OpeningBalanceIn(BaseModel):
    account_code: str
    amount: float
    on_date: Optional[date] = None
    note: Optional[str] = Field(None, max_length=200)


class TransferIn(BaseModel):
    from_code: str
    to_code: str
    amount: float
    on_date: Optional[date] = None
    note: Optional[str] = Field(None, max_length=200)


@router.get("/api/summary")
async def summary(from_date: Optional[date] = None, to_date: Optional[date] = None,
                  db: AsyncSession = Depends(get_async_session), user: User = Depends(get_current_user)):
    out = await treasury_service.summary(db, from_date, to_date)
    out["can_manage"] = has_permission(user, "action_treasury_manage")
    return out


@router.get("/api/statement")
async def statement(account: Optional[str] = None, from_date: Optional[date] = None, to_date: Optional[date] = None,
                    db: AsyncSession = Depends(get_async_session)):
    return await treasury_service.statement(db, account, from_date, to_date)


@router.post("/api/opening-balance", dependencies=[Depends(require_permission("action_treasury_manage"))])
async def opening_balance(data: OpeningBalanceIn, db: AsyncSession = Depends(get_async_session),
                          user: User = Depends(get_current_user)):
    return await treasury_service.set_opening_balance(
        db, account_code=data.account_code, amount=data.amount, on=data.on_date, note=data.note, user=user)


@router.post("/api/transfers", dependencies=[Depends(require_permission("action_treasury_manage"))])
async def transfer(data: TransferIn, db: AsyncSession = Depends(get_async_session),
                   user: User = Depends(get_current_user)):
    return await treasury_service.transfer(
        db, from_code=data.from_code, to_code=data.to_code, amount=data.amount, on=data.on_date, note=data.note,
        user=user)


@router.delete("/api/movements/{journal_id}", dependencies=[Depends(require_permission("action_treasury_manage"))])
async def delete_movement(journal_id: int, db: AsyncSession = Depends(get_async_session),
                          user: User = Depends(get_current_user)):
    return await treasury_service.delete_movement(db, journal_id, user)


@router.get("/", response_class=HTMLResponse)
def page(current_user: User = Depends(get_current_user)):
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Treasury — AZed Farm</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&family=Outfit:wght@400;600;800&family=Cairo:wght@400;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/plans.css">
<link rel="stylesheet" href="/static/treasury.css">
<script src="/static/auth-guard.js"></script>
</head>
<body>
""" + render_app_header(current_user, "page_treasury") + r"""
<div class="wrap">
    <div class="head">
        <div>
            <div class="title">Treasury</div>
            <div class="sub">The bank account and the cash box: what came in, what went out, and what is left.
                Cash payments use the cash box; bank transfers, cards and Visa use the bank.
                Expenses marked "Paid by owner" don't come out of either.</div>
        </div>
        <div class="period">
            <input type="date" id="from" aria-label="From">
            <span>→</span>
            <input type="date" id="to" aria-label="To">
        </div>
    </div>

    <div class="cards" id="cards"></div>

    <div class="actions" id="manage" hidden>
        <button class="btn" id="open-transfer">Move money between cash box and bank</button>
        <button class="btn" id="open-opening">Set an opening balance</button>
    </div>

    <form class="card form" id="transfer-form" hidden>
        <div class="form-title">Move money</div>
        <div class="grid">
            <label>From<select id="t-from"><option value="1000">Cash box</option><option value="1010">Bank</option></select></label>
            <label>To<select id="t-to"><option value="1010">Bank</option><option value="1000">Cash box</option></select></label>
            <label>Amount (EGP)<input id="t-amount" type="number" min="0.01" step="0.01" required></label>
            <label>Date<input id="t-date" type="date"></label>
            <label class="wide">Note<input id="t-note" maxlength="200" placeholder="e.g. Deposited the week's cash sales"></label>
        </div>
        <div class="actions"><span class="status" id="t-status"></span>
            <button type="button" class="btn" data-close="transfer-form">Cancel</button>
            <button class="btn primary">Save transfer</button></div>
    </form>

    <form class="card form" id="opening-form" hidden>
        <div class="form-title">Opening balance</div>
        <div class="card-note">The real balance when you start tracking — from the bank statement, or by counting
            the cash box. This is the account's fresh start: Treasury ignores everything on it from before this date
            (a past date means the start of that day). It is recorded against owner equity, so it is not counted as
            income. Enter 0 for an empty account, or a negative amount for an overdrawn one.</div>
        <div class="grid">
            <label>Account<select id="o-account"><option value="1010">Bank</option><option value="1000">Cash box</option></select></label>
            <label>Balance (EGP)<input id="o-amount" type="number" step="0.01" required></label>
            <label>On date<input id="o-date" type="date"></label>
            <label class="wide">Note<input id="o-note" maxlength="200" placeholder="e.g. Per bank statement"></label>
        </div>
        <div class="actions"><span class="status" id="o-status"></span>
            <button type="button" class="btn" data-close="opening-form">Cancel</button>
            <button class="btn primary">Save opening balance</button></div>
    </form>

    <div class="section-title">Money in and out by source</div>
    <div class="card table-card">
        <table>
            <thead><tr><th>Source</th><th>Money in</th><th>Money out</th><th>Net</th></tr></thead>
            <tbody id="sources"><tr><td colspan="4" class="empty">Loading…</td></tr></tbody>
        </table>
    </div>

    <div class="section-head">
        <div class="section-title">Statement</div>
        <div class="tabs" role="tablist">
            <button data-account="all" class="on">Both</button>
            <button data-account="1010">Bank</button>
            <button data-account="1000">Cash box</button>
        </div>
    </div>
    <div class="card table-card">
        <table class="statement">
            <thead><tr><th>Date</th><th>Description</th><th>Source</th><th>Account</th>
                <th>Money in</th><th>Money out</th><th>Balance</th><th></th></tr></thead>
            <tbody id="lines"><tr><td colspan="8" class="empty">Loading…</td></tr></tbody>
        </table>
    </div>
</div>
<script src="/static/plans.js"></script>
<script src="/static/treasury.js"></script>
</body>
</html>"""
