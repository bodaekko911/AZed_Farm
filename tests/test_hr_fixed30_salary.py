"""Regression tests for the 'fixed_30' salary deal in `hr._earned_base`.

The bug these lock down: an in-progress month used to ACCRUE (covered x
salary/30) while a complete month used the deduction rule (salary - uncovered
x salary/30). The two formulas disagree, so the same recorded attendance
produced a different net depending on which day payroll was run — an employee
with a clean 28-of-28 record was paid 28/30 of his salary instead of all of it.
Absences are now always measured against the days that have ELAPSED.
"""

from datetime import date
from decimal import Decimal

from app.models.hr import Employee

import app.routers.hr as hr


JULY = date(2026, 7, 1)          # 31 days
JULY_DAYS = 31


def emp(base_salary, basis="fixed_30", hire_date=date(2025, 1, 1)) -> Employee:
    return Employee(
        name="test",
        base_salary=Decimal(str(base_salary)),
        salary_days_basis=basis,
        hire_date=hire_date,
    )


def test_clean_record_mid_month_pays_full_salary():
    """Taha's case: 28 days worked out of 28 elapsed, no absences, run on the
    28th. Nothing has been missed, so the full monthly salary is owed."""
    earned = hr._earned_base(
        emp(13500), days_present=28, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=28, month_start=JULY,
    )
    assert earned == Decimal("13500.00")


def test_run_date_does_not_change_the_net():
    """Same attendance, three different run dates → one answer. This is the
    discontinuity that made the net irreconcilable with the recorded days."""
    nets = {
        elapsed: hr._earned_base(
            emp(13500), days_present=elapsed, working_days=JULY_DAYS,
            paid_leave_days=0, days_elapsed=elapsed, month_start=JULY,
        )
        for elapsed in (10, 28, JULY_DAYS)
    }
    assert set(nets.values()) == {Decimal("13500.00")}, nets


def test_each_uncovered_day_docks_exactly_one_thirtieth():
    # 27 covered of 28 elapsed → one missed day at 13500/30 = 450.
    assert hr._earned_base(
        emp(13500), days_present=27, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=28, month_start=JULY,
    ) == Decimal("13050.00")

    # Month closed, 27 covered of 31 → four missed days.
    assert hr._earned_base(
        emp(13500), days_present=27, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=JULY_DAYS, month_start=JULY,
    ) == Decimal("11700.00")


def test_paid_leave_counts_as_covered():
    """Days off drawn from the accrued balance are paid as if present."""
    assert hr._earned_base(
        emp(15000), days_present=18, working_days=JULY_DAYS,
        paid_leave_days=10, days_elapsed=28, month_start=JULY,
    ) == Decimal("15000.00")


def test_february_full_attendance_pays_full_salary():
    """28 worked days in a 28-day month is a complete month, even though the
    daily rate is salary/30."""
    assert hr._earned_base(
        emp(13500), days_present=28, working_days=28,
        paid_leave_days=0, days_elapsed=28, month_start=date(2026, 2, 1),
    ) == Decimal("13500.00")


def test_mid_month_hire_accrues_and_is_never_docked_for_pre_hire_days():
    """Hired 20 July → 9 days worked by the 28th. The deduction rule assumes a
    full month of employment, so a partial month falls back to accrual: he is
    neither docked for the 19 days before he was hired nor paid a full month
    for 9 days of work."""
    new_hire = emp(10000, hire_date=date(2026, 7, 20))
    mid_month = hr._earned_base(
        new_hire, days_present=9, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=28, month_start=JULY,
    )
    month_end = hr._earned_base(
        new_hire, days_present=9, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=JULY_DAYS, month_start=JULY,
    )
    assert mid_month == Decimal("3000.00")      # 9 x 10000/30
    assert month_end == mid_month               # closing the month must not dock him


def test_future_month_owes_nothing():
    """Guard: with zero elapsed days there are zero uncovered days, so the
    deduction rule would otherwise hand out a full salary."""
    assert hr._earned_base(
        emp(13500), days_present=0, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=0, month_start=JULY,
    ) == Decimal("0")


def test_calendar_basis_is_unaffected():
    """'calendar' employees still prorate by the real month length."""
    assert hr._earned_base(
        emp(10500, basis="calendar"), days_present=9, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=28, month_start=JULY,
    ) == Decimal("3048.39")                     # 10500 x 9/31


def test_earned_never_goes_negative():
    assert hr._earned_base(
        emp(13500), days_present=0, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=JULY_DAYS, month_start=JULY,
    ) == Decimal("0")
