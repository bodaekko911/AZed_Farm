"""Reversing a B2B payment, in full or in part.

A payment moves four things: the invoice's amount_paid, its status, the
client's balance, and the ledger. Undoing it has to move all four back — and
the ledger leg is the one a manual database edit always misses, leaving cash
the business never received sitting in the accounts.

Reversal posts a CONTRA entry rather than deleting the original, so the
history stays truthful and the trial balance stays consistent.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.routers.b2b as b2b
from app.core.log import ActivityLog
from app.database import Base
from app.models.accounting import Account, Journal, JournalEntry
from app.models.b2b import B2BClient, B2BInvoice, B2BInvoiceItem, B2BRefund
from app.models.product import Product
from app.models.user import User


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement, params=None):
        return self.session.execute(statement, params or {})

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def refresh(self, obj):
        self.session.refresh(obj)

    async def flush(self):
        self.session.flush()

    def add(self, obj):
        self.session.add(obj)


def run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


USER = SimpleNamespace(id=1, name="Admin", role="admin")
ACCOUNTS = [("1000", "Cash", "asset"), ("1100", "AR", "asset"),
            ("2200", "Deferred Revenue", "liability"), ("4000", "Sales", "revenue")]


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        User.__table__, Product.__table__, ActivityLog.__table__,
        Account.__table__, Journal.__table__, JournalEntry.__table__,
        B2BClient.__table__, B2BInvoice.__table__, B2BInvoiceItem.__table__,
        B2BRefund.__table__,
    ])
    return sessionmaker(bind=engine, expire_on_commit=False)()


def seed(session, *, invoice_type="full_payment", total="1000", paid="0", status="unpaid"):
    session.add(User(id=1, name="Admin", email="a@x.example", password="x", role="admin"))
    for i, (code, name, kind) in enumerate(ACCOUNTS, start=1):
        session.add(Account(id=i, code=code, name=name, type=kind, balance=Decimal("0")))
    session.add(B2BClient(id=1, name="Green Cafe", is_active=True,
                          outstanding=Decimal(total), discount_pct=Decimal("0")))
    session.flush()
    session.add(B2BInvoice(id=1, invoice_number="B2B-00321", client_id=1,
                           invoice_type=invoice_type, status=status,
                           subtotal=Decimal(total), total=Decimal(total),
                           amount_paid=Decimal(paid),
                           created_at=datetime(2026, 8, 2, tzinfo=timezone.utc)))
    session.commit()


def balances(session):
    return {a.code: round(float(a.balance), 2)
            for a in session.execute(select(Account)).scalars().all()}


def pay(session, amount):
    return run(b2b.record_payment(
        1, b2b.PaymentRecord(amount=amount, method="transfer"),
        AsyncSessionAdapter(session), USER,
    ))


def reverse(session, amount=None, reason=None):
    payload = b2b.PaymentReversal(amount=amount, reason=reason)
    return run(b2b.reverse_payment(1, payload, AsyncSessionAdapter(session), USER))


# ── Full reversal ────────────────────────────────────────────────────────────

def test_full_reversal_restores_the_invoice():
    with make_session() as session:
        seed(session)
        pay(session, 1000)
        invoice = session.get(B2BInvoice, 1)
        session.refresh(invoice)
        assert invoice.status == "paid"

        result = reverse(session)
        session.refresh(invoice)

    assert result["reversed"] == 1000.0
    assert result["status"] == "unpaid"
    assert float(invoice.amount_paid) == 0.0
    assert invoice.status == "unpaid"
    assert result["balance_due"] == 1000.0


def test_full_reversal_returns_every_account_to_where_it_started():
    """The point of the feature. A manual row edit leaves the ledger holding
    cash that was never received."""
    with make_session() as session:
        seed(session)
        before = balances(session)
        pay(session, 1000)
        assert balances(session) != before

        reverse(session)
        after = balances(session)

    assert after == before


def test_the_original_payment_entry_is_kept_not_deleted():
    with make_session() as session:
        seed(session)
        pay(session, 1000)
        reverse(session, reason="Cheque bounced")
        journals = session.execute(select(Journal)).scalars().all()
        kinds = sorted(j.ref_type for j in journals)

    assert kinds == ["b2b_payment", "b2b_payment_reversal"]
    reversal = next(j for j in journals if j.ref_type == "b2b_payment_reversal")
    assert "B2B-00321" in reversal.description
    assert "Cheque bounced" in reversal.description


def test_cash_invoice_reverses_only_the_two_legs_it_posted():
    """A cash invoice never touched deferred revenue, so its reversal must not
    either — otherwise reversing invents a revenue movement."""
    with make_session() as session:
        seed(session, invoice_type="cash")
        before = balances(session)
        pay(session, 1000)
        reverse(session)
        after = balances(session)
        journal = session.execute(
            select(Journal).where(Journal.ref_type == "b2b_collection_reversal")
        ).scalar_one()
        legs = session.execute(
            select(JournalEntry).where(JournalEntry.journal_id == journal.id)
        ).scalars().all()

    assert after == before
    assert len(legs) == 2


# ── Partial reversal ─────────────────────────────────────────────────────────

def test_partial_reversal_leaves_the_rest_paid():
    with make_session() as session:
        seed(session)
        pay(session, 1000)
        result = reverse(session, amount=400)
        invoice = session.get(B2BInvoice, 1)
        session.refresh(invoice)

    assert result["reversed"] == 400.0
    assert result["amount_paid"] == 600.0
    assert result["balance_due"] == 400.0
    assert result["status"] == "partial"
    assert invoice.status == "partial"


def test_partial_reversals_can_be_repeated_until_nothing_is_left():
    with make_session() as session:
        seed(session)
        pay(session, 1000)
        reverse(session, amount=400)
        reverse(session, amount=600)
        invoice = session.get(B2BInvoice, 1)
        session.refresh(invoice)
        after = balances(session)

    assert float(invoice.amount_paid) == 0.0
    assert invoice.status == "unpaid"
    assert after == {"1000": 0.0, "1100": 0.0, "2200": 0.0, "4000": 0.0}


def test_reversing_the_whole_balance_of_a_partial_payment():
    with make_session() as session:
        seed(session)
        pay(session, 300)                 # partial payment
        result = reverse(session)         # no amount → reverse it all
        invoice = session.get(B2BInvoice, 1)
        session.refresh(invoice)

    assert result["reversed"] == 300.0
    assert invoice.status == "unpaid"


# ── Guards ───────────────────────────────────────────────────────────────────

def test_cannot_reverse_more_than_was_paid():
    with make_session() as session:
        seed(session)
        pay(session, 300)
        with pytest.raises(HTTPException) as exc:
            reverse(session, amount=500)

    assert exc.value.status_code == 400
    assert "300.00" in exc.value.detail


def test_cannot_reverse_an_invoice_with_no_payment():
    with make_session() as session:
        seed(session)
        with pytest.raises(HTTPException) as exc:
            reverse(session)

    assert exc.value.status_code == 400
    assert "no payment" in exc.value.detail


def test_zero_and_negative_amounts_are_rejected():
    with make_session() as session:
        seed(session)
        pay(session, 300)
        for bad in (0, -50):
            with pytest.raises(HTTPException) as exc:
                reverse(session, amount=bad)
            assert exc.value.status_code == 400


def test_unknown_invoice_is_a_404():
    with make_session() as session:
        seed(session)
        with pytest.raises(HTTPException) as exc:
            run(b2b.reverse_payment(999, b2b.PaymentReversal(),
                                    AsyncSessionAdapter(session), USER))

    assert exc.value.status_code == 404


# ── Knock-on effects ─────────────────────────────────────────────────────────

def test_client_balance_goes_back_up():
    with make_session() as session:
        seed(session)
        pay(session, 1000)
        rows = run(b2b.get_clients(q="", db=AsyncSessionAdapter(session)))
        assert next(r["outstanding"] for r in rows if r["id"] == 1) == 0.0

        result = reverse(session)
        rows = run(b2b.get_clients(q="", db=AsyncSessionAdapter(session)))

    assert next(r["outstanding"] for r in rows if r["id"] == 1) == 1000.0
    assert result["client_outstanding"] == 1000.0


def test_reversal_is_written_to_the_audit_log():
    with make_session() as session:
        seed(session)
        pay(session, 1000)
        reverse(session, amount=250, reason="Wrong invoice")
        entries = session.execute(
            select(ActivityLog).where(ActivityLog.action == "reverse_payment")
        ).scalars().all()

    assert len(entries) == 1
    assert "B2B-00321" in entries[0].description
    assert "Wrong invoice" in entries[0].description


def test_a_non_payment_status_is_preserved_when_the_payment_is_undone():
    """An imported invoice can carry a status that is not a payment state — a
    'consignment' one with a payment already recorded against it. Undoing that
    payment should return it to that status, not flatten it to 'unpaid'."""
    with make_session() as session:
        seed(session, invoice_type="consignment", status="consignment", paid="400")
        result = reverse(session)
        invoice = session.get(B2BInvoice, 1)
        session.refresh(invoice)

    assert result["reversed"] == 400.0
    assert invoice.status == "consignment"
    assert float(invoice.amount_paid) == 0.0


def test_a_paid_status_goes_back_to_unpaid():
    with make_session() as session:
        seed(session)
        pay(session, 1000)
        reverse(session)
        invoice = session.get(B2BInvoice, 1)
        session.refresh(invoice)

    assert invoice.status == "unpaid"
