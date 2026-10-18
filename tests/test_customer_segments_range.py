"""Customer segments over a date range.

With a range, only the paid invoices and refunds inside it count, recency is measured from the range's end (the
segments as they stood then), and customers added after the range are left out. Without one, nothing changes.
A real async SQLite database; ``greatest`` (Postgres) is given to it as a plain function.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.models  # noqa: F401  every table
from app.database import Base
from app.models.customer import Customer
from app.models.invoice import Invoice
from app.models.refund import RetailRefund
from app.routers.customers import get_segments


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())   # tests that follow expect a current loop


def at(y, m, d):
    return datetime(y, m, d, 12, tzinfo=timezone.utc)


async def _db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    event.listen(engine.sync_engine, "connect", lambda conn, _r: conn.create_function("greatest", 2, max))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()
    db.add_all([
        Customer(id=1, name="Amal", created_at=at(2026, 1, 1)),
        Customer(id=2, name="Basem", created_at=at(2026, 1, 1)),
        Customer(id=3, name="Late Comer", created_at=at(2026, 9, 1)),
    ])
    n = iter(range(1, 100))
    inv = lambda cid, total, when: Invoice(invoice_number=f"INV-{next(n)}", customer_id=cid, status="paid",
                                           subtotal=Decimal(total), total=Decimal(total), created_at=when)
    db.add_all([
        inv(1, "500", at(2026, 3, 10)), inv(1, "300", at(2026, 3, 20)),     # Amal: March
        inv(2, "200", at(2026, 3, 15)), inv(2, "900", at(2026, 8, 1)),      # Basem: March and August
        inv(3, "400", at(2026, 9, 5)),                                      # added in September
        RetailRefund(refund_number="RF-1", customer_id=1, total=Decimal("100"), created_at=at(2026, 8, 5)),
    ])
    await db.commit()
    return engine, db


def _by_id(data):
    return {c["id"]: (seg, c) for seg, rows in data["segments"].items() for c in rows}


def test_a_range_counts_only_its_invoices_and_measures_recency_from_its_end():
    async def go():
        engine, db = await _db()
        try:
            data = await get_segments(date_from="2026-03-01", date_to="2026-03-31", db=db)
        finally:
            await db.close(); await engine.dispose()
        return data
    data = run(go())
    rows = _by_id(data)
    assert 3 not in rows                                            # added after the range
    seg1, amal = rows[1]
    assert (amal["net_spent"], amal["inv_count"], amal["last_purchase"]) == (800.0, 2, "2026-03-20")   # August refund not in it
    seg2, basem = rows[2]
    assert (basem["net_spent"], basem["inv_count"]) == (200.0, 1)                                      # August sale not in it
    # As of 31 March both bought within 60 days, for the first time: new.
    assert seg1 == seg2 == "new"
    assert (data["date_from"], data["date_to"], data["as_of"]) == ("2026-03-01", "2026-03-31", "2026-03-31")


def test_recency_from_the_range_end_makes_old_buyers_at_risk():
    async def go():
        engine, db = await _db()
        try:
            return await get_segments(date_from="2026-01-01", date_to="2026-07-15", db=db)
        finally:
            await db.close(); await engine.dispose()
    rows = _by_id(run(go()))
    assert rows[1][0] == "at_risk" and rows[2][0] == "at_risk"     # last bought in March, 61–180 days before 15 July


def test_no_range_is_all_time_as_before():
    async def go():
        engine, db = await _db()
        try:
            return await get_segments(db=db)
        finally:
            await db.close(); await engine.dispose()
    data = run(go())
    rows = _by_id(data)
    assert set(rows) == {1, 2, 3}
    assert rows[1][1]["net_spent"] == 700.0 and rows[2][1]["net_spent"] == 1100.0
    assert data["date_from"] is None and data["date_to"] is None


def test_bad_ranges_are_refused():
    async def go(**kw):
        engine, db = await _db()
        try:
            return await get_segments(db=db, **kw)
        finally:
            await db.close(); await engine.dispose()
    with pytest.raises(HTTPException):
        run(go(date_from="2026-05-01", date_to="2026-04-01"))
    with pytest.raises(HTTPException):
        run(go(date_from="May 1"))
