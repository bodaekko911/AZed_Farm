"""Combined product cost: grown, bought and made, averaged by quantity, down the chain."""

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.database import Base
from app.models.drying import DryingBatch, DryingBatchStage, DryingBatchStageInput, DryingBatchStageOutput
from app.models.product import Product
from app.models.production import BatchInput, BatchOutput, ProductionBatch
from app.models.user import User
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.expense import Expense, ExpenseCategory
from app.models.receipt import ProductReceipt
from app.routers.production import ApplyBatchCosts, apply_costs, preview_costs
from app.services.batch_cost_rollup import roll_up


def p(name, cost=0.0, price=0.0, unit="gram", weight=None):
    return SimpleNamespace(name=name, cost=cost, price=price, unit=unit, unit_weight_kg=weight,
                           sku="", item_type="finished")


def batch(number, inputs, outputs):
    return {"batch_number": number, "inputs": inputs, "outputs": outputs}


def rows_by_name(rows):
    return {r["product"]: r for r in rows}


def test_costs_flow_from_drying_into_the_packs_made_from_the_powder():
    products = {
        1: p("Beetroot (1g)", cost=0.01),
        2: p("Beetroot Powder (1g)", cost=0, price=4),
        3: p("Beetroot Powder (50g)", cost=0, price=150, unit="piece"),
        4: p("Jar", cost=5, unit="piece"),
    }
    rows = rows_by_name(roll_up([
        # Packaging listed first: order must not matter.
        batch("PKG-1", [(2, 500), (4, 10)], [(3, 10)]),
        batch("DRY-1", [(1, 10000)], [(2, 1000)]),     # 10 kg fresh → 1 kg powder
    ], products))

    powder, pack = rows["Beetroot Powder (1g)"], rows["Beetroot Powder (50g)"]
    assert powder["new_cost"] == 0.1                  # 100 EGP of beetroot over 1 000 g
    assert powder["level"] == 1
    assert pack["new_cost"] == 10.0                   # 500 g × 0.1 + 10 jars × 5, over 10 packs
    assert pack["level"] == 2
    assert pack["status"] == "ok"
    assert "Beetroot (1g)" not in rows                # raw inputs are never changed


def test_an_input_with_no_cost_blocks_the_output_and_says_which():
    products = {1: p("Date Syrup (1ml)", cost=0), 2: p("Date Syrup (500ml)", unit="piece", price=300)}
    row = roll_up([batch("PKG-66", [(1, 5000)], [(2, 10)])], products)[0]

    assert row["new_cost"] is None
    assert row["status"] == "incomplete"
    assert row["missing_cost"] == ["no cost on batch input Date Syrup (1ml)"]


def test_a_cost_far_above_the_selling_price_is_flagged():
    # The live Mejdool case: a per-kg cost on a per-gram input.
    products = {1: p("Mejdool A (1g)", cost=190, price=0.4),
                2: p("Mejdool (250g)", unit="piece", price=165, cost=60)}
    row = roll_up([batch("PKG-17", [(1, 1250)], [(2, 5)])], products)[0]

    assert row["new_cost"] == 47500.0
    assert row["status"] == "suspect"


def test_cost_is_the_quantity_weighted_average_of_the_period_batches():
    products = {1: p("Oil (1ml)", cost=0.4), 2: p("Bottle", cost=20, unit="piece"),
                3: p("Oil (500ml)", unit="piece", price=550)}
    row = roll_up([
        batch("PKG-14", [(1, 3000)], [(3, 6)]),                 # 200 each, no bottle
        batch("PKG-69", [(1, 2500), (2, 5)], [(3, 5)]),         # 220 each
    ], products)[0]

    assert row["new_cost"] == round((1200 + 1100) / 11, 3)
    assert row["lines"][0]["label"].startswith("PKG-14")
    assert row["sources"] == [{"source": "made", "qty": 11.0, "unit_cost": row["new_cost"]}]


def test_grown_bought_and_made_are_averaged_by_quantity():
    # The live Mejdool case: 135 kg grown at 0.14 and 50 kg bought at 0.19.
    products = {1: p("Mejdool A (1g)", cost=190, price=0.4),
                2: p("Mejdool (250g)", unit="piece", price=165, cost=60)}
    supplies = {1: [{"source": "grown", "qty": 135000, "unit_cost": 0.14, "label": "season"},
                    {"source": "bought", "qty": 50000, "unit_cost": 0.19, "label": "RCV-00163"}]}
    rows = rows_by_name(roll_up([batch("PKG-17", [(1, 1250)], [(2, 5)])], products, supplies))

    dates, pack = rows["Mejdool A (1g)"], rows["Mejdool (250g)"]
    assert dates["new_cost"] == round((135000 * 0.14 + 50000 * 0.19) / 185000, 3)   # 0.154
    assert [s["source"] for s in dates["sources"]] == ["grown", "bought"]
    assert dates["level"] == 1
    # The pack is made from the combined cost, not the 190 that was on the card.
    assert pack["new_cost"] == round(1250 * dates["new_cost"] / 5, 3)
    assert pack["status"] == "ok"
    assert pack["level"] == 2


def test_a_product_both_made_and_bought_averages_the_two():
    products = {1: p("Oil (1ml)", cost=0.4), 2: p("Oil (500ml)", unit="piece", price=550)}
    supplies = {2: [{"source": "bought", "qty": 10, "unit_cost": 260, "label": "RCV-1"}]}
    row = rows_by_name(roll_up([batch("PKG-1", [(1, 5000)], [(2, 10)])], products, supplies))["Oil (500ml)"]
    assert row["new_cost"] == 230.0                       # (10 × 200 + 10 × 260) ÷ 20


def test_supply_that_cannot_be_valued_blocks_the_average():
    products = {1: p("Basil (1g)", cost=0.3, price=0.6)}
    supplies = {1: [{"source": "grown", "qty": 5000, "unit_cost": None,
                     "label": "harvest not costed: Delivered in kg but the product is priced per gram"},
                    {"source": "bought", "qty": 1000, "unit_cost": 0.5, "label": "RCV-9"}]}
    row = roll_up([], products, supplies)[0]
    assert row["new_cost"] is None
    assert row["status"] == "incomplete"
    assert "priced per gram" in row["missing_cost"][0]


def test_a_product_feeding_its_own_batch_does_not_loop_forever():
    products = {1: p("Starter", cost=1), 2: p("Kombucha", cost=0, unit="ml", price=1)}
    rows = roll_up([batch("B-1", [(1, 10), (2, 100)], [(2, 1000)])], products)
    assert rows[0]["product"] == "Kombucha"


# ── Preview and apply against a database ─────────────────────────────────────

class AsyncSessionAdapter:
    def __init__(self, session):
        self.s = session

    async def execute(self, statement, params=None):
        return self.s.execute(statement, params or {})

    async def commit(self):
        self.s.commit()

    def add(self, obj):
        self.s.add(obj)


def test_preview_then_apply_writes_only_the_chosen_products():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    at = datetime(2026, 5, 10, 12, tzinfo=timezone.utc)
    session.add_all([
        User(id=1, name="Admin", email="a@x", password="x", role="admin"),
        Product(id=1, sku="B", name="Beetroot (1g)", unit="gram", price=Decimal("0.03"), cost=Decimal("0.01")),
        Product(id=2, sku="BP", name="Beetroot Powder (1g)", unit="gram", price=Decimal("4"), cost=0),
        Product(id=3, sku="BP50", name="Beetroot Powder (50g)", unit="piece", price=Decimal("150"), cost=0),
        DryingBatch(id=1, batch_number="DRY-1", status="completed", started_at=at, started_by_id=1),
        DryingBatchStage(id=1, batch_id=1, stage_number=1, logged_by_id=1, logged_at=at,
                         total_input_qty=Decimal("10000"), total_output_qty=Decimal("1000")),
        DryingBatchStageInput(stage_id=1, product_id=1, qty=Decimal("10000")),
        DryingBatchStageOutput(stage_id=1, product_id=2, qty=Decimal("1000")),
        ProductionBatch(id=1, batch_number="PKG-1", created_at=at, waste_pct=0),
        BatchInput(batch_id=1, product_id=2, qty=Decimal("500")),
        BatchOutput(batch_id=1, product_id=3, qty=Decimal("10")),
    ])
    session.commit()
    db = AsyncSessionAdapter(session)
    user = session.get(User, 1)

    preview = asyncio.run(preview_costs(date(2026, 5, 1), date(2026, 5, 31), "direct", db))
    assert {r["product"]: r["new_cost"] for r in preview["products"]} == {
        "Beetroot Powder (1g)": 0.1, "Beetroot Powder (50g)": 5.0,
    }

    result = asyncio.run(apply_costs(
        ApplyBatchCosts(date_from=date(2026, 5, 1), date_to=date(2026, 5, 31), product_ids=[2]), db, user))

    assert result["applied_count"] == 1
    assert session.get(Product, 2).cost == Decimal("0.1")
    assert session.get(Product, 3).cost == 0           # not chosen, not touched
    assert session.get(Product, 1).cost == Decimal("0.01")


def test_preview_costs_only_processed_products_from_their_inputs_cost():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    at = datetime(2026, 5, 10, 12, tzinfo=timezone.utc)
    session.add_all([
        User(id=1, name="Admin", email="a@x", password="x", role="admin"),
        Farm(id=1, name="North", is_active=1),
        ExpenseCategory(id=1, name="Fertiliser", account_code="5001", is_active="1"),
        # 1 400 EGP of farm costs over 10 kg of dates harvested ⇒ 0.14 a gram
        Expense(category_id=1, farm_id=1, amount=Decimal("1400"), expense_date=date(2026, 5, 3)),
        Product(id=1, sku="MEJ", name="Mejdool A (1g)", unit="gram", price=Decimal("0.4"), cost=Decimal("0.16")),
        Product(id=2, sku="MEJ250", name="Mejdool (250g)", unit="piece", price=Decimal("165"), cost=Decimal("60")),
        FarmDelivery(id=1, delivery_number="D-1", farm_id=1, delivery_date=date(2026, 5, 4)),
        FarmDeliveryItem(delivery_id=1, product_id=1, qty=Decimal("10000"), unit="gram"),
        ProductReceipt(ref_number="RCV-1", product_id=1, receive_date=date(2026, 5, 5), qty=Decimal("10000"),
                       unit_cost=Decimal("0.18"), total_cost=Decimal("1800"), amount_paid=Decimal("1800")),
        ProductionBatch(id=1, batch_number="PKG-1", created_at=at, waste_pct=0),
        BatchInput(batch_id=1, product_id=1, qty=Decimal("1000")),
        BatchOutput(batch_id=1, product_id=2, qty=Decimal("4")),
    ])
    session.commit()

    preview = asyncio.run(preview_costs(date(2026, 5, 1), date(2026, 5, 31), "direct", AsyncSessionAdapter(session)))
    rows = {r["product"]: r for r in preview["products"]}

    # Only the processed product is costed here; the raw dates keep their cost
    # (set by Season Analysis and receiving), whatever was grown or bought.
    assert list(rows) == ["Mejdool (250g)"]
    assert rows["Mejdool (250g)"]["new_cost"] == 40.0         # 1 000 g × 0.16 ÷ 4 packs


def test_one_mistyped_receipt_flags_the_product_even_when_the_average_looks_fine():
    # The live RCV-00106: 0.14 g "bought" at 70 000 a gram, inside 150 kg of normal receipts.
    products = {1: p("Mejdool A (1g)", cost=0.14, price=0.4)}
    supplies = {1: [{"source": "bought", "qty": 100000, "unit_cost": 0.12, "label": "RCV-00073"},
                    {"source": "bought", "qty": 0.14, "unit_cost": 70000, "label": "RCV-00106"}]}
    row = roll_up([], products, supplies)[0]
    assert row["new_cost"] < 3 * 0.4
    assert row["status"] == "suspect"
    assert row["suspect_lines"][0].startswith("RCV-00106")


def test_packing_materials_are_not_compared_with_a_placeholder_price():
    jar = p("500g Jar", cost=50, price=1, unit="piece")
    jar.item_type = "packing"
    row = roll_up([], {1: jar}, {1: [{"source": "bought", "qty": 10, "unit_cost": 50, "label": "RCV-5"}]})[0]
    assert row["status"] == "unchanged"
