"""Ask actions for the farm's daily work: a delivery, spoilage and a B2B payment.

Each is only proposed — nothing is written until Confirm — and then runs the
same code as its own page, so stock, journals and logs are exactly what that
page would have produced. Run on a plain SQLite session behind the async API.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.models  # noqa: F401  every table
import app.models.drying  # noqa: F401
from app.database import Base
from app.models.b2b import B2BClient, B2BInvoice
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.product import Product
from app.models.spoilage import SpoilageRecord
from app.services import assistant_actions as act
from app.services import assistant_service


class AsyncSessionAdapter:
    def __init__(self, session):
        self.s = session

    async def execute(self, statement, params=None):
        return self.s.execute(statement, params or {})

    async def commit(self):
        self.s.commit()

    async def rollback(self):
        self.s.rollback()

    async def flush(self):
        self.s.flush()

    async def refresh(self, obj, *a, **k):
        self.s.refresh(obj)

    async def delete(self, obj):
        self.s.delete(obj)

    def add(self, obj):
        self.s.add(obj)


def admin():
    return SimpleNamespace(id=1, name="Abdallah", role="admin", permissions=None)


def farmer():
    """Can record deliveries, nothing else."""
    return SimpleNamespace(id=2, name="Field", role="viewer", permissions="page_farm,action_farm_delivery_create")


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all([
        Farm(id=1, name="North Farm", is_active=1), Farm(id=2, name="South Farm", is_active=1),
        Product(id=1, sku="TOM", name="Tomatoes", unit="kg", price=Decimal("20"), cost=Decimal("8"),
                stock=Decimal("50"), min_stock=Decimal("5"), is_active=True),
        Product(id=2, sku="BAS", name="Basil", unit="gram", price=Decimal("1"), cost=Decimal("0.2"),
                stock=Decimal("500"), min_stock=Decimal("5"), is_active=True),
        B2BClient(id=1, name="Joud & Bahaa", outstanding=Decimal("1500")),
    ])
    session.flush()
    old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    session.add_all([
        B2BInvoice(id=1, invoice_number="B2B-001", client_id=1, invoice_type="credit", status="unpaid",
                   total=Decimal("1000"), amount_paid=Decimal("0"), created_at=old),
        B2BInvoice(id=2, invoice_number="B2B-002", client_id=1, invoice_type="credit", status="partial",
                   total=Decimal("800"), amount_paid=Decimal("300"), created_at=old + timedelta(days=5)),
        B2BInvoice(id=3, invoice_number="B2B-003", client_id=1, invoice_type="consignment", status="unpaid",
                   total=Decimal("400"), amount_paid=Decimal("0"), created_at=old - timedelta(days=5)),
    ])
    session.commit()
    yield session, AsyncSessionAdapter(session)
    asyncio.set_event_loop(asyncio.new_event_loop())


def propose(adb, user, name, **args):
    text, card = run(act.propose(adb, user, name, json.dumps(args)))
    return json.loads(text), card


# --- farm delivery ------------------------------------------------------------------

def test_a_delivery_is_proposed_then_recorded_once_with_stock(db):
    session, adb = db
    note, card = propose(adb, admin(), "propose_farm_delivery", farm="north", date="2026-10-09",
                         items=[{"product": "tomatoes", "qty": 120}, {"product": "basil", "qty": 0.5, "unit": "kg"}])
    assert "NOT done" in note["status"]
    assert dict(card["lines"])["Tomatoes"] == "120 kg"
    assert dict(card["lines"])["Basil"] == "500 gram"                 # 0.5 kg in the product's own unit
    assert session.execute(select(FarmDelivery)).scalars().all() == []

    out = run(act.execute(adb, admin(), card["token"]))
    assert out["ok"] and "FD-0001" in out["message"]
    delivery = session.execute(select(FarmDelivery)).scalar_one()
    assert (delivery.farm_id, str(delivery.delivery_date)) == (1, "2026-10-09")
    assert sorted(float(i.qty) for i in session.execute(select(FarmDeliveryItem)).scalars()) == [120.0, 500.0]
    tom = session.get(Product, 1)
    session.refresh(tom)
    assert float(tom.stock) == 170.0
    with pytest.raises(HTTPException) as again:
        run(act.execute(adb, admin(), card["token"]))
    assert again.value.status_code == 409


def test_a_delivery_refuses_what_it_cannot_read(db):
    _s, adb = db
    note, card = propose(adb, admin(), "propose_farm_delivery", farm="north", items=[{"product": "basil", "qty": 3,
                                                                                    "unit": "crate"}])
    assert card is None and "not crate" in note["error"]
    note, card = propose(adb, admin(), "propose_farm_delivery", farm="farm", items=[{"product": "basil", "qty": 3}])
    assert card is None and "could be several" in note["error"]
    future = (datetime.now() + timedelta(days=3)).date().isoformat()
    note, _c = propose(adb, admin(), "propose_farm_delivery", farm="north", date=future,
                       items=[{"product": "basil", "qty": 3}])
    assert "future" in note["error"]


# --- spoilage ---------------------------------------------------------------------------

def test_spoilage_is_proposed_then_logged_and_takes_stock_out(db):
    session, adb = db
    note, card = propose(adb, admin(), "propose_spoilage", product="tomatoes", qty=10, reason="too warm",
                         farm="north")
    lines = dict(card["lines"])
    assert lines["Stock"] == "50 → 40 kg" and lines["Loss at cost"] == "80.00 EGP"
    out = run(act.execute(adb, admin(), card["token"]))
    assert out["ok"] and "SPL-0001" in out["message"]
    rec = session.execute(select(SpoilageRecord)).scalar_one()
    assert (float(rec.qty), rec.reason, rec.farm_id) == (10.0, "too warm", 1)
    tom = session.get(Product, 1)
    session.refresh(tom)
    assert float(tom.stock) == 40.0


def test_spoilage_cannot_exceed_stock(db):
    _s, adb = db
    note, card = propose(adb, admin(), "propose_spoilage", product="tomatoes", qty=60)
    assert card is None and "Only 50 kg" in note["error"]


# --- B2B payment ------------------------------------------------------------------------

def test_a_payment_goes_to_the_oldest_invoices_first(db):
    session, adb = db
    _n, card = propose(adb, admin(), "propose_b2b_payment", client="joud", amount=1200)
    lines = dict(card["lines"])
    assert lines["Invoice B2B-001"] == "1,000.00 of 1,000.00 (settles it)"
    assert lines["Invoice B2B-002"] == "200.00 of 500.00 (300.00 left)"
    assert "B2B-003" not in " ".join(lines)                          # consignment: settled on the B2B page
    out = run(act.execute(adb, admin(), card["token"]))
    assert out["ok"] and "B2B-001, B2B-002" in out["message"]
    inv1, inv2 = session.get(B2BInvoice, 1), session.get(B2BInvoice, 2)
    session.refresh(inv1), session.refresh(inv2)
    assert (inv1.status, float(inv1.amount_paid)) == ("paid", 1000.0)
    assert (inv2.status, float(inv2.amount_paid)) == ("partial", 500.0)
    client = session.get(B2BClient, 1)
    session.refresh(client)
    assert float(client.outstanding) == 300.0


def test_a_named_invoice_and_an_overpayment(db):
    _s, adb = db
    _n, card = propose(adb, admin(), "propose_b2b_payment", client="joud", amount=500, invoice="B2B-002")
    assert list(dict(card["lines"])) == ["Client", "Amount", "Invoice B2B-002", "Still owed after"]
    note, card = propose(adb, admin(), "propose_b2b_payment", client="joud", amount=5000)
    assert card is None and "owes 1,500.00" in note["error"]


def test_a_payment_is_refused_if_the_invoices_changed_meanwhile(db):
    session, adb = db
    _n, card = propose(adb, admin(), "propose_b2b_payment", client="joud", amount=100)
    inv = session.get(B2BInvoice, 1)
    inv.amount_paid = Decimal("50")                                    # paid elsewhere in the meantime
    session.commit()
    with pytest.raises(HTTPException) as changed:
        run(act.execute(adb, admin(), card["token"]))
    assert changed.value.status_code == 409


# --- permissions ------------------------------------------------------------------------------

def test_each_action_follows_its_pages_permission(db):
    _s, adb = db
    assert "propose_farm_delivery" in assistant_service.allowed_actions(farmer())
    assert "propose_spoilage" not in assistant_service.allowed_actions(farmer())
    note, card = propose(adb, farmer(), "propose_b2b_payment", client="joud", amount=10)
    assert card is None and "permission" in note["error"]


# --- a shop sale typed in chat ----------------------------------------------------------------

def _sale(**args):
    return {"items": [{"product": "tomatoes", "qty": 2}], **args}


def test_a_typed_sale_is_proposed_then_recorded_as_a_pos_sale(db):
    from app.models.invoice import Invoice, InvoiceItem
    session, adb = db
    note, card = propose(adb, admin(), "propose_pos_sale", items=[{"product": "tomatoes", "qty": 2},
                                                                 {"product": "basil", "qty": 0.1, "unit": "kg"}],
                         payment_method="cash")
    lines = dict(card["lines"])
    assert lines["Customer"] == "Walk-in" and lines["Payment"] == "Cash"
    assert lines["Tomatoes"] == "2 kg × 20 = 40.00 EGP"
    assert lines["Basil"] == "100 gram × 1 = 100.00 EGP"          # 0.1 kg in the product's own unit
    assert lines["Total"] == "140.00 EGP"
    assert session.execute(select(Invoice)).scalars().all() == []   # nothing until Confirm

    out = run(act.execute(adb, admin(), card["token"]))
    assert out["ok"] and "140.00" in out["message"]
    inv = session.execute(select(Invoice)).scalar_one()
    assert (inv.status, inv.payment_method, float(inv.total)) == ("paid", "cash", 140.0)
    assert sorted(float(i.qty) for i in session.execute(select(InvoiceItem)).scalars()) == [2.0, 100.0]
    tom = session.get(Product, 1)
    session.refresh(tom)
    assert float(tom.stock) == 48.0
    with pytest.raises(HTTPException) as again:
        run(act.execute(adb, admin(), card["token"]))
    assert again.value.status_code == 409


def test_a_sale_at_a_price_the_user_gave_and_a_past_date(db):
    from app.models.invoice import Invoice
    session, adb = db
    _n, card = propose(adb, admin(), "propose_pos_sale", items=[{"product": "tomatoes", "qty": 5, "line_total": 90}],
                       payment_method="visa", date="2026-10-01")
    assert dict(card["lines"])["Tomatoes"] == "5 kg × 18 (catalogue 20) = 90.00 EGP"
    run(act.execute(adb, admin(), card["token"]))
    inv = session.execute(select(Invoice)).scalar_one()
    assert (inv.payment_method, float(inv.total), str(inv.created_at)[:10]) == ("visa", 90.0, "2026-10-01")


def test_a_named_customer_pays_the_catalogue_price(db):
    from app.models.customer import Customer
    session, adb = db
    session.add(Customer(id=5, name="Ahmed Nour", phone="01001234567"))
    session.commit()
    note, card = propose(adb, admin(), "propose_pos_sale", customer="ahmed", items=[
        {"product": "tomatoes", "qty": 1, "unit_price": 15}])
    assert card is None and "catalogue price" in note["error"]
    _n, card = propose(adb, admin(), "propose_pos_sale", customer_phone="+20 100 123 4567", items=[
        {"product": "tomatoes", "qty": 1}])
    assert dict(card["lines"])["Customer"] == "Ahmed Nour"


def test_an_unknown_customer_is_asked_back_or_added_when_new(db):
    from app.models.customer import Customer
    session, adb = db
    note, card = propose(adb, admin(), "propose_pos_sale", **_sale(customer="Mariam Saleh"))
    assert card is None and "new customer" in note["error"]
    _n, card = propose(adb, admin(), "propose_pos_sale", **_sale(customer="Mariam Saleh", customer_phone="0122",
                                                               new_customer=True, paid=False))
    assert dict(card["lines"])["Customer"] == "Mariam Saleh (new customer)"
    run(act.execute(adb, admin(), card["token"]))
    assert session.execute(select(Customer).where(Customer.name == "Mariam Saleh")).scalar_one()


def test_a_sale_cannot_take_more_than_is_in_stock_or_pay_later_without_a_name(db):
    _s, adb = db
    note, card = propose(adb, admin(), "propose_pos_sale", items=[{"product": "tomatoes", "qty": 80}])
    assert card is None and "Not enough Tomatoes" in note["error"]
    note, card = propose(adb, admin(), "propose_pos_sale", **_sale(paid=False))
    assert card is None and "customer's name" in note["error"]


def test_a_sale_follows_the_pos_permissions(db):
    _s, adb = db
    cashier = SimpleNamespace(id=3, name="Till", role="cashier", permissions="page_pos,action_pos_create_sale")
    assert "propose_pos_sale" in assistant_service.allowed_actions(cashier)
    _n, card = propose(adb, cashier, "propose_pos_sale", **_sale(discount_percent=10))
    assert dict(card["lines"])["Total"] == "36.00 EGP"           # cashiers may give approved discounts
    no_discount = SimpleNamespace(id=4, name="Till", role="cashier", permissions="-action_pos_discount")
    note, card = propose(adb, no_discount, "propose_pos_sale", **_sale(discount_percent=10))
    assert card is None and "permission" in note["error"]
    note, card = propose(adb, farmer(), "propose_pos_sale", **_sale())
    assert card is None and "permission" in note["error"]
