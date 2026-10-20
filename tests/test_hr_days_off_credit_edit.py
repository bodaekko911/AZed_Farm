"""Editing an employee's days-off credit from the Days Off tab.

The credit itself is computed (accrued + credited by payroll - taken), so an
edit is stored as a dated adjustment for the difference. These tests check the
new credit comes out exactly as entered, and that payroll only sees the
adjustment from the month it was made in.
"""

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.routers.hr as hr
from app.database import Base
from app.models.hr import Attendance, Employee, EmployeeDaysOffAdjustment, Payroll


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement, params=None):
        return self.session.execute(statement, params or {})

    async def get(self, model, ident):
        return self.session.get(model, ident)

    def add(self, obj):
        self.session.add(obj)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def refresh(self, obj):
        self.session.refresh(obj)


def run(coro):
    return asyncio.run(coro)


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Employee.__table__,
            Attendance.__table__,
            Payroll.__table__,
            EmployeeDaysOffAdjustment.__table__,
        ],
    )
    return sessionmaker(bind=engine, expire_on_commit=False)()


def setup_employee(session):
    # 2 days/month from August → 6 accrued by October; 7 days off taken in
    # September, so the raw balance is -1 (shown as 0).
    employee = Employee(
        name="A", base_salary=Decimal("6000"), hire_date=date(2026, 8, 1),
        vacation_days_per_month=2, is_active=True,
    )
    session.add(employee)
    session.flush()
    for day in range(3, 10):
        session.add(Attendance(employee_id=employee.id, date=date(2026, 9, day), status="absent"))
    session.commit()
    return employee


def set_credit(db, employee, days, monkeypatch):
    monkeypatch.setattr(hr, "record", lambda *a, **k: None)
    user = SimpleNamespace(id=None, username="tester")
    return run(hr.set_days_off_credit(
        employee.id, hr.DaysOffCreditUpdate(days_left=Decimal(str(days))),
        db=db, current_user=user,
    ))


def test_edit_sets_credit_exactly_even_when_overdrawn(monkeypatch):
    session = make_session()
    employee = setup_employee(session)
    db = AsyncSessionAdapter(session)
    monkeypatch.setattr(hr, "date", type("D", (date,), {"today": staticmethod(lambda: date(2026, 10, 20))}))

    before = run(hr._employee_vacation_summary(db, employee))
    assert before["days_left"] == 0

    after = set_credit(db, employee, 5, monkeypatch)
    assert after["days_left"] == 5
    assert after["adjusted"] == 6

    after = set_credit(db, employee, 2.5, monkeypatch)
    assert after["days_left"] == 2.5
    assert after["adjusted"] == 3.5


def test_payroll_sees_adjustment_from_its_month_only(monkeypatch):
    session = make_session()
    employee = setup_employee(session)
    db = AsyncSessionAdapter(session)
    monkeypatch.setattr(hr, "date", type("D", (date,), {"today": staticmethod(lambda: date(2026, 10, 20))}))

    september_before = run(hr._vacation_available_as_of(db, employee, 2026, 9))
    set_credit(db, employee, 5, monkeypatch)

    assert run(hr._vacation_available_as_of(db, employee, 2026, 9)) == september_before
    # Entering October: 6 accrued - 7 taken in September + 6 adjustment = 5.
    assert run(hr._vacation_available_as_of(db, employee, 2026, 10)) == Decimal("5")
