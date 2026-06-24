"""
Tests for the per-endpoint permission lockdown applied across the app.

For each route hardened in this pass, we verify that a user who has the
page-view permission but NOT the action permission gets a 403 with the
correct permission key in the detail, and a PERMISSION_DENIED audit row.
"""
from collections.abc import AsyncGenerator
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.env_defaults import apply_test_environment_defaults
apply_test_environment_defaults()

import app.app_factory as app_factory
from app.app_factory import create_app
from app.core import security
from app.database import get_async_session


class FakeSession:
    def __init__(self) -> None:
        self.logged = []

    def add(self, obj) -> None:
        self.logged.append(obj)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _make_client(user) -> tuple[TestClient, FakeSession]:
    fake_db = FakeSession()

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
    return TestClient(app), fake_db


def _viewer_with(page_perm: str, *extra: str):
    return SimpleNamespace(
        id=99,
        name="Permission-Locked Viewer",
        role="viewer",
        permissions=",".join([page_perm, *extra]),
        is_active=True,
    )


def _denied(response, expected_key: str, fake_db: FakeSession) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == f"Permission denied: {expected_key}"
    assert any(
        getattr(log, "action", None) == "PERMISSION_DENIED"
        and getattr(log, "ref_id", None) == expected_key
        for log in fake_db.logged
    ), f"Missing PERMISSION_DENIED audit row for {expected_key}"


# ── Customers ─────────────────────────────────────────────────────────────────
def test_customer_create_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_customers"))
    r = client.post("/customers-mgmt/api/add", json={"name": "ACME"})
    _denied(r, "action_customers_create", db)


def test_customer_update_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_customers"))
    r = client.put("/customers-mgmt/api/edit/1", json={"name": "Updated"})
    _denied(r, "action_customers_update", db)


def test_customer_delete_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_customers"))
    r = client.delete("/customers-mgmt/api/delete/1")
    _denied(r, "action_customers_delete", db)


# ── Products ──────────────────────────────────────────────────────────────────
def test_customer_export_requires_excel_permission() -> None:
    client, db = _make_client(_viewer_with("page_customers"))
    r = client.get("/customers-mgmt/api/export.xlsx")
    _denied(r, "action_export_excel", db)


def test_product_create_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_products"))
    r = client.post("/products/api/add", json={
        "sku": "X-1", "name": "Test", "category": "Misc", "price": 10, "cost": 5, "unit": "kg",
    })
    _denied(r, "action_products_create", db)


# ── Suppliers ─────────────────────────────────────────────────────────────────
def test_supplier_create_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_suppliers"))
    r = client.post("/suppliers/api/add", json={"name": "Vendor"})
    _denied(r, "action_suppliers_create", db)


def test_supplier_update_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_suppliers"))
    r = client.put("/suppliers/api/edit/1", json={"name": "Renamed"})
    _denied(r, "action_suppliers_update", db)


def test_supplier_delete_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_suppliers"))
    r = client.delete("/suppliers/api/delete/1")
    _denied(r, "action_suppliers_delete", db)


def test_supplier_pay_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_suppliers"))
    r = client.post("/suppliers/api/1/pay", json={
        "amount": 100, "payment_date": "2026-05-01", "payment_method": "cash",
    })
    _denied(r, "action_suppliers_pay", db)


def test_supplier_purchase_create_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_suppliers"))
    r = client.post("/suppliers/api/purchase/create", json={
        "supplier_id": 1, "items": [{"product_id": 1, "qty": 1, "unit_cost": 5}],
    })
    _denied(r, "action_suppliers_purchase_create", db)


# ── Production ────────────────────────────────────────────────────────────────
def test_production_create_recipe_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_production"))
    r = client.post("/production/api/recipes", json={
        "name": "R", "inputs": [{"product_id": 1, "qty": 1}], "outputs": [{"product_id": 2, "qty": 1}],
    })
    _denied(r, "action_production_manage_recipes", db)


def test_production_create_batch_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_production"))
    r = client.post("/production/api/batches", json={
        "inputs": [{"product_id": 1, "qty": 1}], "outputs": [{"product_id": 2, "qty": 1}],
    })
    _denied(r, "action_production_create_batch", db)


def test_production_delete_batch_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_production"))
    r = client.delete("/production/api/batches/1")
    _denied(r, "action_production_delete_batch", db)


def test_production_log_spoilage_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_production"))
    r = client.post("/production/api/spoilage", json={
        "product_id": 1, "qty": 1, "spoilage_date": "2026-05-01",
    })
    _denied(r, "action_production_log_spoilage", db)


# ── HR ────────────────────────────────────────────────────────────────────────
def test_hr_employee_create_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_hr", "tab_hr_employees"))
    r = client.post("/hr/api/employees", json={"name": "Alice", "base_salary": 1000})
    _denied(r, "action_hr_manage_employees", db)


def test_hr_employee_delete_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_hr", "tab_hr_employees"))
    r = client.delete("/hr/api/employees/1")
    _denied(r, "action_hr_manage_employees", db)


def test_hr_log_attendance_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_hr", "tab_hr_attendance"))
    r = client.post("/hr/api/attendance", json={
        "employee_id": 1, "date": "2026-05-01", "status": "present",
    })
    _denied(r, "action_hr_log_attendance", db)


def test_hr_allowance_advance_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_hr", "tab_hr_employees"))
    r = client.post("/hr/api/employees/1/allowance-advances", json={
        "advance_date": "2026-05-01", "amount": 100,
    })
    _denied(r, "action_hr_manage_allowances", db)


# ── Imports ───────────────────────────────────────────────────────────────────
def test_import_preview_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_import"))
    r = client.post("/import/api/preview", files={"file": ("x.xlsx", b"x", "application/octet-stream")})
    _denied(r, "action_import_preview", db)


def test_import_products_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_import"))
    r = client.post("/import/api/products", files={"file": ("x.xlsx", b"x", "application/octet-stream")})
    _denied(r, "action_import_products", db)


def test_import_sales_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_import"))
    r = client.post(
        "/import/api/sales",
        files={"file": ("x.xlsx", b"x", "application/octet-stream")},
        data={"dry_run": "true"},
    )
    _denied(r, "action_import_sales", db)


def test_import_delete_batch_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_import"))
    r = client.delete("/import/api/sales/batch/abc")
    _denied(r, "action_import_delete_batch", db)


# ── B2B / Accounting (money) ──────────────────────────────────────────────────
def test_b2b_collect_via_accounting_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_accounting", "tab_accounting_b2b"))
    r = client.post("/accounting/api/b2b-invoices/1/collect", json={"amount": 100})
    _denied(r, "action_b2b_collect", db)


def test_b2b_client_refund_via_accounting_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_accounting", "tab_accounting_b2b"))
    r = client.post("/accounting/api/b2b-clients/1/refund", json={"amount": 100})
    _denied(r, "action_b2b_refund", db)


def test_b2b_consignment_settle_via_accounting_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_accounting", "tab_accounting_b2b"))
    r = client.post("/accounting/api/b2b-clients/1/consignment-payment", json={"amount": 100})
    _denied(r, "action_b2b_consignment_settle", db)


# ── Carbon ────────────────────────────────────────────────────────────────────
def test_carbon_create_log_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_carbon"))
    r = client.post("/carbon/api/logs", json={
        "factor_id": 1, "log_date": "2026-05-01", "quantity": 10,
    })
    _denied(r, "action_carbon_log", db)


def test_carbon_create_factor_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_carbon"))
    r = client.post("/carbon/api/factors", json={
        "source_type": "fuel", "source_key": "diesel", "label": "Diesel",
        "factor_kg_co2e_per_unit": 2.68, "unit": "litre",
    })
    _denied(r, "action_carbon_factors", db)


# ── Farm ──────────────────────────────────────────────────────────────────────
def test_farm_create_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_farm"))
    r = client.post("/farm/api/farms?name=NewFarm&location=Test")
    _denied(r, "action_farm_create", db)


# ── Inventory ─────────────────────────────────────────────────────────────────
def test_inventory_draft_purchases_requires_explicit_permission() -> None:
    client, db = _make_client(_viewer_with("page_inventory"))
    r = client.post("/inventory/api/low-stock/draft-purchases", json={"product_ids": [1, 2]})
    _denied(r, "action_suppliers_purchase_create", db)


# ── Audit log UI must require admin ───────────────────────────────────────────
def test_audit_log_ui_requires_admin() -> None:
    user = SimpleNamespace(id=2, name="Viewer", role="viewer", permissions="page_dashboard", is_active=True)
    client, _ = _make_client(user)
    r = client.get("/audit-log/")
    assert r.status_code == 403


# ── B2B client-price endpoints ────────────────────────────────────────────────
def test_b2b_client_price_upsert_requires_update_permission() -> None:
    client, db = _make_client(_viewer_with("page_b2b", "tab_b2b_clients"))
    r = client.put("/b2b/api/clients/1/prices", json={"product_id": 1, "price": 10})
    _denied(r, "action_b2b_clients_update", db)


def test_b2b_client_price_delete_requires_update_permission() -> None:
    client, db = _make_client(_viewer_with("page_b2b", "tab_b2b_clients"))
    r = client.delete("/b2b/api/clients/1/prices/1")
    _denied(r, "action_b2b_clients_update", db)
