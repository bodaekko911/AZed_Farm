import asyncio
import io
from app.app_factory import create_app
from fastapi.testclient import TestClient
import openpyxl

app = create_app()
client = TestClient(app, raise_server_exceptions=False)

def create_mock_excel():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SKU", "Item", "QTY", "Price", "Discount", "Customer", "Date"])
    ws.append(["01234", "Item A", 1, 10.0, 0, "Ahmed", "2026-03-10"])
    ws.append(["1234", "Item A", 1, 10.0, 0, "Ahmed", "2026-03-11"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

from app.core.security import get_current_user
from types import SimpleNamespace

async def override_get_current_user():
    return SimpleNamespace(id=1, name="Admin", role="admin", is_active=True, permissions=[])

app.dependency_overrides[get_current_user] = override_get_current_user

def test_it():
    excel_bytes = create_mock_excel()
    files = {"file": ("test.xlsx", excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    data = {"dry_run": "true", "mode": "history_only", "force": "false"}
    
    resp = client.post("/import/api/sales", files=files, data=data)
    print(resp.status_code)
    print(resp.text)

if __name__ == "__main__":
    test_it()
