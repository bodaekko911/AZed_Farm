"""Farm intake list — date range + search filters on GET /farm/api/deliveries.

The filters must narrow BOTH the count and the page query, otherwise the
pagination ("1–50 of N") disagrees with the rows shown.
"""

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.routers.farm import get_deliveries


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
    """Answers the count with 0 and the page with no rows; keeps the SQL."""

    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(str(statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})))
        return FakeResult(0 if len(self.statements) == 1 else [])


def _run(**params):
    db = RecordingSession()
    out = asyncio.run(get_deliveries(db=db, **params))
    return out, db.statements


def test_no_filters_adds_no_conditions() -> None:
    out, (count_sql, page_sql) = _run()
    assert out == {"total": 0, "deliveries": []}
    assert "WHERE" not in count_sql


def test_date_range_applies_to_count_and_page() -> None:
    _, stmts = _run(date_from="2026-10-01", date_to="2026-10-24")
    for sql in stmts:
        assert "farm_deliveries.delivery_date >= '2026-10-01'" in sql
        assert "farm_deliveries.delivery_date <= '2026-10-24'" in sql


def test_search_matches_header_fields_and_line_products_and_plots() -> None:
    _, stmts = _run(q="  tomato ")
    for sql in stmts:
        assert "%tomato%" in sql and " tomato" not in sql   # trimmed
        assert "farm_deliveries.delivery_number ILIKE" in sql
        assert "farm_deliveries.received_by ILIKE" in sql
        assert "products.name ILIKE" in sql
        assert "products.sku ILIKE" in sql
        assert "farm_plots.name ILIKE" in sql


def test_blank_search_is_ignored() -> None:
    _, (count_sql, _) = _run(q="   ")
    assert "WHERE" not in count_sql


def test_bad_date_is_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        _run(date_from="24/10/2026")
    assert exc.value.status_code == 400
