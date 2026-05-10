import asyncio
from datetime import date
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.routers.reports import pl_report

async def main():
    async with AsyncSessionLocal() as db:
        res_april = await pl_report("2026-04-01", "2026-04-30", db)
        print("April expenses:", res_april["total_expense"])
        print(res_april["expense_lines"])

        res_may = await pl_report("2026-05-01", "2026-05-10", db)
        print("\nMay expenses:", res_may["total_expense"])
        print(res_may["expense_lines"])

if __name__ == "__main__":
    asyncio.run(main())
