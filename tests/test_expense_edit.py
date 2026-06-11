"""Regression tests for expense editing — the paths that broke in production.

Covers three historical failure modes:

1. Farm → General not clearing: ``update_expense_entry`` used
   ``if data.farm_id is not None`` which skipped explicit nulls, so changing
   an expense from a farm back to "General" silently kept the old farm.
   The fix checks ``model_fields_set`` instead. Same for animal_group_id.

2. Omitted fields must NOT be cleared: a payload that doesn't mention
   farm_id at all must leave the existing assignment untouched.

3. Wrong-file fixes: the dead ``app/routers/expenses.py`` looks editable but
   is not mounted — the live router is ``expenses_refactored`` delegating to
   ``app/services/expense_service.py``. These tests run against the service
   module and assert the mounting, so a fix applied to the dead file fails
   the suite immediately instead of "working" until deploy.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.schemas.expense import ExpenseUpdate
from app.services import expense_service


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class FakeSession:
    """Queue-based fake: pops one prepared result per execute() call."""

    def __init__(self, results):
        self._results = list(results)
        self.statements = []
        self.deleted = []
        self.committed = False

    async def execute(self, statement):
        self.statements.append(statement)
        return FakeScalarResult(self._results.pop(0))

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


def _make_category(**overrides):
    base = dict(
        id=7,
        name="Utilities",
        account_code="5001",
        unit_price=None,
        carbon_factor_key=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_expense(category, **overrides):
    base = dict(
        id=42,
        ref_number="EXP-42",
        category=category,
        category_id=category.id,
        amount=150.0,
        payment_method="cash",
        vendor="Vendor",
        description="Desc",
        expense_date=None,
        farm_id=3,
        animal_group_id=9,
        is_animal_expense=False,
        unit_price_used=None,
        consumption=None,
        journal_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def patched_journal(monkeypatch: pytest.MonkeyPatch):
    """Neutralise journal posting / reversal / audit log for unit tests."""

    async def _noop_reverse(db, expense):
        return None

    async def _fake_post(db, **kwargs):
        return SimpleNamespace(id=101)

    monkeypatch.setattr(expense_service, "_reverse_expense_journal", _noop_reverse)
    monkeypatch.setattr(expense_service, "_post_expense_journal", _fake_post)
    monkeypatch.setattr(expense_service, "record", lambda *a, **k: None)


# ── 1. Farm → General must clear the assignment ─────────────────────────────

def test_explicit_null_farm_clears_assignment(patched_journal) -> None:
    category = _make_category()
    expense = _make_expense(category, farm_id=3, animal_group_id=9)
    fake_db = FakeSession(
        [
            expense,  # select(Expense)
            [],       # select(CarbonLog) — no existing auto logs
        ]
    )
    # The edit modal always sends farm_id; explicit null = "General expense".
    data = ExpenseUpdate(farm_id=None, animal_group_id=None)
    assert "farm_id" in data.model_fields_set
    assert "animal_group_id" in data.model_fields_set

    user = SimpleNamespace(id=1, name="Admin")
    result = asyncio.run(
        expense_service.update_expense_entry(fake_db, 42, data, user)
    )

    assert result == {"ok": True}
    assert expense.farm_id is None, "Farm → General must clear farm_id"
    assert expense.animal_group_id is None, "explicit null must clear animal group"
    assert fake_db.committed


# ── 2. Omitted fields must stay untouched ───────────────────────────────────

def test_omitted_farm_field_is_not_cleared(patched_journal) -> None:
    category = _make_category()
    expense = _make_expense(category, farm_id=3, animal_group_id=9)
    fake_db = FakeSession(
        [
            expense,
            [],  # carbon logs
        ]
    )
    # Payload that never mentions farm_id / animal_group_id (e.g. amount-only edit)
    data = ExpenseUpdate(amount=200.0)
    assert "farm_id" not in data.model_fields_set

    user = SimpleNamespace(id=1, name="Admin")
    asyncio.run(expense_service.update_expense_entry(fake_db, 42, data, user))

    assert expense.farm_id == 3, "omitted farm_id must not be cleared"
    assert expense.animal_group_id == 9, "omitted animal_group_id must not be cleared"
    assert expense.amount == 200.0


# ── 3. Edit resyncs the auto-created carbon log ──────────────────────────────

def test_edit_deletes_stale_carbon_logs(patched_journal, monkeypatch) -> None:
    category = _make_category(unit_price=10.0, carbon_factor_key="diesel_liter")
    expense = _make_expense(category, farm_id=None, animal_group_id=None)
    stale_log = SimpleNamespace(id=500, ref_type="expense", ref_id=42)
    fake_db = FakeSession(
        [
            expense,
            [stale_log],  # existing carbon log to be replaced
        ]
    )

    created = {}

    async def _fake_create_log(db, exp, cat, consumption, user):
        created["consumption"] = consumption

    monkeypatch.setattr(
        expense_service, "_create_carbon_log_for_expense", _fake_create_log
    )

    data = ExpenseUpdate(amount=100.0)
    user = SimpleNamespace(id=1, name="Admin")
    asyncio.run(expense_service.update_expense_entry(fake_db, 42, data, user))

    assert stale_log in fake_db.deleted, "stale carbon log must be removed on edit"
    # 100.0 amount / 10.0 category unit price = 10.0 consumption
    assert created["consumption"] == pytest.approx(10.0)
    assert expense.unit_price_used == pytest.approx(10.0)
    assert expense.consumption == pytest.approx(10.0)


# ── 4. Delete cleans up the carbon log ──────────────────────────────────────

def test_delete_removes_expense_and_carbon_log(patched_journal) -> None:
    category = _make_category()
    expense = _make_expense(category)
    auto_log = SimpleNamespace(id=501, ref_type="expense", ref_id=42)
    fake_db = FakeSession(
        [
            expense,
            [auto_log],
        ]
    )
    user = SimpleNamespace(id=1, name="Admin")
    result = asyncio.run(
        expense_service.delete_expense_entry(fake_db, 42, user)
    )

    assert result == {"ok": True}
    assert auto_log in fake_db.deleted
    assert expense in fake_db.deleted
    assert fake_db.committed


# ── 5. Wrong-file guard: the refactored router is what's mounted ────────────

def test_mounted_expense_router_is_the_refactored_one() -> None:
    from app.routers import ROUTERS, expenses_refactored

    assert expenses_refactored.router in ROUTERS, (
        "expenses_refactored.router must be mounted — edits to the legacy "
        "app/routers/expenses.py never reach production"
    )

    try:
        from app.routers import expenses as legacy_expenses
    except ImportError:
        return  # legacy file deleted — even better
    assert getattr(legacy_expenses, "router", None) not in ROUTERS, (
        "the legacy expenses router must NOT be mounted"
    )


def test_refactored_router_delegates_to_service_layer() -> None:
    import inspect
    from app.routers import expenses_refactored

    source = inspect.getsource(expenses_refactored)
    assert "from app.services.expense_service import" in source, (
        "expenses_refactored must delegate to app/services/expense_service.py — "
        "that file is where expense logic fixes belong"
    )