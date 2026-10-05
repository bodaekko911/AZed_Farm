"""Product profitability — revenue, cost of sales and losses per product.

Revenue must reconcile to the Sales report's net sales; cost comes from
batches where a product was made in the period, else the product card; a
product with no cost is reported, never given a made-up one.
"""

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.database import Base
from app.models.accounting import Account, Journal, JournalEntry
from app.models.b2b import B2BClient, B2BInvoice, B2BInvoiceItem
from app.models.customer import Customer
from app.models.drying import (
    DryingBatch, DryingBatchSpoilage, DryingBatchStage, DryingBatchStageInput, DryingBatchStageOutput,
)
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.refund import RetailRefund, RetailRefundItem
from app.models.spoilage import SpoilageRecord
from app.routers.reports import _build_profitability_report, _build_sales_report, parse_dates
from app.services.product_profitability import ProfitabilityLedger


# ── Ledger (pure) ────────────────────────────────────────────────────────────

def product(name, cost=0.0, unit="kg"):
    return SimpleNamespace(name=name, cost=cost, unit=unit, sku="", category="")


def by_name(result):
    return {p["name"]: p for p in result["products"]}


def test_profit_is_revenue_less_cost_of_sales_less_losses():
    ledger = ProfitabilityLedger()
    tomato = product("Tomato", cost=10)
    ledger.add_sale(1, tomato, 10, 200, "pos")
    ledger.add_loss(1, tomato, 3)

    row = by_name(ledger.result())["Tomato"]

    assert row["cogs"] == 100.0
    assert row["gross_profit"] == 100.0
    assert row["gross_margin_pct"] == 50.0
    assert row["loss_cost"] == 30.0
    assert row["profit"] == 70.0
    assert row["margin_pct"] == 35.0


def test_refunds_come_off_quantity_and_revenue():
    ledger = ProfitabilityLedger()
    p = product("Honey", cost=50)
    ledger.add_sale(1, p, 4, 400, "pos")
    ledger.add_refund(1, p, 1, 100)

    row = by_name(ledger.result())["Honey"]

    assert row["qty_sold"] == 3.0
    assert row["revenue"] == 300.0
    assert row["cogs"] == 150.0


def test_batch_cost_beats_the_product_card_and_flags_it_when_stale():
    ledger = ProfitabilityLedger()
    dried = product("Dried Tomato", cost=50)
    ledger.add_batch_costing(
        {"cost_is_complete": True,
         "output_lines": [{"product_id": 2, "product": "Dried Tomato", "qty": 10, "allocated_cost": 1000}]},
        {2: dried},
    )
    ledger.add_sale(2, dried, 1, 300, "b2b")

    result = ledger.result()
    row = by_name(result)["Dried Tomato"]

    assert row["unit_cost"] == 100.0
    assert row["cost_source"] == "batch"
    assert row["card_cost"] == 50.0
    assert result["products_stale_cost"] == ["Dried Tomato"]


def test_a_batch_with_uncosted_inputs_falls_back_to_the_card():
    ledger = ProfitabilityLedger()
    dried = product("Dried Mango", cost=80)
    ledger.add_batch_costing(
        {"cost_is_complete": True,
         "output_lines": [{"product_id": 3, "product": "Dried Mango", "qty": 10, "allocated_cost": 1000}]},
        {3: dried},
    )
    ledger.add_batch_costing(
        {"cost_is_complete": False,
         "output_lines": [{"product_id": 3, "product": "Dried Mango", "qty": 10, "allocated_cost": 10}]},
        {3: dried},
    )
    ledger.add_sale(3, dried, 1, 200, "pos")

    row = by_name(ledger.result())["Dried Mango"]

    assert row["cost_source"] == "product"
    assert row["unit_cost"] == 80.0


def test_no_cost_is_named_not_invented():
    ledger = ProfitabilityLedger()
    ledger.add_sale(4, product("Jam", cost=0, unit="piece"), 1, 40, "pos")

    result = ledger.result()

    assert by_name(result)["Jam"]["cost_source"] == "missing"
    assert result["products_missing_cost"] == ["Jam"]
    assert result["cost_is_complete"] is False


def test_products_only_produced_are_left_out_and_losers_are_found():
    ledger = ProfitabilityLedger()
    ledger.add_batch_costing(
        {"cost_is_complete": True,
         "output_lines": [{"product_id": 5, "product": "Unsold", "qty": 5, "allocated_cost": 50}]},
        {5: product("Unsold", cost=10)},
    )
    ledger.add_sale(6, product("Good", cost=1), 10, 100, "pos")
    ledger.add_loss(7, product("Rotten", cost=5), 4)

    result = ledger.result()

    assert [p["name"] for p in result["products"]] == ["Good", "Rotten"]
    assert result["losing_count"] == 1
    assert result["biggest_drain"]["name"] == "Rotten"
    assert result["top_earner"]["name"] == "Good"


# ── Report (database) ────────────────────────────────────────────────────────

class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement, params=None):
        return self.session.execute(statement, params or {})


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def at(day, hour=12):
    return datetime(2026, 5, day, hour, 0, tzinfo=timezone.utc)


def seed(session):
    session.add_all([
        Account(id=1, code="1000", name="Cash", type="asset", balance=0),
        Customer(id=1, name="Walk-in"),
        B2BClient(id=1, name="Alpha Market", outstanding=0),
        Product(id=1, sku="TOM", name="Tomato", price=Decimal("20"), cost=Decimal("10"), unit="kg", stock=100),
        Product(id=2, sku="DTOM", name="Dried Tomato", price=Decimal("300"), cost=Decimal("50"), unit="kg", stock=10),
        Product(id=3, sku="JAM", name="Jam", price=Decimal("40"), cost=Decimal("0"), unit="piece", stock=10),

        # Drying: 100 kg tomato → 10 kg dried, so 1 000 EGP on 10 kg = 100/kg.
        DryingBatch(id=1, batch_number="DRY-1", status="completed", started_at=at(2), started_by_id=1),
        DryingBatchStage(id=1, batch_id=1, stage_number=1, logged_by_id=1, logged_at=at(3),
                         total_input_qty=Decimal("100"), total_output_qty=Decimal("10")),
        DryingBatchStageInput(stage_id=1, product_id=1, qty=Decimal("100")),
        DryingBatchStageOutput(stage_id=1, product_id=2, qty=Decimal("10")),

        # POS: 800 of lines with a 10% invoice discount → 720 taken.
        Invoice(id=1, invoice_number="INV-1", customer_id=1, status="paid", subtotal=Decimal("800"),
                discount=Decimal("80"), total=Decimal("720"), created_at=at(5)),
        InvoiceItem(invoice_id=1, product_id=1, qty=Decimal("10"), unit_price=Decimal("20"), total=Decimal("200")),
        InvoiceItem(invoice_id=1, product_id=2, qty=Decimal("2"), unit_price=Decimal("300"), total=Decimal("600")),
        Invoice(id=2, invoice_number="INV-2", customer_id=1, status="paid", subtotal=Decimal("40"),
                discount=0, total=Decimal("40"), created_at=at(6)),
        InvoiceItem(invoice_id=2, product_id=3, qty=Decimal("1"), unit_price=Decimal("40"), total=Decimal("40")),

        # B2B: half of a 1 500 invoice collected in the period.
        B2BInvoice(id=1, invoice_number="HB2B-00001", client_id=1, invoice_type="credit", status="partial",
                   total=Decimal("1500"), amount_paid=Decimal("750"), created_at=at(1)),
        B2BInvoiceItem(invoice_id=1, product_id=2, qty=Decimal("5"), unit_price=Decimal("300"), total=Decimal("1500")),
        Journal(id=1, ref_type="b2b_collection", ref_id=1, description="Collected HB2B-00001", created_at=at(10)),
        JournalEntry(journal_id=1, account_id=1, debit=Decimal("750"), credit=0),
        # A collection that names no invoice — counted in sales, not traceable to a product.
        Journal(id=2, ref_type="b2b_payment", ref_id=None, description="Old balance", created_at=at(11)),
        JournalEntry(journal_id=2, account_id=1, debit=Decimal("100"), credit=0),

        RetailRefund(id=1, refund_number="RF-1", invoice_id=1, customer_id=1, total=Decimal("135"), created_at=at(12)),
        RetailRefundItem(refund_id=1, product_id=2, qty=Decimal("0.5"), unit_price=Decimal("270"), total=Decimal("135")),

        SpoilageRecord(ref_number="SPL-1", product_id=1, qty=Decimal("2"), spoilage_date=date(2026, 5, 15)),
        DryingBatchSpoilage(batch_id=1, product_id=1, qty=Decimal("1"), reason="mold", logged_by_id=1, logged_at=at(4)),
        # Outside the period — must not count.
        SpoilageRecord(ref_number="SPL-2", product_id=1, qty=Decimal("50"), spoilage_date=date(2026, 6, 2)),
    ])
    session.commit()


def test_report_joins_sales_batches_and_losses_per_product():
    d_from, d_to = parse_dates("2026-05-01", "2026-05-31")
    with make_session() as session:
        seed(session)
        db = AsyncSessionAdapter(session)
        data = run(_build_profitability_report(db, d_from=d_from, d_to=d_to))
        sales = run(_build_sales_report(db, d_from=d_from, d_to=d_to, include_all=True))

    rows = by_name(data)

    tomato = rows["Tomato"]
    assert tomato["revenue"] == 180.0                 # 200 less its share of the discount
    assert tomato["cost_source"] == "product"
    assert tomato["cogs"] == 100.0
    assert tomato["loss_qty"] == 3.0                  # spoilage + drying-batch spoilage
    assert tomato["profit"] == 50.0

    dried = rows["Dried Tomato"]
    assert dried["qty_sold"] == 4.0                   # 2 POS + 2.5 collected − 0.5 refunded
    assert dried["revenue"] == 1155.0                 # 540 + 750 − 135
    assert dried["revenue_b2b"] == 750.0
    assert dried["cost_source"] == "batch"
    assert dried["unit_cost"] == 100.0
    assert dried["profit"] == 755.0

    assert rows["Jam"]["cost_source"] == "missing"
    assert data["products_missing_cost"] == ["Jam"]
    assert data["products_stale_cost"] == ["Dried Tomato"]

    totals = data["totals"]
    assert totals["revenue"] == 1375.0
    assert totals["unattributed_revenue"] == 100.0
    assert totals["profit"] == 845.0
    # Same money as the Sales report — nothing gained or lost in the split.
    assert totals["net_sales"] == sales["net_sales"] == 1475.0
