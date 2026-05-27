"""Service-layer tests for the Drying Batch module.

Covers the state machine, stock moves, yield calculation, and error guards.
Uses a queue-based FakeDryingSession — no real DB required.

Execute-call ordering notes (important for test construction):
  start_batch:
    1. _load_product_or_404 per input (stock check)
    2. next_batch_number → select max(DryingBatch.id)
    — then flush/add/commit —

  complete_batch:
    1. _load_batch_or_404 (returns batch with inputs already on batch.inputs)
    2. _load_product_or_404 per output item

  cancel_batch:
    1. _load_batch_or_404 (returns batch with inputs)
    2. _load_product_or_404 per input (for refund)

  log_spoilage:
    1. _load_batch_or_404 (no eager loading needed)
    2. _load_product_or_404 for the spoiled product
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.models.inventory import StockMove
from app.models.product import Product
from app.models.drying import DryingBatch, DryingBatchInput, DryingBatchSpoilage
from app.schemas.drying import (
    DryingBatchCancelRequest,
    DryingBatchCompleteRequest,
    DryingBatchInputItem,
    DryingBatchOutputItem,
    DryingBatchSpoilageCreate,
    DryingBatchStartCreate,
)
from app.services.drying_service import (
    cancel_batch,
    complete_batch,
    log_spoilage,
    start_batch,
)


# ---------------------------------------------------------------------------
# Fake infrastructure
# ---------------------------------------------------------------------------

class FakeScalarResult:
    """Wraps a value to look like an SQLAlchemy async result."""

    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else []


class FakeDryingSession:
    """Queue-based fake AsyncSession for drying service tests.

    Responses are consumed in execute() call order. Objects add()-ed get
    an auto-assigned id so flush() doesn't need to do extra work.
    """

    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self._call_idx  = 0
        self.added: list = []
        self.committed   = False

    async def execute(self, _stmt):
        if self._call_idx < len(self._responses):
            val = self._responses[self._call_idx]
            self._call_idx += 1
        else:
            val = None
        if isinstance(val, FakeScalarResult):
            return val
        return FakeScalarResult(val)

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = len(self.added)

    async def flush(self):
        # Simulate autoincrement for objects that have no id yet.
        for i, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = i

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        pass

    async def delete(self, obj):
        pass


# ---------------------------------------------------------------------------
# Stub factories
# ---------------------------------------------------------------------------

def _make_product(*, id=1, name="Olives", unit="kg", stock=100.0) -> Product:
    return Product(id=id, name=name, unit=unit, stock=stock, sku=f"SKU-{id:03d}")


def _make_user():
    return SimpleNamespace(id=99, name="Test User", role="admin")


def _make_in_progress_batch(
    *,
    id=1,
    batch_number="DRY-0001",
    input_qty=50.0,
    input_unit="kg",
    input_product_id=1,
):
    """A SimpleNamespace batch in in_progress state with one input."""
    inp_product = SimpleNamespace(id=input_product_id, unit=input_unit, stock=100.0)
    inp = SimpleNamespace(
        id=10,
        batch_id=id,
        product_id=input_product_id,
        qty=input_qty,
        product=inp_product,
    )
    return SimpleNamespace(
        id=id,
        batch_number=batch_number,
        status="in_progress",
        inputs=[inp],
        outputs=[],
        spoilage=[],
        notes=None,
        completed_by_id=None,
        actual_yield_pct=None,
        completed_at=None,
        cancelled_at=None,
    )


def _make_completed_batch(**kwargs):
    b = _make_in_progress_batch(**kwargs)
    b.status = "completed"
    return b


# ---------------------------------------------------------------------------
# Tests: start_batch
# ---------------------------------------------------------------------------

def test_start_batch_deducts_input_stock():
    """Starting a batch immediately deducts input qty from product.stock."""
    product = _make_product(stock=100.0)
    user    = _make_user()

    # execute call order:
    #   1. _load_product_or_404 for the input product
    #   2. next_batch_number → max(DryingBatch.id) → None
    db = FakeDryingSession([
        FakeScalarResult(product),  # stock check
        FakeScalarResult(None),     # max id → 0 → batch_number DRY-0001
    ])

    data = DryingBatchStartCreate(
        inputs=[DryingBatchInputItem(product_id=product.id, qty=30.0)],
    )
    asyncio.run(start_batch(db, data, user))

    assert float(product.stock) == 70.0
    assert db.committed is True


def test_start_batch_creates_stock_move():
    """start_batch must add a StockMove with type='out' and negative qty."""
    product = _make_product(stock=100.0)
    user    = _make_user()

    db = FakeDryingSession([
        FakeScalarResult(product),
        FakeScalarResult(None),
    ])

    data = DryingBatchStartCreate(
        inputs=[DryingBatchInputItem(product_id=product.id, qty=40.0)],
    )
    asyncio.run(start_batch(db, data, user))

    stock_moves = [obj for obj in db.added if isinstance(obj, StockMove)]
    assert len(stock_moves) == 1
    assert stock_moves[0].type == "out"
    assert float(stock_moves[0].qty) == -40.0
    assert stock_moves[0].ref_type == "drying_batch"


def test_start_batch_status_is_in_progress():
    """The new DryingBatch must have status='in_progress'."""
    product = _make_product(stock=50.0)
    user    = _make_user()

    db = FakeDryingSession([
        FakeScalarResult(product),
        FakeScalarResult(None),
    ])

    data = DryingBatchStartCreate(
        inputs=[DryingBatchInputItem(product_id=product.id, qty=10.0)],
    )
    asyncio.run(start_batch(db, data, user))

    batches = [obj for obj in db.added if isinstance(obj, DryingBatch)]
    assert len(batches) == 1
    assert batches[0].status == "in_progress"


def test_start_batch_rejects_insufficient_stock():
    """Requesting more qty than available must raise HTTPException(400)."""
    from fastapi import HTTPException
    product = _make_product(stock=5.0)
    user    = _make_user()

    db = FakeDryingSession([
        FakeScalarResult(product),
    ])

    data = DryingBatchStartCreate(
        inputs=[DryingBatchInputItem(product_id=product.id, qty=10.0)],
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(start_batch(db, data, user))

    assert exc_info.value.status_code == 400
    assert "Not enough stock" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Tests: complete_batch
# ---------------------------------------------------------------------------

def test_complete_batch_creates_output_stock():
    """Completing a batch adds output qty to the output product's stock."""
    batch   = _make_in_progress_batch(input_qty=100.0, input_unit="kg")
    out_prd = _make_product(id=2, name="Dried Olives", unit="kg", stock=0.0)
    user    = _make_user()

    # execute order: batch lookup, then output product lookup
    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(out_prd),
    ])

    data = DryingBatchCompleteRequest(
        outputs=[DryingBatchOutputItem(product_id=out_prd.id, qty=20.0)],
    )
    asyncio.run(complete_batch(db, batch.id, data, user))

    assert float(out_prd.stock) == 20.0
    assert batch.status == "completed"
    assert db.committed is True


def test_complete_batch_computes_yield_pct():
    """actual_yield_pct = (total_out_kg / total_in_kg) * 100, rounded to 2dp."""
    # input: 100 kg, output: 20 kg → 20.0 %
    batch   = _make_in_progress_batch(input_qty=100.0, input_unit="kg")
    out_prd = _make_product(id=2, name="Dried Olives", unit="kg", stock=0.0)
    user    = _make_user()

    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(out_prd),
    ])

    data = DryingBatchCompleteRequest(
        outputs=[DryingBatchOutputItem(product_id=out_prd.id, qty=20.0)],
    )
    asyncio.run(complete_batch(db, batch.id, data, user))

    assert batch.actual_yield_pct == 20.0


def test_complete_batch_rejects_when_already_completed():
    """Calling complete on a completed batch must raise HTTPException(400)."""
    from fastapi import HTTPException
    batch = _make_completed_batch()
    user  = _make_user()

    db = FakeDryingSession([FakeScalarResult(batch)])

    data = DryingBatchCompleteRequest(
        outputs=[DryingBatchOutputItem(product_id=2, qty=10.0)],
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(complete_batch(db, batch.id, data, user))

    assert exc_info.value.status_code == 400
    assert "not in progress" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# Tests: cancel_batch
# ---------------------------------------------------------------------------

def test_cancel_batch_refunds_input_stock():
    """Cancelling a batch restores input qty back to the input product's stock."""
    batch = _make_in_progress_batch(input_qty=30.0, input_unit="kg")
    # The input product is reloaded in cancel_batch via _load_product_or_404.
    refund_product = _make_product(id=1, stock=10.0)
    user  = _make_user()

    # execute order: batch lookup, then product lookup per input
    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(refund_product),
    ])

    data = DryingBatchCancelRequest(reason="Batch spoiled before completion")
    asyncio.run(cancel_batch(db, batch.id, data, user))

    # 10 + 30 = 40
    assert float(refund_product.stock) == 40.0
    assert batch.status == "cancelled"
    assert db.committed is True


def test_cancel_batch_appends_reason_to_notes():
    """Cancel reason must be appended to batch.notes."""
    batch = _make_in_progress_batch(input_qty=10.0)
    refund_product = _make_product(id=1, stock=50.0)
    user  = _make_user()

    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(refund_product),
    ])

    data = DryingBatchCancelRequest(reason="Weather damage")
    asyncio.run(cancel_batch(db, batch.id, data, user))

    assert "[cancelled] Weather damage" in (batch.notes or "")


def test_cancel_batch_rejects_when_completed():
    """Cancelling an already-completed batch must raise HTTPException(400)."""
    from fastapi import HTTPException
    batch = _make_completed_batch()
    user  = _make_user()

    db = FakeDryingSession([FakeScalarResult(batch)])

    data = DryingBatchCancelRequest()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(cancel_batch(db, batch.id, data, user))

    assert exc_info.value.status_code == 400
    assert "not in progress" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# Tests: log_spoilage
# ---------------------------------------------------------------------------

def test_log_spoilage_deducts_stock():
    """A spoilage event deducts qty from the product's stock immediately."""
    batch   = _make_in_progress_batch()
    product = _make_product(id=1, stock=50.0)
    user    = _make_user()

    # execute order: batch lookup, product lookup
    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(product),
    ])

    data = DryingBatchSpoilageCreate(
        product_id=product.id, qty=10.0, reason="mold"
    )
    asyncio.run(log_spoilage(db, batch.id, data, user))

    assert float(product.stock) == 40.0
    assert db.committed is True


def test_log_spoilage_creates_stock_move():
    """log_spoilage must add a StockMove with ref_type='drying_batch_spoilage'."""
    batch   = _make_in_progress_batch()
    product = _make_product(id=1, stock=50.0)
    user    = _make_user()

    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(product),
    ])

    data = DryingBatchSpoilageCreate(
        product_id=product.id, qty=5.0, reason="pest"
    )
    asyncio.run(log_spoilage(db, batch.id, data, user))

    moves = [obj for obj in db.added if isinstance(obj, StockMove)]
    assert len(moves) == 1
    assert moves[0].type == "out"
    assert moves[0].ref_type == "drying_batch_spoilage"


def test_log_spoilage_rejects_invalid_reason():
    """An unsupported reason string must raise HTTPException(400)."""
    from fastapi import HTTPException
    batch   = _make_in_progress_batch()
    product = _make_product(id=1, stock=50.0)
    user    = _make_user()

    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(product),
    ])

    data = DryingBatchSpoilageCreate(
        product_id=product.id, qty=5.0, reason="invalid_reason"
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(log_spoilage(db, batch.id, data, user))

    assert exc_info.value.status_code == 400
    assert "Invalid spoilage reason" in exc_info.value.detail


def test_log_spoilage_rejects_on_completed_batch():
    """Logging spoilage against a completed batch must raise HTTPException(400)."""
    from fastapi import HTTPException
    batch = _make_completed_batch()
    user  = _make_user()

    db = FakeDryingSession([FakeScalarResult(batch)])

    data = DryingBatchSpoilageCreate(product_id=1, qty=5.0, reason="mold")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(log_spoilage(db, batch.id, data, user))

    assert exc_info.value.status_code == 400
    assert "not in progress" in exc_info.value.detail.lower()
