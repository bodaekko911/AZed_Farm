import asyncio
import io
import re
from decimal import Decimal
from types import SimpleNamespace

import openpyxl

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.routers.products import _product_filters, export_products_excel, products_ui


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ExportSession:
    """Captures the statement the export builds and hands back fixed rows."""

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return _Result(self.rows)


def _product(**overrides):
    base = dict(
        sku="SKU-1",
        name="Dried Mango",
        price=Decimal("48.500"),
        cost=Decimal("21.250"),
        stock=Decimal("12.000"),
        min_stock=Decimal("5.000"),
        reorder_level=None,
        reorder_qty=None,
        unit="kg",
        unit_weight_kg=None,
        category="Dried",
        item_type="finished",
        preferred_supplier_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _drain(response):
    return b"".join([chunk async for chunk in response.body_iterator])


def _export(rows):
    session = _ExportSession(rows)

    async def run():
        response = await export_products_excel(db=session)
        return response, await _drain(response)

    response, body = asyncio.run(run())
    workbook = openpyxl.load_workbook(io.BytesIO(body), data_only=True)
    return response, session, workbook["Products"]


def test_export_writes_the_catalogue_columns() -> None:
    response, _session, sheet = _export([(_product(), "Delta Farms")])

    assert [cell.value for cell in sheet[1]] == [
        "SKU",
        "Name",
        "Category",
        "Type",
        "Price",
        "Cost",
        "Stock",
        "Unit",
        "Unit Weight (kg)",
        "Min Stock",
        "Reorder Level",
        "Reorder Qty",
        "Preferred Supplier",
        "Low Stock",
    ]
    assert [cell.value for cell in sheet[2]] == [
        "SKU-1",
        "Dried Mango",
        "Dried",
        "Finished Product",
        48.5,
        21.25,
        12.0,
        "kg",
        None,
        5.0,
        None,
        None,
        "Delta Farms",
        "No",
    ]
    assert response.media_type == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert re.match(
        r'attachment; filename=products_\d{4}-\d{2}-\d{2}\.xlsx',
        response.headers["Content-Disposition"],
    )


def test_export_flags_low_stock_and_blanks_service_stock() -> None:
    rows = [
        (_product(sku="LOW-1", stock=Decimal("2.000"), reorder_level=Decimal("8.000")), None),
        (_product(sku="SRV-1", item_type="service", stock=Decimal("0.000")), None),
    ]
    _response, _session, sheet = _export(rows)

    low_row = [cell.value for cell in sheet[2]]
    service_row = [cell.value for cell in sheet[3]]

    assert low_row[10] == 8.0 and low_row[13] == "Yes"
    # Services carry no stock, so the column stays empty rather than reading 0.
    assert service_row[3] == "Service"
    assert service_row[6] is None
    assert service_row[13] == "No"


def test_export_applies_the_same_filters_as_the_list() -> None:
    session = _ExportSession([])
    asyncio.run(
        export_products_excel(q="mango", category="Dried", item_type="raw", db=session)
    )
    sql = session.statements[0]

    assert "lower(products.name) LIKE lower" in sql
    assert "products.category =" in sql
    assert "products.item_type =" in sql
    # A left join keeps products that have no preferred supplier.
    assert "LEFT OUTER JOIN suppliers" in sql


def test_low_stock_filter_is_shared_with_the_list_endpoint() -> None:
    plain = [str(c) for c in _product_filters("", False, "", "")]
    filtered = [str(c) for c in _product_filters("", True, "", "")]

    assert len(filtered) == len(plain) + 2
    assert any("products.stock <=" in c for c in filtered)


def test_products_page_offers_an_excel_export() -> None:
    user = SimpleNamespace(
        id=1,
        name="Admin",
        email="admin@example.com",
        role="admin",
        permissions="*",
        is_active=True,
    )

    html = products_ui(user)

    assert 'id="export-btn"' in html
    assert "exportProductsXLSX()" in html
    assert "/products/api/export.xlsx" in html
    assert 'hasPermission("action_export_excel", u)' in html
