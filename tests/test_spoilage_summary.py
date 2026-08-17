"""Spoilage figures — quantity, cost, top item and loss rate.

One definition behind both the spoilage screen and the spoilage report, so the
two cannot drift. The rate's denominator is farm intake: every kilogram the
farm produces enters as a delivery, so adding processed output on top would
count the same produce twice.
"""

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.database import Base
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.product import Product
from app.models.production import BatchOutput, ProductionBatch
from app.models.spoilage import SpoilageRecord
from app.models.user import User
from app.services.spoilage_summary import spoilage_summary


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
        User.__table__, Product.__table__, Farm.__table__,
        FarmDelivery.__table__, FarmDeliveryItem.__table__,
        ProductionBatch.__table__, BatchOutput.__table__,
        SpoilageRecord.__table__,
    ])
    return sessionmaker(bind=engine, expire_on_commit=False)()


def seed(session, *, basil_weight=Decimal("0.04"), basil_cost=Decimal("7")):
    """1,000 kg of tomato and 500 basil bunches harvested; some of each spoiled."""
    session.add_all([
        Farm(id=1, name="North Farm", is_active=1),
        Product(id=1, sku="TOM", name="Cherry tomato", price=Decimal("45"),
                cost=Decimal("30"), unit="kg"),
        Product(id=2, sku="BAS", name="Italian basil", price=Decimal("15"),
                cost=basil_cost, unit="piece", unit_weight_kg=basil_weight),
    ])
    session.flush()

    delivery = FarmDelivery(id=1, delivery_number="FD-1", farm_id=1,
                            delivery_date=date(2026, 8, 5))
    session.add(delivery)
    session.flush()
    session.add_all([
        FarmDeliveryItem(delivery_id=1, product_id=1, qty=Decimal("1000"), unit="kg"),
        FarmDeliveryItem(delivery_id=1, product_id=2, qty=Decimal("500"), unit="piece"),
    ])

    session.add_all([
        SpoilageRecord(id=1, ref_number="SPL-1", product_id=1, qty=Decimal("40"),
                       spoilage_date=date(2026, 8, 10), reason="mold", farm_id=1),
        SpoilageRecord(id=2, ref_number="SPL-2", product_id=2, qty=Decimal("50"),
                       spoilage_date=date(2026, 8, 12), reason="overripe", farm_id=1),
        SpoilageRecord(id=3, ref_number="SPL-3", product_id=1, qty=Decimal("10"),
                       spoilage_date=date(2026, 8, 14), reason="mold", farm_id=1),
    ])
    session.commit()


def summarise(session, d_from=date(2026, 8, 1), d_to=date(2026, 8, 31)):
    return run(spoilage_summary(AsyncSessionAdapter(session), d_from=d_from, d_to=d_to))


def test_quantity_is_summed_in_kilograms():
    """50 basil bunches at 40 g are 2 kg, not 50 of anything addable to tomato."""
    with make_session() as session:
        seed(session)
        data = summarise(session)

    assert data["total_qty_kg"] == 52.0        # 40 + 10 tomato + (50 × 0.04) basil
    assert data["records"] == 3


def test_cost_uses_the_products_cost():
    with make_session() as session:
        seed(session)
        data = summarise(session)

    # 50 kg tomato × 30 + 50 bunches × 7
    assert data["total_cost"] == 1850.0
    assert data["cost_is_complete"] is True


def test_top_item_is_the_most_expensive_loss_not_the_heaviest():
    with make_session() as session:
        seed(session)
        data = summarise(session)

    assert data["top_item"]["product"] == "Cherry tomato"
    assert data["top_item"]["cost"] == 1500.0
    assert data["top_item"]["cost_share_pct"] == 81.1


def test_rate_is_measured_against_farm_intake():
    with make_session() as session:
        seed(session)
        data = summarise(session)

    # 1,000 kg tomato + (500 × 0.04) basil = 1,020 kg harvested
    assert data["delivered_kg"] == 1020.0
    assert data["spoilage_pct"] == round(52 / 1020 * 100, 2)


def test_production_output_is_reported_separately_not_added_to_intake():
    """Processed output comes from the same produce that was delivered, so
    adding it to the denominator would count it twice."""
    with make_session() as session:
        seed(session)
        session.add(ProductionBatch(id=1, batch_number="PRD-1", status="completed",
                                    created_at=datetime(2026, 8, 15, tzinfo=timezone.utc)))
        session.flush()
        session.add(BatchOutput(batch_id=1, product_id=1, qty=Decimal("200")))
        session.commit()
        data = summarise(session)

    assert data["delivered_kg"] == 1020.0          # unchanged by the batch
    assert data["produced_kg"] == 200.0
    assert data["spoilage_pct_of_production"] == round(52 / 200 * 100, 2)


def test_rate_is_withheld_when_a_spoiled_product_has_no_weight():
    """The numerator would be missing rows the denominator still contains, so
    a rate would understate the loss. Better none than wrong."""
    with make_session() as session:
        seed(session, basil_weight=None)
        data = summarise(session)

    assert data["spoilage_pct"] is None
    assert data["products_missing_weight"] == ["Italian basil"]
    assert data["total_qty_kg"] == 50.0            # tomato only


def test_products_without_a_cost_are_named():
    with make_session() as session:
        seed(session, basil_cost=Decimal("0"))
        data = summarise(session)

    assert data["cost_is_complete"] is False
    assert data["products_missing_cost"] == ["Italian basil"]
    assert data["total_cost"] == 1500.0            # what is known, still counted


def test_breakdowns_by_product_and_reason():
    with make_session() as session:
        seed(session)
        data = summarise(session)

    by_reason = {r["reason"]: r for r in data["by_reason"]}
    assert by_reason["mold"]["qty"] == 50.0        # two tomato records merged
    assert by_reason["mold"]["records"] == 2
    assert by_reason["overripe"]["cost"] == 350.0

    assert [p["product"] for p in data["by_product"]] == ["Cherry tomato", "Italian basil"]


def test_date_filter_bounds_the_figures():
    with make_session() as session:
        seed(session)
        data = summarise(session, d_from=date(2026, 8, 11), d_to=date(2026, 8, 13))

    assert data["records"] == 1                    # only the basil record
    assert data["total_cost"] == 350.0
    assert data["top_item"]["product"] == "Italian basil"


def test_empty_period_is_all_zeroes_not_an_error():
    with make_session() as session:
        seed(session)
        data = summarise(session, d_from=date(2026, 1, 1), d_to=date(2026, 1, 31))

    assert data["records"] == 0
    assert data["total_cost"] == 0.0
    assert data["top_item"] is None
    assert data["spoilage_pct"] is None
