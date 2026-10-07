"""P&L expenses filtered by farm; revenue stays company-wide."""

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.database import Base
from app.models.customer import Customer
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm
from app.models.invoice import Invoice
from app.routers.reports import _build_pl_report, parse_dates


class AsyncSessionAdapter:
    def __init__(self, session):
        self.s = session

    async def execute(self, statement, params=None):
        return self.s.execute(statement, params or {})


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all([
        Farm(id=1, name="Organic Farm", is_active=1),
        Farm(id=4, name="Habiba/SPC", is_active=1),
        ExpenseCategory(id=1, name="Maintenance & Repairs", account_code="5100", is_active="1"),
        ExpenseCategory(id=2, name="Salaries & Wages", account_code="5200", is_active="1"),
        Customer(id=1, name="Walk-in"),
        Invoice(id=1, invoice_number="INV-1", customer_id=1, status="paid", total=Decimal("10000"),
                created_at=datetime(2026, 9, 10, 12, tzinfo=timezone.utc)),
        Expense(category_id=1, farm_id=1, amount=Decimal("1000"), expense_date=date(2026, 9, 3)),
        Expense(category_id=1, farm_id=4, amount=Decimal("5000"), expense_date=date(2026, 9, 4),
                description="Greenhouse frame"),
        Expense(category_id=2, farm_id=None, amount=Decimal("3000"), expense_date=date(2026, 9, 5)),
    ])
    session.commit()
    return AsyncSessionAdapter(session)


def pl(db, farm=None):
    d_from, d_to = parse_dates("2026-09-01", "2026-09-30")
    return run(_build_pl_report(db, d_from=d_from, d_to=d_to, farm=farm))


def test_no_filter_shows_every_expense_with_its_farm():
    data = pl(make_db())
    assert data["total_expense"] == 9000.0
    assert data["farm_filtered"] is False
    farms = sorted(e["farm"] for line in data["expense_lines"] for e in line["entries"])
    assert farms == ["Habiba/SPC", "Organic Farm", "Shared (no farm)"]


def test_one_farm_two_farms_and_shared():
    db = make_db()
    spc = pl(db, "4")
    assert spc["total_expense"] == 5000.0
    assert spc["farm_filter"] == "Habiba/SPC"
    assert spc["total_revenue"] == 10000.0            # revenue is company-wide either way

    assert pl(db, "1,4")["total_expense"] == 6000.0
    shared = pl(db, "none")
    assert shared["total_expense"] == 3000.0
    assert shared["farm_filter"] == "Shared (no farm)"
    assert [l["name"] for l in shared["expense_lines"]] == ["Salaries & Wages"]


def test_an_unknown_farm_is_refused():
    with pytest.raises(HTTPException) as exc:
        pl(make_db(), "99")
    assert exc.value.status_code == 404
