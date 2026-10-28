import asyncio
from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.app_factory as app_factory
from app.app_factory import create_app
from app.core import security
from app.core.log import ActivityLog
from app.core.permission_catalog import KNOWN_PERMISSIONS, get_role_permissions
from app.database import get_async_session
from app.models.budget import MonthlyPlan
from app.services import budget_service


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def all(self):
        return self._value

    def scalar(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    def __init__(self, results=()):
        self._results = list(results)
        self.added, self.deleted, self.logged = [], [], []
        self.committed = False

    async def execute(self, _statement):
        return FakeResult(self._results.pop(0))

    def add(self, obj):
        (self.logged if isinstance(obj, ActivityLog) else self.added).append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        return None


def _client(user, fake_db=None) -> TestClient:
    fake_db = fake_db or FakeSession()

    async def override_session() -> AsyncGenerator[FakeSession, None]:
        yield fake_db

    async def override_user():
        return user

    async def noop() -> None:
        return None

    app_factory.configure_logging = lambda: None
    app_factory.configure_monitoring = lambda: None
    app_factory.verify_migration_status = noop

    app = create_app()
    app.dependency_overrides[get_async_session] = override_session
    app.dependency_overrides[security.get_current_user] = override_user
    return TestClient(app)


def _user(permissions: str, role: str = "cashier"):
    return SimpleNamespace(id=5, name="Planner", email="p@example.com", role=role, permissions=permissions,
                           is_active=True)


def test_parse_amount_accepts_blank_and_thousands_separators() -> None:
    assert budget_service.parse_amount("") is None
    assert budget_service.parse_amount(None) is None
    assert budget_service.parse_amount("1,500.5") == Decimal("1500.50")
    assert budget_service.parse_amount(2000) == Decimal("2000.00")


@pytest.mark.parametrize("value", ["-1", "abc", "NaN", "1e20"])
def test_parse_amount_rejects_bad_amounts(value) -> None:
    with pytest.raises(HTTPException) as exc:
        budget_service.parse_amount(value)
    assert exc.value.status_code == 400


def test_month_range_covers_the_whole_month() -> None:
    assert budget_service.month_range(2028, 2) == (date(2028, 2, 1), date(2028, 2, 29))
    assert budget_service.month_range(2026, 12) == (date(2026, 12, 1), date(2026, 12, 31))


def test_permissions_are_known_and_given_to_the_right_roles() -> None:
    assert {"page_budget", "action_budget_manage"} <= KNOWN_PERMISSIONS
    assert {"page_budget", "action_budget_manage"} <= get_role_permissions("accountant")
    assert "page_budget" in get_role_permissions("manager")
    assert "page_budget" in get_role_permissions("viewer")
    assert "action_budget_manage" not in get_role_permissions("viewer")
    assert "page_budget" not in get_role_permissions("cashier")


def test_save_year_creates_updates_clears_and_skips_unchanged() -> None:
    jan = MonthlyPlan(month=date(2026, 1, 1), budget=Decimal("100.00"), sales_target=Decimal("500.00"))
    feb = MonthlyPlan(month=date(2026, 2, 1), budget=Decimal("100.00"), sales_target=None)
    mar = MonthlyPlan(month=date(2026, 3, 1), budget=Decimal("100.00"), sales_target=None)
    db = FakeSession([[jan, feb, mar]])
    user = _user("page_budget,action_budget_manage")

    out = asyncio.run(budget_service.save_year(db, 2026, [
        {"month": 1, "budget": "100", "sales_target": "500"},      # unchanged
        {"month": 2, "budget": "", "sales_target": ""},            # cleared
        {"month": 3, "budget": "250", "sales_target": "1,000"},    # updated
        {"month": 4, "budget": "300", "sales_target": ""},         # new
    ], user))

    assert out["changed"] == [2, 3, 4]
    assert db.deleted == [feb]
    assert (mar.budget, mar.sales_target, mar.updated_by_user_id) == (Decimal("250.00"), Decimal("1000.00"), 5)
    [apr] = db.added
    assert (apr.month, apr.budget, apr.sales_target) == (date(2026, 4, 1), Decimal("300.00"), None)
    assert db.committed and db.logged[0].module == "Budget"


def test_save_year_with_nothing_changed_does_not_commit() -> None:
    jan = MonthlyPlan(month=date(2026, 1, 1), budget=Decimal("100.00"), sales_target=None)
    db = FakeSession([[jan]])
    out = asyncio.run(budget_service.save_year(db, 2026, [{"month": 1, "budget": "100"}], _user("")))
    assert out["changed"] == [] and not db.committed and not db.logged


def test_save_year_rejects_a_bad_month() -> None:
    with pytest.raises(HTTPException):
        asyncio.run(budget_service.save_year(FakeSession(), 2026, [{"month": 13, "budget": "1"}], _user("")))


def test_budget_page_needs_page_permission() -> None:
    response = _client(_user("page_pos")).get("/budget/api/current")
    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: page_budget"


def test_setting_numbers_needs_manage_permission() -> None:
    response = _client(_user("page_budget")).put("/budget/api/year/2026", json={"months": []})
    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: action_budget_manage"


def test_budget_page_renders_with_menu_link() -> None:
    response = _client(_user("page_budget")).get("/budget/")
    assert response.status_code == 200
    assert "Budget &amp; targets" in response.text
    assert 'href="/budget/"' in response.text
    assert "/static/budget.js" in response.text


def test_current_month_reports_plan_and_actuals(monkeypatch) -> None:
    monkeypatch.setattr(budget_service, "today_local", lambda: date(2026, 10, 15))

    async def sales(_db, start, end):
        assert (start, end) == (date(2026, 10, 1), date(2026, 10, 15))
        return 4000.0

    async def spent(_db, start, end):
        return 900.0

    monkeypatch.setattr(budget_service, "sales_between", sales)
    monkeypatch.setattr(budget_service, "spent_between", spent)
    plan = MonthlyPlan(month=date(2026, 10, 1), budget=Decimal("2000.00"), sales_target=Decimal("10000.00"))
    out = asyncio.run(budget_service.current_month(FakeSession([plan])))
    assert out == {"year": 2026, "month": 10, "day": 15, "days_in_month": 31, "budget": 2000.0,
                   "sales_target": 10000.0, "sales": 4000.0, "spent": 900.0}
