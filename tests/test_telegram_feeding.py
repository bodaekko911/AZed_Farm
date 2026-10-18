"""Field bot: workers report feed — «علف الفراخ ٥ كيلو» for an animal group, «أكل للسمك ٥ كيلو» for a fish tank.

The same pipeline as the other text kinds (see test_telegram_bot): a draft, the worker's confirmation, a manager's
approval, and only then the record, with the feed taken out of stock. Animal feed goes to the Animals page's feeding
log; fish feed goes to the fish tank of a sandponic bed (SPC) — fish are kept apart from the Animals page. When the
worker names no tank and there is only one, that is the tank.
"""
import json
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.animal import AnimalGroup, FeedingLog
from app.models.farm import FarmPlot, FishLog, FishTank
from app.models.inventory import LocationStock, StockLocation
from app.models.product import Product
from app.services import field_service as field
from tests.test_telegram_bot import (  # noqa: F401  the fixture is used by name
    MANAGER_CHAT, REVIEW_PERMS, approver, buttons_to, drafts, link, model, msg, scenario, send, tap, telegram,
)

FEED_PERMS = REVIEW_PERMS + ",page_animals,action_animal_feeding_create,action_farm_plot_input"

CHICKEN_FEED = {"type": "animal_feeding", "date": None, "farm": None, "farm_match": None,
                "group": "الفراخ", "group_match": "Layers (poultry)",
                "items": [{"product": "علف فراخ", "product_match": "Layer Feed", "qty": "٥", "unit": "كيلو"}],
                "reason": None, "unclear": []}
FISH_FEED = {"type": "fish_feed", "date": None, "farm": None, "farm_match": None, "tank": None, "tank_match": None,
             "items": [{"product": "علف سمك", "product_match": "Fish Feed 30%", "qty": "٥", "unit": "كيلو"}],
             "reason": None, "unclear": []}


async def stock(db):
    db.add_all([
        AnimalGroup(id=2, name="Layers", animal_type="poultry", headcount=80, status="active"),
        FarmPlot(id=1, farm_id=2, name="SPC bed 1", plot_type="greenhouse", is_active=1),
        FishTank(id=1, plot_id=1, name="Tank 1", species="Tilapia", fish_count=500, is_active=1),
        Product(id=4, sku="FF30", name="Fish Feed 30%", unit="kg", price=Decimal("0"), cost=Decimal("25"),
                stock=Decimal("60"), is_active=True, item_type="raw"),
        Product(id=5, sku="LF", name="Layer Feed", unit="kg", price=Decimal("0"), cost=Decimal("12"),
                stock=Decimal("40"), is_active=True, item_type="raw"),
        StockLocation(id=1, name="Main Store", is_active=True),
        StockLocation(id=2, name="Pond Shed", is_active=True),
    ])
    await db.flush()
    db.add_all([LocationStock(location_id=1, product_id=4, qty=Decimal("15")),
                LocationStock(location_id=2, product_id=4, qty=Decimal("45")),
                LocationStock(location_id=1, product_id=5, qty=Decimal("40"))])
    await db.commit()


async def reported(db, factory, payload, text, kinds, seen=None):
    await stock(db)
    await approver(db, perms=FEED_PERMS)
    await link(db, kinds=kinds)
    await send(db, factory, msg(text), transport=model(payload, seen=seen))
    d = (await drafts(db))[-1]
    await send(db, factory, tap(f"fd:ok:{d.id}", d.bot_message_id))
    return (await drafts(db))[-1]


# ── Fish feed → the sandponic fish tank ──────────────────────────────

def test_fish_feed_goes_to_the_only_tank_and_out_of_the_fullest_store(telegram):
    async def go(db, factory):
        seen = []
        d = await reported(db, factory, FISH_FEED, "أكل للسمك ٥ كيلو", ("fish_feed", "animal_feeding"), seen=seen)
        system = seen[0]["messages"][0]["content"]
        assert '"Tank 1 (SPC bed 1)"' in system and "fish_feed" in system
        body = json.loads(d.draft_json)
        assert d.kind == "fish_feed" and body["tank_match"]["id"] == 1 and body["lines"][0]["product"]["id"] == 4
        summary = [t for t in telegram.texts() if "حوض السمك" in t][-1]
        assert "Tank 1 (SPC bed 1)" in summary and "من المخزن" in summary
        assert json.loads(d.warnings) == []
        assert buttons_to(telegram, MANAGER_CHAT) == [f"fa:ok:{d.id}", f"fa:no:{d.id}"]
        await send(db, factory, tap(f"fa:ok:{d.id}", 1, chat_id=MANAGER_CHAT))

        log = (await db.execute(select(FishLog))).scalar_one()
        assert (log.tank_id, log.log_type, log.product_id, float(log.qty), log.location_id) == (1, "feed", 4, 5.0, 2)
        assert float(log.total_cost) == 125.0 and log.user_id == 3 and "From the field bot" in log.note
        log_id = log.id
        assert (await db.execute(select(FeedingLog))).scalars().all() == []          # nothing on the Animals page
        db.expire_all()
        assert float((await db.get(Product, 4)).stock) == 55.0
        [d] = await drafts(db)
        assert (d.status, d.result_refs) == ("confirmed", f"FF-{log_id}")
        assert "قبولها" in telegram.texts()[-1]
    scenario(go)


def test_fish_feed_with_two_tanks_and_none_named_waits_for_review(telegram):
    async def go(db, factory):
        await stock(db)
        db.add(FishTank(id=2, plot_id=1, name="Tank 2", fish_count=200, is_active=1))
        await db.commit()
        await approver(db, perms=FEED_PERMS)
        await link(db, kinds=("fish_feed",))
        await send(db, factory, msg("أكل للسمك ٥ كيلو"), transport=model(FISH_FEED))
        d = (await drafts(db))[-1]
        assert {"no_tank", "missing:tank"} <= set(json.loads(d.warnings))
        await send(db, factory, tap(f"fd:ok:{d.id}", d.bot_message_id))
        assert buttons_to(telegram, MANAGER_CHAT) == [f"fa:no:{d.id}"]
        d = (await drafts(db))[-1]                                    # now in review
        # A reviewer picks the tank in AZed.
        await field.confirm(db, field_reviewer(), d.id, {"date": field.today_local().isoformat(), "tank_id": 2,
                                                         "items": [{"product_id": 4, "location_id": 1, "qty": 5}]})
        log = (await db.execute(select(FishLog.tank_id, FishLog.location_id))).one()
        assert tuple(log) == (2, 1)
    scenario(go)


# ── Animal feed → the Animals page's feeding log ─────────────────────

def test_animal_feed_is_approved_from_telegram_and_logged_as_a_feeding(telegram):
    async def go(db, factory):
        d = await reported(db, factory, CHICKEN_FEED, "علف الفراخ ٥ كيلو", ("animal_feeding",))
        body = json.loads(d.draft_json)
        assert d.kind == "animal_feeding" and body["group_match"]["id"] == 2 and body["lines"][0]["product"]["id"] == 5
        assert json.loads(d.warnings) == []
        await send(db, factory, tap(f"fa:ok:{d.id}", 1, chat_id=MANAGER_CHAT))
        log = (await db.execute(select(FeedingLog))).scalar_one()
        assert (log.animal_group_id, log.product_id, float(log.qty), log.location_id) == (2, 5, 5.0, 1)
        log_id = log.id
        assert (await drafts(db))[0].result_refs == f"FL-{log_id}"
    scenario(go)


def test_an_unknown_group_or_feed_waits_for_review_in_azed(telegram):
    async def go(db, factory):
        d = await reported(db, factory, {**CHICKEN_FEED, "group": "البط", "group_match": None,
                                         "items": [{"product": "ذرة", "product_match": None, "qty": "٢", "unit": "كيلو"}]},
                           "أكل للبط ٢ كيلو ذرة", ("animal_feeding",))
        assert {"no_group", "missing:group", "unmatched_product"} <= set(json.loads(d.warnings))
        assert buttons_to(telegram, MANAGER_CHAT) == [f"fa:no:{d.id}"]
    scenario(go)


def test_a_short_store_records_nothing(telegram):
    async def go(db, factory):
        d_id = (await reported(db, factory, CHICKEN_FEED, "علف الفراخ ٥ كيلو", ("animal_feeding",))).id
        with pytest.raises(HTTPException) as e:
            await field.confirm(db, field_reviewer(), d_id, {"date": field.today_local().isoformat(), "group_id": 2,
                                                             "items": [{"product_id": 5, "location_id": 2, "qty": 5}]})
        assert e.value.status_code == 400 and "Line 1" in e.value.detail
        assert (await db.execute(select(FeedingLog))).scalars().all() == []
    scenario(go)


def field_reviewer():
    from types import SimpleNamespace
    return SimpleNamespace(id=3, name="Manager", role="viewer", permissions=FEED_PERMS)
