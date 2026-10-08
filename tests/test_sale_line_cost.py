"""The cost saved on each sale line — past margins stay put when costs change."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.database import Base
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.refund import RetailRefund, RetailRefundItem
from app.routers.reports import _build_profitability_report, parse_dates
from app.schemas.invoice import InvoiceCreate, InvoiceItemCreate
from app.services.product_profitability import ProfitabilityLedger
from app.services.sale_cost import cost_snapshot
from tests.test_pos_price_edit import _invoke, _make_user


@pytest.fixture(autouse=True)
def _leave_an_event_loop_behind():
    # asyncio.run() (used by the POS harness) leaves no current loop; later
    # test files call asyncio.get_event_loop() and need one to exist.
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


# ── Saving it ────────────────────────────────────────────────────────────────

def test_a_pos_sale_saves_the_products_cost_on_the_line():
    product = SimpleNamespace(id=1, sku="OLV-500", name="Olive Oil 500ml", price=12.0,
                              cost=Decimal("7.25"), stock=10, is_active=True)
    data = InvoiceCreate(customer_id=None, items=[InvoiceItemCreate(sku="OLV-500", qty=2)],
                         discount_percent=0, payment_method="cash")
    _result, fake_db = _invoke(data, _make_user("manager"), extra_results=[[product]])

    line = next(o for o in fake_db.added if isinstance(o, InvoiceItem))
    assert line.unit_cost == Decimal("7.25")


def test_no_cost_saves_nothing_rather_than_zero():
    assert cost_snapshot(SimpleNamespace(cost=Decimal("0"))) is None
    assert cost_snapshot(SimpleNamespace(cost=None)) is None
    assert cost_snapshot(SimpleNamespace(cost=Decimal("0.149"))) == Decimal("0.149")


# ── Using it ─────────────────────────────────────────────────────────────────

def product(cost):
    return SimpleNamespace(name="Basil", cost=cost, unit="gram", sku="", category="", item_type="finished")


def test_the_cost_saved_at_sale_beats_todays_cost():
    ledger = ProfitabilityLedger()
    ledger.add_sale(1, product(0.40), 1000, 800, "pos", unit_cost=0.23)   # sold when it cost 0.23
    row = ledger.result()["products"][0]
    assert row["cogs"] == 230.0
    assert row["cost_source"] == "sale"
    assert row["today_cost"] == 0.4


def test_older_lines_without_a_saved_cost_use_todays_cost():
    ledger = ProfitabilityLedger()
    ledger.add_sale(1, product(0.40), 1000, 800, "pos", unit_cost=0.23)
    ledger.add_sale(1, product(0.40), 500, 400, "pos")                     # recorded before costs were saved
    row = ledger.result()["products"][0]
    assert row["cogs"] == 430.0                                             # 230 + 500 × 0.40
    assert row["cost_source"] == "mixed"
    assert ledger.result()["totals"]["cogs_saved_pct"] == 53.5


def test_a_refund_returns_goods_at_the_cost_they_were_sold_at():
    ledger = ProfitabilityLedger()
    ledger.add_sale(1, product(0.40), 1000, 800, "pos", unit_cost=0.23)
    ledger.add_refund(1, product(0.40), 200, 160, unit_cost=0.23)
    row = ledger.result()["products"][0]
    assert (row["qty_sold"], row["cogs"]) == (800.0, 184.0)


def test_report_keeps_last_months_margin_after_the_cost_changes():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    at = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    session.add_all([
        Customer(id=1, name="Walk-in"),
        # The cost has since gone up to 0.40 — September's sale was at 0.23.
        Product(id=1, sku="BAS", name="Basil", unit="gram", price=Decimal("0.8"), cost=Decimal("0.40"), stock=0),
        Invoice(id=1, invoice_number="INV-1", customer_id=1, status="paid", subtotal=Decimal("800"),
                total=Decimal("800"), created_at=at),
        InvoiceItem(invoice_id=1, product_id=1, qty=Decimal("1000"), unit_price=Decimal("0.8"),
                    total=Decimal("800"), unit_cost=Decimal("0.23")),
        RetailRefund(id=1, refund_number="RF-1", invoice_id=1, customer_id=1, total=Decimal("160"), created_at=at),
        RetailRefundItem(refund_id=1, product_id=1, qty=Decimal("200"), unit_price=Decimal("0.8"),
                         total=Decimal("160"), unit_cost=Decimal("0.23")),
    ])
    session.commit()

    class Db:
        async def execute(self, statement, params=None):
            return session.execute(statement, params or {})

    d_from, d_to = parse_dates("2026-09-01", "2026-09-30")
    loop = asyncio.new_event_loop()
    try:
        data = loop.run_until_complete(_build_profitability_report(Db(), d_from=d_from, d_to=d_to))
    finally:
        loop.close()

    basil = data["products"][0]
    assert basil["cogs"] == 184.0                   # 800 g × 0.23, not × 0.40
    assert basil["gross_margin_pct"] == 71.2        # (640 − 184) ÷ 640
    assert basil["cost_source"] == "sale"
    assert data["totals"]["cogs_saved_pct"] == 100.0
