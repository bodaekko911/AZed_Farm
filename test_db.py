import asyncio
from datetime import date
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.routers.accounting import profit_loss

async def main():
    async with AsyncSessionLocal() as db:
        res_all = await profit_loss(None, None, db)
        print("All time expenses:", res_all["total_expense"])
        print(res_all["expenses"])

        res_narrow = await profit_loss(date(2025, 1, 1), date(2025, 1, 1), db)
        print("\nFiltered expenses (2025-01-01):", res_narrow["total_expense"])
        print(res_narrow["expenses"])
        
        res_narrow2 = await profit_loss(date(2026, 5, 10), date(2026, 5, 10), db)
        print("\nFiltered expenses (2026-05-10):", res_narrow2["total_expense"])
        print(res_narrow2["expenses"])

if __name__ == "__main__":
    asyncio.run(main())
