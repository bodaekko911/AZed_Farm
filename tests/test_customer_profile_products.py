"""The customer profile's "Most Bought Products" sorts by value or by quantity, so the API gives every product
bought — cutting to the top 10 by quantity hid the most valuable ones — each with its unit. A real async SQLite DB."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.models  # noqa: F401  every table
from app.database import Base
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.routers.customers import customer_profile, customer_profile_ui


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())   # tests that follow expect a current loop


def test_every_product_comes_with_its_unit_so_value_can_be_sorted_too():
    async def go():
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                     connect_args={"check_same_thread": False})
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        db = async_sessionmaker(engine, expire_on_commit=False)()
        try:
            db.add(Customer(id=1, name="Amal"))
            # 11 cheap products bought in bulk, and one expensive product bought once.
            for i in range(1, 13):
                db.add(Product(id=i, sku=f"P{i}", name=f"Product {i}", unit="kg" if i < 12 else "jar",
                               price=Decimal("1"), is_active=True))
            db.add(Invoice(id=1, invoice_number="INV-1", customer_id=1, status="paid", subtotal=Decimal("0"),
                           total=Decimal("0"), created_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
            await db.flush()
            for i in range(1, 12):
                db.add(InvoiceItem(invoice_id=1, product_id=i, sku=f"P{i}", name=f"Product {i}", qty=Decimal("100"),
                                   unit_price=Decimal("1"), total=Decimal("100")))
            db.add(InvoiceItem(invoice_id=1, product_id=12, sku="P12", name="Product 12", qty=Decimal("1"),
                               unit_price=Decimal("5000"), total=Decimal("5000")))
            await db.commit()
            return await customer_profile(1, db=db)
        finally:
            await db.close()
            await engine.dispose()
    products = run(go())["top_products"]
    assert len(products) == 12                                     # not cut to 10
    rich = max(products, key=lambda p: p["revenue"])
    assert (rich["name"], rich["unit"], rich["revenue"]) == ("Product 12", "jar", 5000.0)
    assert products[0]["unit"] == "kg"


def test_the_page_offers_both_sorts():
    html = customer_profile_ui(1)
    html = html.body.decode() if hasattr(html, "body") else html
    assert "setProductSort('value')" in html and "setProductSort('qty')" in html
