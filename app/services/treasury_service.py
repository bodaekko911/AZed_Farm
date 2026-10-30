"""Treasury — the organization's money: the bank account and the cash box.

Money already moves through the journal: a POS sale debits a money account, an expense or a loan credits one, a
supplier payment credits one. This module decides WHICH money account a payment method uses (cash → the cash box,
anything else → the bank), and reads the journal back as balances, money in / money out and a statement.

The only movements posted here are the ones no other page owns: the opening balance of an account and transfers
between the cash box and the bank. Both are ordinary journals (``treasury_opening`` / ``treasury_transfer``), so the
trial balance and P&L see them like everything else; neither touches revenue or expenses.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.log import record
from app.core.time_utils import app_tz, to_app_tz, today_local, utc_bounds
from app.models.accounting import Account, Journal, JournalEntry
from app.models.user import User

CASH_CODE = "1000"
BANK_CODE = "1010"
EQUITY_CODE = "3000"   # the other side of an opening balance
MONEY_ACCOUNTS = {CASH_CODE: "Cash", BANK_CODE: "Bank"}
MONEY_ACCOUNT_CODES = tuple(MONEY_ACCOUNTS)

# Payment methods that move money through the bank. Everything else (cash, or no method) is the cash box.
BANK_METHODS = {"bank", "bank_transfer", "transfer", "card", "visa", "instapay", "cheque", "check"}

OPENING_REF = "treasury_opening"
TRANSFER_REF = "treasury_transfer"
MANUAL_REFS = (OPENING_REF, TRANSFER_REF)

_MONEY = Decimal("0.01")
MAX_AMOUNT = Decimal("999999999999.99")


def money_account_code(payment_method: Optional[str]) -> str:
    """The money account a payment method moves: the bank for transfers/cards, the cash box otherwise."""
    return BANK_CODE if (payment_method or "").strip().lower() in BANK_METHODS else CASH_CODE


def source_of(ref_type: Optional[str], description: Optional[str]) -> str:
    """A plain-words label for where a movement came from, for grouping money in / money out."""
    ref = (ref_type or "").lower()
    desc = (description or "").lower()
    if ref in ("invoice", "payment"):
        return "POS sales"
    if ref == "retail_refund":
        return "POS refunds"
    if ref.startswith("b2b_refund") or (ref.endswith("_reversal") and ref.startswith(("b2b", "consignment"))):
        return "B2B refunds & reversals"
    if ref.startswith(("b2b", "consignment")):
        return "B2B sales"
    if ref == "expense":
        if desc.startswith("salaries & wages expense"):
            return "Payroll"
        if desc.startswith("salary advance"):
            return "Employee loans"
        if desc.startswith("allowance advance"):
            return "Allowance advances"
        return "Expenses"
    if ref == "expense_reversal":
        return "Expense reversals"
    if ref == "supplier_payment":
        return "Supplier payments"
    if ref == OPENING_REF:
        return "Opening balance"
    if ref == TRANSFER_REF:
        return "Transfers"
    if ref in ("manual", "journal"):
        return "Manual journals"
    return (ref.replace("_", " ").capitalize() or "Other")


async def ensure_money_accounts(db: AsyncSession) -> dict[str, Account]:
    """The cash box and bank accounts, created if this database doesn't have them yet."""
    rows = (await db.execute(select(Account).where(Account.code.in_(MONEY_ACCOUNT_CODES)))).scalars().all()
    found = {a.code: a for a in rows}
    for code, name in MONEY_ACCOUNTS.items():
        if code not in found:
            found[code] = Account(code=code, name=name, type="asset", balance=Decimal("0"))
            db.add(found[code])
    await db.flush()
    return found


async def _ensure_account(db: AsyncSession, code: str, name: str, account_type: str) -> Account:
    account = (await db.execute(select(Account).where(Account.code == code))).scalar_one_or_none()
    if account is None:
        account = Account(code=code, name=name, type=account_type, balance=Decimal("0"))
        db.add(account)
        await db.flush()
    return account


async def credited_money_code(db: AsyncSession, journal_id: Optional[int], fallback: str) -> str:
    """The account an existing journal took the money from — so its reversal puts it back in the same place,
    even for entries posted before payments were split between the cash box and the bank."""
    if journal_id:
        code = (await db.execute(
            select(Account.code)
            .join(JournalEntry, JournalEntry.account_id == Account.id)
            .where(JournalEntry.journal_id == journal_id, JournalEntry.credit > 0)
            .order_by(JournalEntry.id)
        )).scalars().first()
        if code:
            return code
    return fallback


async def latest_received_money_code(
    db: AsyncSession, ref_types: Iterable[str], ref_id: int, fallback: str = CASH_CODE,
) -> str:
    """The money account the most recent payment of a document went into (for reversing that payment)."""
    code = (await db.execute(
        select(Account.code)
        .join(JournalEntry, JournalEntry.account_id == Account.id)
        .join(Journal, JournalEntry.journal_id == Journal.id)
        .where(
            Journal.ref_type.in_(list(ref_types)),
            Journal.ref_id == ref_id,
            Account.code.in_(MONEY_ACCOUNT_CODES),
            JournalEntry.debit > 0,
        )
        .order_by(Journal.created_at.desc(), Journal.id.desc())
    )).scalars().first()
    return code or fallback


# ── reading ────────────────────────────────────────────────────────────────

def _num(value) -> float:
    return round(float(value or 0), 2)


def _period(from_date: Optional[date], to_date: Optional[date]) -> tuple[date, date]:
    to_date = to_date or today_local()
    from_date = from_date or to_date.replace(day=1)
    if from_date > to_date:
        raise HTTPException(status_code=400, detail="The start date must be on or before the end date.")
    return from_date, to_date


def _codes_for(account: Optional[str]) -> tuple[str, ...]:
    if not account or account == "all":
        return MONEY_ACCOUNT_CODES
    if account not in MONEY_ACCOUNTS:
        raise HTTPException(status_code=400, detail="Unknown money account.")
    return (account,)


async def _balances_before(db: AsyncSession, codes: tuple[str, ...], before_utc: datetime) -> dict[str, float]:
    rows = (await db.execute(
        select(Account.code, JournalEntry.debit, JournalEntry.credit)
        .join(JournalEntry, JournalEntry.account_id == Account.id)
        .join(Journal, JournalEntry.journal_id == Journal.id)
        .where(Account.code.in_(codes), Journal.created_at < before_utc)
    )).all()
    out = {code: 0.0 for code in codes}
    for code, debit, credit in rows:
        out[code] += float(debit or 0) - float(credit or 0)
    return {code: round(v, 2) for code, v in out.items()}


async def _period_lines(db: AsyncSession, codes: tuple[str, ...], start_utc: datetime, end_utc: datetime) -> list:
    return (await db.execute(
        select(
            Journal.id, Journal.created_at, Journal.ref_type, Journal.description,
            Account.code, JournalEntry.debit, JournalEntry.credit,
        )
        .join(JournalEntry, JournalEntry.journal_id == Journal.id)
        .join(Account, JournalEntry.account_id == Account.id)
        .where(Account.code.in_(codes), Journal.created_at >= start_utc, Journal.created_at <= end_utc)
        .order_by(Journal.created_at, Journal.id, JournalEntry.id)
    )).all()


async def summary(db: AsyncSession, from_date: Optional[date] = None, to_date: Optional[date] = None) -> dict:
    """Each money account's opening, money in, money out and closing for the period, plus money in / out by source.

    Transfers between the cash box and the bank count on each account, but not in the organization's total —
    the money only changed drawer."""
    from_date, to_date = _period(from_date, to_date)
    start_utc, end_utc = utc_bounds(from_date, to_date)
    accounts = await ensure_money_accounts(db)
    opening = await _balances_before(db, MONEY_ACCOUNT_CODES, start_utc)
    lines = await _period_lines(db, MONEY_ACCOUNT_CODES, start_utc, end_utc)

    per = {code: {"in": 0.0, "out": 0.0} for code in MONEY_ACCOUNT_CODES}
    by_source: dict[str, dict[str, float]] = {}
    for _jid, _at, ref_type, description, code, debit, credit in lines:
        money_in, money_out = float(debit or 0), float(credit or 0)
        per[code]["in"] += money_in
        per[code]["out"] += money_out
        if ref_type == TRANSFER_REF:
            continue
        src = by_source.setdefault(source_of(ref_type, description), {"in": 0.0, "out": 0.0})
        src["in"] += money_in
        src["out"] += money_out

    out_accounts = []
    for code in MONEY_ACCOUNT_CODES:
        o, i, x = opening[code], per[code]["in"], per[code]["out"]
        out_accounts.append({
            "code": code, "name": accounts[code].name or MONEY_ACCOUNTS[code],
            "opening": _num(o), "money_in": _num(i), "money_out": _num(x), "closing": _num(o + i - x),
        })
    sources = sorted(
        ({"source": name, "money_in": _num(v["in"]), "money_out": _num(v["out"])} for name, v in by_source.items()),
        key=lambda s: -(s["money_in"] + s["money_out"]),
    )
    total_in = sum(s["money_in"] for s in sources)
    total_out = sum(s["money_out"] for s in sources)
    total_opening = sum(a["opening"] for a in out_accounts)
    return {
        "from": from_date.isoformat(), "to": to_date.isoformat(),
        "accounts": out_accounts,
        "total": {
            "opening": _num(total_opening), "money_in": _num(total_in), "money_out": _num(total_out),
            "closing": _num(total_opening + total_in - total_out),
        },
        "by_source": sources,
        "has_opening": {code: await _has_opening(db, code) for code in MONEY_ACCOUNT_CODES},
    }


async def statement(
    db: AsyncSession, account: Optional[str] = None,
    from_date: Optional[date] = None, to_date: Optional[date] = None,
) -> dict:
    """Every movement on one money account (or both) in the period, oldest first, with a running balance."""
    codes = _codes_for(account)
    from_date, to_date = _period(from_date, to_date)
    start_utc, end_utc = utc_bounds(from_date, to_date)
    accounts = await ensure_money_accounts(db)
    opening = sum((await _balances_before(db, codes, start_utc)).values())
    balance = opening
    lines = []
    for jid, created_at, ref_type, description, code, debit, credit in await _period_lines(db, codes, start_utc, end_utc):
        money_in, money_out = float(debit or 0), float(credit or 0)
        balance += money_in - money_out
        lines.append({
            "journal_id": jid,
            "date": to_app_tz(created_at).date().isoformat() if created_at else None,
            "description": description or "",
            "source": source_of(ref_type, description),
            "account_code": code,
            "account_name": accounts[code].name or MONEY_ACCOUNTS[code],
            "money_in": _num(money_in), "money_out": _num(money_out),
            "balance": _num(balance),
            "manual": ref_type in MANUAL_REFS,
        })
    return {
        "account": account or "all", "from": from_date.isoformat(), "to": to_date.isoformat(),
        "opening": _num(opening), "closing": _num(balance), "lines": lines,
    }


# ── posting ────────────────────────────────────────────────────────────────

def _amount(value) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(_MONEY, rounding=ROUND_HALF_UP)
    except Exception:
        raise HTTPException(status_code=400, detail="Enter a valid amount.") from None
    if amount.copy_abs() > MAX_AMOUNT:
        raise HTTPException(status_code=400, detail="That amount is too large.")
    return amount


def _journal_time(on: Optional[date]) -> Optional[datetime]:
    """When a movement dated ``on`` is stamped: midday (app time) for a past day; None for today, so the database
    stamps it like every other journal and the statement keeps the order things happened in."""
    on = on or today_local()
    if on > today_local():
        raise HTTPException(status_code=400, detail="The date can't be in the future.")
    if on == today_local():
        return None
    return datetime.combine(on, time(12, 0), tzinfo=app_tz()).astimezone(timezone.utc)


async def _post(db: AsyncSession, *, ref_type: str, description: str, created_at: Optional[datetime],
                user: User, legs: list[tuple[Account, Decimal, Decimal]]) -> Journal:
    journal = Journal(ref_type=ref_type, description=description, user_id=user.id)
    if created_at is not None:
        journal.created_at = created_at
    db.add(journal)
    await db.flush()
    for account, debit, credit in legs:
        db.add(JournalEntry(journal_id=journal.id, account_id=account.id, debit=debit, credit=credit))
        account.balance = Decimal(str(account.balance or 0)) + debit - credit
    return journal


async def _has_opening(db: AsyncSession, code: str) -> bool:
    found = (await db.execute(
        select(Journal.id)
        .join(JournalEntry, JournalEntry.journal_id == Journal.id)
        .join(Account, JournalEntry.account_id == Account.id)
        .where(Journal.ref_type == OPENING_REF, Account.code == code)
    )).scalars().first()
    return found is not None


async def set_opening_balance(db: AsyncSession, *, account_code: str, amount, on: Optional[date],
                              note: Optional[str], user: User) -> dict:
    """The real balance an account held on the day tracking starts. Booked against Owner Equity, so it is not
    income. A negative amount is an overdrawn account. One per account; delete it to enter a different one."""
    if account_code not in MONEY_ACCOUNTS:
        raise HTTPException(status_code=400, detail="Unknown money account.")
    value = _amount(amount)
    if value == 0:
        raise HTTPException(status_code=400, detail="The opening balance can't be zero.")
    if await _has_opening(db, account_code):
        raise HTTPException(status_code=400, detail="This account already has an opening balance. "
                                                    "Delete it from the statement first to enter a new one.")
    accounts = await ensure_money_accounts(db)
    money = accounts[account_code]
    equity = await _ensure_account(db, EQUITY_CODE, "Owner Equity", "equity")
    description = f"Opening balance - {money.name}"
    if (note or "").strip():
        description += f" - {note.strip()}"
    zero = Decimal("0")
    legs = [(money, value, zero), (equity, zero, value)] if value > 0 else \
           [(equity, -value, zero), (money, zero, -value)]
    journal = await _post(db, ref_type=OPENING_REF, description=description,
                          created_at=_journal_time(on), user=user, legs=legs)
    record(db, "Treasury", "opening_balance", f"{money.name} opening balance {float(value):,.2f}",
           user=user, ref_type="journal", ref_id=journal.id)
    await db.commit()
    return {"ok": True, "journal_id": journal.id}


async def transfer(db: AsyncSession, *, from_code: str, to_code: str, amount, on: Optional[date],
                   note: Optional[str], user: User) -> dict:
    """Move money between the cash box and the bank — a deposit of cash, or a cash withdrawal."""
    if from_code not in MONEY_ACCOUNTS or to_code not in MONEY_ACCOUNTS:
        raise HTTPException(status_code=400, detail="Unknown money account.")
    if from_code == to_code:
        raise HTTPException(status_code=400, detail="Pick two different accounts.")
    value = _amount(amount)
    if value <= 0:
        raise HTTPException(status_code=400, detail="The amount must be greater than 0.")
    accounts = await ensure_money_accounts(db)
    src, dst = accounts[from_code], accounts[to_code]
    description = f"Transfer {src.name} → {dst.name}"
    if (note or "").strip():
        description += f" - {note.strip()}"
    zero = Decimal("0")
    journal = await _post(db, ref_type=TRANSFER_REF, description=description,
                          created_at=_journal_time(on), user=user,
                          legs=[(dst, value, zero), (src, zero, value)])
    record(db, "Treasury", "transfer", f"{description} - {float(value):,.2f}",
           user=user, ref_type="journal", ref_id=journal.id)
    await db.commit()
    return {"ok": True, "journal_id": journal.id}


async def delete_movement(db: AsyncSession, journal_id: int, user: User) -> dict:
    """Remove an opening balance or a transfer entered here. Sales, expenses, loans and the rest are corrected on
    their own pages, which know what else to undo."""
    journal = (await db.execute(select(Journal).where(Journal.id == journal_id))).scalar_one_or_none()
    if journal is None:
        raise HTTPException(status_code=404, detail="Movement not found.")
    if journal.ref_type not in MANUAL_REFS:
        raise HTTPException(status_code=400, detail="Only opening balances and transfers can be deleted here. "
                                                    "Correct other movements on the page they came from.")
    entries = (await db.execute(select(JournalEntry).where(JournalEntry.journal_id == journal_id))).scalars().all()
    for entry in entries:
        account = await db.get(Account, entry.account_id)
        if account is not None:
            account.balance = Decimal(str(account.balance or 0)) \
                - Decimal(str(entry.debit or 0)) + Decimal(str(entry.credit or 0))
    description = journal.description
    await db.execute(delete(JournalEntry).where(JournalEntry.journal_id == journal_id))
    await db.execute(delete(Journal).where(Journal.id == journal_id))
    record(db, "Treasury", "delete_movement", f"Deleted: {description}", user=user, ref_type="journal", ref_id=journal_id)
    await db.commit()
    return {"ok": True}
