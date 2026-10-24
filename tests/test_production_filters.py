"""Production page filters — batches (dates / recipe / search), spoilage
search, and the per-recipe usage counts the Recipes tab sorts by.

The filters must narrow BOTH the count and the page query, otherwise the
pagination ("1-20 of N") disagrees with the rows shown.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.routers import production


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class RecordingSession:
    """Replies with the queued results in order; keeps the SQL it was sent."""

    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(_sql(statement))
        return FakeResult(self._results.pop(0))


def _batches(**params):
    db = RecordingSession([0, []])
    asyncio.run(production.get_batches(db=db, **{"batch_type": "all", **params}))
    return db.statements


def test_batches_without_filters_have_no_where() -> None:
    count_sql, _ = _batches()
    assert "WHERE" not in count_sql


def test_batch_filters_apply_to_count_and_page() -> None:
    stmts = _batches(batch_type="processing", date_from="2026-10-01", date_to="2026-10-24",
                     recipe_id=7, q=" moringa ")
    for sql in stmts:
        assert "production_batches.batch_number NOT LIKE 'PKG%%'" in sql
        assert "production_batches.created_at >=" in sql
        assert "production_batches.created_at <=" in sql
        assert "production_batches.recipe_id = 7" in sql
        assert "%moringa%" in sql and " moringa" not in sql
        assert "recipes.name ILIKE" in sql
        assert "products.name ILIKE" in sql
        assert "production_batches.notes ILIKE" in sql


def test_batch_dates_cover_whole_local_days(monkeypatch) -> None:
    seen = []
    real = production.utc_bounds
    monkeypatch.setattr(production, "utc_bounds", lambda a, b: seen.append((a, b)) or real(a, b))
    _batches(date_from="2026-10-01", date_to="2026-10-03")
    assert [(a.isoformat(), b.isoformat()) for a, b in seen] == [
        ("2026-10-01", "2026-10-01"), ("2026-10-03", "2026-10-03")]


def test_bad_batch_date_is_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        _batches(date_to="24/10/2026")
    assert exc.value.status_code == 400


def test_spoilage_search_narrows_count_and_rows(monkeypatch) -> None:
    async def no_summary(db, d_from, d_to):
        return {}
    monkeypatch.setattr(production, "spoilage_summary", no_summary)
    db = RecordingSession([0, []])
    asyncio.run(production.get_spoilage(date_from="2026-10-01", date_to="2026-10-24", q="mold", db=db))
    for sql in db.statements:
        assert "spoilage_records.reason ILIKE '%%mold%%'" in sql
        assert "products.name ILIKE" in sql
        assert "farms.name ILIKE" in sql


def test_recipes_carry_usage_counts() -> None:
    product = SimpleNamespace(name="Moringa leaves", unit="kg")
    used = SimpleNamespace(id=1, name="Powder", description=None, created_at=None,
                           inputs=[SimpleNamespace(product_id=3, product=product, qty=10)],
                           outputs=[SimpleNamespace(product_id=4, product=product, qty=1)])
    unused = SimpleNamespace(id=2, name="Tea", description="", created_at=None, inputs=[], outputs=[])
    last = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
    db = RecordingSession([[used, unused], [(1, 12, last)]])
    out = asyncio.run(production.get_recipes(db=db))
    assert "GROUP BY production_batches.recipe_id" in db.statements[1]
    assert (out[0]["batch_count"], out[0]["last_used"]) == (12, last.isoformat())
    assert (out[1]["batch_count"], out[1]["last_used"]) == (0, None)
