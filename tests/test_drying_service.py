"""Service-layer tests for the multi-stage Drying Batch module.

Covers state machine, stock moves, stage metrics, cancel clawbacks,
yield calculation, and error guards.

Execute-call ordering notes (important for FakeSession response queuing):
  start_batch:
    1. _load_product_or_404 per input
    2. next_batch_number → select max(DryingBatch.id)
    — flush(batch) / flush(stage) / then stock deductions —

  add_next_stage:
    1. _load_batch_or_404 (with_stages=True)
    2. _load_product_or_404 per prev_stage_outputs item  (inside _close_open_stage)
    3. _load_product_or_404 per new_stage_inputs item

  finalize_batch:
    1. _load_batch_or_404 (with_stages=True)
    2. _load_product_or_404 per final_outputs item  (inside _close_open_stage)

  cancel_batch:
    1. _load_batch_or_404 (with_stages=True)
    2. _load_product_or_404 per output (if stage closed) per stage (reverse)
    3. _load_product_or_404 per input per stage (reverse)

  log_spoilage:
    1. _load_batch_or_404 (no stages)
    2. _load_product_or_404 for the spoiled product
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.models.drying import (
    DryingBatch,
    DryingBatchStage,
    DryingBatchStageInput,
    DryingBatchStageOutput,
    DryingBatchSpoilage,
)
from app.models.inventory import StockMove
from app.schemas.drying import (
    DryingBatchCancelRequest,
    DryingBatchFinalizeRequest,
    DryingBatchNextStageRequest,
    DryingBatchSpoilageCreate,
    DryingBatchStartCreate,
)
from app.services.drying_service import (
    add_next_stage,
    cancel_batch,
    finalize_batch,
    log_spoilage,
    start_batch,
)

# ---------------------------------------------------------------------------
# Fake infrastructure (same queue-based pattern as test_receive_products.py)
# ---------------------------------------------------------------------------

class FakeScalarResult:
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
    """Queue-based fake AsyncSession.

    Responses consumed in execute() call order.
    add()-ed objects get auto-incremented id.
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

def _product(*, id=1, name="Olives", unit="kg", stock=100.0):
    return SimpleNamespace(id=id, name=name, unit=unit, stock=stock)


def _user():
    return SimpleNamespace(id=99, name="Test User", role="admin")


def _open_stage(*, id=10, stage_number=1, label=None, inputs=None, outputs=None):
    """A stage with total_output_qty=None (open)."""
    return SimpleNamespace(
        id=id,
        stage_number=stage_number,
        label=label,
        notes=None,
        inputs=inputs or [],
        outputs=outputs or [],
        total_input_qty=None,
        total_output_qty=None,
        stage_loss_pct=None,
        cumulative_yield_pct=None,
    )


def _closed_stage(*, id=10, stage_number=1, inputs=None, outputs=None,
                  total_input_qty=100.0, total_output_qty=30.0,
                  stage_loss_pct=70.0, cumulative_yield_pct=30.0):
    """A closed stage (total_output_qty is set)."""
    s = _open_stage(id=id, stage_number=stage_number, inputs=inputs, outputs=outputs)
    s.total_input_qty      = total_input_qty
    s.total_output_qty     = total_output_qty
    s.stage_loss_pct       = stage_loss_pct
    s.cumulative_yield_pct = cumulative_yield_pct
    return s


def _stage_input(*, id=100, stage_id=10, product_id=1, qty=50.0, unit="kg"):
    product = SimpleNamespace(id=product_id, unit=unit, stock=100.0)
    return SimpleNamespace(id=id, stage_id=stage_id, product_id=product_id, qty=qty, product=product)


def _stage_output(*, id=200, stage_id=10, product_id=2, qty=20.0, unit="kg"):
    product = SimpleNamespace(id=product_id, unit=unit, stock=0.0)
    return SimpleNamespace(id=id, stage_id=stage_id, product_id=product_id, qty=qty, product=product)


def _batch_in_progress(*, id=1, batch_number="DRY-0001", stages=None):
    return SimpleNamespace(
        id=id,
        batch_number=batch_number,
        status="in_progress",
        stages=stages or [],
        spoilage=[],
        notes=None,
        completed_at=None,
        cancelled_at=None,
        completed_by_id=None,
    )


def _batch_completed(**kwargs):
    b = _batch_in_progress(**kwargs)
    b.status = "completed"
    return b


# ---------------------------------------------------------------------------
# Tests: start_batch
# ---------------------------------------------------------------------------

def test_start_batch_creates_stage_1():
    """start_batch must add a DryingBatchStage with stage_number=1."""
    product = _product(stock=100.0)
    user = _user()

    db = FakeDryingSession([
        FakeScalarResult(product),   # _load_product_or_404
        FakeScalarResult(None),      # next_batch_number → max id = None → 0
    ])

    data = DryingBatchStartCreate(inputs=[{"product_id": product.id, "qty": 30.0}])
    asyncio.run(start_batch(db, data, user))

    stages = [obj for obj in db.added if isinstance(obj, DryingBatchStage)]
    assert len(stages) == 1
    assert stages[0].stage_number == 1
    # Open stage — metrics are NULL
    assert stages[0].total_output_qty is None


def test_start_batch_deducts_input_stock():
    """start_batch deducts each input qty from product.stock."""
    product = _product(stock=100.0)
    user = _user()

    db = FakeDryingSession([
        FakeScalarResult(product),
        FakeScalarResult(None),
    ])

    data = DryingBatchStartCreate(inputs=[{"product_id": product.id, "qty": 30.0}])
    asyncio.run(start_batch(db, data, user))

    assert float(product.stock) == 70.0
    assert db.committed is True


def test_start_batch_creates_stock_move_with_correct_fields():
    """start_batch adds a StockMove type='out', positive qty, with before/after."""
    product = _product(stock=100.0)
    user = _user()

    db = FakeDryingSession([
        FakeScalarResult(product),
        FakeScalarResult(None),
    ])

    data = DryingBatchStartCreate(inputs=[{"product_id": product.id, "qty": 40.0}])
    asyncio.run(start_batch(db, data, user))

    moves = [obj for obj in db.added if isinstance(obj, StockMove)]
    assert len(moves) == 1
    m = moves[0]
    assert m.type == "out"
    assert float(m.qty) == 40.0          # always positive
    assert m.qty_before == 100.0
    assert m.qty_after  == 60.0
    assert m.ref_type == "drying_batch"
    assert m.user_id == user.id


def test_start_batch_rejects_insufficient_stock():
    """start_batch raises HTTPException(400) when stock < requested qty."""
    from fastapi import HTTPException

    product = _product(stock=5.0)
    user = _user()

    db = FakeDryingSession([FakeScalarResult(product)])

    data = DryingBatchStartCreate(inputs=[{"product_id": product.id, "qty": 10.0}])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(start_batch(db, data, user))

    assert exc.value.status_code == 400
    assert "Not enough stock" in exc.value.detail


# ---------------------------------------------------------------------------
# Tests: add_next_stage
# ---------------------------------------------------------------------------

def _make_2stage_batch():
    """Helper: batch with one open stage (stage 1, 50kg input)."""
    inp = _stage_input(stage_id=10, product_id=1, qty=50.0, unit="kg")
    stage1 = _open_stage(id=10, stage_number=1, inputs=[inp])
    return _batch_in_progress(id=1, stages=[stage1])


def test_add_next_stage_closes_previous_and_opens_new():
    """add_next_stage must close the open stage and create Stage 2."""
    batch = _make_2stage_batch()
    out_product = _product(id=2, name="Dried", unit="kg", stock=0.0)
    new_in_product = _product(id=2, name="Dried", unit="kg", stock=20.0)
    user = _user()

    db = FakeDryingSession([
        FakeScalarResult(batch),        # _load_batch_or_404
        FakeScalarResult(out_product),  # _close_open_stage: load output product
        FakeScalarResult(new_in_product), # new input product
    ])

    data = DryingBatchNextStageRequest(
        prev_stage_outputs=[{"product_id": 2, "qty": 20.0}],
        new_stage_inputs=[{"product_id": 2, "qty": 15.0}],
    )
    asyncio.run(add_next_stage(db, batch.id, data, user))

    new_stages = [obj for obj in db.added if isinstance(obj, DryingBatchStage)]
    assert len(new_stages) == 1
    assert new_stages[0].stage_number == 2
    # New stage is open
    assert new_stages[0].total_output_qty is None


def test_add_next_stage_credits_prev_outputs_and_deducts_new_inputs():
    """add_next_stage: prev outputs credited to stock; new inputs deducted."""
    batch = _make_2stage_batch()
    out_product  = _product(id=2, name="Dried", unit="kg", stock=0.0)
    new_in_product = _product(id=2, name="Dried", unit="kg", stock=20.0)
    user = _user()

    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(out_product),
        FakeScalarResult(new_in_product),
    ])

    data = DryingBatchNextStageRequest(
        prev_stage_outputs=[{"product_id": 2, "qty": 20.0}],
        new_stage_inputs=[{"product_id": 2, "qty": 15.0}],
    )
    asyncio.run(add_next_stage(db, batch.id, data, user))

    moves = [obj for obj in db.added if isinstance(obj, StockMove)]
    # One "in" move (output credited) + one "out" move (new input deducted)
    in_moves  = [m for m in moves if m.type == "in"]
    out_moves = [m for m in moves if m.type == "out"]
    assert len(in_moves) == 1
    assert float(in_moves[0].qty) == 20.0
    assert len(out_moves) == 1
    assert float(out_moves[0].qty) == 15.0


def test_add_next_stage_computes_stage_loss_pct():
    """Stage loss % is set on the closed stage: (1 - out/in) * 100."""
    inp = _stage_input(stage_id=10, product_id=1, qty=100.0, unit="kg")
    stage1 = _open_stage(id=10, stage_number=1, inputs=[inp])
    batch = _batch_in_progress(id=1, stages=[stage1])

    out_product    = _product(id=2, name="Dried", unit="kg", stock=0.0)
    new_in_product = _product(id=2, name="Dried", unit="kg", stock=30.0)
    user = _user()

    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(out_product),
        FakeScalarResult(new_in_product),
    ])

    data = DryingBatchNextStageRequest(
        prev_stage_outputs=[{"product_id": 2, "qty": 30.0}],
        new_stage_inputs=[{"product_id": 2, "qty": 25.0}],
    )
    asyncio.run(add_next_stage(db, batch.id, data, user))

    # stage1 should now have stage_loss_pct = (1 - 30/100) * 100 = 70.0
    assert stage1.stage_loss_pct == 70.0


def test_add_next_stage_computes_cumulative_yield_vs_stage_1():
    """cumulative_yield_pct = (stage outputs) / (stage 1 inputs) * 100."""
    inp = _stage_input(stage_id=10, product_id=1, qty=100.0, unit="kg")
    stage1 = _open_stage(id=10, stage_number=1, inputs=[inp])
    batch = _batch_in_progress(id=1, stages=[stage1])

    out_product    = _product(id=2, name="Dried", unit="kg", stock=0.0)
    new_in_product = _product(id=2, name="Dried", unit="kg", stock=40.0)
    user = _user()

    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(out_product),
        FakeScalarResult(new_in_product),
    ])

    data = DryingBatchNextStageRequest(
        prev_stage_outputs=[{"product_id": 2, "qty": 40.0}],
        new_stage_inputs=[{"product_id": 2, "qty": 35.0}],
    )
    asyncio.run(add_next_stage(db, batch.id, data, user))

    # 40 out / 100 stage-1 in * 100 = 40.0
    assert stage1.cumulative_yield_pct == 40.0


def test_add_next_stage_rejects_on_completed_batch():
    """add_next_stage raises 400 if batch is not in_progress."""
    from fastapi import HTTPException

    batch = _batch_completed()
    user = _user()
    db = FakeDryingSession([FakeScalarResult(batch)])

    data = DryingBatchNextStageRequest(
        prev_stage_outputs=[{"product_id": 1, "qty": 10.0}],
        new_stage_inputs=[{"product_id": 1, "qty": 5.0}],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(add_next_stage(db, 1, data, user))

    assert exc.value.status_code == 400
    assert "not in progress" in exc.value.detail.lower()


# ---------------------------------------------------------------------------
# Tests: finalize_batch
# ---------------------------------------------------------------------------

def test_finalize_batch_writes_final_outputs_and_marks_completed():
    """finalize_batch closes the open stage and sets status=completed."""
    inp = _stage_input(stage_id=10, product_id=1, qty=100.0, unit="kg")
    stage1 = _open_stage(id=10, stage_number=1, inputs=[inp])
    batch = _batch_in_progress(id=1, stages=[stage1])

    out_product = _product(id=2, name="Powder", unit="kg", stock=0.0)
    user = _user()

    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(out_product),
    ])

    data = DryingBatchFinalizeRequest(final_outputs=[{"product_id": 2, "qty": 25.0}])
    asyncio.run(finalize_batch(db, batch.id, data, user))

    assert batch.status == "completed"
    assert db.committed is True

    outputs = [obj for obj in db.added if isinstance(obj, DryingBatchStageOutput)]
    assert len(outputs) == 1
    assert float(outputs[0].qty) == 25.0


def test_finalize_batch_credits_output_stock():
    """finalize_batch credits output qty to product stock."""
    inp = _stage_input(stage_id=10, product_id=1, qty=100.0, unit="kg")
    stage1 = _open_stage(id=10, stage_number=1, inputs=[inp])
    batch = _batch_in_progress(id=1, stages=[stage1])

    out_product = _product(id=2, name="Powder", unit="kg", stock=10.0)
    user = _user()

    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(out_product),
    ])

    data = DryingBatchFinalizeRequest(final_outputs=[{"product_id": 2, "qty": 25.0}])
    asyncio.run(finalize_batch(db, batch.id, data, user))

    assert float(out_product.stock) == 35.0   # 10 + 25

    moves = [obj for obj in db.added if isinstance(obj, StockMove)]
    assert len(moves) == 1
    assert moves[0].type == "in"
    assert float(moves[0].qty) == 25.0
    assert moves[0].qty_before == 10.0
    assert moves[0].qty_after  == 35.0


def test_finalize_batch_rejects_when_no_open_stage():
    """finalize_batch raises 400 when batch is already completed."""
    from fastapi import HTTPException

    batch = _batch_completed()
    user = _user()
    db = FakeDryingSession([FakeScalarResult(batch)])

    data = DryingBatchFinalizeRequest(final_outputs=[{"product_id": 2, "qty": 10.0}])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(finalize_batch(db, 1, data, user))

    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Tests: cancel_batch
# ---------------------------------------------------------------------------

def _make_two_stage_batch_partly_closed():
    """Stage 1 closed (outputs set), Stage 2 open (outputs not set)."""
    # Stage 1 — closed
    in1  = _stage_input(id=100, stage_id=10, product_id=1, qty=100.0, unit="kg")
    out1 = _stage_output(id=200, stage_id=10, product_id=2, qty=30.0, unit="kg")
    stage1 = _closed_stage(
        id=10, stage_number=1,
        inputs=[in1], outputs=[out1],
        total_input_qty=100.0, total_output_qty=30.0,
        stage_loss_pct=70.0, cumulative_yield_pct=30.0,
    )

    # Stage 2 — open
    in2 = _stage_input(id=101, stage_id=20, product_id=2, qty=30.0, unit="kg")
    stage2 = _open_stage(id=20, stage_number=2, inputs=[in2])

    return _batch_in_progress(id=1, stages=[stage1, stage2])


def test_cancel_batch_refunds_inputs_clawback_outputs_across_all_stages():
    """cancel_batch refunds stage2 input, clawbacks stage1 output, refunds stage1 input."""
    batch = _make_two_stage_batch_partly_closed()
    # Reverse order: stage2 first, then stage1
    # Stage 2 (open): no clawback, just refund input (product_id=2)
    # Stage 1 (closed): clawback output (product_id=2), refund input (product_id=1)
    prod_in2  = _product(id=2, name="Dried",  unit="kg", stock=0.0)   # stage2 input refund
    prod_out1 = _product(id=2, name="Dried",  unit="kg", stock=0.0)   # stage1 output clawback
    prod_in1  = _product(id=1, name="Olives", unit="kg", stock=0.0)   # stage1 input refund
    user = _user()

    db = FakeDryingSession([
        FakeScalarResult(batch),
        # Stage 2 (open): input refund → product_id=2
        FakeScalarResult(prod_in2),
        # Stage 1 (closed): clawback output → product_id=2
        FakeScalarResult(prod_out1),
        # Stage 1: input refund → product_id=1
        FakeScalarResult(prod_in1),
    ])

    data = DryingBatchCancelRequest(reason="Test cancel")
    asyncio.run(cancel_batch(db, batch.id, data, user))

    assert batch.status == "cancelled"
    assert "[cancelled] Test cancel" in (batch.notes or "")
    assert db.committed is True

    moves = [obj for obj in db.added if isinstance(obj, StockMove)]
    types = [m.type for m in moves]
    # stage2 input refund (in) + stage1 clawback (out) + stage1 input refund (in)
    assert types.count("in")  == 2
    assert types.count("out") == 1


def test_cancel_batch_three_stages_full_refund():
    """Three-stage cancel: 2 closed + 1 open; all stocks restored in reverse."""
    # Stage 1 closed: 100kg in, 40kg out
    in1  = _stage_input(id=100, stage_id=10, product_id=1, qty=100.0, unit="kg")
    out1 = _stage_output(id=200, stage_id=10, product_id=2, qty=40.0, unit="kg")
    stage1 = _closed_stage(id=10, stage_number=1, inputs=[in1], outputs=[out1])

    # Stage 2 closed: 40kg in, 15kg out
    in2  = _stage_input(id=101, stage_id=20, product_id=2, qty=40.0, unit="kg")
    out2 = _stage_output(id=201, stage_id=20, product_id=3, qty=15.0, unit="kg")
    stage2 = _closed_stage(id=20, stage_number=2, inputs=[in2], outputs=[out2],
                           total_input_qty=40.0, total_output_qty=15.0)

    # Stage 3 open: 15kg in, no outputs yet
    in3 = _stage_input(id=102, stage_id=30, product_id=3, qty=15.0, unit="kg")
    stage3 = _open_stage(id=30, stage_number=3, inputs=[in3])

    batch = _batch_in_progress(id=1, stages=[stage1, stage2, stage3])

    # Six product loads in reverse stage order:
    # Stage 3 (open): input refund → p3
    p3a = _product(id=3, stock=0.0)
    # Stage 2 (closed): clawback out → p3, refund in → p2
    p3b = _product(id=3, stock=0.0)
    p2a = _product(id=2, stock=0.0)
    # Stage 1 (closed): clawback out → p2, refund in → p1
    p2b = _product(id=2, stock=0.0)
    p1  = _product(id=1, stock=0.0)

    user = _user()
    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(p3a),  # stage3 input refund
        FakeScalarResult(p3b),  # stage2 clawback
        FakeScalarResult(p2a),  # stage2 input refund
        FakeScalarResult(p2b),  # stage1 clawback
        FakeScalarResult(p1),   # stage1 input refund
    ])

    data = DryingBatchCancelRequest()
    asyncio.run(cancel_batch(db, batch.id, data, user))

    assert batch.status == "cancelled"
    moves = [obj for obj in db.added if isinstance(obj, StockMove)]
    # stage3: 1 refund (in)
    # stage2: 1 clawback (out) + 1 refund (in)
    # stage1: 1 clawback (out) + 1 refund (in)
    assert len(moves) == 5
    assert sum(1 for m in moves if m.type == "in")  == 3
    assert sum(1 for m in moves if m.type == "out") == 2


def test_cancel_batch_rejects_when_completed():
    """cancel_batch raises 400 on a completed batch."""
    from fastapi import HTTPException

    batch = _batch_completed()
    user = _user()
    db = FakeDryingSession([FakeScalarResult(batch)])

    data = DryingBatchCancelRequest()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cancel_batch(db, batch.id, data, user))

    assert exc.value.status_code == 400
    assert "not in progress" in exc.value.detail.lower()


# ---------------------------------------------------------------------------
# Tests: log_spoilage
# ---------------------------------------------------------------------------

def test_log_spoilage_deducts_stock():
    """log_spoilage deducts qty from product.stock immediately."""
    inp = _stage_input(stage_id=10, product_id=1, qty=50.0, unit="kg")
    stage1 = _open_stage(id=10, inputs=[inp])
    batch = _batch_in_progress(id=1, stages=[stage1])
    product = _product(id=1, stock=50.0)
    user = _user()

    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(product),
    ])

    data = DryingBatchSpoilageCreate(product_id=1, qty=10.0, reason="mold")
    asyncio.run(log_spoilage(db, batch.id, data, user))

    assert float(product.stock) == 40.0
    assert db.committed is True


def test_log_spoilage_creates_stock_move_with_correct_ref_type():
    """log_spoilage adds StockMove with ref_type='drying_batch_spoilage'."""
    batch = _batch_in_progress(id=1, stages=[_open_stage()])
    product = _product(id=1, stock=50.0)
    user = _user()

    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(product),
    ])

    data = DryingBatchSpoilageCreate(product_id=1, qty=5.0, reason="pest")
    asyncio.run(log_spoilage(db, batch.id, data, user))

    moves = [obj for obj in db.added if isinstance(obj, StockMove)]
    assert len(moves) == 1
    assert moves[0].type == "out"
    assert float(moves[0].qty) == 5.0
    assert moves[0].qty_before == 50.0
    assert moves[0].qty_after  == 45.0
    assert moves[0].ref_type == "drying_batch_spoilage"
    assert moves[0].user_id == user.id


def test_log_spoilage_rejects_invalid_reason():
    """log_spoilage raises 400 on an unsupported reason string."""
    from fastapi import HTTPException

    batch = _batch_in_progress(id=1, stages=[_open_stage()])
    product = _product(id=1, stock=50.0)
    user = _user()

    db = FakeDryingSession([
        FakeScalarResult(batch),
        FakeScalarResult(product),
    ])

    data = DryingBatchSpoilageCreate(product_id=1, qty=5.0, reason="invalid_reason")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(log_spoilage(db, batch.id, data, user))

    assert exc.value.status_code == 400
    assert "Invalid spoilage reason" in exc.value.detail


def test_log_spoilage_rejects_on_completed_batch():
    """log_spoilage raises 400 when batch is already completed."""
    from fastapi import HTTPException

    batch = _batch_completed()
    user = _user()
    db = FakeDryingSession([FakeScalarResult(batch)])

    data = DryingBatchSpoilageCreate(product_id=1, qty=5.0, reason="mold")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(log_spoilage(db, batch.id, data, user))

    assert exc.value.status_code == 400
    assert "not in progress" in exc.value.detail.lower()


# ---------------------------------------------------------------------------
# Tests: edit_finalized_batch
# ---------------------------------------------------------------------------

def _import_edit():
    from app.services.drying_service import edit_finalized_batch
    from app.schemas.drying import DryingBatchEditRequest
    return edit_finalized_batch, DryingBatchEditRequest


def test_edit_finalized_recomputes_stock_and_loss():
    """Editing a completed batch's output must clawback the old output,
    credit the new one, leave inputs untouched, and recompute loss."""
    edit_finalized_batch, DryingBatchEditRequest = _import_edit()
    user = _user()

    inp = _stage_input(id=100, stage_id=10, product_id=1, qty=10.0, unit="kg")
    old_out = _stage_output(id=200, stage_id=10, product_id=2, qty=1.5, unit="kg")
    stage = _closed_stage(id=10, inputs=[inp], outputs=[old_out])
    batch = _batch_completed(stages=[stage])

    dried = SimpleNamespace(id=2, name="Dried", unit="kg", stock=1.5)  # current stock after finalize

    session = FakeDryingSession(responses=[
        batch,    # _load_batch_or_404(with_stages=True)
        dried,    # clawback: _load_product_or_404(old output product)
        dried,    # credit: _load_product_or_404(new output product)
    ])

    req = DryingBatchEditRequest(stage_outputs=[{"stage_id": 10, "outputs": [{"product_id": 2, "qty": 2.0}]}], reason="recount")
    asyncio.run(edit_finalized_batch(session, 1, req, user))

    # stock: 1.5 - 1.5 (clawback) + 2.0 (credit) = 2.0
    assert dried.stock == 2.0
    # input product stock never touched (not even loaded)
    assert inp.product.stock == 100.0
    # loss recomputed: (1 - 2/10)*100 = 80
    assert abs(stage.stage_loss_pct - 80.0) < 0.01
    assert stage.total_output_qty == 2.0
    assert session.committed
    # a clawback (out) and a corrected credit (in) move were recorded
    moves = [o for o in session.added if isinstance(o, StockMove)]
    assert any(m.type == "out" and float(m.qty) == 1.5 for m in moves)
    assert any(m.type == "in" and float(m.qty) == 2.0 for m in moves)


def test_edit_rejects_non_completed_batch():
    edit_finalized_batch, DryingBatchEditRequest = _import_edit()
    from fastapi import HTTPException
    batch = _batch_in_progress(stages=[_open_stage(id=10)])
    session = FakeDryingSession(responses=[batch])
    req = DryingBatchEditRequest(stage_outputs=[{"stage_id": 10, "outputs": [{"product_id": 2, "qty": 1.0}]}])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(edit_finalized_batch(session, 1, req, _user()))
    assert ei.value.status_code == 400


def test_edit_rejects_unknown_stage():
    edit_finalized_batch, DryingBatchEditRequest = _import_edit()
    from fastapi import HTTPException
    stage = _closed_stage(id=10, inputs=[_stage_input()], outputs=[_stage_output()])
    batch = _batch_completed(stages=[stage])
    session = FakeDryingSession(responses=[batch])
    req = DryingBatchEditRequest(stage_outputs=[{"stage_id": 999, "outputs": [{"product_id": 2, "qty": 1.0}]}])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(edit_finalized_batch(session, 1, req, _user()))
    assert ei.value.status_code == 404