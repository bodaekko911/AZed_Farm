import asyncio
from app.app_factory import create_app
from fastapi.testclient import TestClient

app = create_app()
client = TestClient(app, raise_server_exceptions=False)

from app.core.security import get_current_user
from types import SimpleNamespace

async def override_get_current_user():
    return SimpleNamespace(id=1, name="Admin", role="admin", is_active=True, permissions=[])

app.dependency_overrides[get_current_user] = override_get_current_user

def test_it():
    # Let's hit the P&L api with specific dates
    resp = client.get("/reports/api/pl?date_from=2026-01-01&date_to=2026-01-01")
    print("Status:", resp.status_code)
    try:
        data = resp.json()
        print("Date from:", data.get("date_from"))
        print("Date to:", data.get("date_to"))
        print("Expense:", data.get("total_expense"))
        print("Expense lines:")
        for line in data.get("expense_lines", []):
            print(" -", line["name"], line["amount"])
            for e in line.get("entries", []):
                print("   *", e["date"], e["amount"])
    except Exception as e:
        print("Error parsing json:", e)
        print(resp.text)

if __name__ == "__main__":
    test_it()
