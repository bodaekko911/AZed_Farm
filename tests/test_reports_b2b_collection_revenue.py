import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.database import Base
from app.models.accounting import Account, Journal, JournalEntry
from app.models.b2b import B2BClient, B2BInvoice, B2BInvoiceItem, B2BRefund, B2BRefundItem
from app.models.customer import Customer
from app.models.invoice import Invoice, InvoiceItem
from app.models.product import Product
from app.models.refund import RetailRefund, RetailRefundItem
from app.models.supplier import Purchase, PurchaseItem, Supplier, SupplierPayment
from app.models.user import User
from app.routers.reports import _build_sales_report


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement, params=None):
        return self.session.execute(statement, params or {})


def run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            Customer.__table__,
            Product.__table__,
            Invoice.__table__,
            InvoiceItem.__table__,
            RetailRefund.__table__,
            RetailRefundItem.__table__,
            Supplier.__table__,
            Purchase.__table__,
            PurchaseItem.__table__,
            SupplierPayment.__table__,
            B2BClient.__table__,
            B2BInvoice.__table__,
            B2BInvoiceItem.__table__,
            B2BRefund.__table__,
            B2BRefundItem.__table__,
            Account.__table__,
            Journal.__table__,
            JournalEntry.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def test_sales_report_counts_b2b_revenue_on_collection_date_not_invoice_date():
    issued_at = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
    collected_at = datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc)

    with make_session() as session:
        cash = Account(id=1, code="1000", name="Cash", type="asset", balance=0)
        client = B2BClient(id=1, name="Alpha Market", payment_terms="full_payment", outstanding=0)
        product = Product(id=1, sku="SKU-1", name="Dates", price=Decimal("100.00"), unit="kg", stock=100)
        invoice = B2BInvoice(
            id=1,
            client_id=1,
            invoice_number="HB2B-00001",
            invoice_type="full_payment",
            status="paid",
            total=Decimal("1000.00"),
            amount_paid=Decimal("1000.00"),
            created_at=issued_at,
        )
        item = B2BInvoiceItem(
            invoice_id=1,
            product_id=1,
            qty=Decimal("10.000"),
            unit_price=Decimal("100.00"),
            total=Decimal("1000.00"),
        )
        collection = Journal(
            id=1,
            ref_type="b2b_collection",
            ref_id=1,
            description="B2B payment collected - HB2B-00001",
            created_at=collected_at,
        )
        collection_entry = JournalEntry(journal_id=1, account_id=1, debit=Decimal("1000.00"), credit=0)
        session.add_all([cash, client, product, invoice, item, collection, collection_entry])
        session.commit()

        data = run(
            _build_sales_report(
                AsyncSessionAdapter(session),
                d_from=datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc),
                d_to=datetime(2026, 5, 16, 23, 59, tzinfo=timezone.utc),
                include_all=True,
            )
        )

    assert data["channels"]["b2b"]["gross_sales"] == 1000.0
    assert data["gross_sales"] == 1000.0
    assert data["net_sales"] == 1000.0
    assert data["daily"] == [
        {
            "date": "2026-05-16",
            "gross_sales": 1000.0,
            "refunds": 0.0,
            "net_sales": 1000.0,
            "cash_collected": 1000.0,
        }
    ]
    assert data["b2b_records"][0]["datetime"] == "2026-05-01 09:00"
    assert data["b2b_records"][0]["collection_datetime"] == "2026-05-16 10:00"
    assert data["b2b_records"][0]["collected_in_period"] == 1000.0


def test_sales_report_excludes_b2b_invoice_when_collection_is_outside_range():
    issued_at = datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc)
    collected_at = datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc)

    with make_session() as session:
        cash = Account(id=1, code="1000", name="Cash", type="asset", balance=0)
        client = B2BClient(id=1, name="Alpha Market", payment_terms="full_payment", outstanding=0)
        invoice = B2BInvoice(
            id=1,
            client_id=1,
            invoice_number="HB2B-00001",
            invoice_type="full_payment",
            status="paid",
            total=Decimal("1000.00"),
            amount_paid=Decimal("1000.00"),
            created_at=issued_at,
        )
        collection = Journal(
            id=1,
            ref_type="b2b_collection",
            ref_id=1,
            description="B2B payment collected - HB2B-00001",
            created_at=collected_at,
        )
        collection_entry = JournalEntry(journal_id=1, account_id=1, debit=Decimal("1000.00"), credit=0)
        session.add_all([cash, client, invoice, collection, collection_entry])
        session.commit()

        data = run(
            _build_sales_report(
                AsyncSessionAdapter(session),
                d_from=datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc),
                d_to=datetime(2026, 5, 16, 23, 59, tzinfo=timezone.utc),
                include_all=True,
            )
        )

    assert data["channels"]["b2b"]["gross_sales"] == 0.0
    assert data["gross_sales"] == 0.0
    assert data["net_sales"] == 0.0
    assert data["b2b_records"] == []
