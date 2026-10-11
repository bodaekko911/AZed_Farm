"""Ask — photos and scans of paper documents, phase 1: expense receipts.

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
from app.models.farm import Farm
from app.models.user import User
from app.routers import assistant as ask_router
from app.routers import documents as doc_router
from app.services import document_reader as reader
from app.services.expense_service import list_expenses


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_API_KEY", "test-key")
    monkeypatch.setattr(settings, "ASSISTANT_MODEL", "test-model")
    monkeypatch.setattr(settings, "ASSISTANT_VISION_MODEL", "vision-model")
    monkeypatch.setattr(settings, "ASSISTANT_BASE_URL", "https://assistant.test/v1")
    monkeypatch.setattr(settings, "ASSISTANT_DAILY_LIMIT", 10)
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
        assert set(d["missing"]) == {"total", "date", "vendor", "items.0.line_total"}
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
        assert reader.allowed_kinds(cashier()) == [] and reader.allowed_kinds(admin()) == ["expense_receipt"]
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
