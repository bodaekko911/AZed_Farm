import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.database import Base
from app.models.b2b import B2BClient, B2BInvoice, B2BInvoiceItem, B2BRefund
from app.models.product import Product
from app.models.user import User
from app.routers.b2b import get_client_analysis


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
            B2BClient.__table__,
            B2BInvoice.__table__,
            B2BInvoiceItem.__table__,
            Product.__table__,
            B2BRefund.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def test_b2b_client_analysis_summarizes_sales_collections_and_risk():
    now = datetime.now(timezone.utc)
    with make_session() as session:
        alpha = B2BClient(
            id=1,
            name="Alpha Market",
            payment_terms="full_payment",
            credit_limit=Decimal("200.00"),
            is_active=True,
        )
        beta = B2BClient(
            id=2,
            name="Beta Cafe",
            payment_terms="cash",
            credit_limit=Decimal("0.00"),
            is_active=True,
        )
        fresh = B2BClient(
            id=3,
            name="Fresh Prospect",
            payment_terms="consignment",
            is_active=True,
        )
        session.add_all([alpha, beta, fresh])
        session.flush()
        session.add_all(
            [
                B2BInvoice(
                    client_id=1,
                    invoice_number="B2B-00001",
                    invoice_type="full_payment",
                    status="partial",
                    total=Decimal("1000.00"),
                    amount_paid=Decimal("700.00"),
                    created_at=now,
                ),
                B2BInvoice(
                    client_id=2,
                    invoice_number="B2B-00002",
                    invoice_type="cash",
                    status="paid",
                    total=Decimal("500.00"),
                    amount_paid=Decimal("500.00"),
                    created_at=now,
                ),
                B2BRefund(
                    client_id=1,
                    refund_number="RFD-00001",
                    subtotal=Decimal("50.00"),
                    discount=Decimal("0.00"),
                    total=Decimal("50.00"),
                    created_at=now,
                ),
            ]
        )
        session.commit()

        data = run(get_client_analysis(db=AsyncSessionAdapter(session)))

    assert data["summary"]["active_clients"] == 3
    assert data["summary"]["clients_with_sales"] == 2
    assert data["summary"]["gross_sales"] == 1500.0
    assert data["summary"]["refunds"] == 50.0
    assert data["summary"]["net_sales"] == 1450.0
    assert data["summary"]["paid_amount"] == 1200.0
    assert data["summary"]["outstanding"] == 250.0
    assert data["summary"]["payment_rate"] == 80.0
    assert data["summary"]["at_risk_clients"] == 1

    by_client = {row["name"]: row for row in data["clients"]}
    assert by_client["Alpha Market"]["net_sales"] == 950.0
    assert by_client["Alpha Market"]["outstanding"] == 250.0
    assert by_client["Alpha Market"]["payment_rate"] == 70.0
    assert by_client["Alpha Market"]["credit_used_pct"] == 125.0
    assert by_client["Alpha Market"]["risk_level"] == "over_limit"
    assert by_client["Beta Cafe"]["risk_level"] == "healthy"
    assert by_client["Fresh Prospect"]["risk_level"] == "new"
    assert data["top_clients"][0]["name"] == "Alpha Market"
    assert data["collection_watch"][0]["name"] == "Alpha Market"
