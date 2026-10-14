"""The business vocabulary (phase 1): local names, nicknames and abbreviations for records.

What is protected here:
* an exact alias beats a similar real name, everywhere a name is resolved;
* one alias means one record per type (a duplicate is refused, naming the record it already means);
* inactive records keep their aliases but are not matched;
* Arabic spellings that mean the same word compare equal;
* the delivery areas, now vocabulary entries, behave exactly as the old hard-coded list;
* the two aliases that were in the prompt's LOCAL_WORDS resolve to the same records;
* the vocabulary is loaded with one query per request, not one per name.
"""
import asyncio
import importlib.util
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.models  # noqa: E402,F401  (every table)
from app.core.config import settings  # noqa: E402
from app.core.log import ActivityLog  # noqa: E402
from app.core.permission_catalog import get_role_permissions  # noqa: E402
from app.core.text_norm import normalize  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.accounting import Account  # noqa: E402
from app.models.b2b import B2BClient  # noqa: E402
from app.models.customer import Customer  # noqa: E402
from app.models.expense import ExpenseCategory  # noqa: E402
from app.models.farm import Farm  # noqa: E402
from app.models.hr import Employee  # noqa: E402
from app.models.product import Product  # noqa: E402
from app.models.supplier import Supplier  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.vocabulary import AREA_SEEDS, VocabularyEntry  # noqa: E402
from app.services import assistant_actions as act  # noqa: E402
from app.services import assistant_service  # noqa: E402
from app.services import document_reader as reader  # noqa: E402
from app.services import field_service  # noqa: E402
from app.services import quotation_service  # noqa: E402
from app.services import vocabulary_service as vocab  # noqa: E402

ADMIN = SimpleNamespace(id=1, name="Boss", role="admin", permissions=None)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_API_KEY", "test-key")
    monkeypatch.setattr(settings, "ASSISTANT_MODEL", "test-model")
    yield
    vocab.invalidate()


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _db(engine_out: list | None = None):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()
    db.add_all([
        User(id=1, name="Boss", email="boss@x.test", password="x", role="admin"),
        Account(code="1000", name="Cash", type="asset", balance=0),
        Account(code="1100", name="Accounts Receivable", type="asset", balance=0),
        Account(code="4000", name="Sales Revenue", type="revenue", balance=0),
        Account(code="5500", name="Other Expenses", type="expense", balance=0),
        ExpenseCategory(id=1, name="Fuel & Diesel", account_code="5500", is_active="1"),
        ExpenseCategory(id=2, name="Seeds", account_code="5500", is_active="1"),
        Farm(id=1, name="North Farm", is_active=1),
        Farm(id=2, name="Habiba/SPC", is_active=1),
        Farm(id=3, name="Old Farm", is_active=0),
        Employee(id=1, name="Taha Mahmoud", base_salary=Decimal("6000"), is_active=True, hire_date=date(2025, 1, 1)),
        Product(id=1, sku="LET", name="Local Lettuce", unit="kg", price=Decimal("30"), cost=Decimal("10"),
                stock=Decimal("50"), is_active=True),
        # Spelled almost exactly like the alias, so the alias has something to beat.
        Product(id=2, sku="MIX", name="خس بلدي مشكل", unit="kg", price=Decimal("40"), cost=Decimal("12"),
                stock=Decimal("20"), is_active=True),
        Product(id=3, sku="OLD", name="Retired Rocket", unit="kg", price=Decimal("20"), stock=0, is_active=False),
        Product(id=10, sku="DEL-SH", name="Sharm Delivery", unit="trip", price=Decimal("50"), stock=0,
                item_type="service", is_active=True),
        Product(id=11, sku="DEL-DH", name="Dahab Delivery", unit="trip", price=Decimal("120"), stock=0,
                item_type="service", is_active=True),
        Customer(id=1, name="Walk-in Customer"),
        Customer(id=2, name="Mona Adel", phone="0100"),
        B2BClient(id=1, name="Wave Hotel Sharm", is_active=True),
        B2BClient(id=2, name="Habiba Kitchen", is_active=True),
        Supplier(id=1, name="Delta Seeds Co", phone="0111"),
    ])
    await db.commit()
    if engine_out is not None:
        engine_out.append(engine)
    return engine, db


def scenario(fn):
    async def go():
        engine, db = await _db()
        vocab.invalidate()
        try:
            return await fn(db)
        finally:
            await db.close()
            await engine.dispose()
    return run(go())


async def alias(db, kind, text, record_id=None, **kw):
    entry, warnings = await vocab.create_entry(db, ADMIN, kind=kind, alias=text, record_id=record_id, **kw)
    return warnings


# ── Arabic normalisation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("a, b", [
    ("أحمد", "احمد"), ("إسماعيل", "اسماعيل"), ("آمنة", "امنه"),           # alef forms; ta marbuta
    ("القاهرة", "القاهره"),                                                 # ta marbuta → ha
    ("بلدى", "بلدي"), ("مستشفى", "مستشفي"),                                  # alef maqsura → ya
    ("طَمَاطِم", "طماطم"), ("خُسّ", "خس"),                                      # tashkeel
    ("كيـــلو", "كيلو"),                                                     # tatweel
    ("٣ قفص", "3 قفص"), ("١٢٫٥", "12 5"), ("۴", "4"),                        # Arabic-Indic and Persian digits
])
def test_arabic_spellings_that_mean_the_same_word_compare_equal(a, b):
    assert normalize(a) == normalize(b)


def test_normalisation_keeps_the_old_basics():
    assert act._norm("  Raw   Honey (500g)! ") == "raw honey 500g"
    assert act._norm("Walk-in") == "walk in"


# ── Matching ─────────────────────────────────────────────────────────────────

def test_an_exact_alias_beats_a_similar_real_name():
    async def go(db):
        products = (await db.execute(select(Product))).scalars().all()
        await vocab.ready(db)
        # Without the alias the near-identical real name wins.
        assert act.best_matches("خس بلدي", products, lambda p: p.name, kind="product")[0][1].id == 2
        await alias(db, "product", "خس بلدي", 1)
        await vocab.ready(db)
        top_score, top = act.best_matches("خَس بلدى", products, lambda p: p.name, kind="product")[0]
        assert (top_score, top.id) == (1.0, 1)
        assert act._one("خس بلدي", products, lambda p: p.name, "product", kind="product").id == 1
        # Without a kind the vocabulary is not consulted: callers opt in, type by type.
        assert act.best_matches("خس بلدي", products, lambda p: p.name)[0][1].id == 2
    scenario(go)


def test_an_alias_of_an_inactive_record_is_skipped():
    async def go(db):
        await alias(db, "product", "جرجير", 3)
        await vocab.ready(db)
        products = (await db.execute(select(Product))).scalars().all()
        assert all(p.id != 3 for _s, p in act.best_matches("جرجير", products, lambda p: p.name, kind="product"))
        # …and kept: it is still listed.
        assert [e["alias"] for e in await vocab.list_entries(db, "product")] == ["جرجير"]
    scenario(go)


def test_a_duplicate_alias_is_refused_naming_the_record():
    async def go(db):
        await alias(db, "product", "خس بلدي", 1)
        with pytest.raises(vocab.VocabularyError, match="already means 'Local Lettuce'"):
            await alias(db, "product", "خَس بلدى", 2)            # the same alias, spelled differently
        # The same text may mean a record of ANOTHER type.
        await alias(db, "farm", "خس بلدي", 1)
    scenario(go)


def test_an_alias_that_is_another_records_name_is_allowed_with_a_warning():
    async def go(db):
        warnings = await alias(db, "product", "خس بلدي مشكل", 1)
        assert warnings and "also the real name of 'خس بلدي مشكل'" in warnings[0]
    scenario(go)


def test_every_change_is_in_the_activity_log():
    async def go(db):
        await alias(db, "product", "خس بلدي", 1)
        entry = (await db.execute(select(VocabularyEntry).where(VocabularyEntry.alias == "خس بلدي"))).scalar_one()
        await vocab.update_entry(db, ADMIN, entry.id, alias="خس", record_id=1, note="short")
        await vocab.delete_entry(db, ADMIN, entry.id)
        actions = [a for (a,) in (await db.execute(select(ActivityLog.action).where(
            ActivityLog.module == "Vocabulary").order_by(ActivityLog.id))).all()]
        assert actions == ["add_alias", "edit_alias", "delete_alias"]
    scenario(go)


# ── Areas: exactly the old hard-coded list ───────────────────────────────────

OLD_AREA_ALIASES = [("sharm", "شرم"), ("dahab", "دهب"), ("nuweiba", "نويبع"), ("taba", "طابا"), ("cairo", "القاهرة"),
                    ("hurghada", "الغردقة"), ("el tor", "الطور")]


def old_pick_delivery(products, *texts):
    """pick_delivery as it was, with the list hard-coded — the reference the vocabulary must reproduce."""
    text = " " + act._norm(" ".join(str(t or "") for t in texts)) + " "
    for latin, arabic in OLD_AREA_ALIASES:
        if latin in act._norm(text) or act._norm(arabic) in text:
            text += f" {latin} {act._norm(arabic)} "
    for p in products:
        if act.is_delivery_text(p.name) and any(w in text for w in act._area_words(p.name)):
            return p
    return None


AREA_TEXTS = ["Hadaba, Sharm El Sheikh", "شرم الشيخ - نبق", "Dahab", "دهب، العصلة", "Nuweiba", "نويبع",
              "Taba Heights", "طابا", "Cairo, Maadi", "القاهرة الجديدة", "Hurghada", "الغردقة", "El Tor", "الطور",
              "Alexandria", "", "Shipping", "توصيل شرم", "delivery to dahab", None]


def test_the_seeded_areas_are_exactly_the_old_list():
    async def go(db):
        groups = sorted((e.target_key, e.alias) for e in (await db.execute(
            select(VocabularyEntry).where(VocabularyEntry.entity_type == "area"))).scalars())
        assert groups == sorted((latin, arabic) for latin, arabic in OLD_AREA_ALIASES)
        assert AREA_SEEDS == OLD_AREA_ALIASES
    scenario(go)


def test_pick_delivery_behaves_exactly_as_with_the_hard_coded_list():
    async def go(db):
        await vocab.ready(db)
        products = [p for p in (await db.execute(select(Product))).scalars().all()]
        names = ["Sharm Delivery", "Dahab Delivery", "Nuweiba Delivery", "Taba Delivery", "Cairo Delivery",
                 "Hurghada Delivery", "El Tor Delivery", "توصيل شرم", "توصيل القاهرة"]
        every = products + [SimpleNamespace(id=100 + n, name=name) for n, name in enumerate(names)]
        for text in AREA_TEXTS:
            for area in (None, "Dahab", "شرم"):
                for pool in (every, list(reversed(every))):
                    new, old = act.pick_delivery(pool, text, area), old_pick_delivery(pool, text, area)
                    assert (new and new.name) == (old and old.name), (text, area)
    scenario(go)


def test_an_area_alias_joins_an_existing_group_and_groups_do_not_split():
    async def go(db):
        await alias(db, "area", "sharm el sheikh", group="sharm")
        # "Sharm" as a new group IS the group "sharm" once normalised — not a second one.
        await alias(db, "area", "شرم الشيخ", new_group="Sharm")
        assert await vocab.area_group_names(db) == sorted({normalize(g) for g, _a in OLD_AREA_ALIASES})
        # A "new" group named after a spelling of another group is refused.
        with pytest.raises(vocab.VocabularyError, match="already a spelling in the area group 'sharm'"):
            await alias(db, "area", "sharmy", new_group="شرم")
        with pytest.raises(vocab.VocabularyError, match="Pick the area group"):
            await alias(db, "area", "ras sudr")
        await vocab.ready(db)
        products = (await db.execute(select(Product))).scalars().all()
        assert act.pick_delivery(products, "sharm el sheikh, naama bay").sku == "DEL-SH"
    scenario(go)


# ── The aliases that were in LOCAL_WORDS ─────────────────────────────────────

def _migration():
    path = next(Path("alembic/versions").glob("*_0058_vocabulary.py"))
    spec = importlib.util.spec_from_file_location("m0058", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_local_words_aliases_resolve_exactly_as_before():
    """Before, the prompt told the model these words; now the vocabulary resolves them. Either way the client is
    Habiba Kitchen and the farm Habiba/SPC — and the official names still resolve as they always did."""
    async def go(db):
        for _twice in range(2):                                 # idempotent
            await (await db.connection()).run_sync(_migration().seed)
            await db.commit()
        seeded = sorted((e.entity_type, e.alias, e.record_id) for e in (await db.execute(
            select(VocabularyEntry).where(VocabularyEntry.entity_type != "area"))).scalars())
        assert seeded == [("b2b_client", "المطبخ", 2), ("b2b_client", "للمطبخ", 2),
                          ("farm", "الساند بونك", 2), ("farm", "من الساند بونك", 2)]
        await vocab.ready(db)
        clients = (await db.execute(select(B2BClient))).scalars().all()
        farms = (await db.execute(select(Farm))).scalars().all()
        for said in ("للمطبخ", "المطبخ", "Habiba Kitchen"):
            assert act._one(said, clients, lambda c: c.name, "B2B client", kind="b2b_client").name == "Habiba Kitchen"
        for said in ("الساند بونك", "من الساند بونك", "Habiba/SPC"):
            assert act._one(said, farms, lambda f: f.name, "farm", kind="farm").name == "Habiba/SPC"
        # The field bot: a B2B sale "للمطبخ" with no suggestion from the model.
        raw = {"client": "للمطبخ", "date": "2026-10-01", "items": [{"product": "Local Lettuce", "qty": 2}]}
        draft = await field_service.match_b2b_sale(db, reader.parse_harvest(raw), raw, await reader._choices(db),
                                                   await reader.b2b_clients(db))
        assert draft["client_match"]["name"] == "Habiba Kitchen"
        # A harvest sheet from "الساند بونك".
        sheet = await reader.match_harvest(db, reader.parse_harvest(
            {"date": "2026-10-01", "farm": "الساند بونك", "items": [{"product": "Local Lettuce", "qty": 3}]}),
            await reader._choices(db))
        assert sheet["farm_match"]["name"] == "Habiba/SPC"
    scenario(go)


def test_the_prompt_still_carries_the_local_words():
    assert "للمطبخ" in assistant_service.LOCAL_WORDS and "هالك" in assistant_service.LOCAL_WORDS


# ── Every call site resolves through the vocabulary ──────────────────────────

def test_ask_actions_resolve_aliases():
    async def go(db):
        await alias(db, "expense_category", "سولار", 1)
        await alias(db, "farm", "الشمالية", 1)
        await alias(db, "employee", "عم طه", 1)
        await alias(db, "product", "خس بلدي", 1)
        await alias(db, "b2b_client", "فندق الموجة", 1)
        await alias(db, "pos_customer", "مدام منى", 2)
        vocab.invalidate()

        card = await act.propose_expense(db, ADMIN, {"amount": 100, "category": "سولار", "farm": "الشمالية"})
        assert dict(card["lines"])["Category"] == "Fuel & Diesel" and dict(card["lines"])["Farm"] == "North Farm"
        card = await act.propose_attendance(db, ADMIN, {"employee": "عم طه", "date": "2026-01-05"})
        assert dict(card["lines"])["Employee"] == "Taha Mahmoud"
        card = await act.propose_stock(db, ADMIN, {"product": "خس بلدي", "change_by": 5})
        assert "Local Lettuce" in dict(card["lines"])["Product"]
        card = await act.propose_price(db, ADMIN, {"product": "خس بلدي", "new_price": 35})
        assert "Local Lettuce" in dict(card["lines"])["Product"]
        card = await act.propose_farm_delivery(db, ADMIN, {"farm": "الشمالية", "items": [{"product": "خس بلدي", "qty": 4}]})
        assert card["payload"]["farm_id"] == 1 and card["payload"]["items"][0]["product_id"] == 1
        card = await act.propose_spoilage(db, ADMIN, {"product": "خس بلدي", "qty": 1, "farm": "الشمالية"})
        assert card["payload"]["product_id"] == 1
        card = await act.propose_b2b_sale(db, ADMIN, {"client": "فندق الموجة", "items": [{"product": "خس بلدي", "qty": 2}]})
        assert card["payload"]["client_id"] == 1
        card = await act.propose_pos_sale(db, ADMIN, {"customer": "مدام منى", "items": [{"product": "خس بلدي", "qty": 1}]})
        assert card["payload"]["customer_id"] == 2
        with pytest.raises(ValueError) as exc:                  # resolved, then refused for a reason of its own
            await act.propose_b2b_payment(db, ADMIN, {"client": "فندق الموجة", "amount": 10})
        assert "Wave Hotel Sharm" in str(exc.value)
        card = await act.propose_quotation(db, ADMIN, {"client": "فندق الموجة", "items": [{"product": "خس بلدي", "qty": 2}]})
        assert card["payload"]["client_id"] == 1
    scenario(go)


def test_pdf_invoices_resolve_aliases():
    async def go(db):
        await alias(db, "product", "خس بلدي", 1)
        await alias(db, "pos_customer", "مدام منى", 2)
        vocab.invalidate()
        (inv,) = await act.match_invoices(db, [{"number": "S-9", "date": "2026-01-01", "customer": "مدام منى",
                                                "discount": 0, "paid": True, "items": [
                                                    {"description": "خس بلدي 500g", "sku": None, "qty": 2,
                                                     "unit_price": 15, "line_total": 30}]}])
        assert inv["customer_match"]["id"] == 2
        assert inv["lines"][0]["product"]["id"] == 1 and inv["lines"][0]["match"] == "alias"
        # A "new" customer that is really an alias of an existing one is reused, not duplicated.
        assert await act._customer_for(db, ADMIN, {"name": "مدام منى"}) == 2
    scenario(go)


def test_the_review_pickers_find_records_by_alias():
    async def go(db):
        await alias(db, "product", "خس بلدي", 1)
        await alias(db, "pos_customer", "مدام منى", 2)
        await alias(db, "supplier", "الدلتا", 1)
        vocab.invalidate()
        assert [p["id"] for p in await act.search(db, "products", "خس بلدي")][:1] == [1]
        assert [c["id"] for c in await act.search(db, "customers", "منى")][:1] == [2]
        assert [s["id"] for s in await reader.search(db, ADMIN, "suppliers", "الدلتا")] == [1]
        assert [p["id"] for p in await reader.search(db, ADMIN, "products", "بلدي")][:1] == [1]
    scenario(go)


def test_documents_resolve_aliases():
    async def go(db):
        await alias(db, "expense_category", "سولار", 1)
        await alias(db, "farm", "الشمالية", 1)
        await alias(db, "supplier", "الدلتا", 1)
        await alias(db, "product", "خس بلدي", 1)
        vocab.invalidate()
        choices = await reader._choices(db)
        exp = await reader.match_expense(db, reader.parse_expense({"vendor": "x", "category": "سولار",
                                                                  "farm": "الشمالية", "total": 10, "items": []}), choices)
        assert exp["category_match"]["id"] == 1 and exp["farm_match"]["id"] == 1
        note = await reader.match_delivery(db, reader.parse_delivery(
            {"supplier": "الدلتا", "items": [{"description": "خس بلدي", "qty": 3}]}))
        assert note["supplier_match"]["id"] == 1 and note["lines"][0]["product"]["id"] == 1
        assert await reader._supplier_for(db, ADMIN, {"name": "الدلتا"}) == 1
        # Harvest sheet: the written local name beats the model's (wrong) suggestion.
        sheet = await reader.match_harvest(db, reader.parse_harvest({"date": "2026-10-01", "farm": "الشمالية",
            "items": [{"product": "خس بلدي", "product_match": "خس بلدي مشكل", "qty": 3}]}), choices)
        assert sheet["farm_match"]["id"] == 1
        assert sheet["lines"][0]["product"]["id"] == 1 and sheet["lines"][0]["match"] == "alias"
    scenario(go)


def test_quotations_know_a_prospect_by_its_local_name():
    async def go(db):
        await alias(db, "b2b_client", "فندق الموجة", 1)
        vocab.invalidate()
        q = SimpleNamespace(client_id=None, prospect_name="فندق الموجة", prospect_phone=None, prospect_email=None)
        with pytest.raises(HTTPException) as exc:
            await quotation_service.make_client(db, ADMIN, q)
        assert exc.value.status_code == 409 and exc.value.detail["existing"][0]["id"] == 1
    scenario(go)


def test_ask_lookups_resolve_aliases_before_filtering():
    async def go(db):
        await alias(db, "product", "خس بلدي", 1)
        await alias(db, "farm", "الشمالية", 1)
        await alias(db, "b2b_client", "فندق الموجة", 1)
        await alias(db, "employee", "عم طه", 1)
        await alias(db, "expense_category", "سولار", 1)
        vocab.invalidate()
        for tool, args, want in [
            ("products", {"search": "خس بلدي"}, {"search": "Local Lettuce"}),
            ("stock", {"product": "خس بلدي"}, {"product": "Local Lettuce"}),
            ("product_profitability", {"product": "خس بلدي"}, {"product": "Local Lettuce"}),
            ("pricing", {"product": "خس بلدي"}, {"product": "Local Lettuce"}),
            ("payroll", {"employee": "عم طه"}, {"employee": "Taha Mahmoud"}),
            ("season_cost", {"farm": "الشمالية", "product": "خس بلدي"}, {"farm": "North Farm", "product": "Local Lettuce"}),
            ("consignment_stock", {"client": "فندق الموجة"}, {"client": "Wave Hotel Sharm"}),
            ("quotations", {"client": "فندق الموجة"}, {"client": "Wave Hotel Sharm"}),
            ("expenses", {"search": "سولار"}, {"search": "Fuel & Diesel"}),
            ("profit_and_loss", {"farm": "1"}, {"farm": "1"}),             # an id: left alone
        ]:
            assert await assistant_service.resolve_aliases(db, tool, args) == want, tool
        out = json.loads(await assistant_service.run_tool(db, ADMIN, "products", json.dumps({"search": "خس بلدي"})))
        assert [p["name"] for p in out["products"]] == ["Local Lettuce"]
    scenario(go)


# ── One query per request ────────────────────────────────────────────────────

def test_the_vocabulary_is_loaded_once_per_request():
    async def go():
        engines = []
        engine, db = await _db(engines)
        try:
            await alias(db, "product", "خس بلدي", 1)
            seen = []
            event.listen(engine.sync_engine, "before_cursor_execute",
                         lambda conn, cur, stmt, *a: seen.append(stmt) if "vocabulary_entries" in stmt else None)
            vocab.invalidate()
            invoices = [{"number": f"S-{n}", "date": "2026-01-01", "customer": "Mona", "discount": 0, "paid": True,
                         "items": [{"description": "خس بلدي", "sku": None, "qty": 1, "unit_price": 30,
                                    "line_total": 30}] * 5} for n in range(4)]
            await act.match_invoices(db, invoices)
            await act.search(db, "products", "خس")
            assert len(seen) == 1
        finally:
            await db.close()
            await engine.dispose()
    run(go())


# ── Who may see and change it ────────────────────────────────────────────────

def test_viewing_is_admins_and_managers_and_managing_is_admins():
    assert "page_vocabulary" in get_role_permissions("manager")
    assert "action_vocabulary_manage" not in get_role_permissions("manager")
    for role in ("viewer", "cashier", "accountant"):
        assert "page_vocabulary" not in get_role_permissions(role), role
    assert get_role_permissions("admin") == {"*"}


def test_the_page_and_its_api_follow_the_permissions():
    from tests.test_permission_enforcement import _make_client
    viewer = SimpleNamespace(id=7, name="V", email="v@x", role="viewer", permissions="", is_active=True)
    manager = SimpleNamespace(id=8, name="M", email="m@x", role="manager", permissions="", is_active=True)
    client, _ = _make_client(viewer)
    assert client.get("/vocabulary/").status_code == 403
    assert client.get("/vocabulary/api/entries").status_code == 403
    client, _ = _make_client(manager)
    page = client.get("/vocabulary/")
    assert page.status_code == 200 and '"canManage": false' in page.text
    assert client.post("/vocabulary/api/entries", json={"type": "product", "alias": "x", "record_id": 1}).status_code == 403
    assert client.delete("/vocabulary/api/entries/1").status_code == 403


def test_the_page_script_parses():
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not installed")
    out = subprocess.run(["node", "--check", "app/static/vocabulary.js"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
