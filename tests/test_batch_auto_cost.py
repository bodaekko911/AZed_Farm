"""Batches cost what they make the moment they are saved."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.database import Base
from app.models.product import Product
from app.models.user import User
from app.routers.production import BatchCreate, create_batch
from app.schemas.drying import DryingBatchFinalizeRequest, DryingBatchNextStageRequest, DryingBatchStartCreate
from app.services import drying_service
from app.services.batch_auto_cost import cost_outputs, summary


def product(pid, name, *, cost=0, price=0, unit="gram", item_type="finished"):
    return SimpleNamespace(id=pid, name=name, cost=Decimal(str(cost)), price=Decimal(str(price)),
                           unit=unit, item_type=item_type, sku="", unit_weight_kg=None)


def line(prod, qty):
    return SimpleNamespace(product=prod, product_id=prod.id, qty=qty)


# ── The rule ─────────────────────────────────────────────────────────────────

def test_a_new_batch_blends_into_the_stock_already_on_hand():
    powder = product(1, "Beetroot Powder (1g)", cost=0.12)
    pack = product(2, "Beetroot Powder (50g)", cost=40, price=180, unit="piece")
    updates = cost_outputs([line(powder, 500)], [line(pack, 10)], {2: 10})

    # this batch: 500 g × 0.12 ÷ 10 packs = 6.00 each; 10 on hand at 40 → (400 + 60) ÷ 20
    assert pack.cost == Decimal("23.000")
    assert updates == [{"product": "Beetroot Powder (50g)", "updated": True,
                        "old_cost": 40.0, "new_cost": 23.0, "batch_unit_cost": 6.0}]
    assert summary(updates) == "Beetroot Powder (50g) cost 40 → 23"


def test_with_nothing_on_hand_the_batch_cost_is_the_cost():
    pack = product(2, "Sesame (100g)", cost=0, price=115, unit="piece")
    cost_outputs([line(product(1, "Sesame (1g)", cost=0.195), 2900)], [line(pack, 29)], {2: 0})
    assert pack.cost == Decimal("19.5")


def test_an_input_without_a_cost_leaves_the_output_alone_and_says_which():
    pack = product(2, "Chamomile (25g)", cost=0, price=75, unit="piece")
    updates = cost_outputs([line(product(1, "Chamomile (1g)", cost=0), 150)], [line(pack, 6)], {2: 0})
    assert pack.cost == 0
    assert summary(updates) == "Chamomile (25g) cost not updated — no cost on Chamomile (1g)"


def test_a_cost_over_three_times_the_price_is_not_written():
    # The old Mejdool A at 190 a gram would have made each 250 g pack 47 500.
    pack = product(2, "Mejdool (250g)", cost=37.25, price=165, unit="piece")
    updates = cost_outputs([line(product(1, "Mejdool A (1g)", cost=190), 1250)], [line(pack, 5)], {2: 20})
    assert pack.cost == Decimal("37.25")
    assert updates[0]["updated"] is False
    assert "over 3×" in updates[0]["reason"]


def test_packing_materials_made_in_a_batch_are_not_compared_with_a_price():
    kit = product(2, "Gift box kit", cost=0, price=1, unit="piece", item_type="packing")
    cost_outputs([line(product(1, "Box", cost=30, unit="piece"), 2)], [line(kit, 1)], {2: 0})
    assert kit.cost == Decimal("60")


# ── Saving batches (database) ────────────────────────────────────────────────

class AsyncSessionAdapter:
    def __init__(self, session):
        self.s = session

    async def execute(self, statement, params=None):
        return self.s.execute(statement, params or {})

    async def flush(self):
        self.s.flush()

    async def commit(self):
        self.s.commit()

    async def refresh(self, obj):
        self.s.refresh(obj)

    async def delete(self, obj):
        self.s.delete(obj)

    def add(self, obj):
        self.s.add(obj)


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all([
        User(id=1, name="Admin", email="a@x", password="x", role="admin"),
        Product(id=1, sku="BEET", name="Beetroot (1g)", unit="gram", price=Decimal("0.03"),
                cost=Decimal("0.01"), stock=Decimal("20000")),
        Product(id=2, sku="BEETC", name="Beetroot Chips (1g)", unit="gram", price=Decimal("1"),
                cost=0, stock=0),
        Product(id=3, sku="BEETP", name="Beetroot Powder (1g)", unit="gram", price=Decimal("4"),
                cost=0, stock=0),
        Product(id=4, sku="BEET50", name="Beetroot Powder (50g)", unit="piece", price=Decimal("180"),
                cost=0, stock=0),
    ])
    session.commit()
    return session, AsyncSessionAdapter(session)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_drying_stages_carry_cost_into_the_packs_made_from_the_powder():
    session, db = make_db()
    user = session.get(User, 1)

    # Stage 1: 10 kg fresh → 2 kg chips. Stage 2: chips → 1 kg powder.
    # Each call is its own request in the app, with its own session — clear
    # the identity map between them so nothing is served from a stale cache.
    batch = run(drying_service.start_batch(db, DryingBatchStartCreate(inputs=[{"product_id": 1, "qty": 10000}]), user))
    session.expunge_all()
    batch = run(drying_service.add_next_stage(db, batch.id, DryingBatchNextStageRequest(
        prev_stage_outputs=[{"product_id": 2, "qty": 2000}],
        new_stage_inputs=[{"product_id": 2, "qty": 2000}]), user))
    assert batch._cost_updates[0]["new_cost"] == 0.05            # 100 EGP of beetroot over 2 000 g
    session.expunge_all()
    batch = run(drying_service.finalize_batch(db, batch.id, DryingBatchFinalizeRequest(
        final_outputs=[{"product_id": 3, "qty": 1000}]), user))
    assert batch._cost_updates[0]["new_cost"] == 0.1             # the same 100 EGP over 1 000 g
    session.expunge_all()
    user = session.get(User, 1)

    # Packing 500 g of powder into 10 packs uses the powder's new cost straight away.
    saved = run(create_batch(BatchCreate(batch_type="packaging", inputs=[{"product_id": 3, "qty": 500}],
                                         outputs=[{"product_id": 4, "qty": 10}]), db, user))
    assert saved["cost_summary"] == "Beetroot Powder (50g) cost 0 → 5"
    assert session.execute(select(Product.cost).where(Product.id == 4)).scalar() == Decimal("5")
