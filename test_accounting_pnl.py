import asyncio
from datetime import date
from sqlalchemy import select, or_, and_, func
from app.database import AsyncSessionLocal
from app.routers.accounting import _apply_period_with_fallback_date
from app.models.accounting import JournalEntry, Journal
from app.models.expense import Expense

async def main():
    async with AsyncSessionLocal() as db:
        # Check subquery logic
        from_date = date(2025, 1, 1)
        to_date = date(2025, 1, 31)
        
        stmt = select(
            JournalEntry.account_id.label("account_id"),
            func.coalesce(func.sum(JournalEntry.debit), 0).label("debit_sum"),
            func.coalesce(func.sum(JournalEntry.credit), 0).label("credit_sum"),
        ).select_from(JournalEntry).join(Journal, Journal.id == JournalEntry.journal_id).outerjoin(Expense, Expense.journal_id == Journal.id)
        
        stmt = _apply_period_with_fallback_date(
            stmt,
            Journal.created_at,
            from_date,
            to_date,
            Expense.expense_date,
        ).group_by(JournalEntry.account_id)
        
        res = await db.execute(stmt)
        print("Subquery rows for Jan 2025:", res.all())
        
        # April 2026
        stmt_apr = _apply_period_with_fallback_date(
            select(
                JournalEntry.account_id.label("account_id"),
                func.coalesce(func.sum(JournalEntry.debit), 0).label("debit_sum"),
                func.coalesce(func.sum(JournalEntry.credit), 0).label("credit_sum"),
            ).select_from(JournalEntry).join(Journal, Journal.id == JournalEntry.journal_id).outerjoin(Expense, Expense.journal_id == Journal.id),
            Journal.created_at,
            date(2026, 4, 1),
            date(2026, 4, 30),
            Expense.expense_date,
        ).group_by(JournalEntry.account_id)
        
        res_apr = await db.execute(stmt_apr)
        print("Subquery rows for April 2026:", res_apr.all())

asyncio.run(main())
