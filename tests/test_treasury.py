"""Treasury — the bank account and the cash box.

* the payment method picks the money account: cash → cash box (1000), transfers / cards / Visa → bank (1010);
* an expense paid by bank transfer comes out of the bank (it used to land on 1200 Inventory), and its reversal
  puts the money back where the original journal took it from — including old entries on 1200;
* a loan paid from the bank shows on the bank as "Employee loans";
* the opening balance is booked against equity, once per account; a transfer moves money between the two without
  changing the organization's total; both can be deleted, other movements can't;
* the statement runs a balance; the page is behind page_treasury and the menu shows it.
A real async SQLite database.
"""
import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.models  # noqa: F401  every table
from app.core.navigation import render_app_header
from app.core.permission_catalog import KNOWN_PERMISSIONS, get_role_permissions
from app.core.time_utils import today_local
from app.database import Base
from app.models.accounting import Account, Journal, JournalEntry
from app.models.expense import Expense, ExpenseCategory
from app.models.hr import Employee, EmployeeLoan
from app.models.user import User
from app.schemas.expense import ExpenseCreate
from app.services import expense_service, treasury_service as svc


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


USER = SimpleNamespace(id=1, name="Manager", role="admin", permissions=None)


async def _db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()
    db.add_all([
        User(id=1, name="Manager", email="m@farm.test", password="x", role="admin"),
        Account(code="1000", name="Cash", type="asset", balance=Decimal("0")),
        Account(code="1010", name="Bank", type="asset", balance=Decimal("0")),
        Account(code="5001", name="Water", type="expense", balance=Decimal("0")),
        ExpenseCategory(id=1, name="Water", account_code="5001", is_active="1"),
        Employee(id=1, name="Saeed", is_active=True, base_salary=Decimal("6000")),
    ])
    await db.commit()
    return engine, db


def scenario(fn):
    async def go():
        engine, db = await _db()
        try:
            return await fn(db)
        finally:
            await db.close()
            await engine.dispose()
    return run(go())


async def balances(db) -> dict[str, float]:
    rows = (await db.execute(select(Account))).scalars().all()
    return {a.code: round(float(a.balance), 2) for a in rows}


async def add_expense(db, amount, method):
    await expense_service.create_expense_entry(db, ExpenseCreate(
        category_id=1, expense_date=today_local().isoformat(), amount=amount, payment_method=method), USER)
    return (await db.execute(select(Expense).order_by(Expense.id.desc()))).scalars().first()


# ── which account ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("method, code", [
    ("cash", "1000"), (None, "1000"), ("", "1000"), ("credit", "1000"),
    ("bank_transfer", "1010"), ("transfer", "1010"), ("card", "1010"), ("visa", "1010"), (" Visa ", "1010"),
])
def test_payment_method_picks_the_money_account(method, code):
    assert svc.money_account_code(method) == code


def test_sources_have_plain_names():
    assert svc.source_of("invoice", "Sale - INV-00001") == "POS sales"
    assert svc.source_of("b2b_collection", "") == "B2B sales"
    assert svc.source_of("b2b_payment_reversal", "") == "B2B refunds & reversals"
    assert svc.source_of("expense", "Salaries & Wages expense - EXP-00009 - Saeed") == "Payroll"
    assert svc.source_of("expense", "Salary advance - EXP-00010 - Saeed") == "Employee loans"
    assert svc.source_of("expense", "Water expense - EXP-00011") == "Expenses"
    assert svc.source_of("supplier_payment", "") == "Supplier payments"
    assert svc.source_of("treasury_transfer", "") == "Transfers"


# ── expenses, loans ───────────────────────────────────────────────────────

def test_bank_expense_comes_out_of_the_bank_and_its_reversal_goes_back():
    async def go(db):
        await add_expense(db, 120, "cash")
        bank_exp = await add_expense(db, 300, "bank_transfer")
        after_add = await balances(db)
        await db.refresh(bank_exp, ["category"])
        await expense_service._reverse_expense_journal(db, bank_exp)
        await db.commit()
        return after_add, await balances(db)

    after_add, after_reverse = scenario(go)
    assert after_add["1000"] == -120.0
    assert after_add["1010"] == -300.0
    assert "1200" not in after_add            # no longer lands on Inventory
    assert after_reverse["1010"] == 0.0
    assert after_reverse["1000"] == -120.0


def test_reversing_an_old_expense_returns_money_to_the_account_it_came_from():
    """Expenses booked before the bank account existed credited 1200; undoing one must not touch the bank."""
    async def go(db):
        legacy = Account(code="1200", name="Inventory", type="asset", balance=Decimal("-50"))
        db.add(legacy)
        await db.flush()
        journal = Journal(ref_type="expense", description="Water expense - EXP-00001")
        db.add(journal)
        await db.flush()
        water = (await db.execute(select(Account).where(Account.code == "5001"))).scalar_one()
        water.balance = Decimal("50")
        db.add_all([JournalEntry(journal_id=journal.id, account_id=water.id, debit=50, credit=0),
                    JournalEntry(journal_id=journal.id, account_id=legacy.id, debit=0, credit=50)])
        expense = Expense(ref_number="EXP-00001", category_id=1, user_id=1, expense_date=date(2026, 1, 5),
                          amount=50, payment_method="bank_transfer", journal_id=journal.id)
        db.add(expense)
        await db.flush()
        await db.refresh(expense, ["category"])
        await expense_service._reverse_expense_journal(db, expense)
        await db.commit()
        return await balances(db)

    after = scenario(go)
    assert after["1200"] == 0.0
    assert after["1010"] == 0.0
    assert after["5001"] == 0.0


def test_loan_paid_from_the_bank_shows_as_employee_loans():
    async def go(db):
        employee = await db.get(Employee, 1)
        loan = EmployeeLoan(id=7, employee_id=1, loan_date=today_local(), amount=Decimal("2000"), status="open")
        db.add(loan)
        await db.flush()
        await expense_service.create_loan_advance_expense(db, loan, employee, USER, payment_method="bank_transfer")
        await db.commit()
        return await balances(db), await svc.summary(db)

    after, summary = scenario(go)
    assert after["1010"] == -2000.0
    assert after["1000"] == 0.0
    loans = next(s for s in summary["by_source"] if s["source"] == "Employee loans")
    assert loans == {"source": "Employee loans", "money_in": 0.0, "money_out": 2000.0}
    bank = next(a for a in summary["accounts"] if a["code"] == "1010")
    assert bank["money_out"] == 2000.0 and bank["closing"] == -2000.0


# ── opening balance, transfers, statement ────────────────────────────────

def test_opening_balance_and_transfer():
    async def go(db):
        await svc.set_opening_balance(db, account_code="1010", amount=10000, on=None, note="Per statement", user=USER)
        with pytest.raises(HTTPException) as again:
            await svc.set_opening_balance(db, account_code="1010", amount=5, on=None, note=None, user=USER)
        await svc.transfer(db, from_code="1010", to_code="1000", amount=2500, on=None, note="Petty cash", user=USER)
        await add_expense(db, 400, "cash")
        return again.value, await balances(db), await svc.summary(db), await svc.statement(db, "1010"), \
            await svc.statement(db, "all")

    refused, after, summary, bank, both = scenario(go)
    assert refused.status_code == 400
    assert after["1010"] == 7500.0
    assert after["1000"] == 2100.0
    assert after["3000"] == -10000.0           # the opening balance is equity, not income
    assert summary["has_opening"] == {"1000": False, "1010": True}
    # The transfer moves money between the two but adds nothing to the total.
    assert summary["total"]["money_in"] == 10000.0
    assert summary["total"]["money_out"] == 400.0
    assert summary["total"]["closing"] == 9600.0
    assert "Transfers" not in {s["source"] for s in summary["by_source"]}
    cash = next(a for a in summary["accounts"] if a["code"] == "1000")
    assert (cash["money_in"], cash["money_out"], cash["closing"]) == (2500.0, 400.0, 2100.0)

    assert [(l["source"], l["money_in"], l["money_out"], l["balance"]) for l in bank["lines"]] == [
        ("Opening balance", 10000.0, 0.0, 10000.0),
        ("Transfers", 0.0, 2500.0, 7500.0),
    ]
    assert both["closing"] == 9600.0
    assert [l["manual"] for l in both["lines"]] == [True, True, True, False]


def test_only_manual_movements_can_be_deleted():
    async def go(db):
        moved = await svc.transfer(db, from_code="1000", to_code="1010", amount=800, on=None, note=None, user=USER)
        expense = await add_expense(db, 100, "cash")
        with pytest.raises(HTTPException) as refused:
            await svc.delete_movement(db, expense.journal_id, USER)
        await svc.delete_movement(db, moved["journal_id"], USER)
        left = (await db.execute(select(Journal).where(Journal.ref_type == svc.TRANSFER_REF))).scalars().all()
        return refused.value, left, await balances(db)

    refused, left, after = scenario(go)
    assert refused.status_code == 400
    assert left == []
    assert after["1010"] == 0.0
    assert after["1000"] == -100.0


@pytest.mark.parametrize("kwargs, message", [
    ({"from_code": "1000", "to_code": "1000", "amount": 5}, "two different"),
    ({"from_code": "1000", "to_code": "1010", "amount": 0}, "greater than 0"),
    ({"from_code": "1000", "to_code": "4000", "amount": 5}, "Unknown"),
])
def test_bad_transfers_are_refused(kwargs, message):
    async def go(db):
        with pytest.raises(HTTPException) as err:
            await svc.transfer(db, on=None, note=None, user=USER, **kwargs)
        return err.value

    assert message in scenario(go).detail


def test_future_dates_are_refused():
    async def go(db):
        with pytest.raises(HTTPException) as err:
            await svc.transfer(db, from_code="1000", to_code="1010", amount=5, on=date(2999, 1, 1), note=None, user=USER)
        return err.value

    assert scenario(go).status_code == 400


# ── access ────────────────────────────────────────────────────────────────

def test_permissions_and_menu():
    assert {"page_treasury", "action_treasury_manage"} <= set(KNOWN_PERMISSIONS)
    assert {"page_treasury", "action_treasury_manage"} <= get_role_permissions("accountant")
    assert "page_treasury" in get_role_permissions("viewer")
    assert "action_treasury_manage" not in get_role_permissions("viewer")

    can = SimpleNamespace(id=2, name="Accountant", role="viewer", permissions="page_treasury")
    cannot = SimpleNamespace(id=3, name="Cashier", role="cashier", permissions=None)
    assert 'href="/treasury/"' in render_app_header(can, "page_treasury")
    assert 'href="/treasury/"' not in render_app_header(cannot, "page_pos")


def _client(user):
    from tests.test_budget import _client as budget_client
    return budget_client(user)


def _user(permissions: str):
    return SimpleNamespace(id=5, name="Clerk", email="c@example.com", role="cashier", permissions=permissions,
                           is_active=True)


def test_page_needs_page_treasury_and_changes_need_manage():
    response = _client(_user("page_pos")).get("/treasury/api/summary")
    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: page_treasury"

    response = _client(_user("page_treasury")).post(
        "/treasury/api/transfers", json={"from_code": "1000", "to_code": "1010", "amount": 5})
    assert response.json()["detail"] == "Permission denied: action_treasury_manage"


def test_page_renders_with_its_scripts():
    response = _client(_user("page_treasury")).get("/treasury/")
    assert response.status_code == 200
    assert "<title>Treasury" in response.text
    assert "/static/plans.js" in response.text and "/static/treasury.js" in response.text
