"""Editing an employee's hire date.

The edit endpoint used to drop the field — its model had no hire_date — so a changed hire date was never saved and
attendance never followed. Now it is saved, and attendance follows it:

* an EARLIER hire date logs the days from it, exactly as adding a new employee does (days already logged stay);
* a LATER hire date removes the attendance before it — but only once the user confirms, since those days may have
  been logged by hand.
"""
import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.models  # noqa: E402,F401  (every table)
import app.routers.hr as hr  # noqa: E402
from app.core.log import ActivityLog  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.hr import Attendance, Employee  # noqa: E402

TODAY = date.today()
USER = SimpleNamespace(id=1, name="Admin", role="admin", permissions=None)


def scenario(fn):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        db = async_sessionmaker(engine, expire_on_commit=False)()
        try:
            hire = TODAY - timedelta(days=10)
            emp = Employee(name="Taha", base_salary=6000, is_active=True, hire_date=hire)
            db.add(emp)
            await db.flush()
            # Ten days logged, one of them by hand as a day off.
            for n in range(10):
                db.add(Attendance(employee_id=emp.id, date=hire + timedelta(days=n),
                                  status="absent" if n == 3 else "present"))
            await db.commit()
            return await fn(db, emp.id, hire)
        finally:
            await db.close()
            await engine.dispose()
    return asyncio.run(go())


async def days(db, emp_id):
    db.expire_all()
    return {d: s for d, s in (await db.execute(select(Attendance.date, Attendance.status)
                                               .where(Attendance.employee_id == emp_id))).all()}


async def edit(db, emp_id, **fields):
    return await hr.edit_employee(emp_id, hr.EmployeeUpdate(**fields), db=db, current_user=USER)


def test_the_hire_date_is_saved():
    async def go(db, emp_id, hire):
        await edit(db, emp_id, hire_date=(hire - timedelta(days=2)).isoformat())
        db.expire_all()
        assert (await db.get(Employee, emp_id)).hire_date == hire - timedelta(days=2)
    scenario(go)


def test_an_earlier_hire_date_logs_the_days_from_it():
    async def go(db, emp_id, hire):
        out = await edit(db, emp_id, hire_date=(hire - timedelta(days=5)).isoformat())
        assert out["attendance_added"] == 5 and out["attendance_removed"] == 0
        logged = await days(db, emp_id)
        assert all(logged[hire - timedelta(days=n)] == "present" for n in range(1, 6))
        assert logged[hire + timedelta(days=3)] == "absent"        # what was logged stays as it was
        assert len(logged) == 15
    scenario(go)


def test_a_later_hire_date_asks_before_removing_days():
    async def go(db, emp_id, hire):
        later = hire + timedelta(days=4)
        with pytest.raises(HTTPException) as exc:
            await edit(db, emp_id, hire_date=later.isoformat())
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "hire_removes_attendance" and exc.value.detail["count"] == 4
        db.expire_all()
        assert (await db.get(Employee, emp_id)).hire_date == hire          # nothing changed yet
        assert len(await days(db, emp_id)) == 10

        out = await edit(db, emp_id, hire_date=later.isoformat(), confirm_attendance_removal=True)
        assert out["attendance_removed"] == 4 and out["hire_date"] == later.isoformat()
        logged = await days(db, emp_id)
        assert min(logged) == later and len(logged) == 6
        log = (await db.execute(select(ActivityLog.description).where(ActivityLog.action == "edit_employee"))).scalar()
        assert "hire date" in log and "4 attendance day(s) before it removed" in log
    scenario(go)


def test_other_edits_leave_attendance_alone():
    async def go(db, emp_id, hire):
        out = await edit(db, emp_id, position="Supervisor", hire_date=hire.isoformat())
        assert out["attendance_added"] == 0 and out["attendance_removed"] == 0
        assert len(await days(db, emp_id)) == 10
    scenario(go)


def test_a_bad_date_is_refused():
    async def go(db, emp_id, hire):
        with pytest.raises(HTTPException) as exc:
            await edit(db, emp_id, hire_date="31/12/2025")
        assert exc.value.status_code == 400
    scenario(go)
