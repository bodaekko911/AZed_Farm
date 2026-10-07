"""Expenses page search — on the server, combined with the other filters."""

import asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.database import Base
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm
from app.services.expense_service import list_expenses


class AsyncSessionAdapter:
    def __init__(self, session):
        self.s = session

    async def execute(self, statement, params=None):
        return self.s.execute(statement, params or {})


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all([
        Farm(id=1, name="Organic Farm", is_active=1),
        Farm(id=4, name="Habiba/SPC", is_active=1),
        ExpenseCategory(id=1, name="Maintenance & Repairs", account_code="5100", is_active="1"),
        ExpenseCategory(id=2, name="Fuel & Transportation", account_code="5300", is_active="1"),
        Expense(id=1, ref_number="EXP-0101", category_id=1, farm_id=4, amount=Decimal("5000"),
                expense_date=date(2026, 9, 4), vendor="El Nour Steel", description="Greenhouse frame"),
        Expense(id=2, ref_number="EXP-0102", category_id=2, farm_id=None, amount=Decimal("1500"),
                expense_date=date(2026, 9, 6), vendor="Wataniya", description="Diesel for the truck"),
        Expense(id=3, ref_number="EXP-0103", category_id=1, farm_id=1, amount=Decimal("1500.50"),
                expense_date=date(2026, 8, 1), vendor="", description="Pump repair"),
    ])
    session.commit()
    return AsyncSessionAdapter(session)


def refs(db, **kw):
    loop = asyncio.new_event_loop()
    try:
        return [e["ref_number"] for e in loop.run_until_complete(list_expenses(db, **kw))]
    finally:
        loop.close()


def test_search_matches_ref_vendor_description_category_and_farm():
    db = make_db()
    assert refs(db, q="EXP-0102") == ["EXP-0102"]
    assert refs(db, q="nour") == ["EXP-0101"]                    # vendor, any case
    assert refs(db, q="greenhouse") == ["EXP-0101"]              # description
    assert refs(db, q="fuel") == ["EXP-0102"]                    # category
    assert refs(db, q="SPC") == ["EXP-0101"]                     # farm
    assert refs(db, q="repair") == ["EXP-0101", "EXP-0103"]      # category or description, newest first
    assert refs(db, q="nothing like this") == []


def test_a_number_finds_that_exact_amount():
    db = make_db()
    assert refs(db, q="1500") == ["EXP-0102"]
    assert refs(db, q="1,500.50") == ["EXP-0103"]


def test_search_combines_with_dates_and_category():
    db = make_db()
    assert refs(db, q="repair", date_from="2026-09-01") == ["EXP-0101"]
    assert refs(db, q="repair", category_id=2) == []
    assert len(refs(db)) == 3
