"""Deleting an expense HR booked undoes the HR side too — through the delete the Expenses page really uses.

* a salary expense → that payroll goes back to unpaid (Pending), with any days off its partial payment credited;
* a loan's expense → the loan is deleted as HR → Loans does it (deductions on unpaid runs given back);
* an allowance advance's expense → the advance is deleted; one already deducted on a payroll can't be;
* each needs the HR permission for what it undoes; an ordinary expense is deleted as before.
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
from app.database import Base
from app.models.accounting import Account
from app.models.expense import Expense, ExpenseCategory
from app.models.hr import (
    Employee, EmployeeAllowanceAdvance, EmployeeLoan, EmployeeLoanRepayment, EmployeePayrollDeduction, Payroll,
)
from app.models.user import User
from app.services import expense_service as svc

HR_PERMS = "page_expenses,action_expenses_delete,action_hr_mark_paid,action_hr_delete_loans,action_hr_delete_allowance"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())   # tests that follow expect a current loop


def user(perms=HR_PERMS, role="viewer"):
    return SimpleNamespace(id=1, name="Manager", role=role, permissions=perms)


async def _db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()
    db.add_all([
        User(id=1, name="Manager", email="m@farm.test", password="x", role="admin"),
        Account(code="1000", name="Cash", type="asset", balance=Decimal("0")),
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


async def _paid_payroll(db, period="2026-09"):
    p = Payroll(id=10, employee_id=1, period=period, base_salary=Decimal("6000"), net_salary=Decimal("6000"),
                paid=True, paid_amount=Decimal("5000"), days_off_credited=Decimal("5"))
    db.add(p)
    await db.flush()
    p.employee = await db.get(Employee, 1)
    expense = await svc.create_payroll_expense(db, p, user(), paid_date=date(2026, 10, 1), amount_override=5000.0)
    await db.commit()
    return expense.id


def test_deleting_a_salary_expense_sets_the_payroll_back_to_unpaid():
    async def go(db):
        expense_id = await _paid_payroll(db)
        out = await svc.delete_expense_entry(db, expense_id, user())
        payroll = await db.get(Payroll, 10)
        await db.refresh(payroll)
        assert (payroll.paid, payroll.paid_at, payroll.paid_amount) == (False, None, None)
        assert payroll.days_off_credited == 0                  # the partial payment's days off go with it
        assert "payroll #10 (2026-09) set back to unpaid" in out["source_sync"]
        assert await db.get(Expense, expense_id) is None
    scenario(go)


def test_un_paying_a_salary_needs_the_hr_permission():
    async def go(db):
        expense_id = await _paid_payroll(db)
        with pytest.raises(HTTPException) as e:
            await svc.delete_expense_entry(db, expense_id, user("page_expenses,action_expenses_delete"))
        assert e.value.status_code == 403 and "action_hr_mark_paid" in e.value.detail
        assert (await db.get(Payroll, 10)).paid is True and await db.get(Expense, expense_id) is not None
    scenario(go)


def test_deleting_a_loans_expense_deletes_the_loan_as_hr_does():
    async def go(db):
        loan = EmployeeLoan(id=5, employee_id=1, loan_date=date(2026, 9, 1), amount=Decimal("3000"), status="open")
        unpaid = Payroll(id=11, employee_id=1, period="2026-10", base_salary=Decimal("6000"),
                         deductions=Decimal("500"), loan_deductions=Decimal("500"), net_salary=Decimal("5500"),
                         paid=False)
        db.add_all([loan, unpaid])
        await db.flush()
        db.add_all([EmployeeLoanRepayment(loan_id=5, employee_id=1, payroll_id=11, repayment_date=date(2026, 10, 1),
                                          amount=Decimal("500")),
                    EmployeePayrollDeduction(employee_id=1, payroll_id=11, period="2026-10", type="loan_repayment",
                                             amount=Decimal("500"))])
        expense = await svc.create_loan_advance_expense(db, loan, await db.get(Employee, 1), user())
        await db.commit()
        out = await svc.delete_expense_entry(db, expense.id, user())
        assert "loan #5 deleted in HR" in out["source_sync"] and "500.00 given back" in out["source_sync"]
        db.expire_all()
        assert await db.get(EmployeeLoan, 5) is None and await db.get(Expense, expense.id) is None
        run_ = await db.get(Payroll, 11)
        assert (run_.loan_deductions, run_.net_salary) == (0, 6000)            # the deduction given back
        assert (await db.execute(select(EmployeeLoanRepayment))).scalars().all() == []
    scenario(go)


def test_deleting_an_allowance_advances_expense_deletes_the_advance_unless_deducted():
    async def go(db):
        employee = await db.get(Employee, 1)
        open_adv = EmployeeAllowanceAdvance(id=7, employee_id=1, advance_date=date(2026, 10, 2),
                                            amount=Decimal("400"), status="open")
        done_adv = EmployeeAllowanceAdvance(id=8, employee_id=1, advance_date=date(2026, 9, 2),
                                            amount=Decimal("300"), status="deducted")
        db.add_all([open_adv, done_adv])
        await db.flush()
        e_open = await svc.create_allowance_advance_expense(db, open_adv, employee, user())
        e_done = await svc.create_allowance_advance_expense(db, done_adv, employee, user())
        await db.commit()
        out = await svc.delete_expense_entry(db, e_open.id, user())
        assert out["source_sync"] == "allowance advance #7 deleted in HR"
        assert await db.get(EmployeeAllowanceAdvance, 7) is None
        with pytest.raises(HTTPException) as e:
            await svc.delete_expense_entry(db, e_done.id, user())
        assert e.value.status_code == 400 and "Reset that month" in e.value.detail
        assert await db.get(EmployeeAllowanceAdvance, 8) is not None
    scenario(go)


def test_an_ordinary_expense_is_deleted_as_before():
    async def go(db):
        db.add(ExpenseCategory(id=1, name="Fuel", account_code="5300", is_active="1"))
        db.add(Expense(id=99, ref_number="EXP-00099", category_id=1, amount=Decimal("120"),
                       expense_date=date(2026, 10, 3)))
        await db.commit()
        out = await svc.delete_expense_entry(db, 99, user("page_expenses,action_expenses_delete"))
        assert out == {"ok": True} and await db.get(Expense, 99) is None
    scenario(go)
