"""Season Analysis — cost price per crop.

The report distributes a farm-wide cost pool across the crops that farm
delivered. Three things decide whether the resulting cost price is trustworthy,
and each is pinned here:

  • the split denominator — kilograms, not a mix of kg and pieces
  • the valuation — what the crop actually sold for, not its list price
  • the cost basis — farm costs and fully-absorbed costs, both, side by side
"""

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.database import Base
from app.models.b2b import B2BClient, B2BInvoice, B2BInvoiceItem, B2BRefund, B2BRefundItem
from app.models.customer import Customer
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.refund import RetailRefund, RetailRefundItem
from app.services.expense_service import get_cost_allocation


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement, params=None):
        return self.session.execute(statement, params or {})


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
        Farm.__table__, Product.__table__, ExpenseCategory.__table__, Expense.__table__,
        Customer.__table__,
        FarmDelivery.__table__, FarmDeliveryItem.__table__,
        Invoice.__table__, InvoiceItem.__table__,
        B2BClient.__table__, B2BInvoice.__table__, B2BInvoiceItem.__table__,
        RetailRefund.__table__, RetailRefundItem.__table__,
        B2BRefund.__table__, B2BRefundItem.__table__,
    ])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def seed_base(session, *, lettuce_weight=Decimal("0.500")):
    """North farm: 16,000 EGP of costs, 800 kg tomato + 200 heads of lettuce."""
    session.add_all([
        Farm(id=1, name="North Farm", is_active=1),
        Product(id=1, sku="TOM", name="Tomato", price=Decimal("20"), unit="kg"),
        Product(id=2, sku="LET", name="Lettuce", price=Decimal("30"), unit="pcs",
                unit_weight_kg=lettuce_weight),
        ExpenseCategory(id=1, name="Fertiliser", account_code="5001", is_active="1"),
        ExpenseCategory(id=2, name="Farm Labour", account_code="5002", is_active="1"),
    ])
    session.flush()
    session.add_all([
        Expense(id=1, category_id=1, farm_id=1, amount=Decimal("6000"),
                expense_date=date(2026, 8, 4), description="Fertiliser"),
        Expense(id=2, category_id=2, farm_id=1, amount=Decimal("10000"),
                expense_date=date(2026, 8, 10), description="Harvest labour"),
    ])
    delivery = FarmDelivery(id=1, delivery_number="FD-1", farm_id=1,
                            delivery_date=date(2026, 8, 12))
    session.add(delivery)
    session.flush()
    session.add_all([
        FarmDeliveryItem(delivery_id=1, product_id=1, qty=Decimal("800"), unit="kg"),
        FarmDeliveryItem(delivery_id=1, product_id=2, qty=Decimal("200"), unit="pcs"),
    ])
    session.commit()


def seed_retail_sale(session, product_id, qty, total, invoice_id=1):
    if not session.get(Customer, 1):
        session.add(Customer(id=1, name="Walk-in"))
        session.flush()
    session.add(Invoice(customer_id=1, id=invoice_id, invoice_number=f"INV-{invoice_id}", total=Decimal(str(total)),
                        created_at=datetime(2026, 8, 20, tzinfo=timezone.utc)))
    session.flush()
    session.add(InvoiceItem(invoice_id=invoice_id, product_id=product_id, qty=Decimal(str(qty)),
                            unit_price=Decimal(str(total / qty)), total=Decimal(str(total))))
    session.commit()


def allocate(session, method="quantity", farm_id="1"):
    return run(get_cost_allocation(
        AsyncSessionAdapter(session), farm_id=farm_id,
        date_from="2026-08-01", date_to="2026-08-31", allocation_method=method,
    ))


# ── Split denominator ────────────────────────────────────────────────────────

def test_pieces_are_converted_to_kilograms_before_splitting():
    """200 heads at 0.5 kg = 100 kg, so the split is 800:100, not 800:200."""
    with make_session() as session:
        seed_base(session)
        data = allocate(session)

    assert data["allocation_method"] == "weight"
    assert data["weight_basis_complete"] is True
    assert data["total_kg"] == 900.0

    by_name = {p["product_name"]: p for p in data["products"]}
    assert by_name["Tomato"]["share_pct"] == 88.9        # 800 / 900
    assert by_name["Lettuce"]["share_pct"] == 11.1       # 100 / 900
    assert by_name["Lettuce"]["total_kg"] == 100.0


def test_missing_unit_weight_falls_back_and_says_why():
    """Without a weight on the lettuce, kg and pieces cannot be compared, so
    the split must not silently pretend 200 pieces == 200 kg."""
    with make_session() as session:
        seed_base(session, lettuce_weight=None)
        seed_retail_sale(session, 1, 100, 2000)
        data = allocate(session)

    assert data["weight_basis_complete"] is False
    assert data["allocation_method"] == "value"
    assert data["products_missing_weight"] == ["Lettuce"]
    assert any("no weight set" in w for w in data["warnings"])


def test_cost_split_is_exhaustive():
    with make_session() as session:
        seed_base(session)
        data = allocate(session)

    assert round(sum(p["allocated_cost"] for p in data["products"]), 2) == data["total_cost"]
    assert round(sum(p["share_pct"] for p in data["products"]), 1) == 100.0


# ── Valuation ────────────────────────────────────────────────────────────────

def test_realised_sale_price_replaces_list_price():
    """Tomato lists at 20 but actually sold at 17.50 — the margin must reflect
    what was banked, not the sticker."""
    with make_session() as session:
        seed_base(session)
        seed_retail_sale(session, 1, 400, 7000)          # 400 kg for 7,000 => 17.50
        data = allocate(session)

    tomato = next(p for p in data["products"] if p["product_name"] == "Tomato")
    assert tomato["price_basis"] == "realised"
    assert tomato["sale_price"] == 17.5
    assert tomato["list_price"] == 20.0
    assert tomato["qty_sold"] == 400.0
    assert data["revenue_basis"] == "mixed"              # lettuce had no sales


def test_refunds_are_netted_out_of_the_realised_price():
    with make_session() as session:
        seed_base(session)
        seed_retail_sale(session, 1, 400, 8000)          # 400 kg @ 20
        session.add(RetailRefund(id=1, refund_number="RR-1", customer_id=1, total=Decimal("2000"),
                                 created_at=datetime(2026, 8, 22, tzinfo=timezone.utc)))
        session.flush()
        session.add(RetailRefundItem(refund_id=1, product_id=1, qty=Decimal("100"),
                                     unit_price=Decimal("20"), total=Decimal("2000")))
        session.commit()
        data = allocate(session)

    tomato = next(p for p in data["products"] if p["product_name"] == "Tomato")
    assert tomato["qty_sold"] == 300.0                   # 400 sold − 100 returned
    assert tomato["revenue_actual"] == 6000.0
    assert tomato["sale_price"] == 20.0


def test_products_with_no_sales_fall_back_to_list_price_and_are_flagged():
    with make_session() as session:
        seed_base(session)
        data = allocate(session)

    assert data["revenue_basis"] == "list"
    assert data["products_missing_sales"] == ["Lettuce", "Tomato"]
    assert any("list price was used" in w for w in data["warnings"])
    tomato = next(p for p in data["products"] if p["product_name"] == "Tomato")
    assert tomato["price_basis"] == "list"
    assert tomato["sale_price"] == 20.0


# ── Cost basis ───────────────────────────────────────────────────────────────

def test_direct_and_absorbed_cost_prices_are_both_returned():
    """30,000 of untagged head-office cost exists; North is the only farm, so
    it carries all of it on the absorbed basis and none on the direct basis."""
    with make_session() as session:
        seed_base(session)
        session.add(Expense(id=99, category_id=1, farm_id=None, amount=Decimal("30000"),
                            expense_date=date(2026, 8, 15), description="Head office"))
        session.commit()
        data = allocate(session)

    assert data["total_cost"] == 16000.0
    assert data["shared_cost_total"] == 30000.0
    assert data["shared_cost_allocated"] == 30000.0      # sole farm ⇒ full share
    assert data["fully_absorbed_cost"] == 46000.0

    tomato = next(p for p in data["products"] if p["product_name"] == "Tomato")
    # 88.9% of 16,000 = 14,222 over 800 kg ⇒ 17.78/kg direct
    assert tomato["cost_per_unit"] == 17.78
    # 88.9% of 46,000 = 40,889 over 800 kg ⇒ 51.11/kg absorbed
    assert tomato["cost_per_unit_absorbed"] == 51.11
    assert tomato["cost_per_unit_absorbed"] > tomato["cost_per_unit"]
    assert tomato["profit_per_unit"] > tomato["profit_per_unit_absorbed"]


def test_absorbed_equals_direct_when_there_is_no_overhead():
    with make_session() as session:
        seed_base(session)
        data = allocate(session)

    assert data["shared_cost_allocated"] == 0.0
    for p in data["products"]:
        assert p["cost_per_unit"] == p["cost_per_unit_absorbed"]


def test_costs_with_no_harvest_are_reported_not_silently_dropped():
    with make_session() as session:
        session.add_all([
            Farm(id=1, name="North Farm", is_active=1),
            ExpenseCategory(id=1, name="Fertiliser", account_code="5001", is_active="1"),
        ])
        session.flush()
        session.add(Expense(id=1, category_id=1, farm_id=1, amount=Decimal("5000"),
                            expense_date=date(2026, 8, 4), description="Fertiliser"))
        session.commit()
        data = allocate(session)

    assert data["total_cost"] == 5000.0
    assert data["products"] == []
    assert any("nothing to spread them across" in w for w in data["warnings"])
