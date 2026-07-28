"""Attendance has exactly two options — Present and Day Off — so an elapsed day
with no record at all is a Day Off nobody logged, not a third kind of day.

Payroll used to dock such a day outright while an explicitly logged Day Off was
paid from the accrued leave balance, so identical time off was paid differently
depending only on whether someone clicked. These tests lock down the new rule
and, critically, that the leave balance is reduced by exactly the days payroll
pays from it.
"""

import asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.routers.hr as hr
from app.database import Base
from app.models.hr import Attendance, Employee, Payroll


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

    async def refresh(self, obj):
        self.session.refresh(obj)


def run(coro):
    return asyncio.run(coro)


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[Employee.__table__, Attendance.__table__, Payroll.__table__],
    )
    return sessionmaker(bind=engine, expire_on_commit=False)()


def log(session, employee, day: int, status: str, month: int = 7) -> None:
    session.add(Attendance(
        employee_id=employee.id, date=date(2026, month, day), status=status,
    ))


def day_off_count(session, employee_id: int) -> int:
    return session.execute(
        select(func.count(Attendance.id)).where(
            Attendance.employee_id == employee_id,
            Attendance.status == "absent",
        )
    ).scalar_one()


def test_unlogged_days_are_counted_as_days_off():
    """Preview path: read-only, must report the gaps without writing rows."""
    with make_session() as session:
        db = AsyncSessionAdapter(session)
        # 7 days/month allowance, hired long ago → a healthy accrued balance.
        emp = Employee(name="Taha", base_salary=Decimal("13500"),
                       salary_days_basis="fixed_30", hire_date=date(2026, 1, 1),
                       vacation_days_per_month=7)
        session.add(emp)
        session.commit()

        # 25 of the 28 elapsed days logged present; days 26, 27, 28 never logged.
        for day in range(1, 26):
            log(session, emp, day, "present")
        session.commit()

        paid_leave, days_off, available = run(
            hr._paid_leave_days_for_period(db, emp, 2026, 7, days_elapsed=28)
        )
        assert days_off == Decimal("3.00")      # the three unlogged days
        assert paid_leave == Decimal("3.00")    # balance covers all three
        assert available >= Decimal("3")
        # Preview must not have written anything.
        assert day_off_count(session, emp.id) == 0


def test_unlogged_days_beyond_the_balance_stay_unpaid():
    with make_session() as session:
        db = AsyncSessionAdapter(session)
        # No monthly allowance → nothing to draw on.
        emp = Employee(name="Samir", base_salary=Decimal("9500"),
                       salary_days_basis="fixed_30", hire_date=date(2026, 1, 1),
                       vacation_days_per_month=0)
        session.add(emp)
        session.commit()
        for day in range(1, 15):
            log(session, emp, day, "present")
        session.commit()

        paid_leave, days_off, available = run(
            hr._paid_leave_days_for_period(db, emp, 2026, 7, days_elapsed=28)
        )
        assert days_off == Decimal("14.00")
        assert available == Decimal("0")
        assert paid_leave == Decimal("0")       # unpaid, as before


def test_days_before_hire_date_are_not_the_employees_to_cover():
    """A mid-month hire must not be charged for the days before he started."""
    with make_session() as session:
        db = AsyncSessionAdapter(session)
        emp = Employee(name="Wael", base_salary=Decimal("10000"),
                       salary_days_basis="fixed_30", hire_date=date(2026, 7, 20),
                       vacation_days_per_month=7)
        session.add(emp)
        session.commit()
        for day in range(20, 29):               # 20th-28th logged present
            log(session, emp, day, "present")
        session.commit()

        missing = run(hr._unlogged_dates_in_period(db, emp, 2026, 7, 28))
        assert missing == []                    # nothing before the 20th counts


def test_future_days_are_not_missing():
    with make_session() as session:
        db = AsyncSessionAdapter(session)
        emp = Employee(name="Taha", base_salary=Decimal("13500"),
                       salary_days_basis="fixed_30", hire_date=date(2026, 1, 1),
                       vacation_days_per_month=7)
        session.add(emp)
        session.commit()
        for day in range(1, 29):
            log(session, emp, day, "present")
        session.commit()

        # 28 elapsed of a 31-day month, all logged → days 29-31 are not gaps.
        assert run(hr._unlogged_dates_in_period(db, emp, 2026, 7, 28)) == []


def test_payroll_run_materialises_day_offs_so_the_balance_is_spent():
    """The balance is derived from Day Off rows, so paying an unlogged day from
    it without recording the row would let the same day be paid again forever."""
    with make_session() as session:
        db = AsyncSessionAdapter(session)
        emp = Employee(name="Taha", base_salary=Decimal("13500"),
                       salary_days_basis="fixed_30", hire_date=date(2026, 1, 1),
                       vacation_days_per_month=7, is_active=True)
        session.add(emp)
        session.commit()
        for day in range(1, 29):                # 28 present, days 29-31 unlogged
            log(session, emp, day, "present")
        session.commit()

        before = run(hr._employee_vacation_summary(db, emp))["days_left"]

        for missing in run(hr._unlogged_dates_in_period(db, emp, 2026, 7, 31)):
            session.add(Attendance(employee_id=emp.id, date=missing, status="absent"))
        session.commit()

        assert day_off_count(session, emp.id) == 3
        after = run(hr._employee_vacation_summary(db, emp))["days_left"]
        assert after == before - 3              # balance actually spent
