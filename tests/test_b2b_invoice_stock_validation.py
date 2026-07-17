import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.models.b2b import B2BInvoice
from app.routers import b2b


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, _stmt):
        return FakeScalarResult(self._results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if not getattr(obj, "id", None):
                obj.id = 1

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        pass

    async def rollback(self):
        self.rolled_back = True


def run(coro):
    return asyncio.run(coro)


async def _noop(*args, **kwargs):
    return None


def test_duplicate_b2b_invoice_lines_cannot_exceed_available_stock():
    client = SimpleNamespace(
        id=1,
        name="Wholesale Client",
        payment_terms="cash",
        discount_pct=0,
        outstanding=0,
    )
    product = SimpleNamespace(
        id=7,
        sku="SKU-7",
        name="One Piece Product",
        price=10,
        stock=1,
        item_type="finished",
    )
    data = b2b.InvoiceCreate(
        client_id=client.id,
        items=[
            b2b.InvoiceItemIn(product_id=product.id, qty=1, unit_price=10),
            b2b.InvoiceItemIn(product_id=product.id, qty=1, unit_price=10),
        ],
    )
    fake_db = FakeSession([client, product])
    user = SimpleNamespace(id=99, name="Seller")

    orig_seed = b2b._seed_deferred_revenue
    b2b._seed_deferred_revenue = _noop
    try:
        with pytest.raises(HTTPException) as exc_info:
            run(b2b.create_invoice(data=data, db=fake_db, current_user=user))
    finally:
        b2b._seed_deferred_revenue = orig_seed

    assert exc_info.value.status_code == 400
    assert "Not enough stock" in exc_info.value.detail
    assert not fake_db.committed
    assert not any(isinstance(obj, B2BInvoice) for obj in fake_db.added)
