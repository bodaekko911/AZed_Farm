from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
import re
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import delete, func, select
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import date, datetime, timezone

from app.database import get_async_session
from app.core.permissions import get_current_user, has_permission, require_permission
from app.core.log import record
from app.core.navigation import render_app_header
from app.models.accounting import Account, Journal, JournalEntry
from app.models.expense import Expense
from app.models.hr import (
    Employee,
    Attendance,
    EmployeeLoan,
    EmployeeLoanRepayment,
    EmployeePayrollDeduction,
    EmployeeAllowanceAdvance,
    Payroll,
)
from app.models.farm import Farm
from app.models.user import User
from app.services.expense_service import (
    create_payroll_expense,
    create_loan_advance_expense,
    reverse_loan_advance_expense,
)

ATTENDANCE_STATUS_PRESENT = "present"
ATTENDANCE_STATUS_ABSENT = "absent"
ATTENDANCE_AUTO_STATUSES = {ATTENDANCE_STATUS_PRESENT, ATTENDANCE_STATUS_ABSENT}
# Only two statuses are supported: Present (paid working day) and Day Off
# (stored as 'absent' — paid leave drawn from the balance). "Late" and "Leave"
# were removed because no payroll/vacation calculation ever read them, so they
# silently docked pay without touching the leave balance.
ATTENDANCE_STATUSES = ATTENDANCE_AUTO_STATUSES
# Any legacy value still arriving from an old client/record is coerced onto a
# real status: a late day was worked (→ present); a leave day was time off
# (→ Day Off / absent).
ATTENDANCE_LEGACY_STATUS_MAP = {
    "late":  ATTENDANCE_STATUS_PRESENT,
    "leave": ATTENDANCE_STATUS_ABSENT,
}

router = APIRouter(
    prefix="/hr",
    tags=["HR"],
    dependencies=[Depends(require_permission("page_hr"))],
)


# ── Schemas ────────────────────────────────────────────
class EmployeeCreate(BaseModel):
    name:                    str
    phone:                   Optional[str]  = None
    position:                Optional[str]  = None
    department:              Optional[str]  = None
    hire_date:               Optional[str]  = None
    base_salary:             float          = 0
    vacation_days_per_month: int            = 0
    food_allowance:          float          = 0
    transportation_allowance: float         = 0
    farm_id:                 Optional[int]  = None
    works_with_animals:      bool           = False
    salary_days_basis:       str            = "calendar"   # calendar | fixed_30

class EmployeeUpdate(BaseModel):
    name:                    Optional[str]   = None
    phone:                   Optional[str]   = None
    position:                Optional[str]   = None
    department:              Optional[str]   = None
    base_salary:             Optional[float] = None
    vacation_days_per_month: Optional[int]   = None
    food_allowance:          Optional[float] = None
    transportation_allowance: Optional[float] = None
    farm_id:                 Optional[int]   = None
    works_with_animals:      Optional[bool]  = None
    is_active:               Optional[bool]  = None
    salary_days_basis:       Optional[str]   = None        # calendar | fixed_30

class AttendanceCreate(BaseModel):
    employee_id: int
    date:        str
    status:      str = "present"
    note:        Optional[str] = None

class PayrollRun(BaseModel):
    period:  str  # "2025-01"
    emp_ids: Optional[List[int]] = None  # None = all employees
    bonuses: Optional[dict[int, Decimal]] = None
    loan_repayments: Optional[dict[int, Decimal]] = None

class PayrollUpdate(BaseModel):
    bonuses:    Decimal = Decimal("0")
    deductions: Decimal = Decimal("0")
    notes:      Optional[str] = None

class PayrollPayRequest(BaseModel):
    payment_method: Optional[str] = "cash"
    # Cash actually paid. If omitted, the full net salary is paid.
    paid_amount: Optional[Decimal] = None
    # If true, the unpaid remainder (net - paid_amount) is converted to paid
    # days off (remaining / daily_rate) and credited to the leave balance.
    convert_remainder_to_days_off: Optional[bool] = False


class EmployeeLoanCreate(BaseModel):
    loan_date: str
    amount: Decimal = Field(gt=0)
    description: Optional[str] = None


class LoanRepaymentCreate(BaseModel):
    repayment_date: str
    amount: Decimal = Field(gt=0)
    note: Optional[str] = None


class DayDeductionCreate(BaseModel):
    period: str
    deduction_date: str
    days: Decimal = Field(gt=0)   # e.g. 1, 0.5, 0.25 — daily rate auto-calculated
    note: Optional[str] = None


class ManualDeductionCreate(BaseModel):
    period: str
    amount: Decimal = Field(gt=0)
    note: Optional[str] = None


class AllowanceAdvanceCreate(BaseModel):
    advance_date: str
    amount: Decimal = Field(gt=0)
    note: Optional[str] = None


class ClearHRDataRequest(BaseModel):
    confirmation: Optional[str] = None


CLEAR_HR_DATA_CONFIRMATION = "CLEAR HR DATA"
LOAN_STATUSES = {"open", "paid", "cancelled"}
DEDUCTION_TYPES = {"loan_repayment", "day_deduction", "manual"}
PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
MONEY_QUANT = Decimal("0.01")
DAY_QUANT = Decimal("0.01")


def _normalize_attendance_status(status: str) -> str:
    normalized = (status or ATTENDANCE_STATUS_PRESENT).strip().lower()
    normalized = ATTENDANCE_LEGACY_STATUS_MAP.get(normalized, normalized)
    if normalized not in ATTENDANCE_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid attendance status")
    return normalized


def _normalize_auto_attendance_status(status: str | None) -> str:
    normalized = (status or ATTENDANCE_STATUS_PRESENT).strip().lower()
    return normalized if normalized in ATTENDANCE_AUTO_STATUSES else ATTENDANCE_STATUS_PRESENT


def _parse_optional_iso_date(value: str | None, field_name: str) -> date | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}. Use YYYY-MM-DD.",
        ) from exc


def _parse_required_iso_date(value: str | None, field_name: str) -> date:
    parsed = _parse_optional_iso_date(value, field_name)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    return parsed


def _validate_period(period: str | None) -> str:
    normalized = (period or "").strip()
    if not PERIOD_RE.match(normalized):
        raise HTTPException(status_code=400, detail="Invalid period. Use YYYY-MM.")
    month = int(normalized[5:7])
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Invalid period. Use YYYY-MM.")
    return normalized


def _dec(value, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


def _money(value) -> Decimal:
    return _dec(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _days(value) -> Decimal:
    return _dec(value).quantize(DAY_QUANT, rounding=ROUND_HALF_UP)


def _as_float(value) -> float:
    return float(_money(value))


def _as_day_float(value) -> float:
    return float(_days(value))


async def _get_employee_or_404(db: AsyncSession, employee_id: int) -> Employee:
    result = await db.execute(select(Employee).where(Employee.id == employee_id))
    employee = result.scalar_one_or_none()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return employee


async def _backfill_attendance(
    db: AsyncSession,
    employee_id: int,
    start_date: date,
    end_date: date,
) -> int:
    """Create 'present' attendance records for all weekdays from start_date to end_date (exclusive).
    Skips days that already have a record."""
    from datetime import timedelta
    count = 0
    current = start_date
    while current < end_date:
        # Log every day (no weekend skip — attendance tracked daily)
        existing = await db.execute(
            select(Attendance.id).where(
                Attendance.employee_id == employee_id,
                Attendance.date == current,
            )
        )
        if not existing.scalar_one_or_none():
            db.add(Attendance(
                employee_id=employee_id,
                date=current,
                status="present",
                note="Auto-logged from hire date",
            ))
            count += 1
        current += timedelta(days=1)
    if count > 0:
        await db.commit()
    return count


async def _get_attendance_for_day(
    db: AsyncSession,
    employee_id: int,
    attendance_date: date,
) -> Attendance | None:
    result = await db.execute(
        select(Attendance)
        .where(
            Attendance.employee_id == employee_id,
            Attendance.date == attendance_date,
        )
        .order_by(Attendance.id.desc())
    )
    return result.scalars().first()


async def _upsert_attendance_for_day(
    db: AsyncSession,
    employee_id: int,
    attendance_date: date,
    status: str,
    note: str | None = None,
) -> tuple[Attendance, bool]:
    status = _normalize_attendance_status(status)
    existing = await _get_attendance_for_day(db, employee_id, attendance_date)
    if existing:
        existing.status = status
        existing.note = note
        return existing, True

    attendance = Attendance(
        employee_id=employee_id,
        date=attendance_date,
        status=status,
        note=note,
    )
    db.add(attendance)
    await db.flush()
    return attendance, False


async def _get_active_farm_or_404(db: AsyncSession, farm_id: int | None) -> Farm | None:
    if farm_id is None:
        return None
    if farm_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid farm_id")
    result = await db.execute(
        select(Farm).where(Farm.id == farm_id, Farm.is_active == 1)
    )
    farm = result.scalar_one_or_none()
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")
    return farm


def _employee_payload(employee: Employee) -> dict:
    # Every getattr is defensive — if a column or relationship is missing
    # (e.g. mid-migration database state), we degrade gracefully rather
    # than 500-ing the whole list endpoint.
    try:
        farm = getattr(employee, "farm", None)
        farm_name = farm.name if farm else None
    except Exception:
        farm_name = None
    return {
        "id":         getattr(employee, "id", None),
        "name":       getattr(employee, "name", "") or "",
        "phone":      getattr(employee, "phone", None) or "—",
        "position":   getattr(employee, "position", None) or "—",
        "department": getattr(employee, "department", None) or "—",
        "hire_date":  str(employee.hire_date) if getattr(employee, "hire_date", None) else "—",
        "base_salary": float(getattr(employee, "base_salary", 0) or 0),
        "is_active":  bool(getattr(employee, "is_active", True)),
        "farm_id":    getattr(employee, "farm_id", None),
        "farm_name":  farm_name,
        "works_with_animals":         bool(getattr(employee, "works_with_animals", False) or False),
        "vacation_days_per_month":    int(getattr(employee, "vacation_days_per_month", 0) or 0),
        "salary_days_basis":          _salary_basis(employee),
        "food_allowance":             float(getattr(employee, "food_allowance", 0) or 0),
        "transportation_allowance":   float(getattr(employee, "transportation_allowance", 0) or 0),
        "attendance_auto_status":     _normalize_auto_attendance_status(
            getattr(employee, "attendance_auto_status", None)
        ),
    }


SALARY_BASIS_CALENDAR = "calendar"
SALARY_BASIS_FIXED_30 = "fixed_30"
SALARY_BASES = {SALARY_BASIS_CALENDAR, SALARY_BASIS_FIXED_30}


def _salary_basis(employee: Employee) -> str:
    basis = (getattr(employee, "salary_days_basis", None) or SALARY_BASIS_CALENDAR).strip().lower()
    return basis if basis in SALARY_BASES else SALARY_BASIS_CALENDAR


def _rate_divisor(employee: Employee, working_days: int) -> int:
    """Days the monthly salary is divided by to get the daily rate:
    the real month length for 'calendar' deals, a flat 30 for 'fixed_30'."""
    if _salary_basis(employee) == SALARY_BASIS_FIXED_30:
        return 30
    return max(1, int(working_days or 0))


def _paid_days_and_rate(employee: Employee, working_days: int = 30) -> tuple[Decimal, Decimal]:
    """Return (paid_days, daily_rate) for an employee in a month.

    paid_days  = working_days   (the whole month is paid: the monthly paid-leave
                 allowance counts as paid time, so a complete month always earns
                 the exact base salary — whether the leave is taken or not)
    daily_rate = base_salary / rate divisor
                 ('calendar' deals divide by the real month length 28–31;
                  'fixed_30' deals divide by a flat 30, so one docked day is
                  always exactly salary/30 regardless of the month)

    `working_days` is the actual number of days in the payroll month (28-31).
    The salary is computed on the FULL month, so unused vacation days are still
    paid (the employee is present those days) and the total is capped at the
    base salary. Only absences BEYOND the monthly vacation allowance reduce pay,
    by one day's share each. See `_earned_base`.
    """
    safe_working_days = max(1, int(working_days or 0))
    paid_days = _days(safe_working_days)   # the full month is paid
    base_salary = _money(employee.base_salary)
    divisor = _dec(_rate_divisor(employee, safe_working_days))
    daily_rate  = _money(base_salary / divisor) if divisor > 0 else Decimal("0")
    return paid_days, daily_rate


def _earned_base(employee: Employee, days_present, working_days: int = 30,
                 paid_leave_days=0, days_elapsed: int | None = None) -> Decimal:
    """Earned base salary for `days_present` days plus `paid_leave_days` days of
    PAID leave, computed WITHOUT pre-rounding the daily rate.

    Two per-employee deals (``employee.salary_days_basis``):

    'calendar' (default — accrual):
        paid   = min(days_present + paid_leave_days, working_days)
        earned = base_salary * paid / working_days

    'fixed_30' (deduction-based monthly deal):
        Daily rate is a flat base_salary / 30.

        While the month is still in progress, pay ACCRUES per covered day
        (min(covered × salary/30, salary)) — so a few days worked shows a
        few days' pay, never the full month prematurely.

        Once the month is COMPLETE (days_elapsed ≥ working_days, which is
        always true for past months), the deal's deduction rule applies:
        the full monthly salary is owed and each uncovered day of the month
        docks salary/30:

            uncovered = working_days - covered
            earned    = max(0, base_salary - uncovered * base_salary / 30)

        Full attendance in February (28 days) therefore still pays the full
        salary, and one absence always costs exactly salary/30 whether the
        month has 28 or 31 days.

    `paid_leave_days` is supplied by the caller as
    min(leave taken this month, accrued balance as of this month) — see
    `_paid_leave_days_for_period`. So:
      * Paid days off come out of the employee's accrued balance, which carries
        over month to month: bank enough leave and a whole month off is still
        paid in full.
      * Leave taken beyond the available balance is unpaid.
      * A complete month lands on the exact base salary.

    Rounding only the final result keeps a full month exact (e.g. 8000, not
    7999.94 from a pre-rounded daily rate).
    """
    safe_working_days = max(1, int(working_days or 0))
    base_salary = _money(employee.base_salary)
    covered = _dec(days_present) + _dec(paid_leave_days)
    if covered < 0:
        covered = Decimal("0")

    if _salary_basis(employee) == SALARY_BASIS_FIXED_30:
        elapsed = safe_working_days if days_elapsed is None else max(0, int(days_elapsed))
        elapsed = min(elapsed, safe_working_days)
        rate30 = base_salary / Decimal("30")
        if elapsed >= safe_working_days:
            # Month complete → deduction rule: full salary is owed, each
            # uncovered day of the month docks salary/30. Full attendance in
            # February (28 days) therefore still pays the full salary.
            uncovered = _dec(safe_working_days) - min(covered, _dec(safe_working_days))
            earned = base_salary - (rate30 * uncovered)
        else:
            # Month still in progress → accrue what's covered so far at the
            # flat /30 rate, so a few days worked shows a few days' pay
            # (never the full month prematurely).
            earned = min(rate30 * covered, base_salary)
        if earned < 0:
            earned = Decimal("0")
        return _money(earned)

    paid = min(covered, _dec(safe_working_days))
    return _money(base_salary * paid / _dec(safe_working_days))


async def _loan_repaid_amounts(db: AsyncSession, loan_ids: list[int]) -> dict[int, Decimal]:
    if not loan_ids:
        return {}
    result = await db.execute(
        select(
            EmployeeLoanRepayment.loan_id,
            func.coalesce(func.sum(EmployeeLoanRepayment.amount), 0),
        )
        .where(EmployeeLoanRepayment.loan_id.in_(loan_ids))
        .group_by(EmployeeLoanRepayment.loan_id)
    )
    return {loan_id: _money(total) for loan_id, total in result.all()}


def _vacation_months_accrued(employee: Employee, as_of: date | None = None) -> int:
    """Number of monthly leave accruals from hire month through the current
    month (inclusive). Each started employment month grants the monthly
    allowance, and unused days carry over."""
    as_of = as_of or date.today()
    hire = getattr(employee, "hire_date", None)
    if hire is None:
        created = getattr(employee, "created_at", None)
        hire = created.date() if created else None
    if hire is None:
        return 0
    months = (as_of.year - hire.year) * 12 + (as_of.month - hire.month) + 1
    return max(0, months)


async def _employee_vacation_summary(db: AsyncSession, employee: Employee) -> dict:
    """Leave balance: monthly allowance accrued from hire (carried over) plus
    days off credited via partial payroll payments, minus days taken (attendance
    marked 'Day Off', stored as status 'absent')."""
    per_month = max(0, int(getattr(employee, "vacation_days_per_month", 0) or 0))
    months = _vacation_months_accrued(employee)
    accrued = _days(per_month * months)

    _credited = await db.execute(
        select(func.coalesce(func.sum(Payroll.days_off_credited), 0)).where(
            Payroll.employee_id == employee.id
        )
    )
    credited = _days(_credited.scalar() or 0)

    _taken = await db.execute(
        select(func.count(Attendance.id)).where(
            Attendance.employee_id == employee.id,
            Attendance.status == "absent",   # the "Day Off" option is stored as 'absent'
        )
    )
    taken = _days(_taken.scalar() or 0)

    # Floor at zero: an employee can overdraw leave (take more than accrued),
    # but payroll only pays what the balance covers and the remaining balance
    # is never negative — it simply hits zero.
    left = _days(accrued + credited - taken)
    if left < 0:
        left = Decimal("0")
    return {
        "per_month": per_month,
        "months_accrued": months,
        "accrued": _as_day_float(accrued),
        "credited_from_payroll": _as_day_float(credited),
        "taken": _as_day_float(taken),
        "days_left": _as_day_float(left),
    }


async def _vacation_available_as_of(
    db: AsyncSession, employee: Employee, year: int, month: int
) -> Decimal:
    """Paid-leave balance available to cover leave taken IN the given payroll
    month — i.e. the balance as it stood entering that month, including that
    month's own accrual:

        accrued (hire → this month, inclusive)
        + days off credited by partial payments in PRIOR months
        − leave taken in PRIOR months

    Floored at zero. This is an *as-of-month* figure: a payroll run (or re-run)
    for a past month only spends the leave that had actually been banked by
    then, so historical runs stay correct and stable across re-runs.
    """
    per_month = max(0, int(getattr(employee, "vacation_days_per_month", 0) or 0))
    if per_month <= 0:
        return Decimal("0")

    from calendar import monthrange
    as_of = date(year, month, monthrange(year, month)[1])
    months = _vacation_months_accrued(employee, as_of)
    accrued = _days(per_month * months)

    period = f"{year:04d}-{month:02d}"
    month_start = date(year, month, 1)

    _credited = await db.execute(
        select(func.coalesce(func.sum(Payroll.days_off_credited), 0)).where(
            Payroll.employee_id == employee.id,
            Payroll.period < period,            # credited by PRIOR months' payrolls
        )
    )
    credited_before = _days(_credited.scalar() or 0)

    _taken_before = await db.execute(
        select(func.count(Attendance.id)).where(
            Attendance.employee_id == employee.id,
            Attendance.status == "absent",      # the "Day Off" option is stored as 'absent'
            Attendance.date < month_start,      # leave taken in PRIOR months
        )
    )
    taken_before = _days(_taken_before.scalar() or 0)

    available = _days(accrued + credited_before - taken_before)
    return available if available > 0 else Decimal("0")


async def _paid_leave_days_for_period(
    db: AsyncSession, employee: Employee, year: int, month: int
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (paid_leave_days, taken_in_month, available_balance).

    `paid_leave_days` is the number of days off TAKEN this month that the
    employee's accrued balance can actually cover — i.e.
    min(taken_in_month, available_balance). These days are paid as if present;
    any leave taken beyond the available balance is unpaid. Days off are stored
    as attendance status 'absent' (the "Day Off" option).
    """
    available = await _vacation_available_as_of(db, employee, year, month)

    _taken = await db.execute(
        select(func.count(Attendance.id)).where(
            Attendance.employee_id == employee.id,
            Attendance.status == "absent",
            func.extract("year",  Attendance.date) == year,
            func.extract("month", Attendance.date) == month,
        )
    )
    taken_in_month = _days(_taken.scalar() or 0)

    paid_leave = taken_in_month if taken_in_month <= available else available
    return _days(paid_leave), taken_in_month, available


async def _employee_loan_balance(db: AsyncSession, employee_id: int) -> Decimal:
    loans_result = await db.execute(
        select(EmployeeLoan).where(
            EmployeeLoan.employee_id == employee_id,
            EmployeeLoan.status != "cancelled",
        )
    )
    loans = loans_result.scalars().all()
    repaid = await _loan_repaid_amounts(db, [loan.id for loan in loans])
    return _money(sum((_money(loan.amount) - repaid.get(loan.id, Decimal("0"))) for loan in loans))


async def _loan_balance(db: AsyncSession, loan: EmployeeLoan) -> Decimal:
    repaid = await _loan_repaid_amounts(db, [loan.id])
    return _money(_money(loan.amount) - repaid.get(loan.id, Decimal("0")))


async def _employee_loan_balance_for_period(
    db: AsyncSession, employee_id: int, year: int, month: int
) -> Decimal:
    """Outstanding balance of loans *dated in the given month*.

    Payroll deducts a loan in the period that matches its loan_date, so a loan
    recorded in May is recovered by May's payroll run — not by whichever run
    happens to come next.
    """
    loans_result = await db.execute(
        select(EmployeeLoan).where(
            EmployeeLoan.employee_id == employee_id,
            EmployeeLoan.status != "cancelled",
            func.extract("year",  EmployeeLoan.loan_date) == year,
            func.extract("month", EmployeeLoan.loan_date) == month,
        )
    )
    loans = loans_result.scalars().all()
    repaid = await _loan_repaid_amounts(db, [loan.id for loan in loans])
    return _money(sum((_money(loan.amount) - repaid.get(loan.id, Decimal("0"))) for loan in loans))


def _loan_payload(loan: EmployeeLoan, repaid: Decimal) -> dict:
    amount = _money(loan.amount)
    balance = Decimal("0") if loan.status == "cancelled" else _money(amount - repaid)
    return {
        "id": loan.id,
        "employee_id": loan.employee_id,
        "loan_date": loan.loan_date.isoformat(),
        "amount": _as_float(amount),
        "repaid_amount": _as_float(repaid),
        "balance": _as_float(balance),
        "status": loan.status,
        "description": loan.description or "",
        "created_at": str(loan.created_at) if loan.created_at else None,
        "updated_at": str(loan.updated_at) if loan.updated_at else None,
    }


def _deduction_payload(deduction: EmployeePayrollDeduction) -> dict:
    payroll = getattr(deduction, "payroll", None)
    return {
        "id": deduction.id,
        "employee_id": deduction.employee_id,
        "payroll_id": deduction.payroll_id,
        "payroll_period": payroll.period if payroll else deduction.period,
        "period": deduction.period,
        "deduction_date": deduction.deduction_date.isoformat() if deduction.deduction_date else None,
        "type": deduction.type,
        "days": _as_day_float(deduction.days) if deduction.days is not None else None,
        "daily_rate": _as_float(deduction.daily_rate) if deduction.daily_rate is not None else None,
        "amount": _as_float(deduction.amount),
        "note": deduction.note or "",
        "created_at": str(deduction.created_at) if deduction.created_at else None,
    }


async def _update_loan_status(db: AsyncSession, loan: EmployeeLoan) -> Decimal:
    if loan.status == "cancelled":
        return Decimal("0")
    balance = await _loan_balance(db, loan)
    loan.status = "paid" if balance <= 0 else "open"
    return max(balance, Decimal("0"))


async def _apply_loan_repayment_to_oldest_loans(
    db: AsyncSession,
    *,
    employee_id: int,
    amount: Decimal,
    repayment_date: date,
    payroll_id: int | None,
    note: str,
    current_user: User,
    loan_year: int | None = None,
    loan_month: int | None = None,
) -> Decimal:
    amount = _money(amount)
    if amount <= 0:
        return Decimal("0")

    loans_stmt = (
        select(EmployeeLoan)
        .where(EmployeeLoan.employee_id == employee_id, EmployeeLoan.status == "open")
    )
    # When a payroll period is supplied, only repay loans dated in that month so
    # a loan is recovered in its own month rather than the next run.
    if loan_year is not None and loan_month is not None:
        loans_stmt = loans_stmt.where(
            func.extract("year",  EmployeeLoan.loan_date) == loan_year,
            func.extract("month", EmployeeLoan.loan_date) == loan_month,
        )
    loans_stmt = loans_stmt.order_by(EmployeeLoan.loan_date, EmployeeLoan.id)
    loans_result = await db.execute(loans_stmt)
    loans = loans_result.scalars().all()

    # Cap the repayment to what is actually outstanding among these loans.
    outstanding = Decimal("0")
    for loan in loans:
        outstanding += await _loan_balance(db, loan)
    if outstanding <= 0:
        return Decimal("0")
    amount = min(amount, _money(outstanding))

    remaining = amount
    for loan in loans:
        if remaining <= 0:
            break
        loan_balance = await _loan_balance(db, loan)
        if loan_balance <= 0:
            loan.status = "paid"
            continue
        applied = min(remaining, loan_balance)
        db.add(
            EmployeeLoanRepayment(
                loan_id=loan.id,
                employee_id=employee_id,
                payroll_id=payroll_id,
                repayment_date=repayment_date,
                amount=applied,
                note=note,
                created_by_user_id=current_user.id,
            )
        )
        remaining = _money(remaining - applied)
        if _money(loan_balance - applied) <= 0:
            loan.status = "paid"

    return amount


async def _count_records(db: AsyncSession, model) -> int:
    result = await db.execute(select(func.count(model.id)))
    return int(result.scalar() or 0)


async def _remove_journal_balances(db: AsyncSession, journal_ids: list[int]) -> None:
    if not journal_ids:
        return

    entries_result = await db.execute(
        select(JournalEntry.account_id, JournalEntry.debit, JournalEntry.credit)
        .where(JournalEntry.journal_id.in_(journal_ids))
    )
    deltas_by_account = {}
    for account_id, debit, credit in entries_result.all():
        if account_id is None:
            continue
        deltas_by_account[account_id] = deltas_by_account.get(account_id, 0) + (
            (debit or 0) - (credit or 0)
        )

    if not deltas_by_account:
        return

    accounts_result = await db.execute(
        select(Account).where(Account.id.in_(deltas_by_account.keys()))
    )
    for account in accounts_result.scalars().all():
        account.balance = (account.balance or 0) - deltas_by_account.get(account.id, 0)


async def _clear_hr_data(db: AsyncSession, current_user: User) -> dict:
    deleted = {
        "attendance": await _count_records(db, Attendance),
        "payroll": await _count_records(db, Payroll),
        "employees": await _count_records(db, Employee),
        "loans": await _count_records(db, EmployeeLoan),
        "loan_repayments": await _count_records(db, EmployeeLoanRepayment),
        "payroll_deductions": await _count_records(db, EmployeePayrollDeduction),
        "hr_expenses": 0,
    }

    expense_result = await db.execute(
        select(Expense.id, Expense.journal_id).where(Expense.payroll_id.is_not(None))
    )
    hr_expense_rows = expense_result.all()
    hr_expense_ids = [row[0] for row in hr_expense_rows]
    hr_journal_ids = sorted({row[1] for row in hr_expense_rows if row[1] is not None})
    if hr_journal_ids:
        shared_journal_result = await db.execute(
            select(Expense.journal_id)
            .where(
                Expense.journal_id.in_(hr_journal_ids),
                Expense.payroll_id.is_(None),
            )
        )
        shared_journal_ids = {
            journal_id
            for journal_id in shared_journal_result.scalars().all()
            if journal_id is not None
        }
        hr_journal_ids = [
            journal_id for journal_id in hr_journal_ids if journal_id not in shared_journal_ids
        ]
    deleted["hr_expenses"] = len(hr_expense_ids)

    await db.execute(delete(Attendance).execution_options(synchronize_session=False))
    await db.execute(delete(EmployeePayrollDeduction).execution_options(synchronize_session=False))
    await db.execute(delete(EmployeeLoanRepayment).execution_options(synchronize_session=False))
    await db.execute(delete(EmployeeLoan).execution_options(synchronize_session=False))

    await _remove_journal_balances(db, hr_journal_ids)
    if hr_journal_ids:
        await db.execute(
            delete(JournalEntry)
            .where(JournalEntry.journal_id.in_(hr_journal_ids))
            .execution_options(synchronize_session=False)
        )
    if hr_expense_ids:
        await db.execute(
            delete(Expense)
            .where(Expense.id.in_(hr_expense_ids))
            .execution_options(synchronize_session=False)
        )
    if hr_journal_ids:
        await db.execute(
            delete(Journal)
            .where(Journal.id.in_(hr_journal_ids))
            .execution_options(synchronize_session=False)
        )

    await db.execute(delete(Payroll).execution_options(synchronize_session=False))
    await db.execute(delete(Employee).execution_options(synchronize_session=False))
    record(
        db,
        "HR",
        "clear_hr_data",
        (
            "Cleared HR data: "
            f"{deleted['employees']} employees, "
            f"{deleted['attendance']} attendance records, "
            f"{deleted['payroll']} payroll records, "
            f"{deleted['loans']} loans, "
            f"{deleted['loan_repayments']} loan repayments, "
            f"{deleted['payroll_deductions']} payroll deductions, "
            f"{deleted['hr_expenses']} payroll expenses"
        ),
        user=current_user,
        ref_type="hr_clear_data",
        ref_id="all",
    )
    return {"ok": True, "deleted": deleted}


# ── EMPLOYEE API ───────────────────────────────────────
@router.get("/api/employees")
async def get_employees(q: str = "", include_inactive: bool = False, db: AsyncSession = Depends(get_async_session)):
    try:
        stmt = select(Employee).options(selectinload(Employee.farm))
        if not include_inactive:
            stmt = stmt.where(Employee.is_active == True)
        if q:
            stmt = stmt.where(
                Employee.name.ilike(f"%{q}%") |
                Employee.position.ilike(f"%{q}%") |
                Employee.department.ilike(f"%{q}%")
            )
        stmt = stmt.order_by(Employee.name)
        _r = await db.execute(stmt)
        emps = _r.scalars().all()
        return [_employee_payload(e) for e in emps]
    except Exception as exc:
        # Surface DB-schema problems clearly instead of a generic 500. The most
        # common cause is a deploy where the new migration didn't run yet.
        msg = str(exc).lower()
        if "works_with_animals" in msg or "does not exist" in msg or "no such column" in msg:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Database is out of date: please run `alembic upgrade head` on Railway. "
                    "(Missing column on the employees table.)"
                ),
            ) from exc
        raise


@router.get("/api/diag")
async def _diagnose_employees(db: AsyncSession = Depends(get_async_session)):
    """Diagnostic endpoint. Returns raw counts so we can see why the
    employees table appears empty in the UI.

    Use:  GET /hr/api/diag
    """
    total = (await db.execute(select(func.count()).select_from(Employee))).scalar() or 0
    active = (await db.execute(
        select(func.count()).select_from(Employee).where(Employee.is_active == True)
    )).scalar() or 0
    inactive = total - active

    # Sample the most-recent 5 rows (regardless of active flag) so we can see
    # what's actually in there.
    sample_q = (
        select(Employee.id, Employee.name, Employee.is_active, Employee.farm_id)
        .order_by(Employee.id.desc())
        .limit(5)
    )
    sample_rows = (await db.execute(sample_q)).all()
    sample = [
        {"id": r[0], "name": r[1], "is_active": bool(r[2]), "farm_id": r[3]}
        for r in sample_rows
    ]

    # Verify the new column actually exists in the live database (this catches
    # the "migration didn't run" case without throwing a 500).
    has_works_with_animals = True
    works_count = None
    try:
        works_count = (
            await db.execute(
                select(func.count()).select_from(Employee).where(Employee.works_with_animals == True)
            )
        ).scalar() or 0
    except Exception as exc:
        has_works_with_animals = False
        works_count = f"ERROR: {exc}"

    return {
        "total_employees":        total,
        "active_employees":       active,
        "inactive_employees":     inactive,
        "sample_recent":          sample,
        "has_works_with_animals": has_works_with_animals,
        "works_with_animals_yes": works_count,
        "build_marker":           "diag-v2-2026-05-18",
    }


@router.get("/api/farms")
async def list_farms_for_employee_assignment(db: AsyncSession = Depends(get_async_session)):
    """Active farms for the HR employee form's "assigned farm" dropdown.

    Proxied here (gated only by page_hr at the router level) so HR users
    don't also need page_farm just to load the picker.
    """
    result = await db.execute(
        select(Farm).where(Farm.is_active == 1).order_by(Farm.name)
    )
    return [
        {
            "id":       f.id,
            "name":     f.name,
            "location": f.location or "",
        }
        for f in result.scalars().all()
    ]


@router.post("/api/employees", dependencies=[Depends(require_permission("action_hr_manage_employees"))])
async def add_employee(data: EmployeeCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    hire = _parse_optional_iso_date(data.hire_date, "hire_date")
    farm = await _get_active_farm_or_404(db, data.farm_id)
    e = Employee(
        name=data.name, phone=data.phone,
        position=data.position, department=data.department,
        hire_date=hire, base_salary=data.base_salary,
        vacation_days_per_month=max(0, int(data.vacation_days_per_month or 0)),
        salary_days_basis=(data.salary_days_basis if data.salary_days_basis in SALARY_BASES else SALARY_BASIS_CALENDAR),
        food_allowance=max(0, float(data.food_allowance or 0)),
        transportation_allowance=max(0, float(data.transportation_allowance or 0)),
        farm_id=farm.id if farm else None,
        works_with_animals=bool(data.works_with_animals),
    )
    db.add(e); await db.flush()
    record(db, "HR", "add_employee",
           f"Added employee: {e.name} — {e.position or ''} / {e.department or ''} — salary: {float(e.base_salary):.2f}",
           ref_type="employee", ref_id=e.id)
    await db.commit(); await db.refresh(e)

    # ── Auto-log attendance from hire_date to today ──
    if hire and hire < date.today():
        await _backfill_attendance(db, e.id, hire, date.today())

    if farm:
        e.farm = farm
    return _employee_payload(e)

@router.put("/api/employees/{emp_id}", dependencies=[Depends(require_permission("action_hr_manage_employees"))])
async def edit_employee(emp_id: int, data: EmployeeUpdate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(
        select(Employee)
        .options(selectinload(Employee.farm))
        .where(Employee.id == emp_id)
    )
    e = _r.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="Employee not found")
    payload = data.model_dump(exclude_unset=True)
    if "farm_id" in payload:
        farm = await _get_active_farm_or_404(db, payload["farm_id"])
        payload["farm_id"] = farm.id if farm else None
        e.farm = farm
    if "works_with_animals" in payload:
        payload["works_with_animals"] = bool(payload["works_with_animals"])
    if "salary_days_basis" in payload:
        basis = (payload["salary_days_basis"] or "").strip().lower()
        if basis not in SALARY_BASES:
            raise HTTPException(status_code=400, detail="salary_days_basis must be 'calendar' or 'fixed_30'")
        payload["salary_days_basis"] = basis
    for k, v in payload.items():
        setattr(e, k, v)
    record(db, "HR", "edit_employee",
           f"Edited employee: {e.name}",
           ref_type="employee", ref_id=emp_id)
    await db.commit()
    return {"ok": True, **_employee_payload(e)}

@router.delete("/api/employees/{emp_id}", dependencies=[Depends(require_permission("action_hr_manage_employees"))])
async def deactivate_employee(emp_id: int, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(select(Employee).where(Employee.id == emp_id))
    e = _r.scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="Employee not found")
    e.is_active = False
    record(db, "HR", "deactivate_employee",
           f"Deactivated employee: {e.name}",
           ref_type="employee", ref_id=emp_id)
    await db.commit()
    return {"ok": True}


# ── LOANS & DEDUCTIONS API ─────────────────────────────
@router.get("/api/employees/{employee_id}/vacation")
async def get_employee_vacation(employee_id: int, db: AsyncSession = Depends(get_async_session)):
    employee = await _get_employee_or_404(db, employee_id)
    return await _employee_vacation_summary(db, employee)


@router.get("/api/vacation-summary")
async def get_vacation_summary(db: AsyncSession = Depends(get_async_session)):
    """Days-off credit summary for active employees who have a monthly days-off
    allowance (vacation_days_per_month > 0). Used by the Days Off tab."""
    _r = await db.execute(
        select(Employee)
        .where(
            Employee.is_active == True,
            Employee.vacation_days_per_month > 0,
        )
        .order_by(Employee.name)
    )
    employees = _r.scalars().all()
    out = []
    for emp in employees:
        summary = await _employee_vacation_summary(db, emp)
        summary["employee_id"] = emp.id
        summary["employee"] = emp.name
        summary["position"] = emp.position or "—"
        out.append(summary)
    return out


@router.get("/api/employees/{employee_id}/loans", dependencies=[Depends(require_permission("action_hr_view_loans"))])
async def get_employee_loans(employee_id: int, db: AsyncSession = Depends(get_async_session)):
    await _get_employee_or_404(db, employee_id)
    result = await db.execute(
        select(EmployeeLoan)
        .where(EmployeeLoan.employee_id == employee_id)
        .order_by(EmployeeLoan.loan_date.desc(), EmployeeLoan.id.desc())
    )
    loans = result.scalars().all()
    repaid = await _loan_repaid_amounts(db, [loan.id for loan in loans])
    return [_loan_payload(loan, repaid.get(loan.id, Decimal("0"))) for loan in loans]


@router.post("/api/employees/{employee_id}/loans", dependencies=[Depends(require_permission("action_hr_manage_loans"))])
async def create_employee_loan(
    employee_id: int,
    data: EmployeeLoanCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    employee = await _get_employee_or_404(db, employee_id)
    loan_date = _parse_required_iso_date(data.loan_date, "loan_date")
    amount = _money(data.amount)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Loan amount must be greater than 0")
    loan = EmployeeLoan(
        employee_id=employee.id,
        loan_date=loan_date,
        amount=amount,
        description=(data.description or "").strip() or None,
        status="open",
        created_by_user_id=current_user.id,
    )
    db.add(loan)
    await db.flush()
    # Book the loan as a Salaries & Wages expense (cash advanced against salary).
    # Safe vs payroll because payroll books NET salary (repayments already
    # deducted), so this does not double-count over the loan's life.
    await create_loan_advance_expense(db, loan, employee, current_user)
    record(
        db,
        "HR",
        "create_employee_loan",
        f"Created loan for {employee.name}: {amount:.2f}",
        user=current_user,
        ref_type="employee_loan",
        ref_id=loan.id,
    )
    await db.commit()
    await db.refresh(loan)
    return {"ok": True, **_loan_payload(loan, Decimal("0"))}


@router.get("/api/loans/backfill-expenses", dependencies=[Depends(require_permission("action_hr_manage_loans"))])
async def backfill_loan_expenses(
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """One-time backfill: book the Salaries & Wages expense for loans created
    before loan->expense recording existed, so older loans appear in the Season
    Analysis / cost reports. Idempotent — loans that already have an expense
    (matched by the [loan:<id>] marker) are skipped, so it is safe to run more
    than once. Cancelled loans are skipped (they no longer deduct from salary)."""
    result = await db.execute(
        select(EmployeeLoan).where(EmployeeLoan.status != "cancelled")
    )
    loans = result.scalars().all()
    created = 0
    already = 0
    retagged = 0
    without_farm = 0
    for loan in loans:
        employee = await db.get(Employee, loan.employee_id)
        if employee is None:
            continue
        emp_farm = getattr(employee, "farm_id", None) or None
        existing = await db.execute(
            select(Expense).where(Expense.description.like(f"%[loan:{loan.id}]%"))
        )
        existing_expense = existing.scalar_one_or_none()
        if existing_expense is not None:
            already += 1
            # Employee may have been assigned a farm after the expense was first
            # booked untagged — sync the tag so it shows in that farm's analysis.
            if existing_expense.farm_id != emp_farm:
                existing_expense.farm_id = emp_farm
                retagged += 1
            if emp_farm is None:
                without_farm += 1
            continue
        expense = await create_loan_advance_expense(db, loan, employee, current_user)
        if expense is None:
            continue
        created += 1
        if emp_farm is None:
            without_farm += 1
    await db.commit()
    return {
        "ok": True,
        "loans_scanned": len(loans),
        "expenses_created": created,
        "already_existed": already,
        "retagged_to_farm": retagged,
        "created_without_farm_tag": without_farm,
        "note": (
            "Loans on employees without a farm don't appear in a single-farm "
            "Season Analysis (no farm tag), but do show on the Expenses page "
            "and under the Shared Org Costs toggle. Assign those employees a "
            "farm and run this again to re-tag their loan expenses."
        ),
    }


@router.post("/api/loans/{loan_id}/repayments", dependencies=[Depends(require_permission("action_hr_manage_loans"))])
async def create_loan_repayment(
    loan_id: int,
    data: LoanRepaymentCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(EmployeeLoan).where(EmployeeLoan.id == loan_id))
    loan = result.scalar_one_or_none()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    if loan.status == "cancelled":
        raise HTTPException(status_code=400, detail="Cannot repay a cancelled loan")
    repayment_date = _parse_required_iso_date(data.repayment_date, "repayment_date")
    amount = _money(data.amount)
    balance = await _loan_balance(db, loan)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Repayment amount must be greater than 0")
    # No upper cap — allow overpayment; the loan will just close
    repayment = EmployeeLoanRepayment(
        loan_id=loan.id,
        employee_id=loan.employee_id,
        repayment_date=repayment_date,
        amount=amount,
        note=(data.note or "").strip() or None,
        created_by_user_id=current_user.id,
    )
    db.add(repayment)
    await db.flush()
    balance = await _update_loan_status(db, loan)
    record(
        db,
        "HR",
        "create_loan_repayment",
        f"Recorded repayment for loan #{loan.id}: {amount:.2f}",
        user=current_user,
        ref_type="employee_loan",
        ref_id=loan.id,
    )
    await db.commit()
    return {
        "ok": True,
        "id": repayment.id,
        "loan_id": loan.id,
        "employee_id": loan.employee_id,
        "amount": _as_float(amount),
        "balance": _as_float(balance),
        "status": loan.status,
    }


@router.post("/api/loans/{loan_id}/cancel", dependencies=[Depends(require_permission("action_hr_manage_loans"))])
async def cancel_employee_loan(
    loan_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(EmployeeLoan).where(EmployeeLoan.id == loan_id))
    loan = result.scalar_one_or_none()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    repayments_result = await db.execute(
        select(func.count(EmployeeLoanRepayment.id)).where(EmployeeLoanRepayment.loan_id == loan.id)
    )
    repayment_count = repayments_result.scalar() or 0
    if repayment_count and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can cancel loans with repayments")
    loan.status = "cancelled"
    record(
        db,
        "HR",
        "cancel_employee_loan",
        f"Cancelled loan #{loan.id}",
        user=current_user,
        ref_type="employee_loan",
        ref_id=loan.id,
    )
    await db.commit()
    return {"ok": True, "loan_id": loan.id, "status": loan.status}


@router.delete("/api/loans/{loan_id}", dependencies=[Depends(require_permission("action_hr_delete_loans"))])
async def delete_employee_loan(
    loan_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(EmployeeLoan).where(EmployeeLoan.id == loan_id))
    loan = result.scalar_one_or_none()
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    loan_amount = _money(loan.amount)

    # Gather this loan's repayments and total how much each payroll run deducted
    # for it, so we can reverse those that haven't been paid out yet.
    _reps = await db.execute(
        select(EmployeeLoanRepayment).where(EmployeeLoanRepayment.loan_id == loan.id)
    )
    repayments = _reps.scalars().all()
    reverse_by_payroll: dict[int, Decimal] = {}
    for rep in repayments:
        if rep.payroll_id:
            reverse_by_payroll[rep.payroll_id] = (
                reverse_by_payroll.get(rep.payroll_id, Decimal("0")) + _money(rep.amount)
            )

    reversed_total = Decimal("0")
    skipped_paid = Decimal("0")
    for payroll_id, amount in reverse_by_payroll.items():
        _p = await db.execute(select(Payroll).where(Payroll.id == payroll_id))
        payroll = _p.scalar_one_or_none()
        if payroll is None:
            continue
        if payroll.paid:
            # The money was already withheld and paid out — leave the paid
            # record exactly as it was; we only drop the repayment link below.
            skipped_paid += amount
            continue
        # Unpaid run: give the deduction back so the loan that no longer exists
        # isn't withheld at payout time.
        give_back = min(amount, _money(payroll.loan_deductions))
        payroll.loan_deductions = _money(_money(payroll.loan_deductions) - give_back)
        payroll.deductions = _money(_money(payroll.deductions) - give_back)
        payroll.net_salary = _money(_money(payroll.net_salary) + give_back)
        reversed_total += give_back
        # Reduce/remove the matching aggregate loan_repayment deduction line(s)
        _lines = await db.execute(
            select(EmployeePayrollDeduction)
            .where(
                EmployeePayrollDeduction.payroll_id == payroll_id,
                EmployeePayrollDeduction.type == "loan_repayment",
            )
            .order_by(EmployeePayrollDeduction.id)
        )
        remaining = give_back
        for line in _lines.scalars().all():
            if remaining <= 0:
                break
            line_amount = _money(line.amount)
            if line_amount <= remaining:
                remaining -= line_amount
                await db.delete(line)
            else:
                line.amount = _money(line_amount - remaining)
                remaining = Decimal("0")

    # Remove the loan's repayments (FK requires this before the loan), then the loan.
    await db.execute(
        delete(EmployeeLoanRepayment).where(EmployeeLoanRepayment.loan_id == loan.id)
    )
    detail = f"Deleted loan #{loan.id} ({loan_amount:.2f}) for employee #{loan.employee_id}"
    if reversed_total > 0:
        detail += f"; reversed {reversed_total:.2f} from unpaid payroll runs"
    if skipped_paid > 0:
        detail += f"; left {skipped_paid:.2f} on already-paid runs unchanged"
    record(
        db,
        "HR",
        "delete_employee_loan",
        detail,
        user=current_user,
        ref_type="employee_loan",
        ref_id=loan.id,
    )
    # Reverse the Salaries & Wages expense booked when the loan was created,
    # so deleting a loan doesn't leave labour cost overstated.
    await reverse_loan_advance_expense(db, loan, current_user)
    await db.delete(loan)
    await db.commit()
    return {
        "ok": True,
        "loan_id": loan_id,
        "deleted": True,
        "reversed_from_unpaid": _as_float(reversed_total),
        "left_on_paid": _as_float(skipped_paid),
    }


# ── ALLOWANCE ADVANCES API ─────────────────────────────
@router.get("/api/employees/{employee_id}/allowance-advances")
async def get_allowance_advances(employee_id: int, db: AsyncSession = Depends(get_async_session)):
    await _get_employee_or_404(db, employee_id)
    result = await db.execute(
        select(EmployeeAllowanceAdvance)
        .where(EmployeeAllowanceAdvance.employee_id == employee_id)
        .order_by(EmployeeAllowanceAdvance.advance_date.desc(), EmployeeAllowanceAdvance.id.desc())
    )
    advances = result.scalars().all()
    return [_allowance_advance_payload(a) for a in advances]


@router.post("/api/employees/{employee_id}/allowance-advances", dependencies=[Depends(require_permission("action_hr_manage_allowances"))])
async def create_allowance_advance(
    employee_id: int,
    data: AllowanceAdvanceCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    employee = await _get_employee_or_404(db, employee_id)
    advance_date = _parse_required_iso_date(data.advance_date, "advance_date")
    amount = _money(data.amount)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")
    advance = EmployeeAllowanceAdvance(
        employee_id=employee.id,
        advance_date=advance_date,
        amount=amount,
        note=(data.note or "").strip() or None,
        status="open",
        created_by_user_id=current_user.id,
    )
    db.add(advance)
    await db.flush()
    record(db, "HR", "create_allowance_advance",
           f"Allowance advance for {employee.name}: {amount:.2f}",
           user=current_user, ref_type="allowance_advance", ref_id=advance.id)
    await db.commit()
    await db.refresh(advance)
    return {"ok": True, **_allowance_advance_payload(advance)}


@router.post("/api/allowance-advances/{advance_id}/cancel", dependencies=[Depends(require_permission("action_hr_manage_allowances"))])
async def cancel_allowance_advance(
    advance_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(EmployeeAllowanceAdvance).where(EmployeeAllowanceAdvance.id == advance_id))
    advance = result.scalar_one_or_none()
    if not advance:
        raise HTTPException(status_code=404, detail="Advance not found")
    if advance.status == "deducted":
        raise HTTPException(status_code=400, detail="Cannot cancel an already deducted advance")
    advance.status = "cancelled"
    await db.commit()
    return {"ok": True}


def _allowance_advance_payload(advance: EmployeeAllowanceAdvance) -> dict:
    return {
        "id": advance.id,
        "employee_id": advance.employee_id,
        "advance_date": advance.advance_date.isoformat(),
        "amount": _as_float(advance.amount),
        "note": advance.note or "",
        "status": advance.status,
        "payroll_id": advance.payroll_id,
        "created_at": str(advance.created_at) if advance.created_at else None,
    }


@router.get("/api/employees/{employee_id}/deductions", dependencies=[Depends(require_permission("action_hr_view_deductions"))])
async def get_employee_deductions(employee_id: int, db: AsyncSession = Depends(get_async_session)):
    await _get_employee_or_404(db, employee_id)
    result = await db.execute(
        select(EmployeePayrollDeduction)
        .options(selectinload(EmployeePayrollDeduction.payroll))
        .where(EmployeePayrollDeduction.employee_id == employee_id)
        .order_by(EmployeePayrollDeduction.created_at.desc(), EmployeePayrollDeduction.id.desc())
    )
    return [_deduction_payload(deduction) for deduction in result.scalars().all()]


@router.post("/api/employees/{employee_id}/deductions/day", dependencies=[Depends(require_permission("action_hr_manage_deductions"))])
async def create_day_deduction(
    employee_id: int,
    data: DayDeductionCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    from calendar import monthrange
    employee = await _get_employee_or_404(db, employee_id)
    period = _validate_period(data.period)
    deduction_date = _parse_required_iso_date(data.deduction_date, "deduction_date")
    days = _days(data.days)
    base_salary = _money(employee.base_salary)
    if base_salary <= 0:
        raise HTTPException(status_code=400, detail="Employee base salary must be greater than 0")
    if days <= 0:
        raise HTTPException(status_code=400, detail="Deduction days must be greater than 0")
    # Auto-calculate working days from the period calendar
    year, month = int(period.split("-")[0]), int(period.split("-")[1])
    total_days = monthrange(year, month)[1]
    working_days = _days(total_days)   # all days in month, no weekend skip
    # Use the same daily rate the payroll uses (base / (working_days - vacation))
    # so docking one day equals exactly one day's pay.
    _paid_days, daily_rate = _paid_days_and_rate(employee, total_days)
    amount = _money(daily_rate * days)
    deduction = EmployeePayrollDeduction(
        employee_id=employee.id,
        period=period,
        deduction_date=deduction_date,
        type="day_deduction",
        days=days,
        daily_rate=daily_rate,
        amount=amount,
        note=(data.note or "").strip() or None,
        created_by_user_id=current_user.id,
    )
    db.add(deduction)
    await db.flush()
    record(
        db,
        "HR",
        "create_day_deduction",
        f"Created {days} day deduction for {employee.name}: {amount:.2f}",
        user=current_user,
        ref_type="employee_payroll_deduction",
        ref_id=deduction.id,
    )
    await db.commit()
    await db.refresh(deduction)
    return {"ok": True, **_deduction_payload(deduction)}


@router.post("/api/employees/{employee_id}/deductions/manual", dependencies=[Depends(require_permission("action_hr_manage_deductions"))])
async def create_manual_deduction(
    employee_id: int,
    data: ManualDeductionCreate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    employee = await _get_employee_or_404(db, employee_id)
    period = _validate_period(data.period)
    amount = _money(data.amount)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Deduction amount must be greater than 0")
    deduction = EmployeePayrollDeduction(
        employee_id=employee.id,
        period=period,
        type="manual",
        amount=amount,
        note=(data.note or "").strip() or None,
        created_by_user_id=current_user.id,
    )
    db.add(deduction)
    await db.flush()
    record(
        db,
        "HR",
        "create_manual_deduction",
        f"Created manual deduction for {employee.name}: {amount:.2f}",
        user=current_user,
        ref_type="employee_payroll_deduction",
        ref_id=deduction.id,
    )
    await db.commit()
    await db.refresh(deduction)
    return {"ok": True, **_deduction_payload(deduction)}


async def _pending_deductions_for_period(
    db: AsyncSession,
    employee_id: int,
    period: str,
) -> tuple[list[EmployeePayrollDeduction], Decimal, Decimal, Decimal]:
    result = await db.execute(
        select(EmployeePayrollDeduction)
        .where(
            EmployeePayrollDeduction.employee_id == employee_id,
            EmployeePayrollDeduction.period == period,
            EmployeePayrollDeduction.payroll_id.is_(None),
            EmployeePayrollDeduction.type.in_(["day_deduction", "manual"]),
        )
        .order_by(EmployeePayrollDeduction.deduction_date, EmployeePayrollDeduction.id)
    )
    deductions = result.scalars().all()
    day_deduction_days = _days(
        sum((_dec(item.days) for item in deductions if item.type == "day_deduction"), Decimal("0"))
    )
    day_deductions = _money(
        sum((_dec(item.amount) for item in deductions if item.type == "day_deduction"), Decimal("0"))
    )
    manual_deductions = _money(
        sum((_dec(item.amount) for item in deductions if item.type == "manual"), Decimal("0"))
    )
    return deductions, day_deduction_days, day_deductions, manual_deductions


async def _open_allowance_advances(
    db: AsyncSession,
    employee_id: int,
) -> tuple[list[EmployeeAllowanceAdvance], Decimal]:
    """Open (not-yet-settled) allowance advances for an employee, and their total.

    Advances are paid out early against the monthly food/transport allowance, so
    the next payroll run subtracts them from the allowance payout and marks them
    'deducted'. They are not tied to a period — any open advance is settled by the
    next run that processes this employee.
    """
    result = await db.execute(
        select(EmployeeAllowanceAdvance)
        .where(
            EmployeeAllowanceAdvance.employee_id == employee_id,
            EmployeeAllowanceAdvance.status == "open",
        )
        .order_by(EmployeeAllowanceAdvance.advance_date, EmployeeAllowanceAdvance.id)
    )
    advances = result.scalars().all()
    total = _money(sum((_dec(a.amount) for a in advances), Decimal("0")))
    return advances, total


async def _payroll_preview_for_employee(
    db: AsyncSession,
    employee: Employee,
    *,
    period: str,
    working_days: int,
    days_elapsed: int,
    year: int,
    month: int,
    include_loans: bool,
    include_deductions: bool,
) -> dict:
    _dp = await db.execute(select(func.count(Attendance.id)).where(
        Attendance.employee_id == employee.id,
        Attendance.status == "present",
        func.extract("year",  Attendance.date) == year,
        func.extract("month", Attendance.date) == month,
    ))
    days_present = _dp.scalar() or 0

    _ar = await db.execute(select(Payroll).where(
        Payroll.employee_id == employee.id,
        Payroll.period == period,
    ))
    already_run = _ar.scalar_one_or_none() is not None

    base_salary  = _money(employee.base_salary)
    food_all     = _money(getattr(employee, "food_allowance", 0) or 0)
    trans_all    = _money(getattr(employee, "transportation_allowance", 0) or 0)
    # Food: prorated by attendance (daily_rate × days_present)
    food_daily   = _money(food_all / Decimal(str(working_days))) if working_days > 0 else Decimal("0")
    earned_food  = _money(food_daily * _dec(days_present))
    # Transport: full monthly (not prorated)
    earned_allowance = _money(earned_food + trans_all)
    # Open allowance advances (paid early) are recovered from this month's
    # allowance payout, capped so the allowance line can't go negative. Any
    # remainder beyond the allowance is carried by the still-open advances.
    _, open_advance_total = await _open_allowance_advances(db, employee.id)
    allowance_advance_applied = _money(min(open_advance_total, earned_allowance)) if include_deductions else Decimal("0")
    earned_allowance = _money(earned_allowance - allowance_advance_applied)
    total_allowance  = earned_allowance
    vacation     = max(0, int(getattr(employee, "vacation_days_per_month", 0) or 0))
    paid_days, daily_rate = _paid_days_and_rate(employee, working_days)

    # Paid leave = days off taken this month that the employee's accrued balance
    # (carried over month to month, as of this month) can cover. These count as
    # present for pay; leave beyond the balance is unpaid.
    paid_leave_days, taken_leave_month, vacation_available = await _paid_leave_days_for_period(
        db, employee, year, month
    )

    # Earned salary = base_salary × min(days_present + paid_leave_days,
    # working_days) / working_days. The month is paid up to the base salary:
    # leave drawn from the accrued balance is paid (so a fully-banked month off
    # is still full pay) and only leave beyond the balance reduces pay. Computed
    # without pre-rounding the daily rate so a full month equals the exact base
    # salary. Allowance (food) is prorated by attendance separately.
    earned_base  = _earned_base(employee, days_present, working_days, paid_leave_days, days_elapsed=days_elapsed)
    earned_total = _money(earned_base + earned_allowance)

    pending_day_days = Decimal("0")
    pending_day_amount = Decimal("0")
    pending_manual_amount = Decimal("0")
    if include_deductions:
        _, pending_day_days, pending_day_amount, pending_manual_amount = await _pending_deductions_for_period(
            db,
            employee.id,
            period,
        )
    outstanding_loan_balance = await _employee_loan_balance_for_period(db, employee.id, year, month) if include_loans else None
    total_pending = _money(pending_day_amount + pending_manual_amount)
    net_before_loan = _money(earned_total - total_pending)
    return {
        "employee_id": employee.id,
        "employee": employee.name,
        "position": employee.position or "—",
        "base_salary": _as_float(base_salary),
        "food_allowance": _as_float(food_all),
        "transportation_allowance": _as_float(trans_all),
        "vacation_days": vacation,
        "paid_days": _as_day_float(paid_days),
        "vacation_paid_days": _as_day_float(paid_leave_days),
        "vacation_taken_month": _as_day_float(taken_leave_month),
        "vacation_available": _as_day_float(vacation_available),
        "working_days": working_days,
        "days_elapsed": days_elapsed,
        "days_present": days_present,
        "days_absent": days_elapsed - days_present,
        "daily_rate": _as_float(daily_rate),
        "earned_base": _as_float(earned_base),
        "total_allowance": _as_float(total_allowance),
        "food_allowance": _as_float(food_all),
        "transportation_allowance": _as_float(trans_all),
        "earned": _as_float(earned_total),
        "already_run": already_run,
        "outstanding_loan_balance": _as_float(outstanding_loan_balance) if outstanding_loan_balance is not None else None,
        "pending_day_deduction_days": _as_day_float(pending_day_days),
        "pending_day_deductions": _as_float(pending_day_amount),
        "pending_manual_deductions": _as_float(pending_manual_amount),
        "pending_total_deductions": _as_float(total_pending),
        "allowance_advance_applied": _as_float(allowance_advance_applied),
        "net_before_loan": _as_float(net_before_loan),
    }


# ── ATTENDANCE API ─────────────────────────────────────
@router.get("/api/attendance")
async def get_attendance(emp_id: int = None, period: str = None, db: AsyncSession = Depends(get_async_session)):
    stmt = select(Attendance).options(selectinload(Attendance.employee))
    if emp_id:
        stmt = stmt.where(Attendance.employee_id == emp_id)
    if period:
        # period like "2025-01"
        year, month = period.split("-")
        stmt = stmt.where(
            func.extract("year",  Attendance.date) == int(year),
            func.extract("month", Attendance.date) == int(month),
        )
    stmt = stmt.order_by(Attendance.date.desc(), Attendance.employee_id)
    # When a month and/or employee is selected the result set is naturally
    # bounded (employees × days in the month), so return all of it — capping
    # here would silently drop the earliest days of the month for businesses
    # with several employees. Only the unfiltered "recent activity" view keeps
    # a small cap.
    if period or emp_id:
        stmt = stmt.limit(10000)
    else:
        stmt = stmt.limit(200)
    _r = await db.execute(stmt)
    records = _r.scalars().all()
    return [
        {
            "id":          r.id,
            "employee_id": r.employee_id,
            "employee":    r.employee.name if r.employee else "—",
            "date":        str(r.date),
            "status":      r.status,
            "note":        r.note or "",
        }
        for r in records
    ]

@router.post("/api/attendance", dependencies=[Depends(require_permission("action_hr_log_attendance"))])
async def log_attendance(data: AttendanceCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    attendance_date = date.fromisoformat(data.date)
    status = _normalize_attendance_status(data.status)
    employee = await _get_employee_or_404(db, data.employee_id)
    attendance, updated = await _upsert_attendance_for_day(
        db,
        data.employee_id,
        attendance_date,
        status,
        data.note,
    )
    if attendance_date == date.today() and status in ATTENDANCE_AUTO_STATUSES:
        employee.attendance_auto_status = status
    await db.commit()
    return {"id": attendance.id, "updated": updated}


class AttendanceUpdate(BaseModel):
    status: str
    note: Optional[str] = None


@router.put("/api/attendance/{att_id}", dependencies=[Depends(require_permission("action_hr_edit_attendance"))])
async def edit_attendance(
    att_id: int,
    data: AttendanceUpdate,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    _r = await db.execute(select(Attendance).where(Attendance.id == att_id))
    att = _r.scalar_one_or_none()
    if not att:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    att.status = _normalize_attendance_status(data.status)
    if data.note is not None:
        att.note = data.note.strip() or None
    record(db, "HR", "edit_attendance",
           f"Edited attendance #{att.id}: {att.status} on {att.date}",
           user=current_user, ref_type="attendance", ref_id=att.id)
    await db.commit()
    return {"ok": True, "id": att.id, "status": att.status}


@router.delete("/api/employees/{emp_id}/deductions/{deduction_id}", dependencies=[Depends(require_permission("action_hr_manage_deductions"))])
async def delete_employee_deduction(
    emp_id: int,
    deduction_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    _r = await db.execute(
        select(EmployeePayrollDeduction).where(
            EmployeePayrollDeduction.id == deduction_id,
            EmployeePayrollDeduction.employee_id == emp_id,
        )
    )
    deduction = _r.scalar_one_or_none()
    if not deduction:
        raise HTTPException(status_code=404, detail="Deduction not found")
    # Only pending deductions can be removed. Once a deduction is locked into a
    # saved payroll run, deleting it here would desync that payroll's totals.
    if deduction.payroll_id is not None:
        raise HTTPException(
            status_code=400,
            detail="This deduction is already applied to a payroll run and can't be deleted.",
        )
    if deduction.type == "loan_repayment":
        raise HTTPException(
            status_code=400,
            detail="Loan repayments are managed from the loan, not here.",
        )
    record(db, "HR", "delete_deduction",
           f"Deleted pending {deduction.type} deduction #{deduction.id} "
           f"({deduction.period}): {_money(deduction.amount):.2f}",
           user=current_user, ref_type="employee_payroll_deduction", ref_id=deduction.id)
    await db.delete(deduction)
    await db.commit()
    return {"ok": True}


@router.delete("/api/attendance/{att_id}", dependencies=[Depends(require_permission("action_hr_edit_attendance"))])
async def delete_attendance(
    att_id: int,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    _r = await db.execute(select(Attendance).where(Attendance.id == att_id))
    att = _r.scalar_one_or_none()
    if not att:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    record(db, "HR", "delete_attendance",
           f"Deleted attendance #{att.id}: {att.status} on {att.date}",
           user=current_user, ref_type="attendance", ref_id=att.id)
    await db.delete(att)
    await db.commit()
    return {"ok": True}


# Maximum number of days back that auto-today will fill in. This bounds the
# work done when an install has been dormant for a long time, while still
# comfortably covering the previous full month (so gaps from, e.g., May are
# filled when the page is next opened in June).
AUTO_ATTENDANCE_BACKFILL_DAYS = 92


async def _auto_fill_missing_days(
    db: AsyncSession,
    employee: Employee,
    start_date: date,
    end_date: date,
) -> int:
    """Fill missing attendance for [start_date, end_date] inclusive using the
    employee's persistent auto status. Skips any day that already has a record
    so manual edits and explicit absences are never overwritten."""
    from datetime import timedelta
    if start_date > end_date:
        return 0
    status = _normalize_auto_attendance_status(
        getattr(employee, "attendance_auto_status", None)
    )
    _existing = await db.execute(
        select(Attendance.date).where(
            Attendance.employee_id == employee.id,
            Attendance.date >= start_date,
            Attendance.date <= end_date,
        )
    )
    existing_dates = {row[0] for row in _existing.all()}
    created = 0
    current = start_date
    while current <= end_date:
        if current not in existing_dates:
            db.add(Attendance(employee_id=employee.id, date=current, status=status))
            created += 1
        current += timedelta(days=1)
    return created


@router.post("/api/attendance/auto-today", dependencies=[Depends(require_permission("action_hr_log_attendance"))])
async def auto_mark_today(db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    """Auto-log all active employees up to today using their persistent
    attendance mode.

    This fills *every* missing day within a bounded recent window (up to
    today) using each employee's persistent attendance mode — not just today —
    so days on which nobody opened the HR page no longer leave gaps in the
    month. Existing records are never overwritten — only genuinely-missing
    days are created — so manual edits and explicit absences are preserved.
    Days before an employee's hire date are skipped. The window is bounded by
    AUTO_ATTENDANCE_BACKFILL_DAYS.
    """
    from datetime import timedelta
    today = date.today()
    earliest = today - timedelta(days=AUTO_ATTENDANCE_BACKFILL_DAYS)

    _r = await db.execute(select(Employee).where(Employee.is_active == True))
    employees = _r.scalars().all()
    created = 0
    present = 0
    absent = 0
    for emp in employees:
        status = _normalize_auto_attendance_status(getattr(emp, "attendance_auto_status", None))
        # Fill the whole window, skipping days that already have a record. We
        # scan the full window (not just days after the latest record) because
        # gaps can sit *before* the most recent entry — e.g. today's row may
        # exist while early-month days are still missing. Never fill days
        # before the employee's hire date.
        start = earliest
        hire = getattr(emp, "hire_date", None)
        if hire and hire > start:
            start = hire
        n = await _auto_fill_missing_days(db, emp, start, today)
        created += n
        if status == ATTENDANCE_STATUS_ABSENT:
            absent += n
        else:
            present += n
    await db.commit()
    return {
        "ok": True,
        "created": created,
        "present": present,
        "absent": absent,
        "date": str(today),
    }

@router.post("/api/attendance/mark-absent", dependencies=[Depends(require_permission("action_hr_log_attendance"))])
async def mark_absent_today(data: AttendanceCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    """Keep an employee absent every day until they are manually marked present."""
    today = date.today()
    employee = await _get_employee_or_404(db, data.employee_id)
    employee.attendance_auto_status = ATTENDANCE_STATUS_ABSENT
    attendance, updated = await _upsert_attendance_for_day(
        db,
        data.employee_id,
        today,
        ATTENDANCE_STATUS_ABSENT,
        data.note,
    )
    await db.commit()
    return {
        "id": attendance.id,
        "updated": updated,
        "auto_status": employee.attendance_auto_status,
    }

@router.post("/api/attendance/mark-present", dependencies=[Depends(require_permission("action_hr_log_attendance"))])
async def mark_present_today(data: AttendanceCreate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    """Return an employee to the default auto-present mode."""
    today = date.today()
    employee = await _get_employee_or_404(db, data.employee_id)
    employee.attendance_auto_status = ATTENDANCE_STATUS_PRESENT
    attendance, updated = await _upsert_attendance_for_day(
        db,
        data.employee_id,
        today,
        ATTENDANCE_STATUS_PRESENT,
        data.note,
    )
    await db.commit()
    return {
        "id": attendance.id,
        "updated": updated,
        "auto_status": employee.attendance_auto_status,
    }


# ── PAYROLL API ────────────────────────────────────────
@router.get("/api/payroll")
async def get_payroll(period: str = None, db: AsyncSession = Depends(get_async_session)):
    stmt = select(Payroll).options(selectinload(Payroll.employee).selectinload(Employee.farm))
    if period:
        stmt = stmt.where(Payroll.period == period)
    stmt = stmt.order_by(Payroll.period.desc(), Payroll.id)
    _r = await db.execute(stmt)
    records = _r.scalars().all()
    return [
        {
            "id":          r.id,
            "employee_id": r.employee_id,
            "employee":    r.employee.name if r.employee else "—",
            "farm_id":     r.employee.farm_id if r.employee else None,
            "farm_name":   r.employee.farm.name if r.employee and r.employee.farm else None,
            "period":      r.period,
            "base_salary": float(r.base_salary) if r.base_salary else 0,
            "days_worked": r.days_worked or 0,
            "working_days":r.working_days or 0,
            "bonuses":     float(r.bonuses)     if r.bonuses     else 0,
            "deductions":  float(r.deductions)  if r.deductions  else 0,
            "loan_deductions": float(r.loan_deductions) if getattr(r, "loan_deductions", None) else 0,
            "day_deduction_days": float(r.day_deduction_days) if getattr(r, "day_deduction_days", None) else 0,
            "day_deductions": float(r.day_deductions) if getattr(r, "day_deductions", None) else 0,
            "manual_deductions": float(r.manual_deductions) if getattr(r, "manual_deductions", None) else 0,
            "net_salary":  float(r.net_salary)  if r.net_salary  else 0,
            "paid":        r.paid,
            "paid_at":     str(r.paid_at) if r.paid_at else None,
            "paid_amount": float(r.paid_amount) if getattr(r, "paid_amount", None) is not None else None,
            "days_off_credited": float(r.days_off_credited) if getattr(r, "days_off_credited", None) else 0,
            "daily_rate":  _as_float(_paid_days_and_rate(r.employee, int(r.working_days or 30) or 30)[1]) if r.employee else 0,
        }
        for r in records
    ]

@router.get("/api/payroll/preview")
async def preview_payroll(
    period: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """
    Preview payroll for a period without saving.
    Calculates each employee's salary based on days worked.
    period format: "2026-04"
    """
    from calendar import monthrange
    period = _validate_period(period)
    year, month = int(period.split("-")[0]), int(period.split("-")[1])
    # Total working days in month (Mon-Fri)
    total_days   = monthrange(year, month)[1]
    working_days = total_days   # all days in month
    # Days so far this month (up to today)
    today = date.today()
    if today.year == year and today.month == month:
        days_elapsed = today.day
    else:
        days_elapsed = working_days

    _r = await db.execute(select(Employee).where(Employee.is_active == True))
    employees = _r.scalars().all()
    result = []
    total_to_pay = 0
    include_loans = has_permission(current_user, "action_hr_view_loans")
    include_deductions = has_permission(current_user, "action_hr_view_deductions")
    for emp in employees:
        row = await _payroll_preview_for_employee(
            db,
            emp,
            period=period,
            working_days=working_days,
            days_elapsed=days_elapsed,
            year=year,
            month=month,
            include_loans=include_loans,
            include_deductions=include_deductions,
        )
        total_to_pay += row["net_before_loan"]
        result.append(row)
    return {
        "period":       period,
        "working_days": working_days,
        "days_elapsed": days_elapsed,
        "employees":    result,
        "total_to_pay": round(total_to_pay, 2),
        "can_view_loans": include_loans,
        "can_view_deductions": include_deductions,
    }

class PayrollResetPeriod(BaseModel):
    period: str
    confirm: str


@router.post("/api/payroll/reset-period", dependencies=[Depends(require_permission("action_hr_reset_payroll"))])
async def reset_payroll_period(data: PayrollResetPeriod, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    """Delete ALL payroll runs for one period (paid and unpaid) and unwind
    every side effect they created, so the month can be re-run cleanly:

      • paid runs: the linked salary expense is deleted, its journal reversed
        and its carbon log removed;
      • loan repayments taken by these runs are deleted and any loan they
        fully settled is reopened;
      • allowance advances settled by these runs go back to 'open';
      • day/manual deductions for this period are DELETED — both ones the
        runs applied and still-pending ones — so a re-run starts clean;
      • days-off credits from partial payments disappear with the rows, so
        vacation balances correct themselves automatically.

    Attendance is NOT touched. Requires typing the period to confirm.
    """
    period = _validate_period(data.period)
    if (data.confirm or "").strip() != period:
        raise HTTPException(status_code=400, detail=f"Type the period exactly ({period}) to confirm the reset")

    runs = (await db.execute(select(Payroll).where(Payroll.period == period))).scalars().all()
    if not runs:
        return {"ok": True, "period": period, "deleted_runs": 0, "deleted_expenses": 0,
                "deleted_repayments": 0, "reopened_loans": 0, "reopened_advances": 0,
                "deleted_deductions": 0}
    run_ids = [r.id for r in runs]

    # Step-tagged so a production failure names the exact stage in the error
    # and the server log carries the full traceback.
    import logging
    from sqlalchemy import update as _sql_update, delete as _sql_delete
    _log = logging.getLogger("hr.reset_payroll")
    step = "start"
    try:
        # 1) Paid runs → delete their salary expenses (journal + carbon unwound).
        step = "unwinding paid salary expenses"
        from app.routers.expenses import _reverse_expense_journal
        from app.models.expense import Expense
        from app.models.carbon import CarbonLog
        from app.models.accounting import Journal, JournalEntry
        _e = await db.execute(
            select(Expense).options(selectinload(Expense.category)).where(Expense.payroll_id.in_(run_ids))
        )
        expenses = _e.scalars().all()
        orig_journal_ids = []
        for exp in expenses:
            # Post the accounting reversal (restores account balances)...
            await _reverse_expense_journal(db, exp)
            if exp.journal_id:
                orig_journal_ids.append(exp.journal_id)
            _cl = await db.execute(
                select(CarbonLog).where(CarbonLog.ref_type == "expense", CarbonLog.ref_id == exp.id)
            )
            for cl in _cl.scalars().all():
                await db.delete(cl)
            await db.delete(exp)
        # Flush the expense deletes FIRST so nothing references the original
        # journals, THEN delete those journals and their entries.
        await db.flush()
        if orig_journal_ids:
            await db.execute(
                _sql_delete(JournalEntry).where(JournalEntry.journal_id.in_(orig_journal_ids))
            )
            await db.execute(
                _sql_delete(Journal).where(Journal.id.in_(orig_journal_ids))
            )
            await db.flush()

        # 2) Loan repayments taken by these runs → delete, then reopen any loan
        #    that no longer has a zero balance.
        step = "removing loan repayments"
        _r = await db.execute(
            select(EmployeeLoanRepayment).where(EmployeeLoanRepayment.payroll_id.in_(run_ids))
        )
        repayments = _r.scalars().all()
        loan_ids = {r.loan_id for r in repayments}
        for rep in repayments:
            await db.delete(rep)
        await db.flush()
        step = "reopening settled loans"
        reopened_loans = 0
        if loan_ids:
            _l = await db.execute(select(EmployeeLoan).where(EmployeeLoan.id.in_(loan_ids)))
            for loan in _l.scalars().all():
                # A loan that these runs had fully repaid ('paid') and that now
                # has an outstanding balance again is reopened. The status CHECK
                # constraint (ck_employee_loans_status) only permits
                # 'open' / 'paid' / 'cancelled' — 'open' is the active state.
                if loan.status == "paid" and (await _loan_balance(db, loan)) > 0:
                    loan.status = "open"
                    reopened_loans += 1

        # 3) Advances referencing these runs → unlink them ALL (any status —
        #    e.g. a cancelled advance keeps its payroll_id and would violate
        #    the FK when the run is deleted). Only the ones the runs actually
        #    settled ('deducted') go back to open; cancelled stays cancelled.
        step = "reopening allowance advances"
        _a = await db.execute(
            select(EmployeeAllowanceAdvance).where(
                EmployeeAllowanceAdvance.payroll_id.in_(run_ids)
            )
        )
        advances_all = _a.scalars().all()
        reopened_advances = 0
        for adv in advances_all:
            if adv.status == "deducted":
                adv.status = "open"
                reopened_advances += 1
            adv.payroll_id = None

        # 4) Deductions belonging to this period → deleted. This covers both
        #    deductions the deleted runs had applied AND still-pending ones
        #    added for this month, so a re-run starts with a clean slate.
        step = "deleting period deductions"
        _d = await db.execute(
            select(EmployeePayrollDeduction).where(
                (EmployeePayrollDeduction.payroll_id.in_(run_ids))
                | (EmployeePayrollDeduction.period == period)
            )
        )
        deductions = _d.scalars().all()
        for ded in deductions:
            await db.delete(ded)

        # 5) Defensive sweep: NULL every remaining payroll_id reference at the
        #    SQL level before deleting the runs. The ORM steps above handle the
        #    business logic (reopening loans/advances, deleting deductions), but
        #    a bulk UPDATE guarantees no stray FK — from a row not loaded into
        #    the session, an unexpected status, or identity-map staleness —
        #    survives to block the delete. Idempotent and cheap.
        step = "clearing payroll references"
        from app.models.expense import Expense as _Exp
        await db.execute(
            _sql_update(EmployeeLoanRepayment)
            .where(EmployeeLoanRepayment.payroll_id.in_(run_ids))
            .values(payroll_id=None)
        )
        await db.execute(
            _sql_update(EmployeeAllowanceAdvance)
            .where(EmployeeAllowanceAdvance.payroll_id.in_(run_ids))
            .values(payroll_id=None)
        )
        await db.execute(
            _sql_update(EmployeePayrollDeduction)
            .where(EmployeePayrollDeduction.payroll_id.in_(run_ids))
            .values(payroll_id=None)
        )
        # Any expense still linked (e.g. one whose journal reversal we skipped
        # for some reason) — clear the link so the run can be deleted rather
        # than 500. Belt-and-suspenders alongside the deletions in step 1.
        await db.execute(
            _sql_update(_Exp).where(_Exp.payroll_id.in_(run_ids)).values(payroll_id=None)
        )
        await db.flush()

        # 6) The runs themselves.
        step = "deleting payroll runs"
        for run in runs:
            await db.delete(run)

        step = "writing audit log"
        record(db, "HR", "reset_payroll_period",
               f"Reset payroll period {period}: {len(runs)} runs deleted "
               f"({len(expenses)} expenses unwound, {len(repayments)} loan repayments removed, "
               f"{reopened_loans} loans reopened, {reopened_advances} advances reopened, "
               f"{len(deductions)} deductions deleted)",
               user=current_user, ref_type="payroll_period", ref_id=None)
        step = "committing"
        await db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        _log.exception("reset_payroll_period failed while %s (period=%s)", step, period)
        # Surface the underlying DB detail (constraint/table) so the exact
        # blocker is visible in the toast, not just "IntegrityError".
        root = getattr(exc, "orig", None) or exc
        detail_msg = str(getattr(root, "args", [root])[0] if getattr(root, "args", None) else root)
        detail_msg = " ".join(detail_msg.split())[:300]
        raise HTTPException(
            status_code=500,
            detail=f"Reset failed while {step}: {type(exc).__name__} — {detail_msg}. Nothing was changed.",
        )
    return {"ok": True, "period": period, "deleted_runs": len(runs),
            "deleted_expenses": len(expenses), "deleted_repayments": len(repayments),
            "reopened_loans": reopened_loans, "reopened_advances": reopened_advances,
            "deleted_deductions": len(deductions)}


@router.post("/api/payroll/run", dependencies=[Depends(require_permission("action_hr_run_payroll"))])
async def run_payroll(data: PayrollRun, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    from calendar import monthrange
    period = _validate_period(data.period)
    year, month = int(period.split("-")[0]), int(period.split("-")[1])
    total_days   = monthrange(year, month)[1]
    working_days = total_days   # all days in month
    # Days of the payroll month that have actually passed — bounds the
    # fixed-30 deduction so running payroll mid-month never docks days that
    # haven't happened yet. Past months: the whole month has elapsed.
    _today = date.today()
    if (_today.year, _today.month) == (year, month):
        days_elapsed = _today.day
    elif (_today.year, _today.month) > (year, month):
        days_elapsed = working_days
    else:
        days_elapsed = 0
    # Date used to stamp payroll-driven repayments/deductions. Use the last day
    # of the *selected* payroll month so loan repayments land in the correct
    # accounting month, regardless of which day payroll is actually run on.
    period_date = date(year, month, total_days)

    emp_stmt = select(Employee).where(Employee.is_active == True)
    if data.emp_ids:
        emp_stmt = emp_stmt.where(Employee.id.in_(data.emp_ids))
    _re = await db.execute(emp_stmt)
    employees = _re.scalars().all()

    bonus_by_employee = data.bonuses or {}
    loan_repayment_by_employee = data.loan_repayments or {}
    created = 0
    skipped = 0
    payroll_ids = []
    try:
        for emp in employees:
            _ex = await db.execute(select(Payroll).where(
                Payroll.employee_id == emp.id,
                Payroll.period == period,
            ))
            payroll = _ex.scalar_one_or_none()

            _dp = await db.execute(select(func.count(Attendance.id)).where(
                Attendance.employee_id == emp.id,
                Attendance.status == "present",
                func.extract("year",  Attendance.date) == year,
                func.extract("month", Attendance.date) == month,
            ))
            days_present = _dp.scalar() or 0

            pending_deductions, pending_day_days, pending_day_amount, pending_manual_amount = await _pending_deductions_for_period(
                db,
                emp.id,
                period,
            )
            requested_loan_repayment = _money(loan_repayment_by_employee.get(emp.id, Decimal("0")))
            bonus_amount = _money(bonus_by_employee.get(emp.id, Decimal("0")))

            if payroll:
                skipped += 1
                is_new_payroll = False
                existing_loan_deductions = _money(getattr(payroll, "loan_deductions", 0))
                existing_day_days = _days(getattr(payroll, "day_deduction_days", 0))
                existing_day_deductions = _money(getattr(payroll, "day_deductions", 0))
                existing_manual_deductions = _money(getattr(payroll, "manual_deductions", 0))
                if emp.id not in bonus_by_employee:
                    bonus_amount = _money(payroll.bonuses)
            else:
                payroll = Payroll(
                    employee_id=emp.id,
                    period=period,
                    paid=False,
                )
                db.add(payroll)
                created += 1
                is_new_payroll = True
                existing_loan_deductions = Decimal("0")
                existing_day_days = Decimal("0")
                existing_day_deductions = Decimal("0")
                existing_manual_deductions = Decimal("0")

            paid_days_val, daily_rate_val = _paid_days_and_rate(emp, working_days)
            # Paid leave = days off taken this month that the accrued balance
            # (carried over, as of this month) can cover. Counts as present for
            # pay; leave beyond the balance is unpaid. Mirrors preview.
            paid_leave_days, _taken_leave_month, _vac_available = await _paid_leave_days_for_period(
                db, emp, year, month
            )
            # Earned = base_salary × min(days_present + paid_leave_days,
            # working_days) / working_days. Month is paid up to base salary;
            # leave drawn from the balance is paid (a fully-banked month off is
            # still full pay) and only leave beyond the balance reduces pay.
            # Computed without pre-rounding the daily rate so a full month equals
            # the exact base salary. Mirrors preview.
            earned_base = _earned_base(emp, days_present, working_days, paid_leave_days, days_elapsed=days_elapsed)

            # Allowances — mirror the preview logic exactly:
            #   Food allowance      → prorated by attendance (daily rate x days present)
            #   Transport allowance → paid in full regardless of attendance
            food_all         = _money(getattr(emp, "food_allowance", 0) or 0)
            trans_all        = _money(getattr(emp, "transportation_allowance", 0) or 0)
            food_daily       = _money(food_all / Decimal(str(working_days))) if working_days > 0 else Decimal("0")
            earned_food      = _money(food_daily * _dec(days_present))
            earned_allowance = _money(earned_food + trans_all)  # transport always in full

            # Recover open allowance advances from this month's allowance payout,
            # capped so the allowance can't go negative.
            open_advances, open_advance_total = await _open_allowance_advances(db, emp.id)
            if is_new_payroll:
                allowance_advance_applied = _money(min(open_advance_total, earned_allowance))
            else:
                # Re-run of an existing period: advances were already settled on
                # the first run and are no longer 'open'. Re-apply the amount that
                # was settled against THIS payroll so the recomputed net matches.
                _settled_q = await db.execute(
                    select(func.coalesce(func.sum(EmployeeAllowanceAdvance.amount), 0))
                    .where(
                        EmployeeAllowanceAdvance.employee_id == emp.id,
                        EmployeeAllowanceAdvance.status == "deducted",
                        EmployeeAllowanceAdvance.payroll_id == payroll.id,
                    )
                )
                allowance_advance_applied = _money(_settled_q.scalar() or 0)
            earned_allowance = _money(earned_allowance - allowance_advance_applied)

            payroll.base_salary = earned_base     # attendance-based salary only
            # Bonus = whatever the user entered in the run form (no auto-allowance injection)
            payroll.bonuses = bonus_amount
            payroll.days_worked = days_present
            payroll.working_days = working_days
            payroll.day_deduction_days = _days(existing_day_days + pending_day_days)
            payroll.day_deductions = _money(existing_day_deductions + pending_day_amount)
            payroll.manual_deductions = _money(existing_manual_deductions + pending_manual_amount)
            payroll.loan_deductions = _money(existing_loan_deductions + requested_loan_repayment)
            payroll.deductions = _money(
                payroll.loan_deductions + payroll.day_deductions + payroll.manual_deductions
            )
            # net_salary includes allowances: food (prorated) + transport (full)
            payroll.net_salary = _money(payroll.base_salary + payroll.bonuses + earned_allowance - payroll.deductions)
            await db.flush()
            payroll_ids.append(payroll.id)

            for deduction in pending_deductions:
                deduction.payroll_id = payroll.id

            # Settle allowance advances we actually recovered this run, oldest
            # first. An advance is only marked 'deducted' once fully covered;
            # a partially-covered advance is split so the remainder stays open
            # for the next run (rare — only when the allowance can't cover it).
            remaining_to_settle = allowance_advance_applied
            for adv in open_advances:
                if remaining_to_settle <= 0:
                    break
                adv_amount = _money(adv.amount)
                if adv_amount <= remaining_to_settle:
                    adv.status = "deducted"
                    adv.payroll_id = payroll.id
                    remaining_to_settle = _money(remaining_to_settle - adv_amount)
                else:
                    # Partial recovery: settle the covered portion, leave the rest open.
                    covered = remaining_to_settle
                    adv.amount = _money(adv_amount - covered)  # remaining still owed
                    db.add(EmployeeAllowanceAdvance(
                        employee_id=emp.id,
                        advance_date=adv.advance_date,
                        amount=covered,
                        note=(adv.note or "") + " (partial settlement)",
                        status="deducted",
                        payroll_id=payroll.id,
                        created_by_user_id=getattr(current_user, "id", None),
                    ))
                    remaining_to_settle = Decimal("0")

            if requested_loan_repayment > 0:
                applied = await _apply_loan_repayment_to_oldest_loans(
                    db,
                    employee_id=emp.id,
                    amount=requested_loan_repayment,
                    repayment_date=period_date,
                    payroll_id=payroll.id,
                    note=f"Payroll loan repayment - {period}",
                    current_user=current_user,
                    loan_year=year,
                    loan_month=month,
                )
                db.add(
                    EmployeePayrollDeduction(
                        employee_id=emp.id,
                        payroll_id=payroll.id,
                        period=period,
                        deduction_date=period_date,
                        type="loan_repayment",
                        amount=applied,
                        note=f"Payroll loan repayment - {period}",
                        created_by_user_id=current_user.id,
                    )
                )

        record(db, "HR", "run_payroll",
               f"Payroll run for {period} — {created} created, {skipped} updated",
               user=current_user, ref_type="payroll", ref_id=period)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Could not run payroll. No payroll changes were saved.") from exc
    return {"created": created, "skipped": skipped, "period": period, "payroll_ids": payroll_ids}


@router.put("/api/payroll/{payroll_id}", dependencies=[Depends(require_permission("action_hr_run_payroll"))])
async def update_payroll(payroll_id: int, data: PayrollUpdate, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(select(Payroll).where(Payroll.id == payroll_id))
    p = _r.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")
    p.bonuses = _money(data.bonuses)
    p.manual_deductions = _money(data.deductions)
    p.deductions = _money(
        _dec(getattr(p, "loan_deductions", 0))
        + _dec(getattr(p, "day_deductions", 0))
        + p.manual_deductions
    )
    p.net_salary = _money(_dec(p.base_salary) + p.bonuses - p.deductions)
    if data.notes and p.manual_deductions > 0:
        db.add(
            EmployeePayrollDeduction(
                employee_id=p.employee_id,
                payroll_id=p.id,
                period=p.period,
                type="manual",
                amount=p.manual_deductions,
                note=data.notes,
                created_by_user_id=current_user.id,
            )
        )
    record(db, "HR", "update_payroll",
           f"Updated payroll #{payroll_id} — bonuses: {p.bonuses}, deductions: {p.deductions}, net: {p.net_salary:.2f}",
           user=current_user, ref_type="payroll", ref_id=payroll_id)
    await db.commit()
    return {"ok": True, "net_salary": float(p.net_salary)}

@router.patch("/api/payroll/{payroll_id}/pay", dependencies=[Depends(require_permission("action_hr_mark_paid"))])
async def mark_paid(payroll_id: int, data: Optional[PayrollPayRequest] = None, db: AsyncSession = Depends(get_async_session), current_user: User = Depends(get_current_user)):
    _r = await db.execute(
        select(Payroll)
        .options(selectinload(Payroll.employee).selectinload(Employee.farm))
        .where(Payroll.id == payroll_id)
    )
    p = _r.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")
    if p.paid:
        raise HTTPException(status_code=400, detail="This payroll is already marked paid.")

    now = datetime.now(timezone.utc)
    payment_method = (data.payment_method if data else "cash") or "cash"

    net = _money(p.net_salary or 0)
    # How much cash is actually being paid out
    if data and data.paid_amount is not None:
        paid_amount = _money(data.paid_amount)
    else:
        paid_amount = net
    if paid_amount <= 0:
        raise HTTPException(status_code=400, detail="Payment amount must be greater than 0.")
    if paid_amount > net:
        raise HTTPException(status_code=400, detail="Payment can't exceed the net salary.")

    remaining = _money(net - paid_amount)
    days_off_credited = Decimal("0")
    if data and data.convert_remainder_to_days_off and remaining > 0:
        # Convert the unpaid remainder into paid days off using the same daily
        # rate the payroll uses, and credit them to the employee's balance.
        employee_for_rate = p.employee
        working_days = int(p.working_days or 30) or 30
        _paid_days, daily_rate = _paid_days_and_rate(employee_for_rate, working_days)
        if daily_rate > 0:
            days_off_credited = (remaining / daily_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    expense = await create_payroll_expense(
        db,
        p,
        current_user,
        payment_method=payment_method,
        paid_date=now.date(),
        amount_override=float(paid_amount),
    )
    p.paid    = True
    p.paid_at = now
    p.paid_amount = paid_amount
    p.days_off_credited = days_off_credited
    pay_detail = f"Marked payroll #{payroll_id} as paid — net: {float(net):.2f}, cash: {float(paid_amount):.2f}"
    if days_off_credited > 0:
        pay_detail += f", credited {float(days_off_credited):.2f} days off"
    record(db, "HR", "mark_payroll_paid",
           pay_detail,
           user=current_user, ref_type="payroll", ref_id=payroll_id)
    await db.commit()
    employee = p.employee
    employee_farm = employee.farm if employee else None
    expense_farm = getattr(expense, "farm", None)
    response = {
        "ok": True,
        "payroll_id": p.id,
        "expense_id": expense.id,
        "expense_ref_number": expense.ref_number,
        "category": expense.category.name if expense.category else "Salaries & Wages",
        "farm_id": expense.farm_id,
        "farm_name": expense_farm.name if expense_farm else (employee_farm.name if employee_farm and expense.farm_id == employee_farm.id else None),
        "amount": float(expense.amount),
        "paid_amount": _as_float(paid_amount),
        "days_off_credited": _as_float(days_off_credited),
    }
    if expense.farm_id is None:
        response["warning"] = "Employee has no farm assigned, so salary expense was not linked to a farm."
    return response


@router.post("/clear-data", dependencies=[Depends(require_permission("action_hr_clear_data"))])
async def clear_hr_data(
    data: Optional[ClearHRDataRequest] = None,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    if data is None or data.confirmation != CLEAR_HR_DATA_CONFIRMATION:
        raise HTTPException(status_code=400, detail='Type "CLEAR HR DATA" to confirm.')

    try:
        result = await _clear_hr_data(db, current_user)
        await db.commit()
        return result
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Could not clear HR data. No records were deleted.",
        ) from exc


@router.get("/api/summary")
async def hr_summary(db: AsyncSession = Depends(get_async_session)):
    from calendar import monthrange
    _te = await db.execute(select(func.count(Employee.id)).where(Employee.is_active == True))
    total_employees = _te.scalar() or 0
    today = date.today()
    _pt = await db.execute(select(func.count(Attendance.id)).where(
        Attendance.date == today,
        Attendance.status == "present",
    ))
    present_today = _pt.scalar() or 0
    _at = await db.execute(select(func.count(Attendance.id)).where(
        Attendance.date == today,
        Attendance.status == "absent",
    ))
    absent_today = _at.scalar() or 0
    _ts = await db.execute(select(func.sum(Employee.base_salary)).where(Employee.is_active == True))
    total_salary = _ts.scalar() or 0

    # to_pay_today: sum of daily_rate for each present employee today
    year, month = today.year, today.month
    total_days = monthrange(year, month)[1]
    working_days = total_days   # all days in month

    _present_emps = await db.execute(
        select(Employee)
        .join(Attendance, (Attendance.employee_id == Employee.id) & (Attendance.date == today) & (Attendance.status == "present"))
        .where(Employee.is_active == True)
    )
    present_employees = _present_emps.scalars().all()
    # to_pay_today = salary daily rate + food daily rate (transport is full monthly, not daily)
    to_pay_today = sum(
        float(
            _paid_days_and_rate(emp, working_days)[1]
            + (
                _money(_money(getattr(emp, "food_allowance", 0) or 0) / Decimal(str(working_days)))
                if working_days > 0 else Decimal("0")
            )
        )
        for emp in present_employees
    )

    return {
        "total_employees": total_employees,
        "present_today":   present_today,
        "absent_today":    absent_today,
        "total_salary":    float(total_salary),
        "to_pay_today":    round(to_pay_today, 2),
        "working_days":    working_days,
    }


# ── UI ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def hr_ui(current_user: User = Depends(require_permission("page_hr"))):
    html_content = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HR & Payroll — AZed Farm</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {
    --bg:      #060810;
    --surface: #0a0d18;
    --card:    #0f1424;
    --card2:   #151c30;
    --border:  rgba(255,255,255,0.06);
    --border2: rgba(255,255,255,0.11);
    --green:   #00ff9d;
    --blue:    #4d9fff;
    --purple:  #a855f7;
    --danger:  #ff4d6d;
    --warn:    #ffb547;
    --text:    #f0f4ff;
    --sub:     #8899bb;
    --muted:   #445066;
    --sans:    'Outfit', sans-serif;
    --mono:    'JetBrains Mono', monospace;
    --r:       12px;
}
body.light{
    --bg:#f4f5ef;--surface:#f1f3eb;--card:#eceee6;--card2:#e4e6de;
    --border:rgba(0,0,0,0.08);--border2:rgba(0,0,0,0.14);
    --green:#0f8a43;
    --text:#1a1e14;--sub:#4a5040;--muted:#7b816f;
}
body.light nav{background:rgba(244,245,239,.92);}
body.light .nav-link:hover{background:rgba(0,0,0,.05);}
body.light tr:hover td{background:rgba(0,0,0,.03);}
.mode-btn{display:flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:10px;border:1px solid var(--border);background:var(--card);color:var(--sub);font-size:16px;cursor:pointer;transition:all .2s;font-family:var(--sans);}
.mode-btn:hover{border-color:var(--border2);transform:scale(1.06);}
.topbar-right{display:flex;align-items:center;gap:12px;}
.account-menu{position:relative;}
.user-pill{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--border);border-radius:40px;padding:7px 16px 7px 10px;cursor:pointer;transition:all .2s;}
.user-pill:hover,.user-pill.open{border-color:var(--border2);}
.user-avatar{width:28px;height:28px;background:linear-gradient(135deg,#7ecb6f,#d4a256);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#0a0c08;}
.user-name{font-size:13px;font-weight:500;color:var(--sub);}
.menu-caret{font-size:11px;color:var(--muted);}
.account-dropdown{position:absolute;right:0;top:calc(100% + 10px);min-width:220px;background:var(--card);border:1px solid var(--border2);border-radius:14px;padding:8px;box-shadow:0 24px 50px rgba(0,0,0,.35);display:none;z-index:500;}
.account-dropdown.open{display:block;}
.account-head{padding:10px 12px 8px;border-bottom:1px solid var(--border);margin-bottom:6px;}
.account-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;}
.account-email{font-size:12px;color:var(--sub);margin-top:4px;word-break:break-word;}
.account-item{width:100%;display:flex;align-items:center;gap:10px;padding:10px 12px;border:none;background:transparent;border-radius:10px;color:var(--sub);font-family:var(--sans);font-size:13px;text-decoration:none;cursor:pointer;text-align:left;}
.account-item:hover{background:var(--card2);color:var(--text);}
.account-item.danger:hover{color:#c97a7a;}
.logout-btn{background:transparent;border:1px solid var(--border);color:var(--muted);font-family:var(--sans);font-size:12px;font-weight:500;padding:8px 16px;border-radius:8px;cursor:pointer;transition:all .2s;letter-spacing:.3px;}
.logout-btn:hover{border-color:#c97a7a;color:#c97a7a;}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--sans); background: var(--bg); color: var(--text); min-height: 100vh; font-size: 14px; }
nav {
    position: sticky; top: 0; z-index: 100;
    display: flex; align-items: center; gap: 10px;
    padding: 0 24px; height: 58px;
    background: rgba(10,13,24,.92); backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border);
}
.logo {
    font-size: 18px; font-weight: 900;
    background: linear-gradient(135deg, var(--green), var(--blue));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text; margin-right: 12px;
}
.nav-link { padding: 7px 14px; border-radius: 8px; color: var(--sub); font-size: 13px; font-weight: 600; text-decoration: none; transition: all .2s; }
.nav-link:hover { background: rgba(255,255,255,.05); color: var(--text); }
.nav-link.active { background: rgba(0,255,157,.1); color: var(--green); }
.nav-spacer { flex: 1; }
.content { max-width: 1300px; margin: 0 auto; padding: 28px 24px; display: flex; flex-direction: column; gap: 20px; }
.page-title { font-size: 24px; font-weight: 800; letter-spacing: -.5px; }
.page-sub   { color: var(--muted); font-size: 13px; margin-top: 3px; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 14px; }
.stat-card { background: var(--card); border: 1px solid var(--border); border-radius: var(--r); padding: 18px 20px; display: flex; flex-direction: column; gap: 8px; position: relative; overflow: hidden; }
.stat-card::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; }
.stat-card.green::before  { background: linear-gradient(90deg,var(--green),transparent); }
.stat-card.blue::before   { background: linear-gradient(90deg,var(--blue),transparent); }
.stat-card.warn::before   { background: linear-gradient(90deg,var(--warn),transparent); }
.stat-card.purple::before { background: linear-gradient(90deg,var(--purple),transparent); }
.stat-label { font-size: 10px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: var(--muted); }
.stat-value { font-family: var(--mono); font-size: 28px; font-weight: 700; }
.stat-value.green  { color: var(--green); }
.stat-value.blue   { color: var(--blue); }
.stat-value.warn   { color: var(--warn); }
.stat-value.purple { color: var(--purple); }
.tabs { display: flex; gap: 4px; background: var(--card); border: 1px solid var(--border); border-radius: var(--r); padding: 4px; width: fit-content; }
.tab { padding: 8px 20px; border-radius: 9px; font-size: 13px; font-weight: 700; cursor: pointer; border: none; background: transparent; color: var(--muted); transition: all .2s; font-family: var(--sans); }
.tab.active { background: var(--card2); color: var(--text); }
.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.search-box { display: flex; align-items: center; gap: 9px; background: var(--card); border: 1px solid var(--border); border-radius: var(--r); padding: 0 14px; flex: 1; min-width: 200px; transition: border-color .2s; }
.search-box:focus-within { border-color: rgba(0,255,157,.3); }
.search-box svg { color: var(--muted); flex-shrink: 0; }
.search-box input { background: transparent; border: none; outline: none; color: var(--text); font-family: var(--sans); font-size: 14px; padding: 11px 0; width: 100%; }
.search-box input::placeholder { color: var(--muted); }
.btn { display: flex; align-items: center; gap: 7px; padding: 10px 16px; border-radius: var(--r); font-family: var(--sans); font-size: 13px; font-weight: 700; cursor: pointer; border: none; transition: all .2s; white-space: nowrap; }
.btn-green  { background: linear-gradient(135deg,var(--green),#00d4ff); color: #021a10; }
.btn-green:hover { filter: brightness(1.1); transform: translateY(-1px); }
.btn-blue   { background: linear-gradient(135deg,var(--blue),var(--purple)); color: white; }
.btn-blue:hover { filter: brightness(1.1); transform: translateY(-1px); }
.btn-purple { background: linear-gradient(135deg,var(--purple),#e879f9); color: white; }
.btn-purple:hover { filter: brightness(1.1); transform: translateY(-1px); }
.btn-danger { background: linear-gradient(135deg,var(--danger),#ef4444); color: white; }
.btn-danger:hover { filter: brightness(1.08); transform: translateY(-1px); }
.btn-danger:disabled { opacity: .45; cursor: not-allowed; filter: none; transform: none; }
.table-wrap { background: var(--card); border: 1px solid var(--border); border-radius: var(--r); overflow: hidden; }
table { width: 100%; border-collapse: collapse; }
thead { background: var(--card2); }
th { text-align: left; font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); padding: 12px 16px; }
td { padding: 12px 16px; border-top: 1px solid var(--border); color: var(--sub); font-size: 13px; }
tr:hover td { background: rgba(255,255,255,.02); }
td.name { color: var(--text); font-weight: 600; }
td.mono { font-family: var(--mono); color: var(--green); }
.action-btn { background: transparent; border: 1px solid var(--border2); color: var(--sub); font-size: 12px; font-weight: 600; padding: 5px 10px; border-radius: 7px; cursor: pointer; transition: all .15s; font-family: var(--sans); }
.action-btn:hover { border-color: var(--blue); color: var(--blue); }
.action-btn.danger:hover { border-color: var(--danger); color: var(--danger); }
.action-btn.green:hover  { border-color: var(--green); color: var(--green); }
.action-btn.purple:hover { border-color: var(--purple); color: var(--purple); }
.status-present { color: var(--green); font-size: 12px; font-weight: 700; }
.status-absent  { color: var(--danger); font-size: 12px; font-weight: 700; }
.status-late    { color: var(--warn); font-size: 12px; font-weight: 700; }
.status-leave   { color: var(--blue); font-size: 12px; font-weight: 700; }
.paid-badge   { display:inline-flex;align-items:center;gap:4px;background:rgba(0,255,157,.1);border:1px solid rgba(0,255,157,.2);color:var(--green);font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px; }
.unpaid-badge { display:inline-flex;align-items:center;gap:4px;background:rgba(255,181,71,.1);border:1px solid rgba(255,181,71,.2);color:var(--warn);font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px; }
.modal-bg { position: fixed; inset: 0; z-index: 500; background: rgba(0,0,0,.7); backdrop-filter: blur(4px); display: none; align-items: center; justify-content: center; }
.modal-bg.open { display: flex; }
.modal { background: var(--card); border: 1px solid var(--border2); border-radius: 16px; padding: 28px; width: 500px; max-width: 95vw; max-height: 90vh; overflow-y: auto; animation: modalIn .2s ease; }
.modal.wide { width: 980px; }
@keyframes modalIn { from{opacity:0;transform:scale(.95)} to{opacity:1;transform:scale(1)} }
.modal-title { font-size: 18px; font-weight: 800; margin-bottom: 20px; }
.modal-title.danger { color: var(--danger); }
.danger-note { border:1px solid rgba(255,77,109,.28); background:rgba(255,77,109,.08); border-radius:12px; padding:12px 14px; color:var(--sub); font-size:13px; line-height:1.45; margin-bottom:16px; }
.confirm-token { font-family:var(--mono); color:var(--danger); font-weight:800; }
.hr-ledger-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.ld-tab{background:none;border:none;border-bottom:2px solid transparent;padding:8px 16px;font-size:13px;font-weight:600;color:var(--muted);cursor:pointer;margin-bottom:-1px;transition:color .15s,border-color .15s}
.ld-tab.active{color:var(--text);border-bottom-color:var(--green)}
.day-quick{background:var(--card2);border:1px solid var(--border2);border-radius:6px;padding:4px 8px;font-size:12px;cursor:pointer;color:var(--text)}
.day-quick:hover{background:var(--border2)}
.hr-ledger-panel { border:1px solid var(--border); border-radius:12px; padding:14px; }
.hr-ledger-title { font-size:11px; font-weight:800; letter-spacing:1.3px; text-transform:uppercase; color:var(--muted); margin-bottom:12px; }
.hr-ledger-table { max-height:220px; overflow:auto; border:1px solid var(--border); border-radius:10px; }
.hr-ledger-table th,.hr-ledger-table td { padding:8px 10px; font-size:12px; }
.money-preview { font-family:var(--mono); color:var(--green); font-weight:800; }
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.fld { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
.fld.span2 { grid-column: span 2; }
.fld label { font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); }
.fld input, .fld select { background: var(--card2); border: 1px solid var(--border2); border-radius: 10px; padding: 10px 12px; color: var(--text); font-family: var(--sans); font-size: 14px; outline: none; transition: border-color .2s; width: 100%; }
.fld input:focus, .fld select:focus { border-color: rgba(0,255,157,.4); }
.modal-actions { display: flex; gap: 10px; margin-top: 6px; justify-content: flex-end; }
.btn-cancel { background: transparent; border: 1px solid var(--border2); color: var(--sub); padding: 10px 18px; border-radius: var(--r); font-family: var(--sans); font-size: 13px; font-weight: 700; cursor: pointer; }
.btn-cancel:hover { border-color: var(--danger); color: var(--danger); }
.toast { position: fixed; bottom: 22px; left: 50%; transform: translateX(-50%) translateY(16px); background: var(--card2); border: 1px solid var(--border2); border-radius: var(--r); padding: 12px 20px; font-size: 13px; font-weight: 600; color: var(--text); box-shadow: 0 20px 50px rgba(0,0,0,.5); opacity: 0; pointer-events: none; transition: opacity .25s, transform .25s; z-index: 999; }
.toast.show { opacity:1; transform: translateX(-50%) translateY(0); }
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 4px; }
</style>
    <script src="/static/auth-guard.js"></script>
</head>
<body>
""" + render_app_header(current_user, "page_hr") + """

<div class="content">
    <div>
        <div class="page-title">HR & Payroll</div>
        <div class="page-sub">Manage employees, attendance and salaries</div>
    </div>

    <!-- STATS -->
    <div class="stats-grid">
        <div class="stat-card green">
            <div class="stat-label">Total Employees</div>
            <div class="stat-value green" id="stat-total">-</div>
        </div>
        <div class="stat-card blue">
            <div class="stat-label">Present Today</div>
            <div class="stat-value blue" id="stat-present">-</div>
        </div>
        <div class="stat-card warn">
            <div class="stat-label">Days Off Today</div>
            <div class="stat-value warn" id="stat-absent">-</div>
        </div>
        <div class="stat-card green">
            <div class="stat-label">To Pay Today</div>
            <div class="stat-value green" id="stat-today-pay" style="font-size:20px">-</div>
            <div style="font-size:11px;color:var(--muted);margin-top:2px">based on present employees</div>
        </div>
        <div class="stat-card blue">
            <div class="stat-label">Monthly Allowances</div>
            <div class="stat-value blue" id="stat-allowance">-</div>
            <div style="font-size:11px;color:var(--muted);margin-top:2px">food + transport (all staff)</div>
        </div>
        <div class="stat-card purple">
            <div class="stat-label">Monthly Payroll</div>
            <div class="stat-value purple" id="stat-salary">-</div>
        </div>
    </div>

    <!-- TABS -->
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
        <div class="tabs">
            <button class="tab active" id="tab-emp"        onclick="switchTab('employees')">Employees</button>
            <button class="tab"        id="tab-att"        onclick="switchTab('attendance')">Attendance</button>
            <button class="tab"        id="tab-pay"        onclick="switchTab('payroll')">Payroll</button>
            <button class="tab"        id="tab-allow"      onclick="switchTab('allowances')">Allowances</button>
            <button class="tab"        id="tab-daysoff"    onclick="switchTab('daysoff')">Days Off</button>
        </div>
        <div style="display:flex;gap:10px;" id="tab-actions">
            <button class="btn btn-green"  id="btn-add-emp"  onclick="openAddEmpModal()">+ Add Employee</button>
            <button class="btn btn-blue"   id="btn-log-att"  onclick="openLogAttModal()" style="display:none">+ Log Attendance</button>
            <button class="btn btn-danger" id="btn-clear-hr-data" onclick="openClearHRDataModal()" style="display:none">Clear HR Data</button>
        </div>
    </div>

    <!-- EMPLOYEES -->
    <div id="section-employees">
        <div class="toolbar">
            <div class="search-box">
                <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
                <input id="emp-search" placeholder="Search by name, position or department..." oninput="onEmpSearch()">
            </div>
            <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:var(--muted);cursor:pointer;white-space:nowrap">
                <input type="checkbox" id="emp-show-inactive" onchange="loadEmployees()" style="cursor:pointer"> Show inactive
            </label>
        </div>
        <div id="farm-load-error" style="display:none;margin-bottom:12px;padding:10px 12px;border:1px solid rgba(255,181,71,.25);border-radius:10px;background:rgba(255,181,71,.08);color:var(--warn);font-size:12px;font-weight:600"></div>
        <div class="table-wrap">
            <table>
                <thead><tr><th>Name</th><th>Position</th><th>Department</th><th>Farm</th><th>Phone</th><th>Hire Date</th><th>Base Salary</th><th>Actions</th></tr></thead>
                <tbody id="emp-body"><tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px">Loading...</td></tr></tbody>
            </table>
        </div>
    </div>

    <!-- ATTENDANCE -->
    <div id="section-attendance" style="display:none">
        <div class="toolbar">
            <div class="fld" style="margin:0;flex:0 0 180px">
                <input id="att-period" type="month" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;background:linear-gradient(135deg,var(--green),var(--blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;font-family:var(--sans);font-size:14px;outline:none;" onchange="loadAttendance()">
            </div>
            <div class="fld" style="margin:0;flex:1;max-width:280px;position:relative">
                <input id="att-search" type="text" placeholder="Search employee…" autocomplete="off" oninput="filterAttendanceSearch()" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;width:100%">
            </div>
            <div class="fld" style="margin:0;flex:0 0 200px">
                <select id="att-emp-filter" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;" onchange="loadAttendance()">
                    <option value="">All Employees</option>
                </select>
            </div>
        </div>

        <!-- TODAY CARD -->
        <div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px;margin-bottom:14px;">
            <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:12px">Today's Attendance</div>
            <div id="today-attendance-grid" style="display:flex;flex-direction:column;gap:8px;"></div>
        </div>

        <div class="table-wrap">
            <table>
                <thead><tr><th>Employee</th><th>Date</th><th>Status</th><th>Note</th><th></th></tr></thead>
                <tbody id="att-body"><tr><td colspan="4" style="text-align:center;color:var(--muted);padding:40px">Loading...</td></tr></tbody>
            </table>
        </div>
    </div>

    <!-- PAYROLL -->
    <div id="section-payroll" style="display:none">
        <div class="toolbar">
            <div class="fld" style="margin:0;flex:0 0 180px">
                <input id="pay-period" type="month" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;color:var(--text);font-family:var(--sans);font-size:14px;outline:none;" onchange="loadPayrollPreview()">
            </div>
        </div>

        <!-- PAYROLL PREVIEW -->
        <div id="payroll-preview-wrap" style="display:none;margin-bottom:14px;">
            <div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px;margin-bottom:12px;">
                <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">
                    <div>
                        <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:4px">Payroll Preview</div>
                        <div style="font-size:13px;color:var(--sub)" id="preview-meta"></div>
                    </div>
                    <div style="text-align:right">
                        <div style="font-size:11px;color:var(--muted);margin-bottom:2px">Total to Pay</div>
                        <div style="font-family:var(--mono);font-size:24px;font-weight:700;color:var(--green)" id="preview-total">-</div>
                    </div>
                </div>
            </div>
            <div class="table-wrap">
                <table>
                    <thead><tr><th>Employee</th><th>Base Salary</th><th>Bonus</th><th>Day Ded.</th><th>Manual Ded.</th><th>Loan Deducted</th><th>Net Preview</th><th>Status</th></tr></thead>
                    <tbody id="preview-body"></tbody>
                </table>
            </div>
            <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:12px;">
                <button class="btn" id="btn-reset-payroll-month" style="border:1px solid var(--danger);color:var(--danger);background:transparent;display:none" onclick="resetPayrollMonth()">Reset Month</button>
                <button class="btn btn-purple" onclick="confirmRunPayroll()">Confirm & Run Payroll</button>
            </div>
        </div>

        <!-- PAYROLL RECORDS -->
        <div class="table-wrap" id="payroll-records-wrap" style="display:none">
            <table>
                <thead><tr><th>Employee</th><th>Period</th><th>Base Salary</th><th>Days</th><th>Bonuses</th><th>Loan</th><th>Day Ded.</th><th>Manual</th><th>Total Ded.</th><th>Net Salary</th><th>Status</th><th>Actions</th></tr></thead>
                <tbody id="pay-body"></tbody>
            </table>
        </div>
    </div>
</div>

<!-- ALLOWANCE TAB PANEL -->
<div id="tab-panel-allowances" style="display:none">
    <div class="table-wrap" style="max-width:1100px;margin:0 auto">
        <table style="margin:0 auto">
            <thead><tr><th>Employee</th><th>Position</th><th style="text-align:center">Food</th><th style="text-align:center">Transport</th><th style="text-align:center">Total / Month</th><th style="text-align:center">Open Advances</th><th style="text-align:center"></th></tr></thead>
            <tbody id="allowance-body"></tbody>
        </table>
    </div>
</div>

<div id="tab-panel-daysoff" style="display:none">
    <div class="table-wrap" style="max-width:1100px;margin:0 auto">
        <table style="margin:0 auto">
            <thead><tr>
                <th>Employee</th><th>Position</th>
                <th style="text-align:center">Allowance / mo</th>
                <th style="text-align:center">Accrued</th>
                <th style="text-align:center">From payroll</th>
                <th style="text-align:center">Taken</th>
                <th style="text-align:center">Days off credit</th>
            </tr></thead>
            <tbody id="daysoff-body"></tbody>
        </table>
        <div style="max-width:1100px;margin:10px auto 0;font-size:11px;color:var(--muted);line-height:1.5">
            Credit = monthly allowance accrued from hire (carried over) + days off earned by paying salary partially &minus; days taken (attendance marked "Day Off"). The balance never goes below zero: if more days off are taken than available, payroll only pays what the balance covers and the rest is unpaid.
        </div>
    </div>
</div>

<!-- ADD EMPLOYEE MODAL -->
<div class="modal-bg" id="emp-modal">
    <div class="modal">
        <div class="modal-title" id="emp-modal-title">Add Employee</div>
        <div class="form-row">
            <div class="fld span2"><label>Full Name *</label><input id="e-name" placeholder="Employee name"></div>
            <div class="fld"><label>Position</label><input id="e-position" placeholder="e.g. Cashier"></div>
            <div class="fld"><label>Department</label><input id="e-department" placeholder="e.g. Sales"></div>
            <div class="fld"><label>Farm</label><select id="e-farm"><option value="">No farm selected</option></select></div>
            <div class="fld"><label>Phone</label><input id="e-phone" placeholder="+20 100 000 0000"></div>
            <div class="fld"><label>Hire Date <span id="e-hire-lock" style="font-size:10px;color:var(--muted)">🔒 locked</span></label><input id="e-hire" type="date"></div>
            <div class="fld"><label>Base Salary (EGP)</label><input id="e-salary" type="number" placeholder="0.00" min="0" oninput="updateEmpDailyRatePreview()"></div>
            <div class="fld"><label>Salary Basis</label><select id="e-salary-basis" onchange="updateEmpDailyRatePreview()">
                <option value="calendar">Calendar month (÷ 28–31 days)</option>
                <option value="fixed_30">Fixed 30 days (÷ 30, absences deduct)</option>
            </select></div>
            <div class="fld"><label>Vacation Days / Month</label><input id="e-vacation" type="number" placeholder="0" min="0" max="20" step="1" oninput="updateEmpDailyRatePreview()"></div>
            <div class="fld"><label>Food Allowance (EGP)</label><input id="e-food" type="number" placeholder="0.00" min="0" oninput="updateEmpDailyRatePreview()"></div>
            <div class="fld"><label>Transportation Allowance (EGP)</label><input id="e-transport" type="number" placeholder="0.00" min="0" oninput="updateEmpDailyRatePreview()"></div>
            <div class="fld span2" id="emp-rate-preview" style="color:var(--muted);font-size:13px;padding:6px 0"></div>
            <div class="fld span2" id="emp-active-row" style="display:none">
                <label>Employment Status</label>
                <select id="e-active">
                    <option value="1">Active</option>
                    <option value="0">Inactive (not paid, hidden from payroll &amp; lists)</option>
                </select>
            </div>
            <div class="fld span2" id="emp-vacation-balance" style="font-size:13px;padding:6px 10px;border-radius:8px;background:var(--card2);display:none"></div>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeEmpModal()">Cancel</button>
            <button class="btn btn-green" onclick="saveEmployee()">Save Employee</button>
        </div>
    </div>
</div>

<!-- LOG ATTENDANCE MODAL -->
<div class="modal-bg" id="att-modal">
    <div class="modal">
        <div class="modal-title">Log Attendance</div>
        <div class="fld"><label>Employee *</label>
            <select id="a-emp"></select>
        </div>
        <div class="fld"><label>Date *</label><input id="a-date" type="date"></div>
        <div class="fld"><label>Status</label>
            <select id="a-status">
                <option value="present">Present</option>
                <option value="absent">Day Off</option>
            </select>
        </div>
        <div class="fld"><label>Note</label><input id="a-note" placeholder="Optional note"></div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeAttModal()">Cancel</button>
            <button class="btn btn-blue" onclick="saveAttendance()">Log Attendance</button>
        </div>
    </div>
</div>

<!-- RUN PAYROLL MODAL -->
<div class="modal-bg" id="pay-run-modal">
    <div class="modal">
        <div class="modal-title">Run Payroll</div>
        <div class="fld"><label>Period *</label><input id="pr-period" type="month"></div>
        <p style="color:var(--muted);font-size:13px;margin-bottom:16px">This will generate payroll records for all active employees for the selected period. Already existing records will be skipped.</p>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeRunPayModal()">Cancel</button>
            <button class="btn btn-purple" onclick="runPayroll()">Run Payroll</button>
        </div>
    </div>
</div>

<!-- EDIT PAYROLL MODAL -->
<div class="modal-bg" id="edit-pay-modal">
    <div class="modal">
        <div class="modal-title">Edit Payroll</div>
        <div class="modal-sub" id="edit-pay-emp" style="color:var(--muted);font-size:13px;margin-bottom:16px"></div>
        <div class="fld"><label>Bonuses</label><input id="ep-bonuses" type="number" placeholder="0" min="0"></div>
        <div class="fld"><label>Deductions</label><input id="ep-deductions" type="number" placeholder="0" min="0"></div>
        <div class="fld"><label>Notes</label><input id="ep-notes" placeholder="Optional notes"></div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeEditPayModal()">Cancel</button>
            <button class="btn btn-green" onclick="savePayrollEdit()">Save</button>
        </div>
    </div>
</div>

<!-- LOANS & DEDUCTIONS MODAL -->
<div class="modal-bg" id="loan-deduction-modal">
    <div class="modal wide">
        <div class="modal-title" id="loan-deduction-title">Loans & Deductions</div>
        <div class="modal-sub" id="loan-deduction-emp" style="color:var(--muted);font-size:13px;margin-bottom:16px"></div>

        <!-- Tab switcher -->
        <div style="display:flex;gap:8px;margin-bottom:18px;border-bottom:1px solid var(--border2);padding-bottom:0">
            <button class="ld-tab active" id="ld-tab-loans" onclick="switchLDTab('loans')">Loans</button>
            <button class="ld-tab" id="ld-tab-deductions" onclick="switchLDTab('deductions')">Penalties / Deductions</button>
        </div>

        <!-- LOANS PANEL -->
        <div id="ld-panel-loans">
            <div id="loan-section">
                <div class="hr-ledger-title" style="margin-bottom:10px">Add Loan</div>
                <div class="form-row" id="loan-create-form">
                    <div class="fld"><label>Date</label><input id="loan-date" type="date"></div>
                    <div class="fld"><label>Amount (EGP)</label><input id="loan-amount" type="number" min="0" step="0.01" placeholder="0.00"></div>
                    <div class="fld span2"><label>Note</label><input id="loan-description" placeholder="Salary advance, personal loan…"></div>
                    <div class="fld span2"><button class="btn btn-green" onclick="saveEmployeeLoan()">+ Add Loan</button></div>
                </div>
                <div style="font-size:11px;color:var(--muted);margin:8px 0 14px">The loan balance will auto-deduct from salary each payroll run.</div>
                <div class="hr-ledger-table"><table><thead><tr><th>Date</th><th>Amount</th><th>Repaid</th><th>Balance</th><th>Status</th><th></th></tr></thead><tbody id="loan-history-body"></tbody></table></div>
            </div>
        </div>

        <!-- DEDUCTIONS PANEL -->
        <div id="ld-panel-deductions" style="display:none">
            <div id="deduction-section">
                <div class="hr-ledger-title" style="margin-bottom:10px">Add Penalty / Deduction</div>
                <div class="form-row" id="day-deduction-form">
                    <div class="fld"><label>Month applied</label><input id="deduct-period" type="month" readonly title="Set automatically from the Date below"></div>
                    <div class="fld"><label>Date</label><input id="deduct-date" type="date" onchange="syncDeductPeriodFromDate()" oninput="syncDeductPeriodFromDate()"></div>
                    <div class="fld">
                        <label>Days Deducted</label>
                        <div style="display:flex;gap:6px;align-items:center">
                            <input id="deduct-days" type="number" min="0" step="0.25" placeholder="e.g. 1, 0.5, 0.25" oninput="updateDayDeductionPreview()" style="flex:1">
                            <div style="display:flex;gap:4px">
                                <button class="day-quick" onclick="setDeductDays(1)">1d</button>
                                <button class="day-quick" onclick="setDeductDays(0.5)">½d</button>
                                <button class="day-quick" onclick="setDeductDays(0.25)">¼d</button>
                            </div>
                        </div>
                    </div>
                    <div class="fld">
                        <label>Amount Preview</label>
                        <div class="money-preview" id="deduct-preview" style="font-size:18px;font-weight:700;color:var(--danger)">0.00 EGP</div>
                        <div style="font-size:11px;color:var(--muted)" id="deduct-rate-hint"></div>
                    </div>
                    <div class="fld span2"><label>Reason</label><input id="deduct-note" placeholder="Late, day off, penalty…"></div>
                    <div class="fld span2"><button class="btn btn-blue" onclick="saveDayDeduction()">+ Add Deduction</button></div>
                </div>
                <div class="hr-ledger-table" style="margin-top:16px"><table><thead><tr><th>Period</th><th>Date</th><th>Days</th><th>Daily Rate</th><th>Amount</th><th>Status</th><th>Reason</th></tr></thead><tbody id="deduction-history-body"></tbody></table></div>
            </div>
        </div>

        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeLoanDeductionModal()">Close</button>
        </div>
    </div>
</div>

<!-- ALLOWANCE ADVANCE MODAL -->
<div class="modal-bg" id="allowance-advance-modal">
    <div class="modal wide">
        <div class="modal-title">Allowance Advance</div>
        <div class="modal-sub" id="allowance-advance-emp" style="color:var(--muted);font-size:13px;margin-bottom:16px"></div>
        <div class="hr-ledger-panel">
            <div class="hr-ledger-title" style="margin-bottom:10px">Give Advance</div>
            <div class="form-row">
                <div class="fld"><label>Date</label><input id="adv-date" type="date"></div>
                <div class="fld"><label>Amount (EGP)</label><input id="adv-amount" type="number" min="0" step="0.01" placeholder="0.00"></div>
                <div class="fld span2"><label>Note</label><input id="adv-note" placeholder="e.g. advance on food allowance"></div>
                <div class="fld span2"><button class="btn btn-blue" onclick="saveAllowanceAdvance()">+ Give Advance</button></div>
            </div>
            <div style="font-size:11px;color:var(--muted);margin:8px 0 14px">Advance will be deducted from the employee's monthly allowance payout at next payroll.</div>
            <div class="hr-ledger-table"><table><thead><tr><th>Date</th><th>Amount</th><th>Status</th><th>Note</th><th></th></tr></thead><tbody id="adv-history-body"></tbody></table></div>
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeAllowanceAdvanceModal()">Close</button>
        </div>
    </div>
</div>

<!-- CLEAR HR DATA MODAL -->
<div class="modal-bg" id="clear-hr-modal">
    <div class="modal">
        <div class="modal-title danger">Clear HR Data</div>
        <div class="danger-note">
            This permanently deletes employees, attendance, payroll, and payroll-linked salary expenses. Other business data is not cleared.
        </div>
        <div class="fld">
            <label>Type <span class="confirm-token">CLEAR HR DATA</span> to confirm</label>
            <input id="clear-hr-confirmation" autocomplete="off" oninput="updateClearHRDataConfirmState()">
        </div>
        <div class="modal-actions">
            <button class="btn-cancel" onclick="closeClearHRDataModal()">Cancel</button>
            <button class="btn btn-danger" id="btn-confirm-clear-hr" onclick="confirmClearHRData()" disabled>Clear HR Data</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
  function setModeButton(isLight){
    const btn = document.getElementById("mode-btn");
    if(btn) btn.innerText = isLight ? "☀️" : "🌙";
}
function toggleMode(){
    const isLight = document.body.classList.toggle("light");
    localStorage.setItem("colorMode", isLight ? "light" : "dark");
    setModeButton(isLight);
}
function initializeColorMode(){
    const isLight = localStorage.getItem("colorMode") === "light";
    document.body.classList.toggle("light", isLight);
    setModeButton(isLight);
}
async function initUser() {
    try {
        const r = await fetch("/auth/me");
        if (!r.ok) { _redirectToLogin(); return; }
        const u = await r.json();
        const nameEl = document.getElementById("user-name");
        const avatarEl = document.getElementById("user-avatar");
        const emailEl = document.getElementById("user-email");
        if (nameEl) nameEl.innerText = u.name;
        if (avatarEl) avatarEl.innerText = u.name.charAt(0).toUpperCase();
        if (emailEl) emailEl.innerText = u.email;
        return u;
    } catch(e) { _redirectToLogin(); }
}
function toggleAccountMenu(event){
    event.stopPropagation();
    const trigger = document.getElementById("account-trigger");
    const dropdown = document.getElementById("account-dropdown");
    const open = dropdown.classList.toggle("open");
    trigger.classList.toggle("open", open);
    trigger.setAttribute("aria-expanded", open ? "true" : "false");
}
document.addEventListener("click", e => {
    const menu = document.getElementById("account-dropdown");
    const trigger = document.getElementById("account-trigger");
    if(!menu || !trigger) return;
    if(menu.contains(e.target) || trigger.contains(e.target)) return;
    menu.classList.remove("open");
    trigger.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
});
async function logout(){
    await fetch("/auth/logout", { method: "POST" });
    window.location.href = "/";
}
  let currentUser = null;
  function permissionSet(u = currentUser){
      const raw = u ? (u.permissions || []) : [];
      if(Array.isArray(raw)) return new Set(raw);
      if(typeof raw === "string"){
          return new Set(raw.split(",").map(v => v.trim()).filter(Boolean));
      }
      return new Set();
  }
  function hasPermission(permission, u = currentUser){
      const role = u ? (u.role || "") : "";
      const perms = permissionSet(u);
      return role === "admin" || perms.has(permission);
  }
  function configureHRPermissions(u){
      currentUser = u;
      const tabMap = [
          {id:"tab-emp", permission:"tab_hr_employees", tab:"employees"},
          {id:"tab-att", permission:"tab_hr_attendance", tab:"attendance"},
          {id:"tab-pay", permission:"tab_hr_payroll", tab:"payroll"},
      ];
      let firstAllowed = null;
      tabMap.forEach(conf => {
          let el = document.getElementById(conf.id);
          if(!el) return;
          if(!hasPermission(conf.permission, u)){
              el.style.display = "none";
          } else if(!firstAllowed) {
              firstAllowed = conf.tab;
          }
      });
      if(firstAllowed) setTimeout(() => switchTab(firstAllowed), 0);
      const clearBtn = document.getElementById("btn-clear-hr-data");
      if(clearBtn) clearBtn.style.display = hasPermission("action_hr_clear_data", u) ? "" : "none";
      const resetBtn = document.getElementById("btn-reset-payroll-month");
      if(resetBtn) resetBtn.style.display = hasPermission("action_hr_reset_payroll", u) ? "" : "none";
  }
  initializeColorMode();
  initUser().then(u => { if(u) configureHRPermissions(u); });
  let employees    = [];
let farms        = [];
let editingEmpId = null;
let editingPayId = null;
let empSearchTimer = null;
let loanDeductionEmployeeId = null;
let loanDeductionEmployeeSalary = 0;
let currentEmployeeLoans = [];

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}
function normalizeDashFallback(value){
    const text = String(value ?? "");
    return text === String.fromCharCode(8212) ? "" : text;
}
function displayText(value){
    const text = normalizeDashFallback(value).trim();
    return text ? escapeHtml(text) : "-";
}
function numberValue(value){
    const n = Number(value || 0);
    return Number.isFinite(n) ? n : 0;
}
function money(value){
    return numberValue(value).toFixed(2);
}
function safeStatusClass(value){
    return String(value || "").replace(/[^a-z0-9_-]/gi, "") || "unknown";
}

/* ── INIT ── */
async function init(){
    // Auto-mark all present today on page load
    await fetch("/hr/api/attendance/auto-today", {method:"POST"});
    // Employees first — the Monthly Allowances stat is derived from this
    // array, so loadSummary() must run AFTER it's populated.
    await loadEmployeeFarms();
    await loadEmployees();
    await loadSummary();
}

async function loadSummary(){
    let d = await (await fetch("/hr/api/summary")).json();
    document.getElementById("stat-total").innerText     = d.total_employees;
    document.getElementById("stat-present").innerText   = d.present_today;
    document.getElementById("stat-absent").innerText    = d.absent_today;
    document.getElementById("stat-today-pay").innerText = money(d.to_pay_today || 0) + " EGP";
    document.getElementById("stat-salary").innerText    = money(d.total_salary) + " EGP";
    // Allowance stat: calculated from employees array after load
    updateAllowanceStat();
}

/* ── TABS ── */
function switchTab(tab){
    const required = {
        employees: "tab_hr_employees",
        attendance: "tab_hr_attendance",
        payroll: "tab_hr_payroll",
    };
    if(required[tab] && !hasPermission(required[tab])) return;
    ["employees","attendance","payroll"].forEach(t => {
        document.getElementById("section-"+t).style.display = t===tab?"":"none";
        document.getElementById("tab-"+t.slice(0,3)).classList.toggle("active", t===tab);
    });
    document.getElementById("btn-add-emp").style.display  = tab==="employees" ?"":"none";
    document.getElementById("btn-log-att").style.display  = tab==="attendance"?"":"none";
    // Allowance tab
    const allowEl  = document.getElementById("tab-panel-allowances");
    const allowTab = document.getElementById("tab-allow");
    if(allowEl)  allowEl.style.display  = tab==="allowances" ? "" : "none";
    if(allowTab) allowTab.classList.toggle("active", tab==="allowances");
    // Days Off tab
    const daysoffEl  = document.getElementById("tab-panel-daysoff");
    const daysoffTab = document.getElementById("tab-daysoff");
    if(daysoffEl)  daysoffEl.style.display  = tab==="daysoff" ? "" : "none";
    if(daysoffTab) daysoffTab.classList.toggle("active", tab==="daysoff");
    if(tab==="attendance")  initAttendanceTab();
    if(tab==="payroll")     initPayrollTab();
    if(tab==="allowances")  loadAllowanceBoard();
    if(tab==="daysoff")     loadDaysOffBoard();
}

/* ── EMPLOYEES ── */
function onEmpSearch(){
    clearTimeout(empSearchTimer);
    empSearchTimer = setTimeout(loadEmployees, 300);
}

async function loadEmployees(){
    let q   = document.getElementById("emp-search").value.trim();
    const showInactive = document.getElementById("emp-show-inactive")?.checked;
    const params = [];
    if(q) params.push("q=" + encodeURIComponent(q));
    if(showInactive) params.push("include_inactive=true");
    let url = `/hr/api/employees${params.length ? "?" + params.join("&") : ""}`;
    let resp;
    try{
        resp = await fetch(url);
    }catch(networkErr){
        document.getElementById("emp-body").innerHTML =
            `<tr><td colspan="8" style="text-align:center;color:#ef4444;padding:40px">Network error loading employees: ${escapeHtml(String(networkErr))}</td></tr>`;
        employees = [];
        updateAllowanceStat();
        return;
    }

    if(!resp.ok){
        let errMsg = `HTTP ${resp.status}`;
        try{
            const body = await resp.json();
            if(body && body.detail) errMsg = body.detail;
        }catch(_){ /* not JSON */ }
        document.getElementById("emp-body").innerHTML =
            `<tr><td colspan="8" style="text-align:center;color:#ef4444;padding:40px;line-height:1.5">
                <b>Employees could not be loaded.</b><br>
                <span style="font-size:12px;color:var(--muted)">${escapeHtml(String(errMsg))}</span>
            </td></tr>`;
        employees = [];
        updateAllowanceStat();
        return;
    }

    employees = await resp.json();
    if(!Array.isArray(employees)){
        // Defensive: if the API returns {detail: "..."} instead of [], surface it
        const msg = (employees && employees.detail) ? employees.detail : "Unexpected response shape";
        document.getElementById("emp-body").innerHTML =
            `<tr><td colspan="8" style="text-align:center;color:#ef4444;padding:40px">${escapeHtml(String(msg))}</td></tr>`;
        employees = [];
        updateAllowanceStat();
        return;
    }

    if(!employees.length){
        document.getElementById("emp-body").innerHTML =
            `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:40px">No employees found</td></tr>`;
        updateAllowanceStat();
        return;
    }

    document.getElementById("emp-body").innerHTML = employees.map(e => {
        const id = numberValue(e.id);
        const salary = numberValue(e.base_salary);
        // Farm cell: show "🐾 Animals" when the employee is allocated to Animals,
        // otherwise show their farm name (or "-" if neither).
        const farmCell = e.works_with_animals
            ? `<span style="color:#84cc16;font-weight:600">🐾 Animals</span>`
            : displayText(e.farm_name);
        return `
        <tr${e.is_active === false ? ' style="opacity:.55"' : ""}>
            <td class="name">${displayText(e.name)}${e.is_active === false ? ' <span style="font-size:10px;font-weight:700;color:var(--danger);border:1px solid var(--danger);border-radius:6px;padding:1px 6px;margin-left:6px">INACTIVE</span>' : ""}</td>
            <td>${displayText(e.position)}</td>
            <td>${displayText(e.department)}</td>
            <td>${farmCell}</td>
            <td style="font-family:var(--mono);font-size:12px">${displayText(e.phone)}</td>
            <td style="font-size:12px;color:var(--muted)">${displayText(e.hire_date)}</td>
            <td class="mono">${money(salary)}</td>
            <td style="display:flex;gap:6px">
                <button class="action-btn" onclick="openEditEmpFromButton(this)" data-id="${id}" data-name="${escapeHtml(normalizeDashFallback(e.name))}" data-position="${escapeHtml(normalizeDashFallback(e.position))}" data-department="${escapeHtml(normalizeDashFallback(e.department))}" data-phone="${escapeHtml(normalizeDashFallback(e.phone))}" data-salary="${salary}" data-farm-id="${e.farm_id || ""}" data-works-animals="${e.works_with_animals ? "1" : ""}" data-vacation="${numberValue(e.vacation_days_per_month)||0}" data-salary-basis="${e.salary_days_basis||'calendar'}" data-food="${numberValue(e.food_allowance)||0}" data-transport="${numberValue(e.transportation_allowance)||0}" data-hire-date="${escapeHtml(e.hire_date||'')}" data-active="${e.is_active === false ? "0" : "1"}">Edit</button>
                ${(hasPermission("action_hr_view_loans") || hasPermission("action_hr_view_deductions"))?`<button class="action-btn purple" onclick="openLoanDeductionModalFromButton(this)" data-id="${id}" data-name="${escapeHtml(normalizeDashFallback(e.name))}" data-salary="${salary}">Loans &amp; Deductions</button>`:""}
                ${hasPermission("action_hr_run_payroll") ? (e.is_active === false
                    ? `<button class="action-btn green" onclick="reactivateEmployeeFromButton(this)" data-id="${id}" data-name="${escapeHtml(normalizeDashFallback(e.name))}">Reactivate</button>`
                    : `<button class="action-btn danger" onclick="deactivateEmployeeFromButton(this)" data-id="${id}" data-name="${escapeHtml(normalizeDashFallback(e.name))}">Remove</button>`) : ""}
            </td>
        </tr>`;
    }).join("");

    // Refresh the Monthly Allowances stat whenever the employees array
    // changes — without this, the box stays at "0 EGP" on first load.
    updateAllowanceStat();
}

function openEditEmpFromButton(btn){
    openEditEmpModal(
        numberValue(btn.dataset.id),
        btn.dataset.name || "",
        btn.dataset.position || "",
        btn.dataset.department || "",
        btn.dataset.phone || "",
        numberValue(btn.dataset.salary),
        btn.dataset.farmId || "",
        numberValue(btn.dataset.vacation) || 0,
        btn.dataset.salaryBasis || "calendar",
        numberValue(btn.dataset.food) || 0,
        numberValue(btn.dataset.transport) || 0,
        btn.dataset.hireDate || "",
        btn.dataset.worksAnimals === "1",
        btn.dataset.active !== "0"
    );
}

function reactivateEmployeeFromButton(btn){
    reactivateEmployee(numberValue(btn.dataset.id), btn.dataset.name || "");
}

function deactivateEmployeeFromButton(btn){
    deactivateEmployee(numberValue(btn.dataset.id), btn.dataset.name || "");
}

function openAddEmpModal(){
    editingEmpId = null;
    document.getElementById("emp-modal-title").innerText = "Add Employee";
    const activeRow = document.getElementById("emp-active-row");
    if(activeRow) activeRow.style.display = "none";
    ["e-name","e-position","e-department","e-phone","e-salary","e-vacation","e-food","e-transport","e-hire"].forEach(id=>document.getElementById(id).value="");
    document.getElementById("e-salary-basis").value = "calendar";
    applyHireDateLock(false);
    updateEmpDailyRatePreview();
    document.getElementById("e-hire").value = "";
    fillEmployeeFarmSelect("");
    const vb = document.getElementById("emp-vacation-balance");
    if(vb){ vb.style.display = "none"; vb.innerHTML = ""; }
    document.getElementById("emp-modal").classList.add("open");
}

function openEditEmpModal(id,name,position,department,phone,salary,farmId,vacationDays,salaryBasis,food,transport,hireDate,worksWithAnimals,isActive){
    editingEmpId = id;
    document.getElementById("emp-modal-title").innerText = "Edit Employee";
    document.getElementById("e-name").value       = name;
    document.getElementById("e-position").value   = normalizeDashFallback(position);
    document.getElementById("e-department").value = normalizeDashFallback(department);
    document.getElementById("e-phone").value      = normalizeDashFallback(phone);
    document.getElementById("e-salary").value     = salary;
    document.getElementById("e-vacation").value   = vacationDays || 0;
    document.getElementById("e-salary-basis").value = (salaryBasis === "fixed_30") ? "fixed_30" : "calendar";
    document.getElementById("e-food").value       = food || 0;
    document.getElementById("e-transport").value  = transport || 0;
    document.getElementById("e-hire").value       = (hireDate && hireDate !== "—") ? hireDate : "";
    // Status control: visible only when editing.
    const activeRow = document.getElementById("emp-active-row");
    if(activeRow){
        activeRow.style.display = "";
        document.getElementById("e-active").value = (isActive === false) ? "0" : "1";
    }
    applyHireDateLock(true);
    // If the employee is flagged works_with_animals, preselect the Animals sentinel;
    // otherwise preselect their farm (if any).
    fillEmployeeFarmSelect(worksWithAnimals ? "__animals__" : (farmId || ""));
    updateEmpDailyRatePreview();
    loadEmpVacationBalance(id);
    document.getElementById("emp-modal").classList.add("open");
}

function closeEmpModal(){
    const modal = document.getElementById("emp-modal");
    if(modal) modal.classList.remove("open");
    editingEmpId = null;
    const vb = document.getElementById("emp-vacation-balance");
    if(vb){ vb.style.display = "none"; vb.innerHTML = ""; }
}

async function loadEmpVacationBalance(empId){
    const box = document.getElementById("emp-vacation-balance");
    if(!box) return;
    box.style.display = "";
    box.innerHTML = "Loading leave balance\u2026";
    try{
        const v = await (await fetch(`/hr/api/employees/${empId}/vacation`)).json();
        const left = numberValue(v.days_left);
        const color = left < 0 ? "var(--danger)" : "var(--green)";
        box.innerHTML =
            `<div style="display:flex;justify-content:space-between;align-items:center">`+
            `<span>Vacation days left</span>`+
            `<b style="color:${color};font-size:15px">${left.toFixed(2)} days</b></div>`+
            `<div style="color:var(--muted);font-size:11px;margin-top:4px">`+
            `${numberValue(v.accrued).toFixed(2)} accrued (${numberValue(v.per_month)}/mo \u00d7 ${numberValue(v.months_accrued)} mo) `+
            `+ ${numberValue(v.credited_from_payroll).toFixed(2)} from payroll \u2212 ${numberValue(v.taken).toFixed(2)} taken</div>`;
    }catch(err){
        box.innerHTML = `<span style="color:var(--danger)">Could not load leave balance</span>`;
    }
}

function formatApiDetail(detail){
    if(Array.isArray(detail)){
        return detail.map(item => item && item.msg ? item.msg : String(item)).join("; ");
    }
    if(detail && typeof detail === "object") return JSON.stringify(detail);
    return detail ? String(detail) : "";
}

function compactApiText(text){
    const trimmed = (text || "").trim();
    if(!trimmed || trimmed.startsWith("<")) return "";
    return trimmed.length > 180 ? trimmed.slice(0, 180) + "..." : trimmed;
}

async function readApiResponse(res){
    const contentType = res.headers.get("content-type") || "";
    let data = null;
    let raw = "";
    if(contentType.includes("application/json")){
        try{ data = await res.json(); }
        catch(err){ raw = "Response was not valid JSON"; }
    }else{
        raw = await res.text();
    }
    if(!res.ok){
        const detail = formatApiDetail(data && data.detail) || compactApiText(raw) || res.statusText || "Request failed";
        throw new Error(`${detail} (${res.status})`);
    }
    if(data && data.detail) throw new Error(formatApiDetail(data.detail));
    return data || {};
}

function updateEmpDailyRatePreview(){
    const salary    = parseFloat(document.getElementById("e-salary")?.value || 0);
    const vacation  = parseInt(document.getElementById("e-vacation")?.value || 0);
    const food      = parseFloat(document.getElementById("e-food")?.value || 0);
    const transport = parseFloat(document.getElementById("e-transport")?.value || 0);
    const basis     = document.getElementById("e-salary-basis")?.value || "calendar";
    const preview   = document.getElementById("emp-rate-preview");
    if(!preview) return;
    if(!salary && !food && !transport){ preview.innerText = ""; return; }
    const fixed30   = basis === "fixed_30";
    const paidDays  = 30;
    const dailyRate = salary / paidDays;
    const totalMonthly = salary + food + transport;
    const basisNote = fixed30
        ? `${money(salary)} ÷ 30 flat — during the month pay accrues per covered day (${money(dailyRate)} each); once the month completes, the full salary is owed and each uncovered day deducts exactly ${money(dailyRate)}, whether the month has 28 or 31 days`
        : `approx — ${money(salary)} ÷ ${paidDays} days; actual payroll divides by each month's real day count (28–31)`;
    preview.innerHTML =
        `Daily rate: <b>${money(dailyRate)} EGP</b> &nbsp;(${basisNote}. ${vacation||0} leave day(s)/month accrue and carry over — days off are paid from that balance, so a fully-banked month off is still full pay; only days off beyond the balance are deducted)<br>` +
        `Total monthly: <b>${money(totalMonthly)} EGP</b> &nbsp;(salary ${money(salary)} + food ${money(food)} + transport ${money(transport)})`;
}

async function saveEmployee(){
    let name = document.getElementById("e-name").value.trim();
    if(!name){ showToast("Name is required"); return; }
    const farmSelVal = document.getElementById("e-farm").value;
    const worksAnimals = farmSelVal === "__animals__";
    const farmIdVal    = worksAnimals ? null : (parseInt(farmSelVal) || null);
    let body = {
        name,
        position:                document.getElementById("e-position").value.trim()||null,
        department:              document.getElementById("e-department").value.trim()||null,
        phone:                   document.getElementById("e-phone").value.trim()||null,
        hire_date:               document.getElementById("e-hire").value||null,
        base_salary:             parseFloat(document.getElementById("e-salary").value)||0,
        vacation_days_per_month: parseInt(document.getElementById("e-vacation").value)||0,
        salary_days_basis:       document.getElementById("e-salary-basis").value || "calendar",
        food_allowance:          parseFloat(document.getElementById("e-food").value)||0,
        transportation_allowance: parseFloat(document.getElementById("e-transport").value)||0,
        farm_id:                 farmIdVal,
        works_with_animals:      worksAnimals,
    };
    // Employment status is only part of the edit flow (the control is hidden
    // when adding). Send it explicitly so a toggle takes effect.
    if(editingEmpId){
        body.is_active = document.getElementById("e-active").value === "1";
    }
    let url    = editingEmpId ? `/hr/api/employees/${editingEmpId}` : "/hr/api/employees";
    let method = editingEmpId ? "PUT" : "POST";
    try{
        let res = await fetch(url,{method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
        await readApiResponse(res);
        closeEmpModal();
        showToast(editingEmpId?"Employee updated":"Employee added");
        loadEmployees(); loadSummary();
    }catch(err){
        showToast("Error: " + (err.message || "Could not save employee"));
    }
}

async function loadEmployeeFarms(){
    const errorBox = document.getElementById("farm-load-error");
    try{
        let res = await fetch("/hr/api/farms");
        if(!res.ok) throw new Error(`Farm API returned ${res.status}`);
        let data = await res.json();
        if(!Array.isArray(data)) throw new Error("Farm API returned an unexpected response");
        farms = data;
        if(errorBox){ errorBox.style.display = "none"; errorBox.innerText = ""; }
        fillEmployeeFarmSelect("");
    }catch(err){
        farms = [];
        if(errorBox){
            errorBox.innerText = "Farm list could not be loaded. Employees can still be viewed, but farm selection is unavailable.";
            errorBox.style.display = "";
        }
        fillEmployeeFarmSelect("");
    }
}

function fillEmployeeFarmSelect(selectedFarmId){
    const sel = document.getElementById("e-farm");
    if(!sel) return;
    const selected = String(selectedFarmId || "");
    const farmOpts = farms.map(f=>`<option value="${numberValue(f.id)}">${displayText(f.name || ("Farm #" + f.id))}</option>`).join("");
    sel.innerHTML = `<option value="">No farm selected</option>` +
        farmOpts +
        `<option value="__animals__">🐾 Animals</option>`;
    sel.value = selected;
}

async function deactivateEmployee(id,name){
    if(!confirm(`Remove "${name}" from active employees?`)) return;
    try{
        const res = await fetch(`/hr/api/employees/${id}`,{method:"DELETE"});
        await readApiResponse(res);
        showToast("Employee removed from active employees");
        loadEmployees(); loadSummary();
    }catch(err){
        showToast("Error: " + (err.message || "Could not remove employee"));
    }
}

async function reactivateEmployee(id,name){
    if(!confirm(`Reactivate "${name}"? They will reappear in payroll and active lists.`)) return;
    try{
        const res = await fetch(`/hr/api/employees/${id}`,{
            method:"PUT", headers:{"Content-Type":"application/json"},
            body: JSON.stringify({is_active: true})});
        await readApiResponse(res);
        showToast("Employee reactivated");
        loadEmployees(); loadSummary();
    }catch(err){
        showToast("Error: " + (err.message || "Could not reactivate employee"));
    }
}

/* ── LOANS & DEDUCTIONS ── */
function openLoanDeductionModalFromButton(btn){
    openLoanDeductionModal(
        numberValue(btn.dataset.id),
        btn.dataset.name || "",
        numberValue(btn.dataset.salary)
    );
}

function syncDeductPeriodFromDate(){
    const dateEl   = document.getElementById("deduct-date");
    const periodEl = document.getElementById("deduct-period");
    if(dateEl && periodEl && dateEl.value && dateEl.value.length >= 7){
        periodEl.value = dateEl.value.slice(0, 7);   // YYYY-MM from the chosen date
    }
    updateDayDeductionPreview();
}

function setDefaultLoanDeductionDates(){
    const today = new Date().toISOString().split("T")[0];
    const period = today.slice(0, 7);
    ["loan-date","repay-date","deduct-date"].forEach(id => {
        const el = document.getElementById(id);
        if(el && !el.value) el.value = today;
    });
    ["deduct-period","manual-deduct-period"].forEach(id => {
        const el = document.getElementById(id);
        if(el && !el.value) el.value = period;
    });
    const working = document.getElementById("deduct-working-days");
    if(working && !working.value) working.value = "30";
}

/* ── LOANS & DEDUCTIONS MODAL ── */
function switchLDTab(tab){
    document.getElementById("ld-panel-loans").style.display      = tab==="loans"      ? "" : "none";
    document.getElementById("ld-panel-deductions").style.display = tab==="deductions" ? "" : "none";
    document.getElementById("ld-tab-loans").classList.toggle("active",      tab==="loans");
    document.getElementById("ld-tab-deductions").classList.toggle("active", tab==="deductions");
}

async function openLoanDeductionModal(employeeId, name, salary){
    loanDeductionEmployeeId = employeeId;
    loanDeductionEmployeeSalary = salary;
    document.getElementById("loan-deduction-emp").innerText = `${name}  ·  Base salary: ${money(salary)} EGP`;
    setDefaultLoanDeductionDates();
    updateDayDeductionPreview();
    switchLDTab("loans");
    document.getElementById("loan-section").style.display      = hasPermission("action_hr_view_loans")      ? "" : "none";
    document.getElementById("loan-create-form").style.display  = hasPermission("action_hr_manage_loans")    ? "" : "none";
    document.getElementById("deduction-section").style.display = hasPermission("action_hr_view_deductions") ? "" : "none";
    document.getElementById("day-deduction-form").style.display = hasPermission("action_hr_manage_deductions") ? "" : "none";
    document.getElementById("loan-deduction-modal").classList.add("open");
    await refreshLoanDeductionModal();
}

function closeLoanDeductionModal(){
    document.getElementById("loan-deduction-modal").classList.remove("open");
}

async function refreshLoanDeductionModal(){
    if(!loanDeductionEmployeeId) return;
    if(hasPermission("action_hr_view_loans"))      await loadEmployeeLoans();
    if(hasPermission("action_hr_view_deductions")) await loadEmployeeDeductions();
}

async function loadEmployeeLoans(){
    const body = document.getElementById("loan-history-body");
    try{
        const res = await fetch(`/hr/api/employees/${loanDeductionEmployeeId}/loans`);
        currentEmployeeLoans = await readApiResponse(res);
        const totalBalance = currentEmployeeLoans.reduce((s,l)=>s+numberValue(l.balance),0);
        body.innerHTML = currentEmployeeLoans.map(loan => `
            <tr>
                <td>${displayText(loan.loan_date)}</td>
                <td class="mono">${money(loan.amount)}</td>
                <td class="mono" style="color:var(--muted)">${money(loan.repaid_amount)}</td>
                <td class="mono" style="font-weight:700;color:${numberValue(loan.balance)>0?"var(--warn)":"var(--muted)"}">${money(loan.balance)}</td>
                <td><span class="status-${loan.status==="open"?"pending":loan.status==="paid"?"success":"danger"}">${displayText(loan.status)}</span></td>
                <td>
                    ${loan.status==="open" && hasPermission("action_hr_manage_loans") ? `<button class="action-btn danger" onclick="cancelLoan(${numberValue(loan.id)})">Cancel</button>` : ""}
                    ${hasPermission("action_hr_delete_loans") ? `<button class="action-btn danger" onclick="deleteLoan(${numberValue(loan.id)})">Delete</button>` : ""}
                </td>
            </tr>`).join("") || `<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:18px">No loans yet</td></tr>`;
        if(totalBalance>0) body.innerHTML += `<tr style="background:var(--card2)"><td colspan="3" style="font-weight:700;color:var(--sub)">Total Outstanding</td><td class="mono" style="font-weight:700;color:var(--warn)">${money(totalBalance)}</td><td colspan="2"></td></tr>`;
    }catch(err){
        body.innerHTML = `<tr><td colspan="6" style="color:var(--danger);padding:18px">Could not load loans</td></tr>`;
    }
}

async function loadEmployeeDeductions(){
    const body = document.getElementById("deduction-history-body");
    try{
        const res = await fetch(`/hr/api/employees/${loanDeductionEmployeeId}/deductions`);
        const deductions = await readApiResponse(res);
        body.innerHTML = deductions.filter(d=>d.type!=="loan_repayment").map(d => `
            <tr>
                <td>${displayText(d.period || d.payroll_period)}</td>
                <td>${d.deduction_date ? displayText(d.deduction_date) : "-"}</td>
                <td style="font-weight:700">${d.days!==null&&d.days!==undefined ? numberValue(d.days)+"d" : "-"}</td>
                <td class="mono" style="color:var(--muted)">${d.daily_rate!==null&&d.daily_rate!==undefined ? money(d.daily_rate) : "-"}</td>
                <td class="mono" style="font-weight:700;color:var(--danger)">${money(d.amount)}</td>
                <td style="font-size:11px;color:${d.payroll_id?"var(--green)":"var(--warn)"}">${d.payroll_id?"Applied to payroll":"Pending"}</td>
                <td style="color:var(--muted);font-size:12px">${escapeHtml(d.note||"-")}${(!d.payroll_id && hasPermission("action_hr_manage_deductions")) ? ` &nbsp;<button class="link-danger" style="background:none;border:none;color:var(--danger);cursor:pointer;font-size:12px;padding:0" onclick="deleteDeduction(${d.id})">Delete</button>` : ""}</td>
            </tr>`).join("") || `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:18px">No deductions yet</td></tr>`;
    }catch(err){
        body.innerHTML = `<tr><td colspan="7" style="color:var(--danger);padding:18px">Could not load deductions</td></tr>`;
    }
}

async function deleteDeduction(deductionId){
    if(!hasPermission("action_hr_manage_deductions")) return;
    if(!confirm("Delete this pending deduction?")) return;
    try{
        const res = await fetch(`/hr/api/employees/${loanDeductionEmployeeId}/deductions/${deductionId}`,{method:"DELETE"});
        await readApiResponse(res);
        showToast("Deduction deleted");
        await loadEmployeeDeductions();
    }catch(err){ showToast("Error: "+(err.message||"Could not delete deduction")); }
}

async function saveEmployeeLoan(){
    if(!hasPermission("action_hr_manage_loans")) return;
    const amt = parseFloat(document.getElementById("loan-amount").value||"0");
    if(!amt||amt<=0){ showToast("Enter a valid loan amount"); return; }
    const body = {
        loan_date: document.getElementById("loan-date").value,
        amount: amt,
        description: document.getElementById("loan-description").value.trim()||null,
    };
    try{
        const res = await fetch(`/hr/api/employees/${loanDeductionEmployeeId}/loans`,{
            method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),
        });
        await readApiResponse(res);
        document.getElementById("loan-amount").value="";
        document.getElementById("loan-description").value="";
        showToast("Loan added — will auto-deduct from next payroll");
        await loadEmployeeLoans();
    }catch(err){ showToast("Error: "+(err.message||"Could not save loan")); }
}

async function cancelLoan(loanId){
    if(!hasPermission("action_hr_manage_loans")) return;
    if(!confirm("Cancel this loan? It will stay in history but no longer deduct.")) return;
    try{
        const res = await fetch(`/hr/api/loans/${loanId}/cancel`,{method:"POST"});
        await readApiResponse(res);
        showToast("Loan cancelled");
        await loadEmployeeLoans();
    }catch(err){ showToast("Error: "+(err.message||"Could not cancel loan")); }
}

async function deleteLoan(loanId){
    if(!hasPermission("action_hr_delete_loans")) return;
    if(!confirm("Permanently delete this loan and its repayment history? Deductions taken in unpaid payroll runs will be added back; already-paid runs are left unchanged. This cannot be undone.")) return;
    try{
        const res = await fetch(`/hr/api/loans/${loanId}`,{method:"DELETE"});
        const out = await readApiResponse(res);
        let msg = "Loan deleted";
        if(out && numberValue(out.reversed_from_unpaid) > 0) msg += ` — ${money(out.reversed_from_unpaid)} reversed from unpaid payroll`;
        showToast(msg);
        await loadEmployeeLoans();
    }catch(err){ showToast("Error: "+(err.message||"Could not delete loan")); }
}

function setDeductDays(d){
    document.getElementById("deduct-days").value = d;
    updateDayDeductionPreview();
}

function updateDayDeductionPreview(){
    const days = numberValue(document.getElementById("deduct-days")?.value);
    const period = document.getElementById("deduct-period")?.value;
    let workingDays = 22;
    if(period){
        const [yr,mo] = period.split("-").map(Number);
        const daysInMonth = new Date(yr,mo,0).getDate();
        let wd=0; for(let d=1;d<=daysInMonth;d++){ if(new Date(yr,mo-1,d).getDay()%6!==0) wd++; }
        workingDays = wd;
    }
    const dailyRate = loanDeductionEmployeeSalary / workingDays;
    const amount = dailyRate * days;
    const preview = document.getElementById("deduct-preview");
    const hint    = document.getElementById("deduct-rate-hint");
    if(preview) preview.innerText = money(amount)+" EGP";
    if(hint) hint.innerText = days>0 ? `Daily rate: ${money(dailyRate)} EGP × ${days}d (${workingDays} working days)` : "";
}

async function saveDayDeduction(){
    if(!hasPermission("action_hr_manage_deductions")) return;
    const days = parseFloat(document.getElementById("deduct-days").value||"0");
    if(!days||days<=0){ showToast("Enter days to deduct (e.g. 1, 0.5, 0.25)"); return; }
    const deductDate = document.getElementById("deduct-date").value;
    if(!deductDate){ showToast("Pick a date for the deduction"); return; }
    // The month the deduction applies to is always taken from the date, so a
    // deduction dated 31 May is applied to May — never the current month.
    const body = {
        period: deductDate.slice(0, 7),
        deduction_date: deductDate,
        days: days,
        note: document.getElementById("deduct-note").value.trim()||null,
    };
    try{
        const res = await fetch(`/hr/api/employees/${loanDeductionEmployeeId}/deductions/day`,{
            method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),
        });
        await readApiResponse(res);
        document.getElementById("deduct-days").value="";
        document.getElementById("deduct-note").value="";
        updateDayDeductionPreview();
        showToast("Deduction saved");
        await loadEmployeeDeductions();
    }catch(err){ showToast("Error: "+(err.message||"Could not save deduction")); }
}

async function saveManualDeduction(){
    return; // Removed — use day deduction for all penalties
}

function updateAllowanceStat(){
    const totalAllow = employees.reduce((s,e) =>
        s + (numberValue(e.food_allowance)||0) + (numberValue(e.transportation_allowance)||0), 0);
    const el = document.getElementById("stat-allowance");
    if(el) el.innerText = money(totalAllow) + " EGP";
}

/* ── HIRE DATE LOCK ── */
function applyHireDateLock(isEdit){
    const hireInput = document.getElementById("e-hire");
    const lockLabel = document.getElementById("e-hire-lock");
    const canEdit   = !isEdit || (currentUser && (currentUser.role==="admin" || currentUser.role==="manager"));
    hireInput.disabled = !canEdit;
    hireInput.style.opacity = canEdit ? "1" : "0.5";
    if(lockLabel) lockLabel.style.display = (isEdit && !canEdit) ? "" : "none";
}

/* ── ALLOWANCE BOARD ── */
let allowanceAdvanceEmployeeId = null;
let allowanceAdvanceEmployeeName = "";

async function loadDaysOffBoard(){
    const body = document.getElementById("daysoff-body");
    if(!body) return;
    body.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:20px">Loading\u2026</td></tr>`;
    try{
        const rows = await (await fetch("/hr/api/vacation-summary")).json();
        if(!rows.length){
            body.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:30px">No employees have a monthly days-off allowance set. Add one in the employee profile (Vacation Days / Month).</td></tr>`;
            return;
        }
        body.innerHTML = rows.map(r=>{
            const left = numberValue(r.days_left);
            const color = left < 0 ? "var(--danger)" : (left > 0 ? "var(--green)" : "var(--muted)");
            return `<tr>
                <td class="name">${displayText(r.employee)}</td>
                <td style="color:var(--muted)">${displayText(r.position)}</td>
                <td style="text-align:center;font-family:var(--mono)">${numberValue(r.per_month)}</td>
                <td style="text-align:center;font-family:var(--mono);color:var(--muted)">${numberValue(r.accrued).toFixed(2)}</td>
                <td style="text-align:center;font-family:var(--mono);color:var(--muted)">${numberValue(r.credited_from_payroll).toFixed(2)}</td>
                <td style="text-align:center;font-family:var(--mono);color:var(--muted)">${numberValue(r.taken).toFixed(2)}</td>
                <td style="text-align:center;font-family:var(--mono);font-weight:700;color:${color}">${left.toFixed(2)}</td>
            </tr>`;
        }).join("");
    }catch(err){
        body.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--danger);padding:20px">Could not load days-off credit</td></tr>`;
    }
}

async function loadAllowanceBoard(){
    const body = document.getElementById("allowance-body");
    if(!body) return;
    body.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:20px">Loading…</td></tr>`;
    try {
        const emps = employees.filter(e => e.is_active !== false &&
            (numberValue(e.food_allowance) > 0 || numberValue(e.transportation_allowance) > 0));
        if(!emps.length){
            body.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:30px">No employees with allowances yet. Add food or transport allowances in the employee profile.</td></tr>`;
            return;
        }
        const rows = await Promise.all(emps.map(async e => {
            let advances = [];
            try {
                const r = await fetch(`/hr/api/employees/${e.id}/allowance-advances`);
                advances = await r.json();
            } catch(err){}
            const openAdv = advances.filter(a => a.status==="open").reduce((s,a)=>s+numberValue(a.amount),0);
            const food  = numberValue(e.food_allowance)||0;
            const trans = numberValue(e.transportation_allowance)||0;
            const total = food+trans;
            return `<tr>
                <td class="name">${escapeHtml(e.name)}</td>
                <td style="color:var(--muted);font-size:12px">${escapeHtml(e.position||"—")}</td>
                <td class="mono">${money(food)}</td>
                <td class="mono">${money(trans)}</td>
                <td class="mono" style="font-weight:700;color:var(--blue)">${money(total)}</td>
                <td class="mono" style="color:${openAdv>0?"var(--warn)":"var(--muted)"}">${openAdv>0?money(openAdv):"—"}</td>
                <td><button class="action-btn blue" onclick="openAllowanceAdvanceModal(${e.id},'${escapeHtml(e.name)}',${food},${trans})">Advance</button></td>
            </tr>`;
        }));
        body.innerHTML = rows.join("");
        const totalAllow = emps.reduce((s,e)=>s+numberValue(e.food_allowance)+numberValue(e.transportation_allowance),0);
        const el = document.getElementById("stat-allowance");
        if(el) el.innerText = money(totalAllow)+" EGP";
    } catch(err){
        body.innerHTML = `<tr><td colspan="7" style="color:var(--danger);padding:18px">Could not load allowances</td></tr>`;
    }
}

async function openAllowanceAdvanceModal(empId, name, food, trans){
    allowanceAdvanceEmployeeId = empId;
    allowanceAdvanceEmployeeName = name;
    document.getElementById("allowance-advance-emp").innerHTML =
        `${escapeHtml(name)} &nbsp;·&nbsp; Food: ${money(food)} EGP &nbsp;·&nbsp; Transport: ${money(trans)} EGP &nbsp;·&nbsp; <b>Total: ${money(food+trans)} EGP/month</b>`;
    document.getElementById("adv-date").value   = new Date().toISOString().split("T")[0];
    document.getElementById("adv-amount").value = "";
    document.getElementById("adv-note").value   = "";
    document.getElementById("allowance-advance-modal").classList.add("open");
    await loadAllowanceAdvances();
}

function closeAllowanceAdvanceModal(){
    document.getElementById("allowance-advance-modal").classList.remove("open");
}

async function loadAllowanceAdvances(){
    const body = document.getElementById("adv-history-body");
    if(!allowanceAdvanceEmployeeId) return;
    try {
        const res = await fetch(`/hr/api/employees/${allowanceAdvanceEmployeeId}/allowance-advances`);
        const advances = await res.json();
        body.innerHTML = advances.map(a => `<tr>
            <td>${displayText(a.advance_date)}</td>
            <td class="mono" style="font-weight:700">${money(a.amount)}</td>
            <td><span class="status-${a.status==="open"?"pending":a.status==="deducted"?"success":"danger"}">${a.status}</span></td>
            <td style="color:var(--muted);font-size:12px">${escapeHtml(a.note||"—")}</td>
            <td>${a.status==="open"?`<button class="action-btn danger" onclick="cancelAllowanceAdvance(${a.id})">Cancel</button>`:""}</td>
        </tr>`).join("") || `<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:14px">No advances yet</td></tr>`;
    } catch(err) {
        body.innerHTML = `<tr><td colspan="5" style="color:var(--danger)">Could not load advances</td></tr>`;
    }
}

async function saveAllowanceAdvance(){
    const amt = parseFloat(document.getElementById("adv-amount").value||"0");
    if(!amt||amt<=0){ showToast("Enter a valid amount"); return; }
    const body = {
        advance_date: document.getElementById("adv-date").value,
        amount: amt,
        note: document.getElementById("adv-note").value.trim()||null,
    };
    try {
        const res = await fetch(`/hr/api/employees/${allowanceAdvanceEmployeeId}/allowance-advances`, {
            method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body),
        });
        await readApiResponse(res);
        document.getElementById("adv-amount").value = "";
        document.getElementById("adv-note").value   = "";
        showToast("Advance recorded");
        await loadAllowanceAdvances();
        loadAllowanceBoard();
    } catch(err){ showToast("Error: "+(err.message||"Could not save advance")); }
}

async function cancelAllowanceAdvance(advId){
    if(!confirm("Cancel this advance?")) return;
    try {
        await fetch(`/hr/api/allowance-advances/${advId}/cancel`, {method:"POST"});
        showToast("Advance cancelled");
        await loadAllowanceAdvances();
        loadAllowanceBoard();
    } catch(err){ showToast("Error: "+(err.message||"Could not cancel")); }
}

/* ── ATTENDANCE ── */
function initAttendanceTab(){
    // Set default period to current month
    let now = new Date();
    let m   = String(now.getMonth()+1).padStart(2,"0");
    document.getElementById("att-period").value = `${now.getFullYear()}-${m}`;

    // Fill employee filter
    let sel = document.getElementById("att-emp-filter");
    sel.innerHTML = `<option value="">All Employees</option>` +
        employees.map(e=>`<option value="${numberValue(e.id)}">${displayText(e.name)}</option>`).join("");

    // Fill attendance log employee select
    let aEmp = document.getElementById("a-emp");
    aEmp.innerHTML = employees.map(e=>`<option value="${numberValue(e.id)}">${displayText(e.name)}</option>`).join("");

    // Set today as default date
    document.getElementById("a-date").value = new Date().toISOString().split("T")[0];

    loadTodayAttendance();
    loadAttendance();
}

let allAttendanceRecords = [];

function filterAttendanceSearch(){
    let query = (document.getElementById("att-search").value || "").toLowerCase().trim();
    let filtered = query
        ? allAttendanceRecords.filter(r => (r.employee||"").toLowerCase().includes(query))
        : allAttendanceRecords;
    renderAttendanceRows(filtered);
}

function renderAttendanceRows(records){
    if(!records.length){
        document.getElementById("att-body").innerHTML =
            `<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:40px">No attendance records found</td></tr>`;
        return;
    }
    document.getElementById("att-body").innerHTML = records.map(r => {
        let status = String(r.status || "");
        let cls = `status-${safeStatusClass(status)}`;
        let labels = {present:"Present",absent:"Day Off",late:"Late",leave:"Leave"};
        let canEdit = hasPermission("action_hr_edit_attendance");
        return `
        <tr>
            <td class="name">${displayText(r.employee)}</td>
            <td style="font-family:var(--mono);font-size:12px">${displayText(r.date)}</td>
            <td>
                ${canEdit
                    ? `<select onchange="editAttendanceStatus(${r.id}, this.value)" style="background:var(--card2);border:1px solid var(--border2);border-radius:6px;padding:4px 8px;font-size:12px;color:var(--text);cursor:pointer">
                        <option value="present" ${status==="present"?"selected":""}>Present</option>
                        <option value="absent" ${status==="absent"?"selected":""}>Day Off</option>
                    </select>`
                    : `<span class="${cls}">${displayText(labels[status] || status)}</span>`
                }
            </td>
            <td style="color:var(--muted);font-size:12px">${escapeHtml(r.note || "-")}</td>
            <td>${canEdit ? `<button class="action-btn danger" onclick="deleteAttendance(${r.id})" style="font-size:11px;padding:3px 8px">✕</button>` : ""}</td>
        </tr>`;
    }).join("");
}

async function loadAttendance(){
    let period = document.getElementById("att-period").value;
    let empId  = document.getElementById("att-emp-filter").value;
    let url    = `/hr/api/attendance?period=${period}`;
    if(empId) url += `&emp_id=${empId}`;
    let records = await (await fetch(url)).json();
    allAttendanceRecords = records;
    document.getElementById("att-search").value = "";
    renderAttendanceRows(records);
}

async function editAttendanceStatus(attId, newStatus){
    try {
        let res = await fetch(`/hr/api/attendance/${attId}`, {
            method:"PUT",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({status:newStatus}),
        });
        let data = await res.json();
        if(data.detail){ showToast("Error: "+data.detail); return; }
        showToast("Attendance updated");
        loadAttendance(); loadTodayAttendance(); loadSummary();
    } catch(err){ showToast("Error: "+err.message); }
}

async function deleteAttendance(attId){
    if(!confirm("Delete this attendance record?")) return;
    try {
        let res = await fetch(`/hr/api/attendance/${attId}`, {method:"DELETE"});
        let data = await res.json();
        if(data.detail){ showToast("Error: "+data.detail); return; }
        showToast("Attendance deleted");
        loadAttendance(); loadTodayAttendance(); loadSummary();
    } catch(err){ showToast("Error: "+err.message); }
}

function openLogAttModal(){
    document.getElementById("att-modal").classList.add("open");
}
function closeAttModal(){ document.getElementById("att-modal").classList.remove("open"); }

async function saveAttendance(){
    let emp_id = document.getElementById("a-emp").value;
    let dt     = document.getElementById("a-date").value;
    if(!emp_id||!dt){ showToast("Select employee and date"); return; }
    let body = {
        employee_id: parseInt(emp_id),
        date:        dt,
        status:      document.getElementById("a-status").value,
        note:        document.getElementById("a-note").value.trim()||null,
    };
    let res  = await fetch("/hr/api/attendance",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeAttModal();
    showToast(data.updated?"Attendance updated":"Attendance logged");
    if(dt === new Date().toISOString().split("T")[0] && ["present","absent"].includes(body.status)){
        await loadEmployees();
    }
    loadTodayAttendance(); loadAttendance(); loadSummary();
}

/* ── ATTENDANCE TODAY CARD ── */
async function loadTodayAttendance(){
    let today = new Date().toISOString().split("T")[0];
    let records = await (await fetch(`/hr/api/attendance?period=${today.slice(0,7)}`)).json();
    let todayRecs = records.filter(r => r.date === today);
    let grid = document.getElementById("today-attendance-grid");
    if(!grid) return;
    if(!employees.length){ grid.innerHTML = `<div style="color:var(--muted);font-size:13px">No employees found</div>`; return; }
    const todayLabels = {present:"Present",absent:"Day Off",late:"Late",leave:"Leave"};
    grid.innerHTML = employees.map(emp => {
        let rec    = todayRecs.find(r => r.employee_id === emp.id);
        let autoStatus = emp.attendance_auto_status || "present";
        let status = rec ? rec.status : autoStatus;
        let isAbs  = status === "absent";
        let isAutoAbsent = autoStatus === "absent";
        let statusText = isAutoAbsent ? "Day off until marked present" : (todayLabels[status] || status || "Present");
        let statusColor = isAbs || isAutoAbsent ? "var(--danger)" : "var(--green)";
        return `<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--card2);border:1px solid ${isAbs?"rgba(255,77,109,.2)":"rgba(0,255,157,.1)"};border-radius:9px;">
            <div>
                <span style="font-weight:600;font-size:13px;color:var(--text)">${displayText(emp.name)}</span>
                <span style="font-size:11px;color:var(--muted);margin-left:8px">${escapeHtml(normalizeDashFallback(emp.position))}</span>
            </div>
            <div style="display:flex;align-items:center;gap:10px">
                <span style="font-size:12px;font-weight:700;color:${statusColor}">
                    ${escapeHtml(statusText)}
                </span>
                ${hasPermission("action_hr_run_payroll") ? ((isAbs || isAutoAbsent)
                    ? `<button class="action-btn green" onclick="markPresentToday(${emp.id})">Mark Present</button>`
                    : `<button class="action-btn danger" onclick="markAbsentToday(${emp.id})">Mark Day Off</button>`
                ) : ""}
            </div>
        </div>`;
    }).join("");
}

async function markAbsentToday(empId){
    let res = await fetch("/hr/api/attendance/mark-absent",{
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({employee_id: empId, date: new Date().toISOString().split("T")[0], status:"absent"}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast("Marked day off");
    await loadEmployees();
    loadTodayAttendance(); loadAttendance(); loadSummary();
}

async function markPresentToday(empId){
    let res = await fetch("/hr/api/attendance/mark-present",{
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({employee_id: empId, date: new Date().toISOString().split("T")[0], status:"present"}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast("Marked present");
    await loadEmployees();
    loadTodayAttendance(); loadAttendance(); loadSummary();
}

/* ── PAYROLL ── */
function initPayrollTab(){
    let now = new Date();
    let m   = String(now.getMonth()+1).padStart(2,"0");
    document.getElementById("pay-period").value = `${now.getFullYear()}-${m}`;
    loadPayrollPreview();
}

async function loadPayrollPreview(){
    let period = document.getElementById("pay-period").value;
    if(!period) return;

    document.getElementById("payroll-preview-wrap").style.display  = "";
    document.getElementById("payroll-records-wrap").style.display  = "none";
    document.getElementById("preview-body").innerHTML =
        `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:20px">Loading preview...</td></tr>`;

    let d    = await (await fetch(`/hr/api/payroll/preview?period=${period}`)).json();
    let [yr, mo] = period.split("-");
    let monthName = new Date(parseInt(yr), parseInt(mo)-1, 1).toLocaleDateString("en-GB",{month:"long",year:"numeric"});

    document.getElementById("preview-meta").innerHTML =
        `<b>${escapeHtml(monthName)}</b> &nbsp;-&nbsp; ${numberValue(d.days_elapsed)} of ${numberValue(d.working_days)} working days elapsed`;
    document.getElementById("preview-total").innerText = money(d.total_to_pay) + " EGP";

    document.getElementById("preview-body").innerHTML = d.employees.map(e => {
        const loanBal = (e.outstanding_loan_balance !== null && e.outstanding_loan_balance !== undefined) ? numberValue(e.outstanding_loan_balance) : 0;
        const dayDed  = numberValue(e.pending_day_deductions);
        const manDed  = numberValue(e.pending_manual_deductions);
        const base    = numberValue(e.base_salary);
        const net     = base - dayDed - manDed - loanBal;
        const paidLeave = numberValue(e.vacation_paid_days)||0;
        const takenLeave= numberValue(e.vacation_taken_month)||0;
        const leaveBal  = (e.vacation_available!==null&&e.vacation_available!==undefined) ? numberValue(e.vacation_available) : null;
        const vacInfo   = e.vacation_days > 0
            ? ` · ${paidLeave} paid leave${takenLeave > paidLeave ? ` of ${takenLeave} taken` : ""}${leaveBal!==null ? ` · ${leaveBal}d balance` : ""}`
            : "";
        const allowances = (numberValue(e.food_allowance)||0) + (numberValue(e.transportation_allowance)||0);
        return `
        <tr>
            <td class="name">${displayText(e.employee)}<br><span style="font-size:11px;color:var(--muted)">${displayText(e.position)} · ${numberValue(e.days_present)} present / ${numberValue(e.paid_days||30)} paid days${vacInfo}</span></td>
            <td style="font-family:var(--mono)" title="Daily rate: ${money(e.daily_rate)} EGP&#10;Food: ${money(e.food_allowance||0)}&#10;Transport: ${money(e.transportation_allowance||0)}">${money(base)}<br><span style="font-size:10px;color:var(--muted)">${money(e.daily_rate)}/day${allowances>0?" + "+money(allowances)+" allow.":""}</span></td>
            <td><input class="pay-bonus-input" data-emp-id="${numberValue(e.employee_id)}" type="number" min="0" step="0.01" value="0" style="width:86px;background:var(--card2);border:1px solid var(--border2);border-radius:8px;color:var(--text);padding:6px" oninput="updatePayrollPreviewNet(this)"></td>
            <td style="font-family:var(--mono);color:var(--danger)" data-day-ded="${dayDed}">${numberValue(e.pending_day_deduction_days)}d / ${money(dayDed)}</td>
            <td style="font-family:var(--mono);color:var(--danger)" data-manual-ded="${manDed}">${money(manDed)}</td>
            <td style="font-family:var(--mono);color:var(--warn);font-weight:700" data-loan-bal="${loanBal}">${loanBal > 0 ? money(loanBal) : "—"}</td>
            <td class="mono pay-net-preview" style="font-weight:700;color:${net<0?"var(--danger)":"var(--green)"}" data-base="${base}" data-loan="${loanBal}">${money(net)}</td>
            <td><span style="font-size:11px;color:${e.already_run?"var(--warn)":"var(--muted)"}">${e.already_run?"Will update":"New"}</span></td>
        </tr>`;
    }).join("") +
        `<tr style="background:var(--card2)">
            <td colspan="6" style="font-weight:700;color:var(--sub)">Total to Pay</td>
            <td style="font-family:var(--mono);font-size:16px;font-weight:700;color:var(--green)">${money(d.total_to_pay)}</td>
            <td></td>
        </tr>`;
}

function updatePayrollPreviewNet(input){
    const row = input.closest("tr");
    if(!row) return;
    const netCell = row.querySelector(".pay-net-preview");
    const base    = numberValue(netCell?.dataset.base);
    const loan    = numberValue(netCell?.dataset.loan);
    const bonus   = numberValue(row.querySelector(".pay-bonus-input")?.value);
    const day     = numberValue(row.querySelector("[data-day-ded]")?.dataset.dayDed);
    const manual  = numberValue(row.querySelector("[data-manual-ded]")?.dataset.manualDed);
    const net     = base + bonus - day - manual - loan;
    netCell.innerText = money(net);
    netCell.style.color = net < 0 ? "var(--danger)" : "var(--green)";
}

async function confirmRunPayroll(){
    let period = document.getElementById("pay-period").value;
    if(!period){ showToast("Select a period first"); return; }
    const bonuses = {};
    document.querySelectorAll(".pay-bonus-input").forEach(input => {
        const value = numberValue(input.value);
        if(value > 0) bonuses[numberValue(input.dataset.empId)] = value;
    });
    const loan_repayments = {};
    document.querySelectorAll("[data-loan-bal]").forEach(cell => {
        const value = numberValue(cell.dataset.loanBal);
        const empId = numberValue(cell.closest("tr")?.querySelector(".pay-bonus-input")?.dataset.empId);
        if(value > 0 && empId) loan_repayments[empId] = value;
    });
    let res  = await fetch("/hr/api/payroll/run",{
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({period, bonuses, loan_repayments}),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    showToast(`Payroll saved - ${data.created} created, ${data.skipped} updated`);
    loadPayrollRecords();
}

function closeRunPayModal(){
    document.getElementById("pay-run-modal").classList.remove("open");
}

async function resetPayrollMonth(){
    if(!hasPermission("action_hr_reset_payroll")) return;
    const period = document.getElementById("pay-period").value;
    if(!period){ showToast("Pick a period first"); return; }
    const typed = prompt(
        `This deletes ALL payroll runs for ${period} — paid and unpaid.\n` +
        `Paid runs: their salary expenses are deleted and journals reversed.\n` +
        `Loan repayments are removed (loans reopen), settled advances reopen,\n` +
        `and this month's deductions are deleted. Attendance is NOT touched.\n\n` +
        `Type the period (${period}) to confirm:`
    );
    if(typed === null) return;
    try{
        const res = await fetch("/hr/api/payroll/reset-period",{
            method:"POST",headers:{"Content-Type":"application/json"},
            body:JSON.stringify({period, confirm:(typed||"").trim()}),
        });
        const data = await readApiResponse(res);
        showToast(data.deleted_runs
            ? `${period} reset — ${data.deleted_runs} runs deleted, ${data.deleted_expenses} expenses unwound`
            : `No payroll runs found for ${period}`);
        await loadPayrollPreview();
        if(typeof loadPayrollRecords === "function") await loadPayrollRecords();
    }catch(err){ showToast("Error: "+(err.message||"Could not reset period")); }
}

async function runPayroll(){
    const modalPeriod = document.getElementById("pr-period").value;
    if(modalPeriod) document.getElementById("pay-period").value = modalPeriod;
    closeRunPayModal();
    await confirmRunPayroll();
}

async function loadPayrollRecords(){
    let period = document.getElementById("pay-period").value;
    let records = await (await fetch(`/hr/api/payroll${period?"?period="+period:""}`)).json();
    window._payrollRecords = records;
    document.getElementById("payroll-preview-wrap").style.display = "none";
    document.getElementById("payroll-records-wrap").style.display = "";

    if(!records.length){
        document.getElementById("pay-body").innerHTML =
            `<tr><td colspan="12" style="text-align:center;color:var(--muted);padding:40px">No payroll records. Use preview above to generate.</td></tr>`;
        return;
    }
    let totalNet = records.reduce((s,r)=>s + (r.paid ? 0 : numberValue(r.net_salary)), 0);
    let totalPaid = records.reduce((s,r)=>s + (r.paid ? numberValue(r.paid_amount!=null ? r.paid_amount : r.net_salary) : 0), 0);
    document.getElementById("pay-body").innerHTML = records.map(r=>`
        <tr>
            <td class="name">${displayText(r.employee)}</td>
            <td style="font-family:var(--mono);font-size:12px;color:var(--muted)">${displayText(r.period)}</td>
            <td style="font-family:var(--mono)">${money(r.base_salary)}</td>
            <td style="font-family:var(--mono);color:var(--sub)">${r.days_worked ? numberValue(r.days_worked) : "-"} / ${r.working_days ? numberValue(r.working_days) : "-"}</td>
            <td style="font-family:var(--mono);color:var(--green)">+${money(r.bonuses)}</td>
            <td style="font-family:var(--mono);color:var(--danger)">-${money(r.loan_deductions)}</td>
            <td style="font-family:var(--mono);color:var(--danger)">${numberValue(r.day_deduction_days)}d / -${money(r.day_deductions)}</td>
            <td style="font-family:var(--mono);color:var(--danger)">-${money(r.manual_deductions)}</td>
            <td style="font-family:var(--mono);color:var(--danger)">-${money(r.deductions)}</td>
            <td style="font-family:var(--mono);font-size:15px;font-weight:700;color:var(--green)">${money(r.net_salary)}</td>
            <td>${r.paid?`<span class="paid-badge">Paid</span>${(r.paid_amount!=null && numberValue(r.paid_amount) < numberValue(r.net_salary))?`<div style="font-size:10px;color:var(--muted);margin-top:2px">cash ${money(r.paid_amount)}${numberValue(r.days_off_credited)>0?` · +${numberValue(r.days_off_credited).toFixed(2)}d off`:""}</div>`:""}`:`<span class="unpaid-badge">Pending</span>`}</td>
            <td style="display:flex;gap:6px">
                <button class="action-btn purple" onclick="openEditPayFromButton(this)" data-id="${numberValue(r.id)}" data-employee="${escapeHtml(normalizeDashFallback(r.employee))}" data-bonuses="${numberValue(r.bonuses)}" data-deductions="${numberValue(r.manual_deductions)}">Edit</button>
                ${!r.paid && hasPermission("action_hr_mark_paid")?`<button class="action-btn green" onclick="markPaid(${numberValue(r.id)})">Mark Paid</button>`:""}
            </td>
        </tr>`).join("") +
        `<tr style="background:var(--card2)">
            <td colspan="9" style="font-weight:700;color:var(--sub)">Remaining to Pay${totalPaid>0?` <span style="font-weight:400;color:var(--muted);font-size:12px">(already paid ${money(totalPaid)})</span>`:""}</td>
            <td style="font-family:var(--mono);font-size:16px;font-weight:700;color:var(--green)">${money(totalNet)}</td>
            <td colspan="2"></td>
        </tr>`;
}

function openEditPayFromButton(btn){
    openEditPayModal(
        numberValue(btn.dataset.id),
        btn.dataset.employee || "",
        numberValue(btn.dataset.bonuses),
        numberValue(btn.dataset.deductions)
    );
}

function openEditPayModal(id,empName,bonuses,deductions){
    editingPayId = id;
    document.getElementById("edit-pay-emp").innerText  = empName;
    document.getElementById("ep-bonuses").value        = bonuses;
    document.getElementById("ep-deductions").value     = deductions;
    document.getElementById("ep-notes").value          = "";
    document.getElementById("edit-pay-modal").classList.add("open");
}
function closeEditPayModal(){ document.getElementById("edit-pay-modal").classList.remove("open"); }

async function savePayrollEdit(){
    let body = {
        bonuses:    parseFloat(document.getElementById("ep-bonuses").value)||0,
        deductions: parseFloat(document.getElementById("ep-deductions").value)||0,
        notes:      document.getElementById("ep-notes").value.trim()||null,
    };
    let res  = await fetch(`/hr/api/payroll/${editingPayId}`,{
        method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body),
    });
    let data = await res.json();
    if(data.detail){ showToast("Error: "+data.detail); return; }
    closeEditPayModal();
    showToast(`Payroll updated. Net: ${money(data.net_salary)}`);
    loadPayrollRecords();
}

function markPaid(id){
    if(!hasPermission("action_hr_mark_paid")) return;
    const rec = (window._payrollRecords||[]).find(r=>numberValue(r.id)===numberValue(id));
    const net  = rec ? numberValue(rec.net_salary) : 0;
    const rate = rec ? numberValue(rec.daily_rate) : 0;
    openPayModal(id, net, rate);
}

function closePayModal(){
    const o = document.getElementById("pay-modal-overlay");
    if(o) o.remove();
}

function openPayModal(id, net, rate){
    closePayModal();
    const overlay = document.createElement("div");
    overlay.id = "pay-modal-overlay";
    overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:9999";
    overlay.innerHTML = `
      <div style="background:var(--card2,#fff);color:var(--text,#111);padding:20px;border-radius:12px;width:min(92vw,360px);box-shadow:0 12px 40px rgba(0,0,0,.35)">
        <div style="font-weight:700;font-size:16px;margin-bottom:10px">Pay salary</div>
        <div style="font-size:13px;color:var(--muted,#888);margin-bottom:10px">Net owed: <b>${money(net)} EGP</b></div>
        <label style="font-size:13px;display:block;margin-bottom:4px">Cash amount to pay</label>
        <input id="pay-amount-input" type="number" step="0.01" min="0" max="${net}" value="${net}" style="width:100%;padding:8px;border:1px solid var(--border,#ccc);border-radius:8px;margin-bottom:10px;background:transparent;color:inherit">
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-bottom:6px;cursor:pointer">
          <input id="pay-convert-toggle" type="checkbox"> Convert remaining to paid days off
        </label>
        <div id="pay-days-hint" style="font-size:12px;color:var(--muted,#888);min-height:18px;margin-bottom:14px"></div>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button onclick="closePayModal()" style="padding:8px 14px;border-radius:8px;border:1px solid var(--border,#ccc);background:none;color:inherit;cursor:pointer">Cancel</button>
          <button id="pay-confirm-btn" style="padding:8px 14px;border-radius:8px;border:none;background:var(--green,#16a34a);color:#fff;cursor:pointer;font-weight:600">Confirm payment</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const amt  = document.getElementById("pay-amount-input");
    const tog  = document.getElementById("pay-convert-toggle");
    const hint = document.getElementById("pay-days-hint");
    function refresh(){
        const paid = numberValue(amt.value);
        const remaining = Math.max(0, net - paid);
        if(tog.checked && remaining>0 && rate>0){
            hint.innerText = `Remaining ${money(remaining)} EGP \u2192 ${(remaining/rate).toFixed(2)} days off credited (rate ${money(rate)}/day)`;
        } else if(remaining>0){
            hint.innerText = `Remaining ${money(remaining)} EGP will not be paid`;
        } else {
            hint.innerText = "";
        }
    }
    amt.addEventListener("input", refresh);
    tog.addEventListener("change", refresh);
    refresh();
    document.getElementById("pay-confirm-btn").onclick = function(){ confirmPay(id, net); };
}

async function confirmPay(id, net){
    const amt = numberValue(document.getElementById("pay-amount-input").value);
    const convert = document.getElementById("pay-convert-toggle").checked;
    if(amt<=0 || amt>net){ showToast("Enter an amount between 0 and "+money(net)); return; }
    try{
        const res = await fetch(`/hr/api/payroll/${id}/pay`,{
            method:"PATCH",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({payment_method:"cash", paid_amount:amt, convert_remainder_to_days_off:convert})
        });
        const data = await res.json();
        if(data.detail){ showToast("Error: "+data.detail); return; }
        let msg = `Paid ${money(data.paid_amount!=null?data.paid_amount:amt)} EGP`;
        if(numberValue(data.days_off_credited)>0) msg += ` \u2014 ${numberValue(data.days_off_credited).toFixed(2)} days off credited`;
        showToast(data.warning || msg);
        closePayModal();
        loadPayrollRecords();
    }catch(err){ showToast("Error: "+(err.message||"Could not record payment")); }
}

/* ── CLEAR HR DATA ── */
function openClearHRDataModal(){
    if(!hasPermission("action_hr_clear_data")){
        showToast("Permission denied: action_hr_clear_data");
        return;
    }
    const input = document.getElementById("clear-hr-confirmation");
    if(input) input.value = "";
    updateClearHRDataConfirmState();
    document.getElementById("clear-hr-modal").classList.add("open");
    if(input) input.focus();
}

function closeClearHRDataModal(){
    document.getElementById("clear-hr-modal").classList.remove("open");
    const input = document.getElementById("clear-hr-confirmation");
    if(input) input.value = "";
    updateClearHRDataConfirmState();
}

function updateClearHRDataConfirmState(){
    const input = document.getElementById("clear-hr-confirmation");
    const btn = document.getElementById("btn-confirm-clear-hr");
    if(!btn || !input) return;
    btn.disabled = input.value !== "CLEAR HR DATA";
}

function clearHRForms(){
    editingEmpId = null;
    editingPayId = null;
    ["e-name","e-position","e-department","e-phone","e-salary","e-hire","a-note","ep-bonuses","ep-deductions","ep-notes"].forEach(id => {
        const el = document.getElementById(id);
        if(el) el.value = "";
    });
    fillEmployeeFarmSelect("");
    const today = new Date().toISOString().split("T")[0];
    const aDate = document.getElementById("a-date");
    if(aDate) aDate.value = today;
}

function activeHRTab(){
    if(document.getElementById("tab-att").classList.contains("active")) return "attendance";
    if(document.getElementById("tab-pay").classList.contains("active")) return "payroll";
    return "employees";
}

async function refreshHRDataAfterClear(){
    clearHRForms();
    employees = [];
    await loadSummary();
    await loadEmployeeFarms();
    await loadEmployees();
    const active = activeHRTab();
    if(active === "attendance" && hasPermission("tab_hr_attendance")) initAttendanceTab();
    if(active === "payroll" && hasPermission("tab_hr_payroll")) initPayrollTab();
}

async function confirmClearHRData(){
    if(!hasPermission("action_hr_clear_data")){
        showToast("Permission denied: action_hr_clear_data");
        return;
    }
    const input = document.getElementById("clear-hr-confirmation");
    const btn = document.getElementById("btn-confirm-clear-hr");
    if(!input || input.value !== "CLEAR HR DATA"){
        showToast("Type CLEAR HR DATA to confirm");
        updateClearHRDataConfirmState();
        return;
    }
    btn.disabled = true;
    try{
        const res = await fetch("/hr/clear-data", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({confirmation: input.value}),
        });
        const data = await readApiResponse(res);
        closeClearHRDataModal();
        await refreshHRDataAfterClear();
        const d = data.deleted || {};
        showToast(`HR data cleared - ${numberValue(d.employees)} employees, ${numberValue(d.attendance)} attendance, ${numberValue(d.payroll)} payroll, ${numberValue(d.hr_expenses)} payroll expenses`);
    }catch(err){
        showToast("Error: " + (err.message || "Could not clear HR data"));
        updateClearHRDataConfirmState();
    }
}

/* ── MODAL CLOSE ON BG ── */
["emp-modal","att-modal","pay-run-modal","edit-pay-modal","loan-deduction-modal","allowance-advance-modal","clear-hr-modal"].forEach(id=>{
    document.getElementById(id).addEventListener("click",function(e){
        if(e.target!==this) return;
        if(id === "clear-hr-modal") closeClearHRDataModal();
        else this.classList.remove("open");
    });
});

let toastTimer=null;
function showToast(msg){
    let t=document.getElementById("toast");
    t.innerText=msg; t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer=setTimeout(()=>t.classList.remove("show"),3500);
}

init();
</script>
</body>
</html>
"""
    return HTMLResponse(content=html_content, media_type="text/html; charset=utf-8")