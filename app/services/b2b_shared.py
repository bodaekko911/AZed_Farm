"""
Shared helpers used by both app/routers/b2b.py and the B2B sales import service.
Extracted to avoid duplication and to allow the import service to pass
created_at / ref_id that the router doesn't need.
"""
from decimal import Decimal
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.sql import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.b2b import B2BInvoice, Consignment
from app.models.accounting import Account, Journal, JournalEntry


async def post_journal(
    db: AsyncSession,
    description: str,
    ref_type: str,
    entries: list,
    user_id: Optional[int] = None,
    created_at: Optional[datetime] = None,
    ref_id: Optional[int] = None,
) -> None:
    journal = Journal(ref_type=ref_type, description=description, user_id=user_id)
    if created_at is not None:
        journal.created_at = created_at
    if ref_id is not None:
        journal.ref_id = ref_id
    db.add(journal)
    await db.flush()
    for code, debit, credit in entries:
        _r = await db.execute(select(Account).where(Account.code == code))
        acc = _r.scalar_one_or_none()
        if acc:
            db.add(JournalEntry(
                journal_id=journal.id, account_id=acc.id,
                debit=debit, credit=credit,
            ))
            acc.balance += Decimal(str(debit)) - Decimal(str(credit))


async def seed_deferred_revenue(db: AsyncSession) -> None:
    """Ensure account 2200 Deferred Revenue exists."""
    _r = await db.execute(select(Account).where(Account.code == "2200"))
    if _r.scalar_one_or_none() is None:
        db.add(Account(
            code="2200", name="Deferred Revenue",
            type="liability", balance=Decimal("0"),
        ))
        await db.commit()


async def next_b2b_number(db: AsyncSession) -> str:
    _r = await db.execute(select(sa_func.max(B2BInvoice.id)))
    max_id = _r.scalar() or 0
    return f"B2B-{str(max_id + 1).zfill(5)}"


async def next_cons_number(db: AsyncSession) -> str:
    _r = await db.execute(select(sa_func.max(Consignment.id)))
    max_id = _r.scalar() or 0
    return f"CONS-{str(max_id + 1).zfill(4)}"


async def get_b2b_client_top_products(db: AsyncSession) -> dict:
    """
    Returns the top 5 products purchased by each client.
    Returns: {client_id: [{"product_id": int, "name": str, "total_qty": float}, ...]}
    """
    from app.models.b2b import B2BInvoice, B2BInvoiceItem
    from app.models.product import Product

    query = (
        select(
            B2BInvoice.client_id,
            Product.id.label("product_id"),
            Product.name.label("product_name"),
            sa_func.sum(B2BInvoiceItem.qty).label("total_qty")
        )
        .select_from(B2BInvoice)
        .join(B2BInvoiceItem, B2BInvoice.id == B2BInvoiceItem.invoice_id)
        .join(Product, B2BInvoiceItem.product_id == Product.id)
        .group_by(B2BInvoice.client_id, Product.id, Product.name)
    )
    result = await db.execute(query)

    client_products = {}
    for client_id, product_id, product_name, total_qty in result.all():
        if client_id not in client_products:
            client_products[client_id] = []
        client_products[client_id].append({
            "product_id": product_id,
            "name": product_name,
            "total_qty": float(total_qty)
        })

    # Sort by total_qty descending and take top 5 for each client
    for client_id in client_products:
        client_products[client_id].sort(key=lambda x: x["total_qty"], reverse=True)
        client_products[client_id] = client_products[client_id][:5]

    return client_products
