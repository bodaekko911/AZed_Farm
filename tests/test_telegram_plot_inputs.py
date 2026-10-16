"""Field bot: workers report seeds and fertilizer put into a greenhouse or field.

The same pipeline as the other text kinds (see test_telegram_bot): the message becomes a draft, the worker confirms
it, a manager approves it — here from Telegram when nothing needs a closer look — and only then is it recorded,
through the farm page's own code. A product matched surely is taken out of stock; anything else is recorded as bought
directly, by its written name and cost. A merely close product match is never taken out of stock on its own.
"""
import json
from decimal import Decimal

from sqlalchemy import select

from app.models.farm import FarmPlot, PlotInput
from app.models.inventory import LocationStock, StockLocation
from app.models.product import Product
from app.models.telegram import TelegramChat
from app.services import field_service as field
from tests.test_telegram_bot import (  # noqa: F401  the fixture is used by name
    MANAGER_CHAT, REVIEW_PERMS, admin, approver, buttons_to, drafts, link, model, msg, scenario, send, tap, telegram,
)

PLOT_PERMS = REVIEW_PERMS + ",action_farm_plot_input"

FERTILIZER = {"type": "plot_input", "date": None, "farm": None, "farm_match": None,
              "plot": "الصوبة ١", "plot_match": "Greenhouse 1 (North Farm)", "crop": "طماطم",
              "items": [{"product": "سماد NPK", "product_match": "NPK 20-20-20", "qty": "٢٠", "unit": "كيلو",
                         "input_type": "fertilizer", "cost": None}],
              "reason": None, "unclear": []}
SEEDS = {**FERTILIZER, "items": [{"product": "بذور طماطم", "product_match": None, "qty": "٣", "unit": "باكو",
                                  "input_type": "seed", "cost": "٤٥٠"}]}


async def plots(db):
    db.add_all([
        FarmPlot(id=1, farm_id=1, name="Greenhouse 1", plot_type="greenhouse", is_active=1),
        FarmPlot(id=2, farm_id=2, name="East Field", plot_type="field", is_active=1),
        Product(id=3, sku="NPK", name="NPK 20-20-20", unit="kg", price=Decimal("0"), cost=Decimal("40"),
                stock=Decimal("100"), is_active=True, item_type="raw"),
        StockLocation(id=1, name="Main Store", is_active=True),
        StockLocation(id=2, name="Farm Shed", is_active=True),
    ])
    await db.flush()
    db.add_all([LocationStock(location_id=1, product_id=3, qty=Decimal("30")),
                LocationStock(location_id=2, product_id=3, qty=Decimal("70"))])
    await db.commit()


async def reported(db, factory, payload, text="سماد ٢٠ كيلو للصوبة ١", seen=None):
    """The worker's message read into a draft and sent for review."""
    await plots(db)
    await approver(db, perms=PLOT_PERMS)
    await link(db, kinds=("plot_input", "spoilage"))
    await send(db, factory, msg(text), transport=model(payload, seen=seen))
    d = (await drafts(db))[-1]
    await send(db, factory, tap(f"fd:ok:{d.id}", d.bot_message_id))
    return (await drafts(db))[-1]


def test_fertilizer_from_stock_is_approved_from_telegram_and_leaves_the_fullest_store(telegram):
    async def go(db, factory):
        seen = []
        d = await reported(db, factory, FERTILIZER, seen=seen)
        system = seen[0]["messages"][0]["content"]
        assert '"Greenhouse 1 (North Farm)"' in system and "plot_input" in system       # the plot list is given
        body = json.loads(d.draft_json)
        assert d.kind == "plot_input" and body["plot_match"]["id"] == 1 and body["lines"][0]["product"]["id"] == 3
        worker_summary = [t for t in telegram.texts() if "بذور / سماد" in t][-1]
        assert "Greenhouse 1 (North Farm)" in worker_summary and "من المخزن" in worker_summary
        assert json.loads(d.warnings) == []
        assert buttons_to(telegram, MANAGER_CHAT) == [f"fa:ok:{d.id}", f"fa:no:{d.id}"]     # clean: approvable here
        await send(db, factory, tap(f"fa:ok:{d.id}", 1, chat_id=MANAGER_CHAT))

        entry = (await db.execute(select(PlotInput))).scalar_one()
        assert (entry.plot_id, entry.product_id, entry.input_type, float(entry.qty)) == (1, 3, "fertilizer", 20.0)
        assert entry.location_id == 2 and float(entry.total_cost) == 800.0 and entry.crop == "طماطم"
        assert entry.user_id == 3 and "From the field bot" in entry.note                     # the approving manager
        entry_id = entry.id
        db.expire_all()
        assert float((await db.get(Product, 3)).stock) == 80.0
        shed = (await db.execute(select(LocationStock).where(LocationStock.location_id == 2))).scalar_one()
        assert float(shed.qty) == 50.0
        [d] = await drafts(db)
        assert (d.status, d.result_refs) == ("confirmed", f"PI-{entry_id}")
        assert "قبولها" in telegram.texts()[-1]                                              # the worker is told
    scenario(go)


def test_seeds_bought_directly_are_recorded_by_name_and_cost_without_touching_stock(telegram):
    async def go(db, factory):
        d = await reported(db, factory, SEEDS, text="زرعنا ٣ باكو بذور طماطم في الصوبة ١ بـ ٤٥٠")
        line = json.loads(d.draft_json)["lines"][0]
        # "بذور طماطم" is close to Tomatoes by spelling — offered on the card, never taken out of stock.
        assert line["product"] is None and line["source"] == "direct"
        assert "شراء مباشر" in [t for t in telegram.texts() if "بذور / سماد" in t][-1]
        await send(db, factory, tap(f"fa:ok:{d.id}", 1, chat_id=MANAGER_CHAT))
        entry = (await db.execute(select(PlotInput))).scalar_one()
        assert (entry.product_id, entry.item_name, entry.unit, float(entry.qty)) == (None, "بذور طماطم", "باكو", 3.0)
        assert (entry.input_type, float(entry.total_cost)) == ("seed", 450.0)
        db.expire_all()
        assert float((await db.get(Product, 1)).stock) == 50.0                               # tomatoes untouched
    scenario(go)


def test_what_needs_a_closer_look_waits_for_review_in_azed(telegram):
    async def go(db, factory):
        # No greenhouse / field found, and an unmatched item with no cost.
        d = await reported(db, factory, {**SEEDS, "plot": "الصوبة ٩", "plot_match": None,
                                         "items": [{**SEEDS["items"][0], "cost": None}]})
        warnings = json.loads(d.warnings)
        assert {"no_plot", "missing:plot", "not_in_stock"} <= set(warnings)
        assert buttons_to(telegram, MANAGER_CHAT) == [f"fa:no:{d.id}"]                        # reject only
        # More than the stores hold is flagged too.
        body = {"plot_match": {"id": 1}, "missing": [], "unclear": [],
                "lines": [{"product": {"id": 3, "unit": "kg", "stock": 100}, "qty": 500, "unit": "kg"}]}
        assert "low_stock" in field.warnings_for("plot_input", body)
    scenario(go)


def test_a_manager_records_it_in_azed_from_a_chosen_store(telegram):
    async def go(db, factory):
        d = await reported(db, factory, FERTILIZER)
        out = await field.confirm(db, field_reviewer(), d.id, {
            "date": field.today_local().isoformat(), "plot_id": 1,
            "items": [{"input_type": "fertilizer", "product_id": 3, "location_id": 1, "qty": 25, "total_cost": 900}]})
        entry = (await db.execute(select(PlotInput))).scalar_one()
        assert (entry.location_id, float(entry.qty), float(entry.total_cost)) == (1, 25.0, 900.0)
        assert out["refs"] == f"PI-{entry.id}"
        store = (await db.execute(select(LocationStock).where(LocationStock.location_id == 1))).scalar_one()
        await db.refresh(store)
        assert float(store.qty) == 5.0
    scenario(go)


def test_too_much_from_one_store_records_nothing(telegram):
    async def go(db, factory):
        d = await reported(db, factory, FERTILIZER)
        from fastapi import HTTPException
        try:
            await field.confirm(db, admin(), d.id, {         # an admin: the lines' products differ from the draft
                "date": field.today_local().isoformat(), "plot_id": 1,
                "items": [{"input_type": "seed", "item_name": "Seeds", "qty": 1, "total_cost": 10},
                          {"input_type": "fertilizer", "product_id": 3, "location_id": 1, "qty": 40}]})
            raise AssertionError("should have failed")
        except HTTPException as exc:
            assert exc.status_code == 400 and exc.detail.startswith("Line 2:")
        assert (await db.execute(select(PlotInput))).scalars().all() == []                     # the first line isn't kept
        assert (await drafts(db))[-1].status == "in_review"
    scenario(go)


def test_a_chat_not_allowed_seeds_and_fertilizer_is_told_so(telegram):
    async def go(db, factory):
        await plots(db)
        await link(db, kinds=("spoilage",))
        await send(db, factory, msg("سماد ٢٠ كيلو للصوبة ١"), transport=model(FERTILIZER))
        assert "لا يمكنك إرسال هذا النوع" in telegram.texts()[-1]
        assert (await db.execute(select(PlotInput))).scalars().all() == []
        chat = (await db.execute(select(TelegramChat))).scalar_one()
        assert "plot_input" in field.SUBMISSION_KINDS and "plot_input" not in chat.allowed_kinds
    scenario(go)


def field_reviewer():
    from types import SimpleNamespace
    return SimpleNamespace(id=3, name="Manager", role="viewer", permissions=PLOT_PERMS)
