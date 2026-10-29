"""Paid leave shown as money on payroll.

A rotation worker works 23 days and takes 7 days off from the leave balance. Those 7 days are paid, so the month
pays the full salary — and the preview, the saved run and the payslip all show the leave pay as its own amount
instead of a lump "salary for 23 days" that looks like 7 days were docked.
"""

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.routers.hr as hr
from app.core.log import ActivityLog
from app.database import Base
from app.models.farm import Farm
from app.models.hr import (Attendance, Employee, EmployeeAllowanceAdvance, EmployeeDaysOffAdjustment, EmployeeLoan,
                           EmployeeLoanRepayment, EmployeePayrollDeduction, Payroll)
from app.models.user import User


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement, params=None):
        return self.session.execute(statement, params or {})

    def add(self, obj):
        self.session.add(obj)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        User.__table__, Farm.__table__, Employee.__table__, Attendance.__table__, Payroll.__table__, EmployeeLoan.__table__,
        EmployeeLoanRepayment.__table__, EmployeeAllowanceAdvance.__table__, EmployeePayrollDeduction.__table__,
        EmployeeDaysOffAdjustment.__table__, ActivityLog.__table__,
    ])
    return sessionmaker(bind=engine, expire_on_commit=False)()


def rotation_worker(session, *, per_month=7, basis="calendar"):
    """September 2026 (30 days): present the 1st-23rd, Day Off the 24th-30th."""
    session.add(User(id=1, name="Admin", email="a@x.example", password="x", role="admin"))
    emp = Employee(name="Karim", base_salary=Decimal("9000"), salary_days_basis=basis, hire_date=date(2026, 1, 1),
                   vacation_days_per_month=per_month, is_active=True)
    session.add(emp)
    session.flush()
    for day in range(1, 31):
        session.add(Attendance(employee_id=emp.id, date=date(2026, 9, day), status="present" if day <= 23 else "absent"))
    session.commit()
    return emp


def preview(session, emp):
    return asyncio.run(hr._payroll_preview_for_employee(
        AsyncSessionAdapter(session), emp, period="2026-09", working_days=30, days_elapsed=30, year=2026, month=9,
        include_loans=False, include_deductions=False))


def test_seven_days_off_from_the_balance_are_paid_as_leave_money():
    with make_session() as session:
        row = preview(session, rotation_worker(session))
    assert row["earned_base"] == 9000.0                  # the full salary, nothing docked
    assert (row["worked_pay"], row["leave_pay"]) == (6900.0, 2100.0)
    assert (row["vacation_paid_days"], row["vacation_unpaid_days"]) == (7.0, 0.0)


def test_fixed_30_deal_splits_the_same_way():
    with make_session() as session:
        row = preview(session, rotation_worker(session, basis="fixed_30"))
    assert (row["earned_base"], row["worked_pay"], row["leave_pay"]) == (9000.0, 6900.0, 2100.0)


def test_days_off_beyond_the_balance_show_as_unpaid():
    with make_session() as session:
        # Hired in September with 4 days a month: only 4 of the 7 days off are covered.
        emp = rotation_worker(session, per_month=4)
        emp.hire_date = date(2026, 9, 1)
        session.commit()
        row = preview(session, emp)
    assert (row["vacation_paid_days"], row["vacation_unpaid_days"]) == (4.0, 3.0)
    assert (row["earned_base"], row["leave_pay"]) == (8100.0, 1200.0)


def test_payroll_run_stores_the_leave_days_and_the_payslip_lists_them():
    with make_session() as session:
        db = AsyncSessionAdapter(session)
        rotation_worker(session)
        admin = session.get(User, 1)
        asyncio.run(hr.run_payroll(SimpleNamespace(period="2026-09", emp_ids=None, bonuses={}, loan_repayments={}),
                                   db=db, current_user=admin))
        [payroll] = session.query(Payroll).all()
        assert (payroll.base_salary, payroll.paid_leave_days, payroll.net_salary) == (
            Decimal("9000.00"), Decimal("7.00"), Decimal("9000.00"))

        [record] = asyncio.run(hr.get_payroll(period="2026-09", db=db))
        assert (record["paid_leave_days"], record["leave_pay"]) == (7.0, 2100.0)

        slip = asyncio.run(hr._payslip(db, payroll))
        assert [(e["label"], e["amount"]) for e in slip["earnings"]] == [
            ("Salary for 23 of 30 days", 6900.0), ("Paid leave (7 days)", 2100.0)]
        assert slip["gross"] == slip["net"] == 9000.0


# ── 'work_days' deal: 15,000 for 23 working days, 7 days off ─────────────────

def work_days_worker(session, worked: dict[int, int]):
    """{month: days present} in 2026, hired long ago, no monthly leave allowance; the other days are Day Off."""
    from calendar import monthrange
    session.add(User(id=1, name="Admin", email="a@x.example", password="x", role="admin"))
    emp = Employee(name="Abdallah Ahmed", base_salary=Decimal("15000"), salary_days_basis="work_days",
                   work_days_per_month=23, hire_date=date(2025, 1, 1), vacation_days_per_month=0, is_active=True)
    session.add(emp)
    session.flush()
    for month, present in worked.items():
        for day in range(1, monthrange(2026, month)[1] + 1):
            session.add(Attendance(employee_id=emp.id, date=date(2026, month, day),
                                   status="present" if day <= present else "absent"))
    session.commit()
    return emp


def run_month(session, period):
    asyncio.run(hr.run_payroll(SimpleNamespace(period=period, emp_ids=None, bonuses={}, loan_repayments={}),
                               db=AsyncSessionAdapter(session), current_user=session.get(User, 1)))
    return session.query(Payroll).filter_by(period=period).one()


def pay(session, payroll, amount, monkeypatch):
    async def fake_expense(*_args, **_kwargs):
        return SimpleNamespace(id=1, ref_number="EXP-1", category=None, farm_id=None, farm=None, amount=amount)

    monkeypatch.setattr(hr, "create_payroll_expense", fake_expense)
    return asyncio.run(hr.mark_paid(payroll.id, hr.PayrollPayRequest(paid_amount=amount, convert_remainder_to_days_off=True),
                                    db=AsyncSessionAdapter(session), current_user=session.get(User, 1)))


def test_work_days_deal_pays_23_days_as_the_full_salary_and_days_off_cost_nothing():
    with make_session() as session:
        emp = work_days_worker(session, {9: 23})
        assert run_month(session, "2026-09").base_salary == Decimal("15000.00")
        assert asyncio.run(hr._employee_vacation_summary(AsyncSessionAdapter(session), emp))["taken"] == 0


def test_extra_day_is_paid_or_banked_as_one_day_of_credit(monkeypatch):
    with make_session() as session:
        emp = work_days_worker(session, {9: 24})
        payroll = run_month(session, "2026-09")
        assert payroll.base_salary == Decimal("15652.17")          # 24 × 15,000 / 23

        [record] = asyncio.run(hr.get_payroll(period="2026-09", db=AsyncSessionAdapter(session)))
        assert (record["work_days_per_month"], record["extra_pay"], record["daily_rate"]) == (23, 652.17, 652.17)

        out = pay(session, payroll, Decimal("15000.00"), monkeypatch)
        assert out["days_off_credited"] == 1.0
        assert asyncio.run(hr._employee_vacation_summary(AsyncSessionAdapter(session), emp))["days_left"] == 1.0


def test_a_short_month_is_topped_up_from_the_credit(monkeypatch):
    with make_session() as session:
        emp = work_days_worker(session, {8: 24, 9: 22})
        pay(session, run_month(session, "2026-08"), Decimal("15000.00"), monkeypatch)   # banks 1 day

        september = run_month(session, "2026-09")
        assert (september.base_salary, september.paid_leave_days) == (Decimal("15000.00"), Decimal("1.00"))
        assert asyncio.run(hr._employee_vacation_summary(AsyncSessionAdapter(session), emp))["days_left"] == 0


def test_a_monthly_leave_allowance_left_on_the_profile_does_not_pile_up_as_credit():
    """Days off are the schedule on this deal, so an old 'Vacation Days / Month' must not accrue credit."""
    with make_session() as session:
        emp = work_days_worker(session, {9: 23})
        emp.vacation_days_per_month = 7
        session.commit()
        summary = asyncio.run(hr._employee_vacation_summary(AsyncSessionAdapter(session), emp))
        assert (summary["accrued"], summary["days_left"]) == (0, 0)
