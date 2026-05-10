import asyncio
from app.database import AsyncSessionLocal
from sqlalchemy import select
from app.models.accounting import JournalEntry, Journal, Account

async def main():
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(JournalEntry, Journal, Account)
            .join(Journal, Journal.id == JournalEntry.journal_id)
            .join(Account, Account.id == JournalEntry.account_id)
            .where(Account.type == "expense")
        )
        for je, j, a in res.all():
            print(f"Expense {a.name} ({a.code}): {je.debit} at {j.created_at}")

if __name__ == "__main__":
    asyncio.run(main())
