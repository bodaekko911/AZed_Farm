"""Supplier payment service.

Records payments made to a supplier against their outstanding balance.
A payment does NOT create a new expense — the expense was already booked
when stock was received on credit. A payment simply:

  • reduces supplier.balance
  • books a journal: Debit Accounts Payable, Credit Cash
  • is stored in supplier_payments for the audit trail
"""
from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.log import record
from app.models.accounting import Account, Journal, JournalEntry
from app.models.supplier import Supplier, SupplierPayment
from app.models.user import User

_MONEY = Decimal("0.01")

CASH_ACCOUNT_CODE = "1000"
CASH_ACCOUNT_NAME = "Cash"
AP_ACCOUNT_CODE   = "2000"
AP_ACCOUNT_NAME   = "Accounts Payable"


async def _ensure_account(
    db: AsyncSession,
    code: str,
    name: str,
    account_type: str,
) -> Account:
    result  = await db.execute(select(Account).where(Account.code == code))
    account = result.scalar_one_or_none()
    if account is None:
        account = Account(code=code, name=name, type=account_type, balance=0)
        db.add(account)
        await db.flush()
    return account


async def _next_payment_ref(db: AsyncSession) -> str:
    result = await db.execute(select(func.max(SupplierPayment.id)))
    max_id = result.scalar() or 0
    return f"SPY-{str(max_id + 1).zfill(5)}"


async def record_supplier_payment(
    db: AsyncSession,
    *,
    supplier_id: int,
    amount: float,
    payment_date: date_type,
    payment_method: str = "cash",
    notes: Optional[str] = None,
    current_user: User,
) -> dict[str, Any]:
    """Record a payment to a supplier. Reduces supplier balance, debits A/P,
    credits cash. Does not create an expense."""
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")

    result = await db.execute(select(Supplier).where(Supplier.id == supplier_id))
    supplier = result.scalar_one_or_none()
    if supplier is None:
        raise HTTPException(status_code=404, detail="Supplier not found")

    amount_dec = Decimal(str(amount)).quantize(_MONEY, rounding=ROUND_HALF_UP)
    current_balance = Decimal(str(supplier.balance or 0))
    if amount_dec > current_balance:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Payment ({amount_dec}) exceeds outstanding balance "
                f"({current_balance})."
            ),
        )

    ref_number = await _next_payment_ref(db)

    # Journal
    journal = Journal(
        ref_type="supplier_payment",
        description=f"Payment to {supplier.name} — {ref_number}",
        user_id=current_user.id,
    )
    db.add(journal)
    await db.flush()

    ap_acc   = await _ensure_account(db, AP_ACCOUNT_CODE,   AP_ACCOUNT_NAME,   "liability")
    cash_acc = await _ensure_account(db, CASH_ACCOUNT_CODE, CASH_ACCOUNT_NAME, "asset")
    amount_f = float(amount_dec)

    db.add(JournalEntry(journal_id=journal.id, account_id=ap_acc.id,   debit=amount_f, credit=0))
    db.add(JournalEntry(journal_id=journal.id, account_id=cash_acc.id, debit=0,        credit=amount_f))

    ap_acc.balance   = Decimal(str(ap_acc.balance   or 0)) - amount_dec
    cash_acc.balance = Decimal(str(cash_acc.balance or 0)) - amount_dec
    supplier.balance = current_balance - amount_dec

    payment = SupplierPayment(
        ref_number=ref_number,
        supplier_id=supplier.id,
        user_id=current_user.id,
        payment_date=payment_date,
        amount=amount_f,
        payment_method=(payment_method or "cash"),
        notes=(notes or None),
        journal_id=journal.id,
    )
    db.add(payment)

    record(
        db,
        "Suppliers",
        "supplier_payment",
        f"{ref_number} — {supplier.name} — {amount_f:.2f}",
        user=current_user,
        ref_type="supplier_payment",
        ref_id=None,
    )
    await db.commit()
    await db.refresh(payment)

    return {
        "id":             payment.id,
        "ref_number":     ref_number,
        "supplier_id":    supplier.id,
        "supplier_name":  supplier.name,
        "payment_date":   payment_date.isoformat(),
        "amount":         amount_f,
        "payment_method": payment.payment_method,
        "notes":          payment.notes,
        "new_balance":    float(supplier.balance),
    }


async def list_supplier_payments(
    db: AsyncSession,
    supplier_id: int,
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(SupplierPayment)
        .options(selectinload(SupplierPayment.user))
        .where(SupplierPayment.supplier_id == supplier_id)
        .order_by(SupplierPayment.payment_date.desc(), SupplierPayment.id.desc())
    )
    rows = result.scalars().all()
    return [
        {
            "id":             p.id,
            "ref_number":     p.ref_number,
            "payment_date":   p.payment_date.isoformat() if p.payment_date else None,
            "amount":         float(p.amount or 0),
            "payment_method": p.payment_method,
            "notes":          p.notes,
            "recorded_by":    p.user.name if p.user else None,
            "created_at":     p.created_at.isoformat() if p.created_at else None,
        }
        for p in rows
    ]


async def supplier_account_statement(
    db: AsyncSession,
    supplier_id: int,
) -> dict[str, Any]:
    """Combined view of a supplier: profile + receipts on credit + payments + balance."""
    from app.models.receipt import ProductReceipt

    sup_result = await db.execute(select(Supplier).where(Supplier.id == supplier_id))
    supplier = sup_result.scalar_one_or_none()
    if supplier is None:
        raise HTTPException(status_code=404, detail="Supplier not found")

    rcv_result = await db.execute(
        select(ProductReceipt)
        .options(selectinload(ProductReceipt.product))
        .where(ProductReceipt.supplier_id == supplier_id)
        .order_by(ProductReceipt.receive_date.desc(), ProductReceipt.id.desc())
    )
    receipts = rcv_result.scalars().all()

    receipt_rows = []
    for r in receipts:
        total = float(r.total_cost or 0)
        paid  = float(r.amount_paid or 0)
        receipt_rows.append({
            "id":           r.id,
            "ref_number":   r.ref_number,
            "receive_date": r.receive_date.isoformat() if r.receive_date else None,
            "product_name": r.product.name if r.product else None,
            "qty":          float(r.qty or 0),
            "unit_cost":    float(r.unit_cost) if r.unit_cost is not None else None,
            "total":        total,
            "paid":         paid,
            "unpaid":       max(total - paid, 0.0),
            "notes":        r.notes,
        })

    payments = await list_supplier_payments(db, supplier_id)

    return {
        "supplier": {
            "id":      supplier.id,
            "name":    supplier.name,
            "phone":   supplier.phone or "",
            "email":   supplier.email or "",
            "address": supplier.address or "",
            "balance": float(supplier.balance or 0),
        },
        "receipts": receipt_rows,
        "payments": payments,
    }