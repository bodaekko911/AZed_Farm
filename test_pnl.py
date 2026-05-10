import asyncio
from datetime import date
from sqlalchemy import select, func, or_, and_
from sqlalchemy.orm import declarative_base

from app.models.accounting import Journal, JournalEntry
from app.models.expense import Expense
from app.routers.accounting import _apply_period_with_fallback_date

stmt = select(
    JournalEntry.account_id.label("account_id"),
    func.coalesce(func.sum(JournalEntry.debit), 0).label("debit_sum"),
    func.coalesce(func.sum(JournalEntry.credit), 0).label("credit_sum"),
).select_from(JournalEntry).join(Journal, Journal.id == JournalEntry.journal_id).outerjoin(Expense, Expense.journal_id == Journal.id)

stmt = _apply_period_with_fallback_date(
    stmt,
    Journal.created_at,
    date(2026, 1, 1),
    date(2026, 1, 31),
    Expense.expense_date
).group_by(JournalEntry.account_id)

print(stmt.compile(compile_kwargs={"literal_binds": True}))
