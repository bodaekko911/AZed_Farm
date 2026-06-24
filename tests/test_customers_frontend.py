import re
import io
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import openpyxl

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.routers.customers import _customer_export_buffer, customers_ui


def test_customers_page_keeps_csv_newline_escaped_for_browser_js() -> None:
    user = SimpleNamespace(
        id=1,
        name="Admin",
        email="admin@example.com",
        role="admin",
        permissions="*",
        is_active=True,
    )

    html = customers_ui(user)
    scripts = "\n".join(
        re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.IGNORECASE | re.DOTALL)
    )

    assert 'join("\\n")' in scripts
    assert 'includes("\\n")' in scripts
    assert 'join("\n")' not in scripts
    assert 'includes("\n")' not in scripts


def test_customers_page_exports_xlsx_button_for_excel_permission() -> None:
    user = SimpleNamespace(
        id=1,
        name="Admin",
        email="admin@example.com",
        role="admin",
        permissions="*",
        is_active=True,
    )

    html = customers_ui(user)

    assert "exportXLSX()" in html
    assert "Export Excel" in html


def test_customer_export_workbook_uses_clean_typed_data() -> None:
    rows = [
        SimpleNamespace(
            id=7,
            name="  Nile   Market  ",
            phone=None,
            email=" sales@example.com ",
            address=" Cairo\nNasr City ",
            discount_pct=Decimal("12.50"),
            created_at=datetime(2026, 6, 1, 9, 30),
            inv_count=3,
            inv_total=Decimal("250.75"),
            ref_total=Decimal("10.25"),
        )
    ]

    buf = _customer_export_buffer(rows, title="Customers Export", filter_label="All customers")
    workbook = openpyxl.load_workbook(io.BytesIO(buf.getvalue()), data_only=True)
    sheet = workbook["Customers"]

    assert [cell.value for cell in sheet[5]] == [
        "ID",
        "Customer Name",
        "Phone",
        "Email",
        "Address",
        "Discount %",
        "Invoices",
        "Paid Sales",
        "Refunds",
        "Net Spent",
        "Created",
    ]
    assert sheet["B6"].value == "Nile Market"
    assert sheet["C6"].value is None
    assert sheet["D6"].value == "sales@example.com"
    assert sheet["E6"].value == "Cairo Nasr City"
    assert sheet["F6"].value == 12.5
    assert sheet["G6"].value == 3
    assert sheet["J6"].value == 240.5
    assert sheet["K6"].value.date().isoformat() == "2026-06-01"
