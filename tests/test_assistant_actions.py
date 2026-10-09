"""Ask actions: proposed in chat, done only on Confirm — and sales invoices recorded from a PDF.

A real async SQLite session is used, so the app's own code paths (expense entry, attendance,
stock adjustment, pos_service.create_invoice) run for real. The model is an httpx MockTransport.
"""

import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.core.config import settings
from app.core.log import ActivityLog
from app.database import Base
from app.models.accounting import Account, Journal
from app.models.customer import Customer
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm
from app.models.hr import Attendance, Employee
from app.models.inventory import StockMove
from app.models.invoice import Invoice
from app.models.product import Product
from app.models.user import User
from app.services import assistant_actions as act
from app.services import assistant_service


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_API_KEY", "test-key")
    monkeypatch.setattr(settings, "ASSISTANT_MODEL", "test-model")
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
    return SimpleNamespace(id=2, name="Cashier", role="cashier", permissions=None)


async def session_with_data():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()
    db.add_all([
        User(id=1, name="Boss", email="boss@x.test", password="x", role="admin"),
        User(id=2, name="Cashier", email="c@x.test", password="x", role="cashier"),
        Account(code="1000", name="Cash", type="asset", balance=0),
        Account(code="1100", name="Accounts Receivable", type="asset", balance=0),
        Account(code="4000", name="Sales Revenue", type="revenue", balance=0),
        Account(code="5500", name="Other Expenses", type="expense", balance=0),
        ExpenseCategory(id=1, name="Fuel & Diesel", account_code="5500", is_active="1"),
        ExpenseCategory(id=2, name="Seeds", account_code="5500", is_active="1"),
        Farm(id=1, name="Farm 2"),
        Employee(id=1, name="Taha Mahmoud", base_salary=Decimal("6000"), is_active=True,
                 hire_date=date(2025, 1, 1), attendance_auto_status="absent"),
        Product(id=1, sku="BAS", name="Organic Italian Basil", unit="gram", price=Decimal("0.8"),
                cost=Decimal("0.2"), stock=Decimal("1500"), is_active=True),
        Product(id=2, sku="HON", name="Raw Honey 500g", unit="piece", price=Decimal("150"),
                cost=Decimal("90"), stock=Decimal("10"), is_active=True),
        Customer(id=1, name="Walk-in Customer"),
        Customer(id=2, name="Mona Adel", phone="0100"),
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


# ── Actions: propose → confirm ───────────────────────────────────────────────

def test_an_expense_is_only_proposed_then_added_on_confirm_once():
    async def go(db):
        text, card = await act.propose(db, admin(), "propose_expense", json.dumps(
            {"amount": 500, "category": "diesel", "farm": "farm 2", "vendor": "Wataniya"}))
        assert card and card["kind"] == "expense" and "NOT done" in json.loads(text)["status"]
        assert dict(card["lines"])["Category"] == "Fuel & Diesel"
        assert (await db.execute(select(Expense))).scalars().all() == []          # nothing written yet

        out = await act.execute(db, admin(), card["token"])
        assert out["ok"] and "500.00" in out["message"]
        expense = (await db.execute(select(Expense))).scalar_one()
        assert (float(expense.amount), expense.farm_id, expense.vendor) == (500.0, 1, "Wataniya")

        with pytest.raises(HTTPException) as again:                                # one use only
            await act.execute(db, admin(), card["token"])
        assert again.value.status_code == 409
    scenario(go)


def test_a_confirmation_is_bound_to_its_user_unforgeable_and_expires(monkeypatch):
    async def go(db):
        _t, card = await act.propose(db, admin(), "propose_expense", json.dumps({"amount": 20, "category": "seeds"}))
        with pytest.raises(HTTPException) as other:
            await act.execute(db, SimpleNamespace(id=99, name="X", role="admin", permissions=None), card["token"])
        assert other.value.status_code == 403
        raw, sig = card["token"].split(".")
        with pytest.raises(HTTPException) as forged:
            await act.execute(db, admin(), raw[:-2] + "AA." + sig)
        assert forged.value.status_code == 400
        monkeypatch.setattr(act.time, "time", lambda: time.time_ns() / 1e9 + act.TOKEN_TTL + 5)
        with pytest.raises(HTTPException) as expired:
            await act.execute(db, admin(), card["token"])
        assert expired.value.status_code == 410
    scenario(go)


def test_actions_follow_permissions_and_ambiguity_is_asked_back():
    async def go(db):
        text, card = await act.propose(db, cashier(), "propose_expense", json.dumps({"amount": 5, "category": "seeds"}))
        assert card is None and "permission" in json.loads(text)["error"]
        assert "propose_expense" not in assistant_service.allowed_actions(cashier())
        text, card = await act.propose(db, admin(), "propose_expense", json.dumps({"amount": 5, "category": "s"}))
        assert card is None and "error" in json.loads(text)
    scenario(go)


def test_attendance_for_a_range_is_proposed_then_logged():
    async def go(db):
        db.add(Attendance(employee_id=1, date=date(2025, 9, 4), status="absent"))
        await db.commit()
        _t, card = await act.propose(db, admin(), "propose_attendance", json.dumps(
            {"employee": "taha", "date_from": "2025-09-01", "date_to": "2025-09-30", "status": "present"}))
        lines = dict(card["lines"])
        assert lines["Employee"] == "Taha Mahmoud" and "30 days" in lines["Days"]
        assert lines["Changes"].startswith("1 day") and "Auto mode" in lines["Note"]
        out = await act.execute(db, admin(), card["token"])
        assert "30 days marked present" in out["message"]
        rows = (await db.execute(select(Attendance).where(Attendance.employee_id == 1))).scalars().all()
        assert len(rows) == 30 and all(r.status == "present" for r in rows)
    scenario(go)


def test_stock_is_set_to_a_counted_level_on_confirm():
    async def go(db):
        _t, card = await act.propose(db, admin(), "propose_stock_adjustment", json.dumps(
            {"product": "basil", "set_to": 1200, "note": "count"}))
        assert dict(card["lines"])["Change"] == "-300 gram"
        out = await act.execute(db, admin(), card["token"])
        assert "1200" in out["message"]
        product = (await db.execute(select(Product).where(Product.id == 1))).scalar_one()
        await db.refresh(product)
        assert float(product.stock) == 1200
    scenario(go)


def test_the_assistant_returns_a_proposal_card_and_writes_nothing():
    def handler(request):
        body = json.loads(request.content)
        if not [m for m in body["messages"] if m["role"] == "tool"]:
            assert any(t["function"]["name"] == "propose_expense" for t in body["tools"])
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "propose_expense", "arguments": json.dumps({"amount": 500, "category": "diesel"})}}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                                                  "content": "Once you confirm, I'll add it."}}]})

    async def go(db):
        result = await assistant_service.ask(db, admin(), "add 500 diesel", transport=httpx.MockTransport(handler))
        assert len(result["proposals"]) == 1 and result["proposals"][0]["kind"] == "expense"
        assert (await db.execute(select(Expense))).scalars().all() == []
    scenario(go)


# ── PDF invoices ─────────────────────────────────────────────────────────────

PDF_JSON = {"invoices": [
    {"number": "1043", "date": "2025-09-14", "customer": "mona adel",
     "items": [{"description": "Honey 500 gm", "sku": None, "qty": 2, "unit_price": 150, "line_total": 300},
               {"description": "Italian basil", "sku": "BAS", "qty": 100, "unit_price": 0.8, "line_total": 80}],
     "discount": 0, "total": 380, "paid": True},
    {"number": "1044", "date": "14/09/2025", "customer": "Cash",
     "items": [{"description": "Rw hony", "qty": 1, "unit_price": 140, "line_total": 140}],
     "discount": 0, "total": 140, "paid": None},
]}


def model_reading(seen):
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                              "content": "```json\n" + json.dumps(PDF_JSON) + "\n```"}}],
                              "usage": {"prompt_tokens": 3000, "completion_tokens": 400}})
    return httpx.MockTransport(handler)


def test_pdf_pages_are_read_by_the_model_and_matched_to_records():
    async def go(db):
        seen = []
        page = "data:image/jpeg;base64,/9j/AAAA"
        invoices, usage = await act.read_invoices([page, page], transport=model_reading(seen))
        parts = seen[0]["messages"][1]["content"]
        assert [p["type"] for p in parts] == ["text", "image_url", "image_url"] and usage["prompt_tokens"] == 3000
        assert invoices[1]["date"] is None            # not ISO → left for the user to set

        matched = await act.match_invoices(db, invoices)
        first, second = matched
        assert first["customer_match"]["name"] == "Mona Adel" and not first["walk_in"]
        # A printed SKU is a sure match; without one the closest product by name is always picked, and a
        # loose match is marked so the review card says "check it".
        assert first["lines"][1]["product"]["sku"] == "BAS" and first["lines"][1]["match"] == "sku"
        # "Honey 500 gm" vs "Raw Honey 500g": sizes aside, the names agree.
        assert first["lines"][0]["product"]["sku"] == "HON" and first["lines"][0]["match"] == "name"
        assert second["walk_in"] and second["customer_match"] is None
        assert second["lines"][0]["product"]["sku"] == "HON"             # "Rw hony" → closest by name
        assert second["lines"][0]["match"] == "closest"
    scenario(go)


def test_an_invoice_is_recorded_as_a_pos_sale_dated_to_the_pdf_only_when_totals_match():
    async def go(db):
        base = {"number": "1043", "date": "2025-09-14", "customer_id": 2, "paid": True, "discount": 0,
                "filename": "sept.pdf", "items": [{"product_id": 2, "qty": 2, "unit_price": 150},
                                                  {"product_id": 1, "qty": 100, "unit_price": 0.8}]}
        with pytest.raises(HTTPException) as off:
            await act.record_invoice(db, admin(), dict(base, pdf_total=390))
        assert off.value.status_code == 400 and "doesn't match" in off.value.detail

        out = await act.record_invoice(db, admin(), dict(base, pdf_total=380))
        assert out["total"] == 380.0 and out["date"] == "2025-09-14"
        inv = (await db.execute(select(Invoice))).scalar_one()
        assert inv.customer_id == 2 and inv.status == "paid" and "PDF invoice 1043" in inv.notes
        assert str(inv.created_at)[:10] == "2025-09-14"
        honey = (await db.execute(select(Product).where(Product.id == 2))).scalar_one()
        await db.refresh(honey)
        assert float(honey.stock) == 8                                   # stock deducted like a POS sale
        moves = (await db.execute(select(StockMove).where(StockMove.ref_id == inv.id))).scalars().all()
        assert moves and all(str(m.created_at)[:10] == "2025-09-14" for m in moves)
        journal = (await db.execute(select(Journal).where(Journal.description == f"Sale - {inv.invoice_number}"))).scalar_one()
        assert str(journal.created_at)[:10] == "2025-09-14"

        with pytest.raises(HTTPException) as dup:
            await act.record_invoice(db, admin(), dict(base, pdf_total=380))
        assert dup.value.status_code == 409
    scenario(go)


def test_pos_rules_still_apply_to_pdf_invoices():
    async def go(db):
        # A named customer can't be charged a non-catalogue price (create_invoice refuses it).
        with pytest.raises(HTTPException) as price:
            await act.record_invoice(db, admin(), {"number": "1044", "date": "2025-09-14", "customer_id": 2,
                                                   "pdf_total": 140, "items": [{"product_id": 2, "qty": 1, "unit_price": 140}]})
        assert price.value.status_code == 400
        # …but as Walk-in it is allowed.
        out = await act.record_invoice(db, admin(), {"number": "1044", "date": "2025-09-14", "customer_id": None,
                                                     "pdf_total": 140, "items": [{"product_id": 2, "qty": 1, "unit_price": 140}]})
        assert out["total"] == 140.0
        # Not enough stock → refused, nothing recorded.
        with pytest.raises(HTTPException) as stock:
            await act.record_invoice(db, admin(), {"number": "1045", "date": "2025-09-14", "pdf_total": 1500,
                                                   "items": [{"product_id": 2, "qty": 10, "unit_price": 150}]})
        assert stock.value.status_code == 400
        assert len((await db.execute(select(Invoice))).scalars().all()) == 1
        # A discount needs the discount permission, as at the POS checkout.
        no_discount = SimpleNamespace(id=2, name="Cashier", role="cashier", permissions="-action_pos_discount")
        with pytest.raises(HTTPException) as perm:
            await act.record_invoice(db, no_discount, {"number": "1046", "date": "2025-09-14", "pdf_total": 135,
                                                     "discount": 15, "items": [{"product_id": 2, "qty": 1, "unit_price": 150}]})
        assert perm.value.status_code == 403
        # No POS sale permission at all → refused.
        with pytest.raises(HTTPException) as none:
            await act.record_invoice(db, SimpleNamespace(id=3, name="HR", role="hr", permissions=None),
                                     {"pdf_total": 150, "items": [{"product_id": 2, "qty": 1, "unit_price": 150}]})
        assert none.value.status_code == 403
    scenario(go)


def test_a_discount_on_the_pdf_is_applied_so_the_total_matches():
    total, pct = act.invoice_total([{"qty": 3, "unit_price": 150}], 45)
    assert total == Decimal("405.00") and round(pct, 6) == 10.0


def test_shop_packs_are_matched_and_recorded_in_azed_units():
    """The online shop sells "tomato (500g)" packs; Azed keeps "Tomato (1g)" in grams."""
    async def go(db):
        db.add(Product(id=3, sku="TOM", name="Tomato (1g)", unit="gram", price=Decimal("0.02"),
                       cost=Decimal("0.01"), stock=Decimal("5000"), is_active=True))
        await db.commit()
        [inv] = await act.match_invoices(db, [{"number": "A-77", "date": "2025-09-20", "customer": None,
            "items": [{"description": "tomato (500g)", "sku": None, "qty": 2, "unit_price": 10, "line_total": 20}],
            "discount": 0, "total": 20, "paid": True}])
        line = inv["lines"][0]
        assert line["product"]["sku"] == "TOM" and line["match"] == "name"        # the size doesn't spoil the match
        assert line["pack"] == {"amount": 500.0, "base": "g"} and line["product"]["pack"] == {"amount": 1.0, "base": "g"}

        # The page converts 2 × 500 g at 10.00 → 1000 g at 0.02; a price a hair off is snapped to the catalogue.
        out = await act.record_invoice(db, admin(), {"number": "A-77", "date": "2025-09-20", "customer_id": 2,
                                                     "pdf_total": 20, "items": [{"product_id": 3, "qty": 1000,
                                                                                 "unit_price": 0.0199999}]})
        assert out["total"] == 20.0
        tomato = (await db.execute(select(Product).where(Product.id == 3))).scalar_one()
        await db.refresh(tomato)
        assert float(tomato.stock) == 4000                                          # 1000 g out of stock
    scenario(go)


@pytest.mark.parametrize("text, pack", [
    ("tomato (500g)", {"amount": 500.0, "base": "g"}), ("طماطم ٥٠٠ جم", {"amount": 500.0, "base": "g"}),
    ("Honey 1.5 kg", {"amount": 1500.0, "base": "g"}), ("Cheese (0,5kg)", {"amount": 500.0, "base": "g"}),
    ("Milk 1L", {"amount": 1000.0, "base": "ml"}), ("زيت ١ لتر", {"amount": 1000.0, "base": "ml"}),
    ("Eggs x30", None), ("Pack of 6 lemons", None),
], ids=["g", "arabic-g", "kg", "comma-kg", "litre", "arabic-litre", "no-size", "count"])
def test_pack_sizes_are_read_from_names(text, pack):
    assert act.pack_of(text) == pack


def test_a_customer_the_invoice_names_is_found_by_phone_or_offered_as_new():
    async def go(db):
        found, new = await act.match_invoices(db, [
            {"number": "1", "date": "2025-09-20", "customer": "Mona A.", "customer_phone": "+20 100",
             "items": [], "discount": 0, "total": 0, "paid": True},
            {"number": "2", "date": "2025-09-20", "customer": "Karim Hassan", "customer_phone": "0122 333 4444",
             "customer_email": "k@x.test", "customer_address": "Zamalek", "items": [], "discount": 0, "total": 0,
             "paid": True},
        ])
        assert found["customer_match"]["name"] == "Mona Adel" and found["new_customer"] is None
        assert new["customer_match"] is None and not new["walk_in"]
        assert new["new_customer"] == {"name": "Karim Hassan", "phone": "0122 333 4444", "email": "k@x.test",
                                       "address": "Zamalek"}
    scenario(go)


def test_a_new_customer_is_created_with_the_sale_and_reused_next_time():
    async def go(db):
        sale = {"date": "2025-09-20", "pdf_total": 150, "items": [{"product_id": 2, "qty": 1, "unit_price": 150}],
                "new_customer": {"name": "Karim Hassan", "phone": "0122 333 4444", "email": "k@x.test",
                                 "address": "Zamalek"}}
        out = await act.record_invoice(db, admin(), dict(sale, number="2"))
        karim = (await db.execute(select(Customer).where(Customer.name == "Karim Hassan"))).scalar_one()
        assert (karim.phone, karim.email, karim.address) == ("0122 333 4444", "k@x.test", "Zamalek")
        assert out["customer_id"] == karim.id
        inv = (await db.execute(select(Invoice))).scalar_one()
        assert inv.customer_id == karim.id

        # Same phone written differently on the next invoice → the same customer, no duplicate.
        again = dict(sale, number="3", new_customer={"name": "K. Hassan", "phone": "+201223334444"})
        out2 = await act.record_invoice(db, admin(), again)
        assert out2["customer_id"] == karim.id
        assert len((await db.execute(select(Customer).where(Customer.phone.is_not(None)))).scalars().all()) == 2
    scenario(go)


def test_no_customer_is_left_behind_when_the_sale_fails_or_isnt_allowed():
    async def go(db):
        failing = {"number": "9", "date": "2025-09-20", "pdf_total": 15000, "new_customer": {"name": "Ghost Buyer"},
                   "items": [{"product_id": 2, "qty": 100, "unit_price": 150}]}           # only 10 honey in stock
        with pytest.raises(HTTPException):
            await act.record_invoice(db, admin(), failing)
        assert (await db.execute(select(Customer).where(Customer.name == "Ghost Buyer"))).scalar_one_or_none() is None

        cashier_sale = {"number": "10", "date": "2025-09-20", "pdf_total": 150, "new_customer": {"name": "New One"},
                        "items": [{"product_id": 2, "qty": 1, "unit_price": 150}]}
        with pytest.raises(HTTPException) as perm:                                         # cashiers can't add customers
            await act.record_invoice(db, cashier(), cashier_sale)
        assert perm.value.status_code == 403
        assert (await db.execute(select(Invoice))).scalars().all() == []
    scenario(go)


def test_phone_numbers_compare_in_any_format():
    assert act.phone_key("01001234567") == act.phone_key("+20 100 123 4567") == act.phone_key("00201001234567")
    assert act.phone_key("٠١٠٠١٢٣٤٥٦٧") == act.phone_key("01001234567")
    assert act.phone_key("123") == ""


async def add_delivery_items(db):
    db.add_all([Product(id=10, sku="DEL-SH", name="Sharm Delivery", unit="trip", price=Decimal("50"), stock=0,
                        item_type="service", is_active=True),
                Product(id=11, sku="DEL-DH", name="Dahab Delivery", unit="trip", price=Decimal("120"), stock=0,
                        item_type="service", is_active=True),
                Product(id=12, sku="DHB", name="Dahab Honey 250g", unit="piece", price=Decimal("90"), stock=5,
                        is_active=True)])
    await db.commit()


def invoice(**extra):
    base = {"number": "S-1", "date": "2025-09-20", "customer": None, "discount": 0, "paid": True,
            "items": [{"description": "Raw honey 500g", "sku": None, "qty": 1, "unit_price": 150, "line_total": 150}]}
    return dict(base, **extra)


def test_shipping_to_sharm_or_dahab_picks_that_delivery_item():
    async def go(db):
        await add_delivery_items(db)
        sharm, dahab, arabic, unknown, as_item = await act.match_invoices(db, [
            invoice(shipping=50, shipping_label="Shipping", customer_address="Hadaba, Sharm El Sheikh", total=200),
            invoice(shipping=120, delivery_area="Dahab", total=270),
            invoice(shipping=50, customer_address="شرم الشيخ - نبق", total=200),
            invoice(shipping=40, customer_address="Somewhere else", total=190),
            invoice(items=[{"description": "Dahab honey", "qty": 1, "unit_price": 90, "line_total": 90},
                           {"description": "توصيل دهب", "qty": 1, "unit_price": 120, "line_total": 120}],
                    shipping=120, total=210),
        ])
        last = lambda inv: inv["lines"][-1]
        assert last(sharm)["product"]["sku"] == "DEL-SH" and last(sharm)["match"] == "delivery"
        assert (last(sharm)["qty"], last(sharm)["unit_price"]) == (1, 50)
        assert last(dahab)["product"]["sku"] == "DEL-DH"
        assert last(arabic)["product"]["sku"] == "DEL-SH"                   # شرم → Sharm
        assert last(unknown)["product"] is None and last(unknown)["delivery"]
        assert {c["sku"] for c in last(unknown)["candidates"]} == {"DEL-SH", "DEL-DH"}   # pick from delivery items
        # Shipping printed as an item line: one delivery line, not two; honey stays honey.
        assert len(as_item["lines"]) == 2
        assert as_item["lines"][0]["product"]["sku"] == "DHB" and as_item["lines"][1]["product"]["sku"] == "DEL-DH"
    scenario(go)


def test_an_invoice_with_sharm_delivery_records_the_delivery_item():
    async def go(db):
        await add_delivery_items(db)
        out = await act.record_invoice(db, admin(), {"number": "S-9", "date": "2025-09-20", "pdf_total": 200,
                                                     "customer_id": 2, "items": [
                                                         {"product_id": 2, "qty": 1, "unit_price": 150},
                                                         {"product_id": 10, "qty": 1, "unit_price": 50}]})
        assert out["total"] == 200.0
        from app.models.invoice import InvoiceItem
        names = [i.name for i in (await db.execute(select(InvoiceItem))).scalars().all()]
        assert names == ["Raw Honey 500g", "Sharm Delivery"]
    scenario(go)


def test_a_price_change_is_proposed_then_applied_once_and_refused_if_the_price_moved():
    async def go(db):
        _t, card = await act.propose(db, admin(), "propose_price_change", json.dumps({"product": "honey", "new_price": 165}))
        lines = dict(card["lines"])
        assert lines["Price now"].startswith("150") and lines["Change"] == "+10.0%" and "45.5%" in lines["Margin at new price"]
        out = await act.execute(db, admin(), card["token"])
        assert "165" in out["message"]
        honey = (await db.execute(select(Product).where(Product.id == 2))).scalar_one()
        await db.refresh(honey)
        assert float(honey.price) == 165
        # A suggestion made against an older price is refused rather than overwriting a newer change.
        _t, stale = await act.propose(db, admin(), "propose_price_change", json.dumps({"product": "honey", "new_price": 170}))
        honey.price = Decimal("168")
        await db.commit()
        with pytest.raises(HTTPException) as moved:
            await act.execute(db, admin(), stale["token"])
        assert moved.value.status_code == 409
        # Cashiers can't change prices.
        text, none = await act.propose(db, cashier(), "propose_price_change", json.dumps({"product": "honey", "new_price": 1}))
        assert none is None and "permission" in json.loads(text)["error"]
    scenario(go)
