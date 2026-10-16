"""Field bot: workers report feed given to animals — «أكل للسمك ٥ كيلو».

The same pipeline as the other text kinds (see test_telegram_bot): a draft, the worker's confirmation, a manager's
approval, and only then a feeding log through the Animals page's own code, with the feed taken out of stock. When the
worker just says "السمك" and there is one fish group, that is the group.
"""
import json
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from app.models.animal import AnimalGroup, FeedingLog
from app.models.inventory import LocationStock, StockLocation
from app.models.product import Product
from app.services import field_service as field
from tests.test_telegram_bot import (  # noqa: F401  the fixture is used by name
    MANAGER_CHAT, REVIEW_PERMS, admin, approver, buttons_to, drafts, link, model, msg, scenario, send, tap, telegram,
)

FEED_PERMS = REVIEW_PERMS + ",page_animals,action_animal_feeding_create"

FISH_FEED = {"type": "animal_feeding", "date": None, "farm": None, "farm_match": None,
             "group": "السمك", "group_match": None,
             "items": [{"product": "علف سمك", "product_match": "Fish Feed 30%", "qty": "٥", "unit": "كيلو"}],
             "reason": None, "unclear": []}


async def animals(db):
    db.add_all([
        AnimalGroup(id=1, name="Tilapia pond", animal_type="fish", headcount=500, status="active"),
        AnimalGroup(id=2, name="Layers", animal_type="poultry", headcount=80, status="active"),
        Product(id=4, sku="FF30", name="Fish Feed 30%", unit="kg", price=Decimal("0"), cost=Decimal("25"),
                stock=Decimal("60"), is_active=True, item_type="raw"),
        StockLocation(id=1, name="Main Store", is_active=True),
        StockLocation(id=2, name="Pond Shed", is_active=True),
    ])
    await db.flush()
    db.add_all([LocationStock(location_id=1, product_id=4, qty=Decimal("15")),
                LocationStock(location_id=2, product_id=4, qty=Decimal("45"))])
    await db.commit()


async def reported(db, factory, payload=FISH_FEED, text="أكل للسمك ٥ كيلو", seen=None):
    await animals(db)
    await approver(db, perms=FEED_PERMS)
    await link(db, kinds=("animal_feeding",))
    await send(db, factory, msg(text), transport=model(payload, seen=seen))
    d = (await drafts(db))[-1]
    await send(db, factory, tap(f"fd:ok:{d.id}", d.bot_message_id))
    return (await drafts(db))[-1]


def test_fish_feed_is_approved_from_telegram_and_logged_as_a_feeding(telegram):
    async def go(db, factory):
        seen = []
        d = await reported(db, factory, seen=seen)
        system = seen[0]["messages"][0]["content"]
        assert '"Tilapia pond (fish)"' in system and "animal_feeding" in system            # the groups are given
        body = json.loads(d.draft_json)
        # "السمك" with one fish group: that group.
        assert d.kind == "animal_feeding" and body["group_match"]["id"] == 1 and body["lines"][0]["product"]["id"] == 4
        summary = [t for t in telegram.texts() if "السمك" in t and "Tilapia pond" in t][-1]
        assert "Fish Feed 30%" in summary and "من المخزن" in summary
        assert json.loads(d.warnings) == []
        assert buttons_to(telegram, MANAGER_CHAT) == [f"fa:ok:{d.id}", f"fa:no:{d.id}"]
        await send(db, factory, tap(f"fa:ok:{d.id}", 1, chat_id=MANAGER_CHAT))

        log = (await db.execute(select(FeedingLog))).scalar_one()
        assert (log.animal_group_id, log.product_id, float(log.qty), log.location_id) == (1, 4, 5.0, 2)
        assert log.user_id == 3 and "From the field bot" in log.note
        log_id = log.id
        db.expire_all()
        assert float((await db.get(Product, 4)).stock) == 55.0
        shed = (await db.execute(select(LocationStock).where(LocationStock.location_id == 2))).scalar_one()
        assert float(shed.qty) == 40.0
        [d] = await drafts(db)
        assert (d.status, d.result_refs) == ("confirmed", f"FL-{log_id}")
        assert "قبولها" in telegram.texts()[-1]
    scenario(go)


def test_an_unknown_group_or_feed_waits_for_review_in_azed(telegram):
    async def go(db, factory):
        d = await reported(db, factory, {**FISH_FEED, "group": "البط",
                                         "items": [{"product": "ذرة", "product_match": None, "qty": "٢", "unit": "كيلو"}]},
                           text="أكل للبط ٢ كيلو ذرة")
        warnings = set(json.loads(d.warnings))
        assert {"no_group", "missing:group", "unmatched_product"} <= warnings
        assert buttons_to(telegram, MANAGER_CHAT) == [f"fa:no:{d.id}"]
    scenario(go)


def test_a_manager_records_it_in_azed_and_a_short_store_records_nothing(telegram):
    async def go(db, factory):
        d_id = (await reported(db, factory)).id
        reviewer = field_reviewer()
        with __import__("pytest").raises(HTTPException) as e:
            await field.confirm(db, reviewer, d_id, {"date": field.today_local().isoformat(), "group_id": 1,
                                                     "items": [{"product_id": 4, "location_id": 1, "qty": 20}]})
        assert e.value.status_code == 400 and "Line 1" in e.value.detail
        assert (await db.execute(select(FeedingLog))).scalars().all() == []
        await field.confirm(db, reviewer, d_id, {"date": field.today_local().isoformat(), "group_id": 2,
                                                 "items": [{"product_id": 4, "location_id": 1, "qty": 10}]})
        row = (await db.execute(select(FeedingLog.animal_group_id, FeedingLog.location_id, FeedingLog.qty))).one()
        assert (row[0], row[1], float(row[2])) == (2, 1, 10.0)
    scenario(go)


def test_fish_is_an_animal_type_on_the_page(telegram):
    from types import SimpleNamespace
    from tests.test_permission_enforcement import _make_client
    client, _ = _make_client(SimpleNamespace(id=1, name="A", email="a@x", role="admin", permissions="", is_active=True))
    assert client.get("/animals/").text.count('<option value="fish">Fish</option>') == 2


def field_reviewer():
    from types import SimpleNamespace
    return SimpleNamespace(id=3, name="Manager", role="viewer", permissions=FEED_PERMS)
