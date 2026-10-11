"""Ask — photos and scans of paper documents: expense receipts (phase 1), supplier delivery notes (phase 2) and
handwritten harvest sheets (phase 3).

A real async SQLite session runs the app's own expense code; the vision model is an httpx MockTransport, so nothing
calls a real model. What is protected:

* the model only drafts — nothing is written until a reviewed receipt is recorded;
* every number comes from the image: unreadable values stay empty (never guessed), Arabic-Indic digits are read;
* a receipt whose lines don't add up to its printed total cannot be recorded;
* the same receipt entered twice is caught, and can still be recorded on purpose;
* permissions are checked on the server for reading, recording and viewing the kept image;
* the image kept as evidence is exactly the one the model read, linked to every expense made from it.
"""
import asyncio
import base64
import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.core.config import settings
from app.core.log import ActivityLog
from app.database import Base
from app.models.accounting import Account, Journal
from app.models.document import DocumentFile, DocumentLink
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.product import Product
from app.models.receipt import ProductReceipt
from app.models.supplier import Supplier
from app.models.user import User
from app.routers import assistant as ask_router
from app.routers import documents as doc_router
from app.services import document_reader as reader
from app.services.expense_service import list_expenses
from app.services.receive_service import list_receipts


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_API_KEY", "test-key")
    monkeypatch.setattr(settings, "ASSISTANT_MODEL", "test-model")
    monkeypatch.setattr(settings, "ASSISTANT_VISION_MODEL", "vision-model")
    monkeypatch.setattr(settings, "ASSISTANT_BASE_URL", "https://assistant.test/v1")
    monkeypatch.setattr(settings, "ASSISTANT_DAILY_LIMIT", 10)
    from app.services import assistant_service
    monkeypatch.setattr(assistant_service, "RETRY_DELAYS", (0, 0))     # no real waiting between retries in tests
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def admin():
    return SimpleNamespace(id=1, name="Boss", role="admin", permissions=None)


def cashier():
    """Sells at the POS; may not record expenses."""
    return SimpleNamespace(id=2, name="Till", role="cashier", permissions=None)


def image(tag: str) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0 fake jpeg " + tag.encode()).decode()


PART1, PART2 = image("top half"), image("bottom half")


async def session_with_data():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()
    db.add_all([
        User(id=1, name="Boss", email="boss@x.test", password="x", role="admin"),
        User(id=2, name="Till", email="t@x.test", password="x", role="cashier"),
        Account(code="1000", name="Cash", type="asset", balance=0),
        Account(code="5500", name="Other Expenses", type="expense", balance=0),
        Account(code="5300", name="Fuel", type="expense", balance=0),
        ExpenseCategory(id=1, name="Fuel & Diesel", account_code="5300", is_active="1"),
        ExpenseCategory(id=2, name="Seeds", account_code="5500", is_active="1"),
        ExpenseCategory(id=3, name="Old category", account_code="5500", is_active="0"),
        Farm(id=1, name="North Farm"),
        Farm(id=2, name="South Farm"),
        ExpenseCategory(id=4, name="Products", account_code="5500", is_active="1"),
        Supplier(id=1, name="Al Baraka Farms", phone="01001234567", balance=0),
        Product(id=1, sku="TOM", name="Tomatoes", unit="kg", price=Decimal("20"), cost=Decimal("10"),
                stock=Decimal("5"), is_active=True),
        Product(id=2, sku="HON", name="Raw Honey (1g)", unit="gram", price=Decimal("0.6"), cost=Decimal("0.3"),
                stock=Decimal("0"), is_active=True),
        Product(id=3, sku="BAS", name="Basil", unit="gram", price=Decimal("1"), cost=Decimal("0.2"),
                stock=Decimal("0"), is_active=True),
    ])
    await db.commit()
    return engine, db


def scenario(fn):
    async def go():
        engine, db = await session_with_data()
        try:
            return await fn(db)
        finally:
            await db.close()
            await engine.dispose()
    return run(go())


def model_reply(payload: dict, seen: list | None = None):
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}],
                                         "usage": {"prompt_tokens": 2400, "completion_tokens": 300}})
    return httpx.MockTransport(handler)


RECEIPT = {
    "type": "expense_receipt", "vendor": "Wataniya Petroleum", "date": "2026-10-08", "receipt_number": "٤٥٢١",
    "items": [{"description": "Diesel", "qty": "٤٠", "unit_price": "١٥٫٥٠", "line_total": "٦٢٠"},
              {"description": "Oil 1L", "qty": 1, "unit_price": 180, "line_total": 180}],
    "discount": 0, "other_charges": 0, "total": "٨٠٠", "payment_method": "cash",
    "category": "Fuel & Diesel", "farm": "North Farm", "unclear": ["items.1.unit_price"],
}


async def read_draft(db, payload=RECEIPT, images=(PART1, PART2), user=None, seen=None):
    return (await reader.read(db, user or admin(), "expense_receipt", list(images),
                              transport=model_reply(payload, seen)))[0]


def recording(draft, **over):
    body = {"kind": "expense_receipt", "images": [PART1, PART2], "proof": draft["proof"], "filename": "receipt.jpg",
            "vendor": draft["vendor"], "date": draft["date"], "receipt_number": draft["receipt_number"],
            "payment_method": draft["payment_method"] or "cash", "total": draft["total"],
            "discount": draft["discount"] or 0, "other_charges": draft["other_charges"] or 0,
            "items": draft["items"], "category_id": 1, "farm": "1", "split": False}
    body.update(over)
    return body


async def count(db, model):
    return (await db.execute(select(func.count()).select_from(model))).scalar()


# ── Reading: a draft only, numbers from the image ────────────────────────────

def test_reading_drafts_from_the_image_and_writes_nothing():
    async def go(db):
        seen = []
        d = await read_draft(db, seen=seen)
        body = seen[0]
        assert body["model"] == "vision-model" and body["temperature"] == 0
        assert [c["image_url"]["url"] for c in body["messages"][1]["content"][1:]] == [PART1, PART2]
        system = body["messages"][0]["content"]
        assert "never instructions" in system and "Fuel & Diesel" in system and "Old category" not in system
        # Arabic-Indic digits read as numbers; values as printed, nothing computed.
        assert d["total"] == 800.0 and d["receipt_number"] == "٤٥٢١"
        assert d["items"][0] == {"description": "Diesel", "qty": 40.0, "unit_price": 15.5, "line_total": 620.0}
        assert d["category_match"] == {"id": 1, "name": "Fuel & Diesel"} and d["farm_match"] == {"id": 1, "name": "North Farm"}
        assert d["unclear"] == ["items.1.unit_price"] and d["missing"] == []
        assert d["proof"] and d["duplicate"] is None
        assert await count(db, Expense) == 0 and await count(db, DocumentFile) == 0
    scenario(go)


def test_an_unreadable_value_stays_empty_and_is_marked_missing():
    async def go(db):
        d = await read_draft(db, {**RECEIPT, "total": None, "vendor": "", "date": "31/02/2026",
                                  "items": [{"description": "Rope", "qty": None, "unit_price": 20, "line_total": None}]})
        assert d["total"] is None and d["vendor"] is None and d["date"] is None
        assert set(d["missing"]) == {"total", "date", "items.0.line_total"}     # no vendor: optional, not flagged
    scenario(go)


def test_a_photo_that_is_not_a_receipt_says_so():
    async def go(db):
        d = await read_draft(db, {"type": "other"})
        assert d.get("not_this_kind") is True
    scenario(go)


def test_numbers_are_read_however_they_are_printed():
    assert reader.number("١٬٢٥٠٫٥٠") == 1250.5
    assert reader.number("1,250.50 EGP") == 1250.5
    assert reader.number("12,50") == 12.5
    assert reader.number("٣٠٠ جنيه") == 300.0
    assert reader.number("about forty") is None and reader.number(None) is None and reader.number(True) is None


# ── Recording ────────────────────────────────────────────────────────────────

def test_a_reviewed_receipt_becomes_one_expense_with_its_images_kept():
    async def go(db):
        d = await read_draft(db)
        out = await reader.record_expense(db, admin(), recording(d))
        [exp] = (await db.execute(select(Expense))).scalars().all()
        assert out["expenses"][0]["ref_number"] == exp.ref_number
        assert (float(exp.amount), exp.category_id, exp.farm_id, exp.vendor, str(exp.expense_date)) == \
            (800.0, 1, 1, "Wataniya Petroleum", "2026-10-08")
        assert "Diesel" in exp.description and "Receipt ٤٥٢١" in exp.description
        assert exp.journal_id and await count(db, Journal) == 1
        files = (await db.execute(select(DocumentFile).order_by(DocumentFile.page_no))).scalars().all()
        assert [f.page_no for f in files] == [1, 2] and files[0].data.startswith(b"\xff\xd8")
        assert {f.doc_key for f in files} == {out["doc_key"]}
        [link] = (await db.execute(select(DocumentLink))).scalars().all()
        assert (link.ref_type, link.ref_id, link.doc_key) == ("expense", exp.id, out["doc_key"])
        rows = await list_expenses(db)
        assert rows[0]["has_document"] is True
    scenario(go)


def test_lines_that_dont_add_up_to_the_printed_total_cannot_be_recorded():
    async def go(db):
        d = await read_draft(db)
        with pytest.raises(HTTPException) as bad:
            await reader.record_expense(db, admin(), recording(d, total=850))
        assert bad.value.status_code == 400 and "printed total is 850.00" in bad.value.detail
        assert await count(db, Expense) == 0 and await count(db, DocumentFile) == 0
        # A discount and extra charges printed on the receipt are part of the sum.
        await reader.record_expense(db, admin(), recording(d, total=830, discount=20, other_charges=50))
        assert await count(db, Expense) == 1
    scenario(go)


def test_a_receipt_split_by_category_makes_one_expense_each_linked_to_one_document():
    async def go(db):
        d = await read_draft(db)
        out = await reader.record_expense(db, admin(), recording(d, split=True, category_id=None, allocations=[
            {"category_id": 1, "amount": 620, "lines": [0]}, {"category_id": 2, "amount": 180, "lines": [1]}]))
        exps = (await db.execute(select(Expense).order_by(Expense.id))).scalars().all()
        assert [(e.category_id, float(e.amount)) for e in exps] == [(1, 620.0), (2, 180.0)]
        assert "Diesel" in exps[0].description and "Oil" not in exps[0].description
        assert await count(db, DocumentFile) == 2                       # the images are kept once …
        links = (await db.execute(select(DocumentLink))).scalars().all()
        assert sorted(l.ref_id for l in links) == [e.id for e in exps]  # … and linked to both expenses
        assert {l.doc_key for l in links} == {out["doc_key"]}
    scenario(go)


def test_a_split_must_add_up_to_the_total_and_saves_nothing_otherwise():
    async def go(db):
        d = await read_draft(db)
        with pytest.raises(HTTPException) as bad:
            await reader.record_expense(db, admin(), recording(d, split=True, allocations=[
                {"category_id": 1, "amount": 600, "lines": [0]}, {"category_id": 2, "amount": 180, "lines": [1]}]))
        assert "adds up to 780.00" in bad.value.detail
        assert await count(db, Expense) == 0 and await count(db, Journal) == 0
    scenario(go)


def test_a_category_must_be_active_and_a_date_not_in_the_future():
    async def go(db):
        d = await read_draft(db)
        with pytest.raises(HTTPException) as cat:
            await reader.record_expense(db, admin(), recording(d, category_id=3))
        assert "category" in cat.value.detail
        tomorrow = (date.today() + timedelta(days=2)).isoformat()
        with pytest.raises(HTTPException) as future:
            await reader.record_expense(db, admin(), recording(d, date=tomorrow))
        assert "future" in future.value.detail
    scenario(go)


def test_the_same_receipt_twice_is_warned_about_and_can_be_recorded_anyway():
    async def go(db):
        d = await read_draft(db)
        await reader.record_expense(db, admin(), recording(d))
        again = await read_draft(db)
        assert "already recorded" in again["duplicate"]
        with pytest.raises(HTTPException) as dup:
            await reader.record_expense(db, admin(), recording(again))
        assert dup.value.status_code == 409
        await reader.record_expense(db, admin(), recording(again, force=True))
        assert await count(db, Expense) == 2
    scenario(go)


def test_the_kept_images_must_be_the_ones_that_were_read():
    async def go(db):
        d = await read_draft(db)
        with pytest.raises(HTTPException) as swapped:
            await reader.record_expense(db, admin(), recording(d, images=[PART1, image("a different receipt")]))
        assert "aren't the ones that were read" in swapped.value.detail
        with pytest.raises(HTTPException) as other_user:   # a proof is bound to the user who read it
            await reader.record_expense(db, SimpleNamespace(id=9, name="X", role="admin", permissions=None), recording(d))
        assert other_user.value.status_code == 400
        assert await count(db, Expense) == 0
    scenario(go)


# ── Permissions, the daily limit and the activity log ────────────────────────

def test_a_user_without_expense_rights_can_neither_read_nor_record_receipts():
    async def go(db):
        assert reader.allowed_kinds(cashier()) == []
        assert reader.allowed_kinds(admin()) == ["expense_receipt", "delivery_note", "harvest_sheet"]
        with pytest.raises(HTTPException) as read:
            await read_draft(db, user=cashier())
        assert read.value.status_code == 403
        d = await read_draft(db)
        with pytest.raises(HTTPException) as rec:
            await reader.record_expense(db, cashier(), recording(d))
        assert rec.value.status_code == 403
    scenario(go)


class _Req:
    def __init__(self, body: dict):
        self._raw = json.dumps(body).encode()
        self.headers = {"content-length": str(len(self._raw))}

    async def stream(self):
        yield self._raw


def test_a_read_counts_as_a_question_and_is_logged_with_tokens(monkeypatch):
    async def go(db):
        async def fake_vision(system, images, transport=None):
            return RECEIPT, {"prompt_tokens": 2400, "completion_tokens": 300}
        monkeypatch.setattr(reader, "_vision", fake_vision)
        out = await ask_router.read_document(_Req({"kind": "expense_receipt", "images": [PART1], "filename": "r.jpg"}),
                                             db=db, user=admin())
        assert out["document"]["total"] == 800.0 and out["questions_left"] == 9
        [entry] = (await db.execute(select(ActivityLog).where(ActivityLog.action == "ask"))).scalars().all()
        assert "Document (expense_receipt)" in entry.description and "tokens 2400+300" in entry.description
        monkeypatch.setattr(settings, "ASSISTANT_DAILY_LIMIT", 1)
        with pytest.raises(HTTPException) as limit:
            await ask_router.read_document(_Req({"kind": "expense_receipt", "images": [PART1]}), db=db, user=admin())
        assert limit.value.status_code == 429
    scenario(go)


# ── The kept image: only through the logged-in route, by page permission ────

def test_the_receipt_image_is_served_only_to_those_who_may_see_expenses():
    async def go(db):
        d = await read_draft(db)
        out = await reader.record_expense(db, admin(), recording(d))
        f = (await db.execute(select(DocumentFile).order_by(DocumentFile.page_no))).scalars().first()
        resp = await doc_router.document_file(f.id, db=db, user=admin())
        assert resp.media_type == "image/jpeg" and resp.body == f.data
        assert resp.headers["cache-control"].startswith("private")
        with pytest.raises(HTTPException) as hidden:
            await doc_router.document_file(f.id, db=db, user=cashier())
        assert hidden.value.status_code == 404
        page = (await doc_router.document_view("expense", out["expenses"][0]["id"], db=db, user=admin())).body.decode()
        assert page.count("/documents/file/") == 2 and "SHA-256" in page
        with pytest.raises(HTTPException):
            await doc_router.document_view("expense", out["expenses"][0]["id"], db=db, user=cashier())
    scenario(go)


# ── The AI service failing now and then ──────────────────────────────────────

def flaky(responses: list, calls: list):
    """Answers in turn with each (status, payload) given, recording every call."""
    def handler(request: httpx.Request):
        calls.append(1)
        status, payload = responses[min(len(calls), len(responses)) - 1]
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "An unexpected error occurred.", "type": "api_error"}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}]})
    return httpx.MockTransport(handler)


def test_a_passing_error_of_the_ai_service_is_retried():
    """The provider answers 500 on about one request in four and the same request then goes through."""
    async def go(db):
        calls = []
        d, _usage = await reader.read(db, admin(), "expense_receipt", [PART1],
                                      transport=flaky([(500, None), (200, RECEIPT)], calls))
        assert len(calls) == 2 and d["total"] == 800.0
    scenario(go)


def test_it_gives_up_after_three_tries_with_an_honest_message():
    async def go(db):
        calls = []
        with pytest.raises(HTTPException) as down:
            await reader.read(db, admin(), "expense_receipt", [PART1], transport=flaky([(500, None)], calls))
        assert len(calls) == 3 and "temporary error" in down.value.detail
        assert "VISION_MODEL" not in down.value.detail          # not the model's fault: don't send the admin hunting
    scenario(go)


def test_a_refusal_is_not_retried():
    async def go(db):
        calls = []
        with pytest.raises(HTTPException) as refused:
            await reader.read(db, admin(), "expense_receipt", [PART1], transport=flaky([(400, None)], calls))
        assert len(calls) == 1 and "ASSISTANT_VISION_MODEL" in refused.value.detail
    scenario(go)


def test_a_receipt_without_a_vendor_can_be_recorded():
    async def go(db):
        d = await read_draft(db, {**RECEIPT, "vendor": None})
        out = await reader.record_expense(db, admin(), recording(d, vendor=""))
        [exp] = (await db.execute(select(Expense))).scalars().all()
        assert exp.vendor is None and out["expenses"][0]["amount"] == 800.0
    scenario(go)



# ══ Phase 2: supplier delivery notes ═════════════════════════════════════════

def receiver():
    """May receive stock; may not add suppliers or record expenses."""
    return SimpleNamespace(id=3, name="Store", role="viewer",
                           permissions="page_receive_products,action_receive_products_create")


NOTE = {
    "type": "delivery_note", "supplier": "Baraka Farm", "supplier_phone": "+20 100 123 4567", "date": "2026-10-09",
    "document_number": "DN-٧٧",
    "items": [{"description": "Tomatoes", "sku": "TOM", "qty": "٢٥", "unit": "kg", "unit_cost": "١٢", "line_total": "٣٠٠"},
              {"description": "Raw honey 500g", "sku": None, "qty": 4, "unit": "jar", "unit_cost": 150, "line_total": 600}],
    "discount": 0, "other_charges": 0, "total": "٩٠٠", "amount_paid": 400, "paid_in_full": False,
    "unclear": ["items.1.unit_cost"],
}


async def read_note(db, payload=NOTE, user=None, kind="delivery_note"):
    return (await reader.read(db, user or admin(), kind, [PART1], transport=model_reply(payload)))[0]


def receiving(draft, **over):
    """What the review card sends: quantities and costs in each product's own unit (4 jars of 500 g = 2000 g)."""
    body = {"kind": "delivery_note", "images": [PART1], "proof": draft["proof"], "filename": "note.jpg",
            "date": draft["date"], "document_number": draft["document_number"], "supplier_id": 1,
            "items": [{"product_id": 1, "qty": 25, "unit_cost": 12}, {"product_id": 2, "qty": 2000, "unit_cost": 0.3}],
            "total": 900, "discount": 0, "other_charges": 0, "amount_paid": 400, "expense_category_id": 4,
            "farm": "1", "location_id": None}
    body.update(over)
    return body


def test_a_delivery_note_is_read_and_matched_and_nothing_is_written():
    async def go(db):
        d = await read_note(db)
        assert d["kind"] == "delivery_note" and d["total"] == 900.0 and d["document_number"] == "DN-٧٧"
        assert d["supplier_match"] == {"id": 1, "name": "Al Baraka Farms", "phone": "01001234567"}   # by phone
        tom, honey = d["lines"]
        assert tom["product"]["id"] == 1 and tom["match"] == "sku" and tom["qty"] == 25.0
        assert honey["product"]["id"] == 2 and honey["pack"] == {"amount": 500.0, "base": "g"}         # 4 × 500 g
        assert d["unclear"] == ["items.1.unit_cost"] and d["missing"] == [] and d["duplicate"] is None
        assert d["locations"] is not None and d["categories"]
        assert await count(db, ProductReceipt) == 0
    scenario(go)


def test_a_reviewed_note_is_received_like_the_receive_page_with_its_image_kept():
    async def go(db):
        d = await read_note(db)
        out = await reader.record_delivery(db, admin(), receiving(d))
        assert out["count"] == 2 and out["total_cost"] == 900.0 and out["total_paid"] == 400.0
        assert out["total_unpaid"] == 500.0
        tom, honey = await db.get(Product, 1), await db.get(Product, 2)
        await db.refresh(tom), await db.refresh(honey)
        assert float(tom.stock) == 30.0 and float(honey.stock) == 2000.0
        assert round(float(tom.cost), 3) == round((5 * 10 + 25 * 12) / 30, 3)        # blended, as on the Receive page
        supplier = await db.get(Supplier, 1)
        await db.refresh(supplier)
        assert float(supplier.balance) == 500.0                                        # what is still owed
        receipts = (await db.execute(select(ProductReceipt).order_by(ProductReceipt.id))).scalars().all()
        assert {r.supplier_ref for r in receipts} == {"DN-٧٧"} and all(r.supplier_id == 1 for r in receipts)
        links = (await db.execute(select(DocumentLink))).scalars().all()
        assert sorted(l.ref_id for l in links) == [r.id for r in receipts]
        assert {l.ref_type for l in links} == {"product_receipt"} and await count(db, DocumentFile) == 1
        history = await list_receipts(db)
        assert all(row["has_document"] for row in history["items"])
    scenario(go)


def test_a_discount_on_the_note_is_spread_over_the_line_costs():
    async def go(db):
        d = await read_note(db)
        await reader.record_delivery(db, admin(), receiving(d, total=810, discount=90, amount_paid=810))
        receipts = (await db.execute(select(ProductReceipt).order_by(ProductReceipt.id))).scalars().all()
        assert [float(r.total_cost) for r in receipts] == [270.0, 540.0]               # 10% off each line
        assert sum(float(r.total_cost) for r in receipts) == 810.0
    scenario(go)


def test_lines_that_dont_add_up_to_the_total_receive_nothing():
    async def go(db):
        d = await read_note(db)
        with pytest.raises(HTTPException) as bad:
            await reader.record_delivery(db, admin(), receiving(d, total=950))
        assert bad.value.status_code == 400 and "printed total is 950.00" in bad.value.detail
        assert await count(db, ProductReceipt) == 0 and await count(db, DocumentFile) == 0
        tom = await db.get(Product, 1)
        await db.refresh(tom)
        assert float(tom.stock) == 5.0
    scenario(go)


def test_a_note_without_prices_receives_quantities_only():
    async def go(db):
        d = await read_note(db, {**NOTE, "total": None, "amount_paid": None,
                                 "items": [{**NOTE["items"][0], "unit_cost": None, "line_total": None}]})
        assert d["missing"] == []                         # no prices printed: nothing to flag
        await reader.record_delivery(db, admin(), receiving(d, items=[{"product_id": 1, "qty": 25, "unit_cost": None}],
                                                           total=None, amount_paid=None, expense_category_id=None))
        [r] = (await db.execute(select(ProductReceipt))).scalars().all()
        assert float(r.qty) == 25.0 and r.total_cost is None and r.expense_id is None
    scenario(go)


def test_an_unpaid_amount_needs_a_supplier():
    async def go(db):
        d = await read_note(db)
        with pytest.raises(HTTPException) as bad:
            await reader.record_delivery(db, admin(), receiving(d, supplier_id=None))
        assert "Pick the supplier" in bad.value.detail
    scenario(go)


def test_a_new_supplier_is_added_with_the_receive_only_by_someone_allowed_to():
    async def go(db):
        d = await read_note(db, {**NOTE, "supplier": "Delta Packaging", "supplier_phone": "0122 555 0199"})
        assert d["supplier_match"] is None and d["new_supplier"] == {"name": "Delta Packaging", "phone": "0122 555 0199"}
        with pytest.raises(HTTPException) as denied:
            await reader.record_delivery(db, receiver(), receiving(
                {**d, "proof": reader.proof_for(receiver(), "delivery_note", [__import__("hashlib").sha256(
                    reader.image_bytes([PART1])[0]).hexdigest()])},
                supplier_id=None, new_supplier=d["new_supplier"], expense_category_id=4))
        assert denied.value.status_code == 403
        assert await count(db, Supplier) == 1 and await count(db, ProductReceipt) == 0
        await reader.record_delivery(db, admin(), receiving(d, supplier_id=None, new_supplier=d["new_supplier"]))
        new = (await db.execute(select(Supplier).where(Supplier.name == "Delta Packaging"))).scalar_one()
        await db.refresh(new)
        assert float(new.balance) == 500.0
    scenario(go)


def test_the_same_note_twice_is_warned_about_and_can_be_received_anyway():
    async def go(db):
        d = await read_note(db)
        await reader.record_delivery(db, admin(), receiving(d))
        again = await read_note(db)
        assert "already received" in again["duplicate"]
        with pytest.raises(HTTPException) as dup:
            await reader.record_delivery(db, admin(), receiving(again))
        assert dup.value.status_code == 409
        await reader.record_delivery(db, admin(), receiving(again, force=True))
        assert await count(db, ProductReceipt) == 4
    scenario(go)


def test_a_cost_far_from_the_usual_one_is_checked_like_on_the_receive_page():
    async def go(db):
        d = await read_note(db)
        # 1 200 per kg: a per-box price typed as per-kg — the Receive page's own check stops it.
        body = receiving(d, items=[{"product_id": 1, "qty": 25, "unit_cost": 1200}], total=30000, amount_paid=0)
        with pytest.raises(HTTPException) as check:
            await reader.record_delivery(db, admin(), body)
        assert check.value.status_code == 409 and check.value.detail["code"] == "cost_check"
        assert await count(db, ProductReceipt) == 0 and await count(db, DocumentFile) == 0
        await reader.record_delivery(db, admin(), {**body, "confirm_cost": True})
        assert await count(db, ProductReceipt) == 1
    scenario(go)


def test_not_sure_lets_the_model_say_which_kind_it_is():
    async def go(db):
        seen = []
        d, _u = await reader.read(db, admin(), "auto", [PART1], transport=model_reply(NOTE, seen))
        assert d["kind"] == "delivery_note" and d["supplier_match"]["id"] == 1
        system = seen[0]["messages"][0]["content"]
        assert '"expense_receipt"' in system and '"delivery_note"' in system
        other = (await reader.read(db, admin(), "auto", [PART1], transport=model_reply({"type": "other"})))[0]
        assert other["not_this_kind"] is True
    scenario(go)


def test_delivery_notes_follow_the_receive_permissions():
    async def go(db):
        assert reader.allowed_kinds(receiver()) == ["delivery_note"]
        assert reader.allowed_kinds(cashier()) == []
        with pytest.raises(HTTPException) as read:
            await read_note(db, user=cashier())
        assert read.value.status_code == 403
        with pytest.raises(HTTPException) as search:
            await reader.search(db, cashier(), "suppliers", "bar")
        assert search.value.status_code == 403
        assert [x["id"] for x in await reader.search(db, receiver(), "suppliers", "bar")] == [1]
        assert [x["id"] for x in await reader.search(db, receiver(), "products", "honey")] == [2]
        # The kept note is shown to whoever may open the Receive page, not to the cashier.
        d = await read_note(db)
        out = await reader.record_delivery(db, admin(), receiving(d))
        f = (await db.execute(select(DocumentFile))).scalars().first()
        assert (await doc_router.document_file(f.id, db=db, user=receiver())).body == f.data
        with pytest.raises(HTTPException):
            await doc_router.document_file(f.id, db=db, user=cashier())
        page = await doc_router.document_view("product_receipt", out["receipts"][0]["id"], db=db, user=receiver())
        assert "/documents/file/" in page.body.decode()
    scenario(go)



# ══ Phase 3: handwritten harvest sheets ══════════════════════════════════════

def farmhand():
    """May record farm deliveries; nothing else."""
    return SimpleNamespace(id=4, name="Field", role="viewer", permissions="page_farm,action_farm_delivery_create")


SHEET = {
    "type": "harvest_sheet", "date": "2026-10-10", "farm": "المزرعة الشمالية", "farm_match": "North Farm",
    "items": [{"farm": None, "farm_match": None, "product": "طماطم", "product_match": "Tomatoes", "qty": "١٢٠", "unit": "كيلو"},
              {"farm": "القبلية", "farm_match": "South Farm", "product": "ريحان", "product_match": "Basil", "qty": "٢", "unit": "kg"}],
    "received_by": "عم سيد", "notes": None, "unclear": ["items.1.qty"],
}


async def read_sheet(db, payload=SHEET, user=None, seen=None):
    return (await reader.read(db, user or admin(), "harvest_sheet", [PART1], transport=model_reply(payload, seen)))[0]


def recording_sheet(draft, **over):
    """What the review card sends: 2 kg of basil in the product's own unit (grams)."""
    body = {"kind": "harvest_sheet", "images": [PART1], "proof": draft["proof"], "filename": "sheet.jpg",
            "date": draft["date"], "received_by": draft["received_by"], "notes": "",
            "items": [{"farm_id": 1, "product_id": 1, "qty": 120, "written": "طماطم · ١٢٠ · كيلو"},
                      {"farm_id": 2, "product_id": 3, "qty": 2000, "written": "القبلية · ريحان · ٢ · kg"}]}
    body.update(over)
    return body


def test_an_arabic_sheet_is_read_and_matched_to_english_records():
    async def go(db):
        seen = []
        d = await read_sheet(db, seen=seen)
        system = seen[0]["messages"][0]["content"]
        assert 'FARMS: ["North Farm", "South Farm"]' in system and '"Tomatoes"' in system and "PRODUCTS:" in system
        assert d["kind"] == "harvest_sheet" and d["date"] == "2026-10-10" and d["received_by"] == "عم سيد"
        tom, basil = d["lines"]
        assert (tom["farm_id"], tom["product"]["id"], tom["qty"], tom["match"]) == (1, 1, 120.0, "suggested")
        assert (basil["farm_id"], basil["product"]["id"], basil["qty"]) == (2, 3, 2.0)   # its own farm, not the sheet's
        assert basil["pack"] == {"amount": 1000.0, "base": "g"}                           # 2 kg → 2000 g on the card
        assert tom["pack"] == {"amount": 1000.0, "base": "g"}                             # كيلو is a kilo too
        assert d["unclear"] == ["items.1.qty"] and d["missing"] == [] and d["duplicates"] == []
        assert await count(db, FarmDelivery) == 0
    scenario(go)


def test_a_suggestion_that_isnt_on_the_list_is_not_used():
    async def go(db):
        d = await read_sheet(db, {**SHEET, "farm": "مزرعة الوادي", "farm_match": "Valley Farm",
                                  "items": [{**SHEET["items"][0], "product": "مانجو", "product_match": "Mango"}]})
        [line] = d["lines"]
        assert line["farm_id"] is None and line["product"] is None            # never invented
        assert "items.0.farm" in d["missing"]
    scenario(go)


def test_a_reviewed_sheet_becomes_one_delivery_per_farm_with_its_image_kept():
    async def go(db):
        from app.routers.farm import get_deliveries
        d = await read_sheet(db)
        out = await reader.record_harvest(db, admin(), recording_sheet(d))
        assert [(x["farm"], x["lines"]) for x in out["deliveries"]] == [("North Farm", 1), ("South Farm", 1)]
        deliveries = (await db.execute(select(FarmDelivery).order_by(FarmDelivery.id))).scalars().all()
        assert [(x.farm_id, str(x.delivery_date), x.received_by) for x in deliveries] == \
            [(1, "2026-10-10", "عم سيد"), (2, "2026-10-10", "عم سيد")]
        items = (await db.execute(select(FarmDeliveryItem).order_by(FarmDeliveryItem.id))).scalars().all()
        assert [(i.product_id, float(i.qty), i.unit) for i in items] == [(1, 120.0, "kg"), (3, 2000.0, "gram")]
        assert items[0].notes == "طماطم · ١٢٠ · كيلو"                         # what was written stays with the line
        tom, basil = await db.get(Product, 1), await db.get(Product, 3)
        await db.refresh(tom), await db.refresh(basil)
        assert float(tom.stock) == 125.0 and float(basil.stock) == 2000.0
        links = (await db.execute(select(DocumentLink))).scalars().all()
        assert sorted(l.ref_id for l in links) == [x.id for x in deliveries]
        assert {l.ref_type for l in links} == {"farm_delivery"} and await count(db, DocumentFile) == 1
        listed = await get_deliveries(db=db)
        assert all(row["has_document"] for row in listed["deliveries"])
    scenario(go)


def test_every_line_needs_a_farm_a_product_and_a_quantity():
    async def go(db):
        d = await read_sheet(db)
        for bad, message in (({"farm_id": None}, "pick the farm"), ({"product_id": None}, "pick the product"),
                             ({"qty": 0}, "above 0")):
            items = [{**recording_sheet(d)["items"][0], **bad}]
            with pytest.raises(HTTPException) as err:
                await reader.record_harvest(db, admin(), recording_sheet(d, items=items))
            assert message in err.value.detail
        assert await count(db, FarmDelivery) == 0 and await count(db, DocumentFile) == 0
    scenario(go)


def test_a_sheet_already_delivered_is_warned_about_and_can_be_recorded_anyway():
    async def go(db):
        d = await read_sheet(db)
        await reader.record_harvest(db, admin(), recording_sheet(d))
        again = await read_sheet(db)
        assert len(again["duplicates"]) == 2 and "North Farm already has 120 kg of Tomatoes" in again["duplicates"][0]
        with pytest.raises(HTTPException) as dup:
            await reader.record_harvest(db, admin(), recording_sheet(again))
        assert dup.value.status_code == 409
        await reader.record_harvest(db, admin(), recording_sheet(again, force=True))
        assert await count(db, FarmDelivery) == 4
    scenario(go)


def test_harvest_sheets_follow_the_farm_delivery_permissions():
    async def go(db):
        assert reader.allowed_kinds(farmhand()) == ["harvest_sheet"]
        assert "harvest_sheet" not in reader.allowed_kinds(receiver())
        with pytest.raises(HTTPException) as read:
            await read_sheet(db, user=receiver())
        assert read.value.status_code == 403
        # A farm hand may search products for the card, but not suppliers.
        assert [x["id"] for x in await reader.search(db, farmhand(), "products", "basil")] == [3]
        with pytest.raises(HTTPException):
            await reader.search(db, farmhand(), "suppliers", "bar")
        d = await read_sheet(db, user=farmhand())
        out = await reader.record_harvest(db, farmhand(), recording_sheet(d))
        f = (await db.execute(select(DocumentFile))).scalars().first()
        assert (await doc_router.document_file(f.id, db=db, user=farmhand())).body == f.data
        with pytest.raises(HTTPException):
            await doc_router.document_file(f.id, db=db, user=receiver())
        with pytest.raises(HTTPException):
            await reader.record_harvest(db, receiver(), recording_sheet(d))
        assert out["deliveries"][0]["delivery_number"].startswith("FD-")
    scenario(go)


def test_not_sure_can_also_find_a_harvest_sheet():
    async def go(db):
        seen = []
        d, _u = await reader.read(db, admin(), "auto", [PART1], transport=model_reply(SHEET, seen))
        assert d["kind"] == "harvest_sheet" and len(d["lines"]) == 2
        assert '"harvest_sheet"' in seen[0]["messages"][0]["content"]
    scenario(go)
