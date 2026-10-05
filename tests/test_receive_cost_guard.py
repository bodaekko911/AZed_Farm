"""Receiving and product cost — the cost is per the product's own unit.

A per-kg price typed on a per-gram product (the live Mejdool case: 190 per
"gram") must be stopped for confirmation; a confirmed receipt blends into the
cost of what is already on hand instead of replacing it; editing a receipt
keeps per-storage stock in step with the total.
"""

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.database import Base
from app.models.inventory import LocationStock, StockLocation, StockMove
from app.models.product import Product
from app.models.receipt import ProductReceipt
from app.models.user import User
from app.services.receive_service import (
    ReceiptUpdate,
    blended_cost,
    cost_check_message,
    update_receipt,
)


def product(cost=0, price=0, unit="gram", name="Mejdool A (1g)"):
    return SimpleNamespace(name=name, unit=unit, cost=cost, price=price)


# ── Cost check ───────────────────────────────────────────────────────────────

def test_a_per_kg_cost_on_a_per_gram_product_is_caught():
    message = cost_check_message(product(cost=0.14, price=0.4), Decimal("190"))
    assert "far from its current cost" in message


def test_a_first_cost_far_above_the_selling_price_is_caught():
    message = cost_check_message(product(cost=0, price=0.4), Decimal("190"))
    assert "more than 3× its selling price" in message


def test_a_normal_price_change_passes():
    assert cost_check_message(product(cost=0.14, price=0.4), Decimal("0.19")) is None
    assert cost_check_message(product(cost=0, price=0), Decimal("50")) is None


# ── Weighted average ─────────────────────────────────────────────────────────

def test_receipt_blends_with_stock_on_hand():
    # 1 000 g at 0.12 plus 500 g at 0.18 → 0.14
    assert blended_cost(Decimal("1000"), Decimal("0.12"), Decimal("500"), Decimal("0.18")) == Decimal("0.140")


def test_with_nothing_on_hand_the_receipt_cost_is_the_cost():
    assert blended_cost(Decimal("0"), Decimal("0.12"), Decimal("500"), Decimal("0.18")) == Decimal("0.18")
    assert blended_cost(Decimal("-20"), Decimal("0.12"), Decimal("5"), Decimal("0.18")) == Decimal("0.18")
    assert blended_cost(Decimal("100"), Decimal("0"), Decimal("5"), Decimal("0.18")) == Decimal("0.18")


# ── Editing a receipt (database) ─────────────────────────────────────────────

class AsyncSessionAdapter:
    def __init__(self, session):
        self.s = session

    async def execute(self, statement, params=None):
        return self.s.execute(statement, params or {})

    async def flush(self):
        self.s.flush()

    async def commit(self):
        self.s.commit()

    async def delete(self, obj):
        self.s.delete(obj)

    def add(self, obj):
        self.s.add(obj)


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def seed_mistyped_receipt(session):
    """The live RCV-00163: 50 kg typed as 50 g at 190 (the per-kg price)."""
    session.add_all([
        User(id=1, name="Admin", email="a@x", password="x", role="admin"),
        Product(id=1, sku="MEJ", name="Mejdool A (1g)", unit="gram", price=Decimal("0.4"),
                cost=Decimal("190"), stock=Decimal("1050")),
        StockLocation(id=1, code="MAIN", name="Main Warehouse", is_active=True),
        LocationStock(location_id=1, product_id=1, qty=Decimal("1050")),
        ProductReceipt(id=163, ref_number="RCV-00163", product_id=1, user_id=1,
                       receive_date=date(2026, 9, 8), qty=Decimal("50"), unit_cost=Decimal("190"),
                       total_cost=Decimal("9500"), amount_paid=Decimal("9500"), location_id=1),
        StockMove(product_id=1, type="in", qty=Decimal("50"), qty_before=Decimal("1000"),
                  qty_after=Decimal("1050"), ref_type="receipt", ref_id=163),
    ])
    session.commit()


def fix(session, **overrides):
    data = dict(qty=50000, unit_cost=0.19, receive_date=date(2026, 9, 8), product_type="products")
    data.update(overrides)
    user = session.get(User, 1)
    return asyncio.run(update_receipt(AsyncSessionAdapter(session), 163, ReceiptUpdate(**data), user))


def test_correcting_the_receipt_needs_confirmation_then_fixes_cost_and_every_stock_figure():
    with make_session() as session:
        seed_mistyped_receipt(session)

        with pytest.raises(HTTPException) as exc:
            fix(session)
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "cost_check"
        session.rollback()

        result = fix(session, confirm_cost=True)

        product = session.get(Product, 1)
        loc = session.execute(select(LocationStock)).scalar_one()
        move = session.execute(select(StockMove)).scalar_one()

    assert result["total_cost"] == 9500.0                # same money, right units
    assert product.cost == Decimal("0.19")
    assert product.stock == Decimal("51000")              # 1 000 before + 50 000
    assert loc.qty == Decimal("51000")                    # storage moves with it
    assert (move.qty, move.qty_after) == (Decimal("50000"), Decimal("51000"))


# ── Searching receipt history ────────────────────────────────────────────────

def test_history_search_finds_receipts_by_number_product_supplier_or_reference():
    from app.models.supplier import Supplier
    from app.services.receive_service import list_receipts

    with make_session() as session:
        session.add_all([
            Product(id=1, sku="MEJ", name="Mejdool A (1g)", unit="gram", price=Decimal("0.4"), cost=0, stock=0),
            Product(id=2, sku="EGG", name="Baladi Eggs", unit="piece", price=Decimal("12"), cost=0, stock=0),
            Supplier(id=1, name="Siwa Dates Co."),
            ProductReceipt(id=106, ref_number="RCV-00106", product_id=1, receive_date=date(2025, 10, 14),
                           qty=Decimal("0.14"), unit_cost=Decimal("70000"), supplier_id=1),
            ProductReceipt(id=163, ref_number="RCV-00163", product_id=1, receive_date=date(2026, 9, 8),
                           qty=Decimal("50000"), unit_cost=Decimal("0.19"), supplier_ref="INV-2026-114"),
            ProductReceipt(id=111, ref_number="RCV-00111", product_id=2, receive_date=date(2026, 1, 3),
                           qty=Decimal("10"), unit_cost=Decimal("120")),
        ])
        session.commit()
        db = AsyncSessionAdapter(session)

        def refs(q):
            return [r["ref_number"] for r in asyncio.run(list_receipts(db, limit=100, q=q))["items"]]

        assert refs("RCV-00106") == ["RCV-00106"]
        assert refs("106") == ["RCV-00106"]                   # digits alone find the number
        assert refs("rcv-0016") == ["RCV-00163"]              # partial, any case
        assert refs("mejdool") == ["RCV-00163", "RCV-00106"]  # newest first
        assert refs("siwa") == ["RCV-00106"]                  # supplier
        assert refs("INV-2026") == ["RCV-00163"]              # supplier reference
        assert refs("EGG") == ["RCV-00111"]                   # SKU
        assert refs("nothing like this") == []
        assert len(refs("")) == 3
