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
from app.services import budget_service, sales_target_service


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
    assert {"page_budget", "action_budget_manage", "page_sales_targets", "action_sales_targets_manage"} <= KNOWN_PERMISSIONS
    assert {"page_budget", "action_budget_manage", "page_sales_targets"} <= get_role_permissions("accountant")
    assert {"page_budget", "page_sales_targets", "action_sales_targets_manage"} <= get_role_permissions("manager")
    assert {"page_budget", "page_sales_targets"} <= get_role_permissions("viewer")
    assert not {"action_budget_manage", "action_sales_targets_manage"} & get_role_permissions("viewer")
    assert not {"page_budget", "page_sales_targets"} & get_role_permissions("cashier")


def test_saving_the_budget_leaves_the_sales_target_alone() -> None:
    jan = MonthlyPlan(month=date(2026, 1, 1), budget=Decimal("100.00"), sales_target=Decimal("500.00"),
                      sales_floor=Decimal("425.00"), sales_stretch=Decimal("600.00"))
    feb = MonthlyPlan(month=date(2026, 2, 1), budget=Decimal("100.00"))
    mar = MonthlyPlan(month=date(2026, 3, 1), budget=Decimal("100.00"))
    db = FakeSession([[jan, feb, mar]])

    out = asyncio.run(budget_service.save_year(db, 2026, [
        {"month": 1, "budget": ""},          # budget cleared, the target stays: the row stays
        {"month": 2, "budget": ""},          # nothing left on the row: removed
        {"month": 3, "budget": "250"},       # updated
        {"month": 4, "budget": "300"},       # new
        {"month": 5, "budget": ""},          # nothing, no row: skipped
    ], _user("page_budget,action_budget_manage")))

    assert out["changed"] == [1, 2, 3, 4]
    assert (jan.budget, jan.sales_target) == (None, Decimal("500.00"))
    assert db.deleted == [feb]
    assert (mar.budget, mar.updated_by_user_id) == (Decimal("250.00"), 5)
    [apr] = db.added
    assert (apr.month, apr.budget, apr.sales_target) == (date(2026, 4, 1), Decimal("300.00"), None)
    assert db.committed and db.logged[0].module == "Budget"


def test_save_with_nothing_changed_does_not_commit() -> None:
    jan = MonthlyPlan(month=date(2026, 1, 1), budget=Decimal("100.00"))
    db = FakeSession([[jan]])
    out = asyncio.run(budget_service.save_year(db, 2026, [{"month": 1, "budget": "100"}], _user("")))
    assert out["changed"] == [] and not db.committed and not db.logged


def test_save_rejects_a_bad_month() -> None:
    with pytest.raises(HTTPException):
        asyncio.run(budget_service.save_year(FakeSession(), 2026, [{"month": 13, "budget": "1"}], _user("")))


def test_a_target_gets_a_floor_of_85_and_a_stretch_of_120_percent() -> None:
    assert sales_target_service.levels(Decimal("10000.00")) == (Decimal("8500.00"), Decimal("10000.00"),
                                                                Decimal("12000.00"))
    assert sales_target_service.levels(Decimal("333.33")) == (Decimal("283.33"), Decimal("333.33"), Decimal("400.00"))
    assert sales_target_service.levels(None, Decimal("5"), Decimal("9")) == (None, None, None)


def test_a_floor_or_stretch_typed_by_hand_is_kept_but_must_fit_around_the_target() -> None:
    assert sales_target_service.levels(Decimal("100"), Decimal("90"), None) == (Decimal("90"), Decimal("100"),
                                                                               Decimal("120.00"))
    with pytest.raises(HTTPException):
        sales_target_service.levels(Decimal("100"), Decimal("101"), None)
    with pytest.raises(HTTPException):
        sales_target_service.levels(Decimal("100"), None, Decimal("99"))


def test_saving_targets_fills_levels_and_leaves_the_budget_alone() -> None:
    jan = MonthlyPlan(month=date(2026, 1, 1), budget=Decimal("100.00"), sales_target=Decimal("500.00"),
                      sales_floor=Decimal("425.00"), sales_stretch=Decimal("600.00"))
    db = FakeSession([[jan]])

    out = asyncio.run(sales_target_service.save_year(db, 2026, [
        {"month": 1, "sales_target": "", "sales_floor": "425", "sales_stretch": "600"},   # cleared with its levels
        {"month": 2, "sales_target": "20,000"},                                              # new, levels filled in
    ], _user("page_sales_targets,action_sales_targets_manage")))

    assert out["changed"] == [1, 2]
    assert (jan.budget, jan.sales_floor, jan.sales_target, jan.sales_stretch) == (Decimal("100.00"), None, None, None)
    assert not db.deleted
    [feb] = db.added
    assert (feb.sales_floor, feb.sales_target, feb.sales_stretch, feb.budget) == (
        Decimal("17000.00"), Decimal("20000.00"), Decimal("24000.00"), None)
    assert db.logged[0].module == "Sales targets"


def test_budget_page_needs_page_permission() -> None:
    response = _client(_user("page_pos")).get("/budget/api/current")
    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: page_budget"


def test_sales_targets_page_needs_its_own_permission() -> None:
    response = _client(_user("page_budget")).get("/sales-targets/api/current")
    assert response.status_code == 403
    assert response.json()["detail"] == "Permission denied: page_sales_targets"


def test_setting_numbers_needs_manage_permission() -> None:
    response = _client(_user("page_budget")).put("/budget/api/year/2026", json={"months": []})
    assert response.json()["detail"] == "Permission denied: action_budget_manage"
    response = _client(_user("page_sales_targets")).put("/sales-targets/api/year/2026", json={"months": []})
    assert response.json()["detail"] == "Permission denied: action_sales_targets_manage"


@pytest.mark.parametrize("url,perm,title,script", [
    ("/budget/", "page_budget", "<title>Budget", "/static/budget.js"),
    ("/sales-targets/", "page_sales_targets", "<title>Sales targets", "/static/sales-targets.js"),
])
def test_pages_render_with_menu_link(url, perm, title, script) -> None:
    response = _client(_user(perm)).get(url)
    assert response.status_code == 200
    assert title in response.text
    assert f'href="{url}"' in response.text
    assert "/static/plans.js" in response.text and script in response.text


def test_current_month_reports_target_levels_and_sales(monkeypatch) -> None:
    monkeypatch.setattr(sales_target_service, "today_local", lambda: date(2026, 10, 15))

    async def sales(_db, start, end):
        assert (start, end) == (date(2026, 10, 1), date(2026, 10, 15))
        return 4000.0

    monkeypatch.setattr(sales_target_service, "sales_between", sales)
    plan = MonthlyPlan(month=date(2026, 10, 1), budget=Decimal("2000.00"), sales_target=Decimal("10000.00"),
                       sales_floor=Decimal("8500.00"), sales_stretch=Decimal("12000.00"))
    out = asyncio.run(sales_target_service.current_month(FakeSession([plan])))
    assert out == {"year": 2026, "month": 10, "day": 15, "days_in_month": 31, "sales_floor": 8500.0,
                   "sales_target": 10000.0, "sales_stretch": 12000.0, "sales": 4000.0}
