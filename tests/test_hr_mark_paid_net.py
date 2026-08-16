"""Marking payroll paid — the net the server checks against must be the net
the screen showed.

The payroll list recomputes net from its parts so that rows written before
allowances were folded into payroll still display correctly. Marking paid used
to validate against the *stored* net instead, so for any employee with an
allowance the two disagreed and a full payment was rejected with
"Payment can't exceed the net salary."
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
from app.models.hr import Employee, EmployeeAllowanceAdvance, Payroll
from app.models.user import User


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement, params=None):
        return self.session.execute(statement, params or {})

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def refresh(self, obj):
        self.session.refresh(obj)

    async def flush(self):
        self.session.flush()

    def add(self, obj):
        self.session.add(obj)


def run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        User.__table__, Employee.__table__, Payroll.__table__,
        EmployeeAllowanceAdvance.__table__, ActivityLog.__table__,
    ])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def seed(session, *, stored_net=Decimal("3000.00"), food=Decimal("600"), transport=Decimal("400")):
    """Mona earns 3,000 base with a 600 food + 400 transport allowance.

    She worked every one of the 30 days, so the earned allowance is the full
    1,000 and her true net is 4,000 — but the stored net says 3,000, exactly
    the shape a row written by the older build has.
    """
    session.add(User(id=1, name="Admin", email="a@x.example", password="x", role="admin"))
    session.add(Employee(id=1, name="Mona Field", phone="0100", position="Grower",
                         hire_date=date(2024, 1, 5), base_salary=Decimal("3000"),
                         food_allowance=food, transportation_allowance=transport,
                         is_active=True))
    session.flush()
    session.add(Payroll(id=1, employee_id=1, period="2026-08",
                        base_salary=Decimal("3000"), bonuses=Decimal("0"),
                        deductions=Decimal("0"), net_salary=stored_net,
                        paid=False, days_worked=30, working_days=30))
    session.commit()


def listed_net(session, payroll_id=1):
    """The net the payroll screen shows for this row."""
    rows = run(hr.get_payroll(period="2026-08", db=AsyncSessionAdapter(session)))
    return next(r["net_salary"] for r in rows if r["id"] == payroll_id)


def mark_paid(session, amount, payroll_id=1, monkeypatch=None):
    request = SimpleNamespace(payment_method="cash", paid_amount=Decimal(str(amount)),
                             convert_remainder_to_days_off=False)
    return run(hr.mark_paid(
        payroll_id, request, AsyncSessionAdapter(session),
        SimpleNamespace(id=1, name="Admin", role="admin"),
    ))


def stub_payroll_expense(monkeypatch):
    """create_payroll_expense posts a journal and needs the accounting tables;
    the behaviour under test is the net-salary check, so it is stubbed out."""
    async def fake(db, payroll, current_user, **kwargs):
        return SimpleNamespace(
            id=1, ref_number="EXP-00001", amount=kwargs.get("amount_override") or 0,
            farm_id=None, farm=None, category=SimpleNamespace(name="Salaries & Wages"),
        )
    monkeypatch.setattr(hr, "create_payroll_expense", fake)


def test_listed_net_includes_the_allowance(monkeypatch):
    with make_session() as session:
        seed(session)
        assert listed_net(session) == 4000.0        # 3000 + 1000 allowance


def test_paying_the_amount_the_screen_showed_is_accepted(monkeypatch):
    """The regression: the screen offers 4,000, so paying 4,000 must work even
    though the stored net still says 3,000."""
    stub_payroll_expense(monkeypatch)
    with make_session() as session:
        seed(session)
        shown = listed_net(session)
        result = mark_paid(session, shown)

    assert result["ok"] is True
    assert result["paid_amount"] == 4000.0


def test_stored_net_is_healed_so_the_record_matches_what_was_paid(monkeypatch):
    stub_payroll_expense(monkeypatch)
    with make_session() as session:
        seed(session)
        mark_paid(session, listed_net(session))
        payroll = session.get(Payroll, 1)
        session.refresh(payroll)

    assert float(payroll.net_salary) == 4000.0
    assert float(payroll.paid_amount) == 4000.0


def test_overpayment_is_still_rejected(monkeypatch):
    """The guard must keep working — it just has to use the right number."""
    from fastapi import HTTPException
    stub_payroll_expense(monkeypatch)
    with make_session() as session:
        seed(session)
        try:
            mark_paid(session, 4000.01)
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "exceed" in exc.detail
        else:
            raise AssertionError("overpayment was accepted")


def test_partial_payment_still_allowed(monkeypatch):
    stub_payroll_expense(monkeypatch)
    with make_session() as session:
        seed(session)
        result = mark_paid(session, 1500)

    assert result["paid_amount"] == 1500.0


def test_open_allowance_advance_reduces_the_net_that_can_be_paid(monkeypatch):
    """An advance already handed over is settled against the allowance, so the
    payable net drops — and the check must follow it down."""
    from fastapi import HTTPException
    stub_payroll_expense(monkeypatch)
    with make_session() as session:
        seed(session)
        session.add(EmployeeAllowanceAdvance(employee_id=1, amount=Decimal("400"),
                                             advance_date=date(2026, 8, 3), status="open"))
        session.commit()
        shown = listed_net(session)
        assert shown == 3600.0                      # 3000 + (1000 − 400 advance)
        result = mark_paid(session, shown)
        assert result["paid_amount"] == 3600.0

        session.rollback()

    with make_session() as session:
        seed(session)
        session.add(EmployeeAllowanceAdvance(employee_id=1, amount=Decimal("400"),
                                             advance_date=date(2026, 8, 3), status="open"))
        session.commit()
        try:
            mark_paid(session, 4000)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("payment above the advance-reduced net was accepted")


def test_advance_already_settled_against_this_run_is_not_handed_back(monkeypatch):
    """An advance settled on the first run flips from 'open' to 'deducted'.
    Only counting open advances would make the recomputed allowance jump back
    up, and the employee would be paid the advance a second time."""
    from fastapi import HTTPException
    stub_payroll_expense(monkeypatch)
    with make_session() as session:
        seed(session)
        session.add(EmployeeAllowanceAdvance(employee_id=1, amount=Decimal("400"),
                                             advance_date=date(2026, 8, 3),
                                             status="deducted", payroll_id=1))
        session.commit()
        # 3000 base + (1000 allowance − 400 already advanced) = 3600, not 4000
        assert listed_net(session) == 3600.0
        try:
            mark_paid(session, 4000)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("the advance was effectively paid twice")


def test_error_message_states_the_payable_net(monkeypatch):
    from fastapi import HTTPException
    stub_payroll_expense(monkeypatch)
    with make_session() as session:
        seed(session)
        try:
            mark_paid(session, 9999)
        except HTTPException as exc:
            assert "4,000.00" in exc.detail
        else:
            raise AssertionError("overpayment was accepted")


def test_employees_without_allowances_are_unaffected(monkeypatch):
    stub_payroll_expense(monkeypatch)
    with make_session() as session:
        seed(session, stored_net=Decimal("3000.00"), food=Decimal("0"), transport=Decimal("0"))
        assert listed_net(session) == 3000.0
        result = mark_paid(session, 3000)

    assert result["paid_amount"] == 3000.0
