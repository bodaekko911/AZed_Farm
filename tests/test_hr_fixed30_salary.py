"""Regression tests for the 'fixed_30' salary deal in `hr._earned_base`.

Two rules are locked down here, and they pull in opposite directions:

* An IN-PROGRESS month accrues (covered x salary/30). Six clean days on the
  6th of the month must show six days' pay, not a full month's — "nothing
  missed yet" is not the same as "the month is done".
* A COMPLETE month uses the deduction rule (salary - uncovered x salary/30),
  so a clean record lands on the exact base salary.

What makes a clean month reach full salary at close is that unlogged days
count as Day Offs drawn from the leave balance (see
test_hr_unlogged_days_are_day_off.py) — NOT treating an unfinished month as
if it were already over.
"""

from datetime import date
from decimal import Decimal

from app.models.hr import Employee

import app.routers.hr as hr


JULY = date(2026, 7, 1)
JULY_DAYS = 31
AUGUST = date(2026, 8, 1)


def emp(base_salary, basis="fixed_30", hire_date=date(2025, 1, 1)) -> Employee:
    return Employee(
        name="test",
        base_salary=Decimal(str(base_salary)),
        salary_days_basis=basis,
        hire_date=hire_date,
    )


# ── in-progress month: accrual ────────────────────────────────────────────
def test_early_in_the_month_does_not_pay_a_full_salary():
    """The August bug: 6 days worked, no absences, run on the 6th. Measuring
    absences against elapsed days made this read as 'nothing missed' and paid
    the whole month."""
    earned = hr._earned_base(
        emp(13500), days_present=6, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=6, month_start=AUGUST,
    )
    assert earned == Decimal("2700.00")         # 6 x 13500/30, not 13500


def test_in_progress_month_accrues_per_covered_day():
    for elapsed, expected in ((10, "4500.00"), (20, "9000.00"), (28, "12600.00")):
        assert hr._earned_base(
            emp(13500), days_present=elapsed, working_days=JULY_DAYS,
            paid_leave_days=0, days_elapsed=elapsed, month_start=JULY,
        ) == Decimal(expected)


def test_accrual_never_exceeds_the_base_salary():
    """31 covered days at salary/30 would overshoot without the cap."""
    assert hr._earned_base(
        emp(13500), days_present=31, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=30, month_start=JULY,
    ) == Decimal("13500.00")


# ── complete month: deduction rule ────────────────────────────────────────
def test_complete_month_fully_covered_pays_exactly_the_base_salary():
    """Taha's case at close: 28 worked + 3 unlogged days drawn from his leave
    balance as Day Offs = 31 covered, so nothing is docked."""
    assert hr._earned_base(
        emp(13500), days_present=28, working_days=JULY_DAYS,
        paid_leave_days=3, days_elapsed=JULY_DAYS, month_start=JULY,
    ) == Decimal("13500.00")


def test_complete_month_docks_one_thirtieth_per_uncovered_day():
    # 27 covered of 31 → four uncovered days at 450 each.
    assert hr._earned_base(
        emp(13500), days_present=27, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=JULY_DAYS, month_start=JULY,
    ) == Decimal("11700.00")


def test_february_full_attendance_pays_full_salary():
    """28 worked days in a 28-day month is a complete month, even though the
    daily rate is salary/30."""
    assert hr._earned_base(
        emp(13500), days_present=28, working_days=28,
        paid_leave_days=0, days_elapsed=28, month_start=date(2026, 2, 1),
    ) == Decimal("13500.00")


def test_paid_leave_counts_as_covered():
    """Days off drawn from the accrued balance are paid as if present."""
    assert hr._earned_base(
        emp(15000), days_present=21, working_days=JULY_DAYS,
        paid_leave_days=10, days_elapsed=JULY_DAYS, month_start=JULY,
    ) == Decimal("15000.00")


# ── mid-month hires ───────────────────────────────────────────────────────
def test_mid_month_hire_is_never_docked_for_pre_hire_days():
    """Hired 20 July → 9 days worked. The deduction rule assumes a full month
    of employment, so a partial month always accrues: he is neither docked for
    the 19 days before he was hired nor paid a full month for 9 days' work."""
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


# ── edges ─────────────────────────────────────────────────────────────────
def test_future_month_owes_nothing():
    assert hr._earned_base(
        emp(13500), days_present=0, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=0, month_start=JULY,
    ) == Decimal("0")


def test_earned_never_goes_negative():
    assert hr._earned_base(
        emp(13500), days_present=0, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=JULY_DAYS, month_start=JULY,
    ) == Decimal("0")


def test_calendar_basis_is_unaffected():
    """'calendar' employees still prorate by the real month length."""
    assert hr._earned_base(
        emp(10500, basis="calendar"), days_present=9, working_days=JULY_DAYS,
        paid_leave_days=0, days_elapsed=28, month_start=JULY,
    ) == Decimal("3048.39")                     # 10500 x 9/31
