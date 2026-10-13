"""B2B quotations (phase 1): pricing, totals and estimates, the margin guard, statuses and expiry, the printed and
e-mailed copies, and permissions. A real async SQLite database; e-mail is mocked.
"""
import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.models  # noqa: F401  every table
from app.core.log import ActivityLog
from app.core.time_utils import today_local
from app.database import Base
from app.models.b2b import B2BClient, B2BInvoice
from app.models.product import Product
from app.models.quotation import B2BQuotation
from app.models.user import User
from app.routers import b2b_quotations as qr
from app.services import mail_service
from app.services import quotation_service as qs

TODAY = today_local()


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def admin():
    return SimpleNamespace(id=1, name="Boss", role="admin", permissions=None)


def manager():
    return SimpleNamespace(id=2, name="Mona", role="manager", permissions=None)


async def seeded():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()
    db.add_all([
        User(id=1, name="Boss", email="boss@farm.test", password="x", role="admin"),
        User(id=2, name="Mona", email="mona@farm.test", password="x", role="manager"),
        B2BClient(id=1, name="Blue Lagoon Lodge", discount_pct=Decimal("10"), payment_terms="net30",
                  email="orders@bluelagoon.test", outstanding=Decimal("0")),
        Product(id=1, sku="VEG", name="Mixed vegetables", unit="kg", price=Decimal("20"), cost=Decimal("8"),
                stock=Decimal("100"), is_active=True),
        Product(id=2, sku="HON", name="Raw Honey", unit="jar", price=Decimal("150"), cost=Decimal("90"),
                stock=Decimal("40"), is_active=True),
        Product(id=3, sku="JAM", name="Fig Jam", unit="jar", price=Decimal("60"), cost=Decimal("0"),
                stock=Decimal("10"), is_active=True),
        Product(id=4, sku="OLD", name="Old Product", unit="kg", price=Decimal("10"), cost=Decimal("5"),
                stock=Decimal("0"), is_active=False),
    ])
    await db.commit()
    return engine, db


def scenario(fn):
    async def go():
        engine, db = await seeded()
        try:
            return await fn(db)
        finally:
            await db.close()
            await engine.dispose()
    return run(go())


def weekly_offer(**over):
    """20 kg mixed vegetables weekly (5% line discount) and 10 jars of honey once, delivered Fridays in Dahab."""
    body = {"client_id": 1, "delivery_area": "Dahab", "delivery_day": "Friday", "delivery_fee": 100,
            "lines": [{"product_id": 1, "qty": 20, "line_discount_pct": 5, "frequency": "weekly", "note": "seasonal mix"},
                      {"product_id": 2, "qty": 10, "frequency": "once"}]}
    body.update(over)
    return body


# ── Pricing and totals ───────────────────────────────────────────────────────

def test_the_clients_discount_sets_the_price_and_the_line_discount_goes_on_top():
    async def go(db):
        q, checks = await qs.save(db, admin(), weekly_offer())
        q = await qs.load(db, q.id)
        veg, honey = q.items
        assert (veg.unit_price, honey.unit_price) == (Decimal("18.00"), Decimal("135.00"))      # catalogue − 10%
        assert veg.line_total == Decimal("342.00")                                               # 20 × 18 × 0.95
        assert checks[0]["effective_discount_pct"] == 14.5                                      # 1 − 17.10/20
        assert q.number == "QUO-00001" and q.status == "draft" and q.payment_terms == "net30"    # from the client
        assert q.valid_until == TODAY + timedelta(days=14)
        assert q.terms and "Egyptian pounds" in q.terms
    scenario(go)


def test_totals_discounts_delivery_and_weekly_and_monthly_estimates():
    async def go(db):
        q, _checks = await qs.save(db, admin(), weekly_offer())
        out = qs.serialize(await qs.load(db, q.id), Decimal("30"), for_staff=True)
        t = out["totals"]
        assert t["subtotal"] == 360 + 1350 and t["discount_total"] == 18.0                     # 360 − 342
        assert t["delivery_fee"] == 100 and t["total"] == 342 + 1350 + 100
        assert (t["weekly_produce"], t["weekly_delivery"], t["weekly_value"]) == (342.0, 100.0, 442.0)
        assert t["monthly_value"] == round(442 * 52 / 12, 2) and t["monthly_delivery"] == round(100 * 52 / 12, 2)
        twice = await qs.save(db, admin(), weekly_offer(lines=[{"product_id": 1, "qty": 10, "frequency": "twice_weekly"}]))
        t2 = qs.serialize(await qs.load(db, twice[0].id), Decimal("30"), for_staff=True)["totals"]
        assert (t2["weekly_produce"], t2["weekly_delivery"]) == (360.0, 200.0)                  # two deliveries a week
    scenario(go)


def test_a_prospect_pays_the_catalogue_price():
    async def go(db):
        q, _ = await qs.save(db, admin(), {"prospect_name": "Sharm Reef Hotel", "prospect_email": "chef@reef.test",
                                           "prospect_area": "Sharm El Sheikh",
                                           "lines": [{"product_id": 1, "qty": 5}]})
        q = await qs.load(db, q.id)
        assert q.client_id is None and q.items[0].unit_price == Decimal("20.00")
        assert qs.recipient_name(q) == "Sharm Reef Hotel" and qs.recipient_email(q) == "chef@reef.test"
        with pytest.raises(HTTPException):
            await qs.save(db, admin(), {"lines": [{"product_id": 1, "qty": 5}]})                 # nobody to quote
    scenario(go)


# ── The margin guard ─────────────────────────────────────────────────────────

def test_below_target_warns_no_cost_is_flagged_inactive_is_refused():
    async def go(db):
        _q, checks = await qs.save(db, admin(), weekly_offer(lines=[
            {"product_id": 2, "qty": 1, "unit_price": 120},           # (120 − 90) / 120 = 25%: under 30%
            {"product_id": 3, "qty": 1}]))
        honey, jam = checks
        assert honey["below_target"] and not honey["below_cost"] and honey["margin_pct"] == 25.0
        assert jam["no_cost"] and jam["cost"] is None
        with pytest.raises(HTTPException) as e:
            await qs.save(db, admin(), weekly_offer(lines=[{"product_id": 4, "qty": 1}]))
        assert "inactive" in e.value.detail
    scenario(go)


def test_a_price_below_cost_blocks_saving_unless_approved_by_someone_allowed():
    async def go(db):
        cheap = weekly_offer(lines=[{"product_id": 1, "qty": 10, "unit_price": 9, "line_discount_pct": 20}])  # 7.20 < 8
        with pytest.raises(HTTPException) as e:
            await qs.save(db, admin(), cheap)
        assert e.value.status_code == 400 and e.value.detail["below_cost"][0]["product"] == "Mixed vegetables"
        with pytest.raises(HTTPException) as e:
            await qs.save(db, manager(), {**cheap, "approve_below_cost": True})              # managers can't approve
        assert e.value.status_code == 403
        assert (await db.execute(select(B2BQuotation.id))).first() is None
        q, _ = await qs.save(db, admin(), {**cheap, "approve_below_cost": True})
        assert q.below_cost_approved_by_id == 1
        logged = (await db.execute(select(ActivityLog.description, ActivityLog.user_name)
                                   .where(ActivityLog.action == "quotation_below_cost"))).first()
        assert "approved by Boss" in logged[0] and logged[1] == "Boss"
    scenario(go)


# ── Statuses, expiry and renewal ─────────────────────────────────────────────

def test_status_changes_record_who_and_when_and_rejecting_needs_a_reason():
    async def go(db):
        q, _ = await qs.save(db, admin(), weekly_offer())
        q = await qs.set_status(db, manager(), await qs.load(db, q.id), "sent")
        assert q.status == "sent" and q.sent_at
        with pytest.raises(HTTPException):
            await qs.set_status(db, manager(), await qs.load(db, q.id), "rejected", "  ")
        q = await qs.set_status(db, manager(), await qs.load(db, q.id), "rejected", "Too expensive")
        assert (q.status, q.reject_reason, q.decided_by_id) == ("rejected", "Too expensive", 2) and q.decided_at
        with pytest.raises(HTTPException) as e:
            await qs.save(db, admin(), weekly_offer(), await qs.load(db, q.id))               # only drafts are edited
        assert e.value.status_code == 409
    scenario(go)


def test_a_quotation_expires_on_its_own_and_must_be_renewed_against_todays_costs():
    async def go(db):
        q, _ = await qs.save(db, admin(), weekly_offer())
        qid = q.id                                    # a refused renewal rolls back, expiring loaded objects
        await db.execute(update(B2BQuotation).values(valid_until=TODAY - timedelta(days=1), status="sent"))
        await db.commit()
        listed = await qr.list_quotations(status=None, q=None, date_from=None, date_to=None, db=db)
        assert listed["quotations"][0]["status"] == "expired"
        expired = await qs.load(db, qid)
        assert expired.total > 0 and len(expired.items) == 2                                # nothing else changed
        with pytest.raises(HTTPException) as e:
            await qs.set_status(db, admin(), expired, "accepted")
        assert e.value.status_code == 409 and "Renew" in e.value.detail
        # Costs went up: the vegetables now cost more than they were offered for — renewing is blocked.
        await db.execute(update(Product).where(Product.id == 1).values(cost=Decimal("17.50")))
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await qs.renew(db, manager(), await qs.load(db, qid), (TODAY + timedelta(days=10)).isoformat())
        assert e.value.detail["below_cost"][0]["product"] == "Mixed vegetables"
        assert (await qs.load(db, qid)).status == "expired"
        renewed, checks = await qs.renew(db, admin(), await qs.load(db, qid), (TODAY + timedelta(days=10)).isoformat(),
                                         approve_below_cost=True)
        assert renewed.status == "draft" and renewed.valid_until == TODAY + timedelta(days=10)
        assert (await qs.load(db, qid)).items[0].unit_cost == Decimal("17.500")
    scenario(go)


def test_duplicate_makes_a_new_draft():
    async def go(db):
        q, _ = await qs.save(db, admin(), weekly_offer())
        await qs.set_status(db, admin(), await qs.load(db, q.id), "sent")
        copy, _ = await qs.duplicate(db, admin(), await qs.load(db, q.id))
        copy = await qs.load(db, copy.id)
        assert copy.number == "QUO-00002" and copy.status == "draft" and len(copy.items) == 2
    scenario(go)


# ── The printed and e-mailed copy: never cost or margin ──────────────────────

def _request():
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})


def test_the_printed_copy_has_no_cost_or_margin_in_either_language():
    async def go(db):
        q, _ = await qs.save(db, admin(), weekly_offer(delivery_fee=0))
        html = (await qr.print_quotation(q.id, _request(), db=db)).body.decode()
        assert "QUO-00001" in html and "Blue Lagoon Lodge" in html and "Mixed vegetables" in html
        assert "Delivery included" in html and "Estimated regular supply" in html
        for leak in (">8.00<", ">90.00<", "Margin", "margin_pct", "Cost", "unit_cost"):     # costs are 8 and 90
            assert leak not in html, leak
        ar = await qs.save(db, admin(), weekly_offer(language="ar"))
        html_ar = (await qr.print_quotation(ar[0].id, _request(), db=db)).body.decode()
        assert "direction: rtl" in html_ar and "عرض سعر" in html_ar and "رسوم التوصيل" in html_ar
        assert "90.00" not in html_ar
    scenario(go)


def test_the_printed_wording_is_formal_with_no_dashes():
    for lang, words in qs.PRINT_TEXT.items():
        for key, text in words.items():
            assert "—" not in text and "–" not in text and " - " not in text, (lang, key)


def test_emailing_sends_the_same_content_logs_it_and_marks_it_sent(monkeypatch):
    sent = []

    async def fake_send(recipients, subject, html, text, transport=None):
        sent.append((recipients, subject, html, text))
    monkeypatch.setattr(mail_service, "send", fake_send)

    async def go(db):
        q, _ = await qs.save(db, admin(), weekly_offer())
        out = await qr.email_quotation(q.id, db=db, user=manager())
        [(to, subject, html, text)] = sent
        assert to == ["orders@bluelagoon.test"] and "QUO-00001" in subject
        assert "Mixed vegetables" in html and "1,792.00" in html
        for leak in (">90.00<", ">8.00<", "Margin", "Cost"):                  # cost 90 / 8, never shown
            assert leak not in html and leak not in text, leak
        assert out["quotation"]["status"] == "sent" and out["quotation"]["sent_to"] == "orders@bluelagoon.test"
        assert (await db.execute(select(ActivityLog.id).where(ActivityLog.action == "email_quotation"))).first()
        nomail, _ = await qs.save(db, admin(), {"prospect_name": "Walk-in Lodge", "lines": [{"product_id": 1, "qty": 1}]})
        with pytest.raises(HTTPException) as e:
            await qr.email_quotation(nomail.id, db=db, user=manager())
        assert "no e-mail" in e.value.detail and len(sent) == 1
    scenario(go)


# ── Permissions; nothing touches stock or accounting ─────────────────────────

def test_permissions_follow_the_b2b_pattern():
    from app.core.permissions import get_effective_permissions
    managers = get_effective_permissions("manager", None)
    assert {"tab_b2b_quotations", "action_b2b_quotations_create", "action_b2b_quotations_send",
            "action_b2b_quotations_convert"} <= managers
    assert "action_b2b_quotations_below_cost" not in managers
    assert "*" in get_effective_permissions("admin", None)
    deps = {r.path: {getattr(d.call, "__qualname__", "") for d in r.dependant.dependencies}
            for r in qr.router.routes if hasattr(r, "dependant")}
    assert all(deps)                                                     # every route is guarded
    assert any("require_permission" in str(d) for d in deps["/b2b/api/quotations/{quotation_id}/email"])


def test_a_quotation_never_moves_stock_or_creates_an_invoice():
    async def go(db):
        q, _ = await qs.save(db, admin(), weekly_offer())
        await qs.set_status(db, admin(), await qs.load(db, q.id), "accepted")
        veg = await db.get(Product, 1)
        await db.refresh(veg)
        assert float(veg.stock) == 100.0 and (await db.execute(select(B2BInvoice.id))).first() is None
    scenario(go)


# ══ Phase 2: convert to invoice, prospect to client ══════════════════════════

from app.core.product_types import SERVICE_ITEM_TYPE  # noqa: E402
from app.models.accounting import Account, Journal  # noqa: E402


async def accepted(db, **over):
    """An accepted quotation, with what invoicing needs: ledger accounts and a Dahab delivery item."""
    if not await db.get(Account, 1):
        db.add_all([Account(id=1, code="1100", name="Receivables", type="asset", balance=0),
                    Account(id=2, code="2200", name="Deferred revenue", type="liability", balance=0),
                    Account(id=3, code="4000", name="Sales", type="revenue", balance=0),
                    Product(id=9, sku="DLV-DAHAB", name="Dahab delivery", unit="trip", price=Decimal("100"),
                            cost=Decimal("0"), stock=Decimal("0"), is_active=True, item_type=SERVICE_ITEM_TYPE)])
        await db.commit()
    q, _ = await qs.save(db, admin(), weekly_offer(**over))
    await qs.set_status(db, admin(), await qs.load(db, q.id), "accepted")
    return q.id


def test_an_accepted_quotation_becomes_an_invoice_through_the_existing_code():
    async def go(db):
        from app.models.b2b import B2BInvoiceItem
        qid = await accepted(db)
        out = await qs.convert_to_invoice(db, manager(), await qs.load(db, qid))
        inv = (await db.execute(select(B2BInvoice))).scalar_one()
        assert inv.quotation_id == qid and out["delivery"] == "first delivery" and "QUO-00001" in inv.notes
        assert abs(float(inv.total) - 1792.0) <= 0.05
        assert out["rounding_difference"] == 0.0                              # 18.00 / 0.9 = 20.00 exactly
        lines = {i.product_id: i for i in (await db.execute(select(B2BInvoiceItem))).scalars()}
        assert set(lines) == {1, 2, 9}                                        # vegetables, honey, Dahab delivery
        veg, honey = await db.get(Product, 1), await db.get(Product, 2)
        await db.refresh(veg)
        await db.refresh(honey)
        assert (float(veg.stock), float(honey.stock)) == (80.0, 30.0)          # stock out, as with any invoice
        assert (await db.execute(select(Journal.id))).first()                   # the invoice's own journal
        client = await db.get(B2BClient, 1)
        await db.refresh(client)
        assert float(client.outstanding) == float(inv.total)
        view = await qr.get_quotation(qid, db=db)
        assert [i["invoice_number"] for i in view["invoices"]] == [inv.invoice_number]
    scenario(go)


def test_weekly_quotations_invoice_one_delivery_at_a_time():
    async def go(db):
        from app.models.b2b import B2BInvoiceItem
        qid = await accepted(db)
        await qs.convert_to_invoice(db, admin(), await qs.load(db, qid))
        second = await qs.convert_to_invoice(db, admin(), await qs.load(db, qid))
        assert second["delivery"] == "next delivery"
        items = (await db.execute(select(B2BInvoiceItem.product_id)
                                  .where(B2BInvoiceItem.invoice_id == second["invoice_id"]))).scalars().all()
        assert sorted(items) == [1, 9]                                          # the weekly vegetables and their delivery
        once = await accepted(db, lines=[{"product_id": 2, "qty": 1}])
        await qs.convert_to_invoice(db, admin(), await qs.load(db, once))
        with pytest.raises(HTTPException) as e:
            await qs.convert_to_invoice(db, admin(), await qs.load(db, once))
        assert e.value.status_code == 409
    scenario(go)


def test_converting_checks_stock_now_and_needs_an_accepted_client_quotation():
    async def go(db):
        qid = await accepted(db)
        await db.execute(update(Product).where(Product.id == 1).values(stock=Decimal("5")))
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await qs.convert_to_invoice(db, admin(), await qs.load(db, qid))
        assert "Not enough stock" in str(e.value.detail)
        assert (await db.execute(select(B2BInvoice.id))).first() is None
        draft, _ = await qs.save(db, admin(), weekly_offer())
        with pytest.raises(HTTPException) as e:
            await qs.convert_to_invoice(db, admin(), await qs.load(db, draft.id))
        assert e.value.status_code == 409
        prospect, _ = await qs.save(db, admin(), {"prospect_name": "Reef Hotel", "lines": [{"product_id": 2, "qty": 1}]})
        await qs.set_status(db, admin(), await qs.load(db, prospect.id), "accepted")
        with pytest.raises(HTTPException) as e:
            await qs.convert_to_invoice(db, admin(), await qs.load(db, prospect.id))
        assert "Make them a B2B client first" in e.value.detail
    scenario(go)


def test_a_delivery_fee_needs_a_delivery_item_or_is_left_out_on_purpose():
    async def go(db):
        qid = await accepted(db, delivery_area="Nuweiba")
        with pytest.raises(HTTPException) as e:
            await qs.convert_to_invoice(db, admin(), await qs.load(db, qid))
        assert e.value.detail["needs_delivery_item"]
        out = await qs.convert_to_invoice(db, admin(), await qs.load(db, qid), skip_delivery=True)
        assert "not invoiced" in out["note"] and abs(out["total"] - 1692.0) <= 0.05
    scenario(go)


def test_a_full_discount_client_is_invoiced_as_paid():
    async def go(db):
        db.add(B2BClient(id=5, name="Habiba Kitchen", discount_pct=Decimal("100"), payment_terms="immediate",
                         outstanding=Decimal("0")))
        await db.commit()
        # Every line nets to zero for a 100% client: below cost, so an admin approves it.
        qid = await accepted(db, client_id=5, delivery_fee=0, approve_below_cost=True)
        out = await qs.convert_to_invoice(db, admin(), await qs.load(db, qid))
        inv = await db.get(B2BInvoice, out["invoice_id"])
        assert inv.status == "paid" and float(inv.total) == 0.0
    scenario(go)


def test_a_prospect_becomes_a_client_in_one_step():
    async def go(db):
        q, _ = await qs.save(db, admin(), {"prospect_name": "Sharm Reef Hotel", "prospect_contact": "Chef Ali",
                                           "prospect_phone": "0100 111 2222", "prospect_email": "chef@reef.test",
                                           "prospect_area": "Sharm El Sheikh", "payment_terms": "net15",
                                           "lines": [{"product_id": 1, "qty": 5}]})
        clerk = SimpleNamespace(id=2, name="Mona", role="viewer", permissions="page_b2b,tab_b2b_quotations")
        with pytest.raises(HTTPException) as e:
            await qs.make_client(db, clerk, await qs.load(db, q.id))
        assert e.value.status_code == 403
        out = await qs.make_client(db, manager(), await qs.load(db, q.id))
        client = await db.get(B2BClient, out["client_id"])
        assert (client.name, client.contact_person, client.email, client.address, client.payment_terms) == \
            ("Sharm Reef Hotel", "Chef Ali", "chef@reef.test", "Sharm El Sheikh", "net15")
        loaded = await qs.load(db, q.id)
        assert loaded.client_id == client.id and qs.recipient_name(loaded) == "Sharm Reef Hotel"
        assert loaded.items[0].unit_price == Decimal("20.00")                  # prices stay as quoted
        # The same prospect again: offered the existing client instead of a duplicate.
        again, _ = await qs.save(db, admin(), {"prospect_name": "sharm reef hotel", "lines": [{"product_id": 1, "qty": 1}]})
        with pytest.raises(HTTPException) as e:
            await qs.make_client(db, manager(), await qs.load(db, again.id))
        assert e.value.status_code == 409 and e.value.detail["existing"][0]["id"] == client.id
        linked = await qs.make_client(db, manager(), await qs.load(db, again.id), link_client_id=client.id)
        assert linked["created"] is False and (await qs.load(db, again.id)).client_id == client.id
    scenario(go)


def test_converting_needs_both_the_convert_and_the_invoice_permission():
    route = next(r for r in qr.router.routes if getattr(r, "path", "") == "/b2b/api/quotations/{quotation_id}/invoice")
    needed = set()
    for dep in route.dependant.dependencies:
        for cell in (dep.call.__closure__ or ()):
            if isinstance(cell.cell_contents, str):
                needed.add(cell.cell_contents)
    assert {"action_b2b_quotations_convert", "action_b2b_invoices_create"} <= needed
