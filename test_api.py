import asyncio
import httpx
from datetime import date
from app.database import async_session_maker
from app.models.accounting import Account, Journal, JournalEntry
from app.models.expense import Expense

async def main():
    async with async_session_maker() as db:
        # We assume there are expenses in DB already, maybe we can just query the endpoint
        pass

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000") as client:
        # Call without dates
        res_all = await client.get("/accounting/api/profit-loss")
        print("All time expenses:")
        print(res_all.json()["expenses"])

        # Call with narrow dates
        res_narrow = await client.get("/accounting/api/profit-loss", params={"from_date": "2026-05-10", "to_date": "2026-05-10"})
        print("\nFiltered expenses (2026-05-10):")
        print(res_narrow.json()["expenses"])

if __name__ == "__main__":
    asyncio.run(main())
