"""Receive Products service.

Handles stock intake: updates inventory, creates a StockMove, and — when a
cost is provided — posts a linked Expense + double-entry Journal.

Supports three payment modes when a supplier is linked:
  • cash     — fully paid at receive   → Dr Expense / Cr Cash
  • credit   — fully on supplier credit → Dr Expense / Cr Accounts Payable
  • partial  — split between cash + credit
                → Dr Expense / Cr Cash (paid portion) + Cr A/P (unpaid)

If no supplier is linked, behaviour is exactly cash (legacy).
The supplier's `balance` is increased by the unpaid portion (we owe them).

The internal `_create_receipt_core` does all work without committing so that
`create_receipt_batch` can process multiple products in one transaction.
"""
from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.log import record
from app.models.accounting import Account, Journal, JournalEntry
from app.models.expense import Expense, ExpenseCategory
from app.models.inventory import LocationStock, StockLocation, StockMove
from app.models.product import Product
from app.models.receipt import ProductReceipt
from app.models.supplier import Supplier
from app.models.user import User
from app.services.location_inventory_service import (
    ensure_default_stock_location,
    get_or_create_location_stock,
    quantize_qty,
)

_MONEY = Decimal("0.01")
_QTY   = Decimal("0.001")

STOCK_PURCHASE_ACCOUNT_CODE  = "5011"
STOCK_PURCHASE_CATEGORY_NAME = "Products"
PACKAGING_CATEGORY_NAME = "Packaging Materials"
PACKAGING_CATEGORY_ACCOUNT_CODE = "5007"
PRODUCT_TYPE_PRODUCTS = "products"
PRODUCT_TYPE_PACKAGING = "packaging_materials"
RECEIPT_PRODUCT_TYPE_TO_CATEGORY = {
    PRODUCT_TYPE_PRODUCTS: (STOCK_PURCHASE_CATEGORY_NAME, STOCK_PURCHASE_ACCOUNT_CODE),
    PRODUCT_TYPE_PACKAGING: (PACKAGING_CATEGORY_NAME, PACKAGING_CATEGORY_ACCOUNT_CODE),
}

CASH_ACCOUNT_CODE = "1000"
CASH_ACCOUNT_NAME = "Cash"
AP_ACCOUNT_CODE   = "2000"
AP_ACCOUNT_NAME   = "Accounts Payable"


# ── Input schemas ─────────────────────────────────────────────────────────────

class ReceiptCreate(BaseModel):
    """Single-product receipt (used by the batch assembler and the legacy endpoint)."""
    product_id:   int             = Field(..., ge=1)
    qty:          float           = Field(..., gt=0)
    unit_cost:    Optional[float] = Field(None, ge=0)
    # product_type is now legacy — kept for the importer and old API clients.
    # The intake UI sends expense_category_id directly. Defaults to "products"
    # so the auto-derivation path still works when no category override is given.
    product_type: Literal["products", "packaging_materials"] = "products"
    receive_date: date_type
    supplier_ref: Optional[str]   = Field(None, max_length=150)
    supplier_id:  Optional[int]   = Field(None, ge=1)
    amount_paid:  Optional[float] = Field(None, ge=0)  # cash paid at receive time
    notes:        Optional[str]   = None
    affect_stock: bool            = True
    location_id:  Optional[int]   = Field(None, ge=1)  # storage to receive into; default Main Warehouse
    farm_id:      Optional[int]   = Field(None, ge=1)  # cost allocation: farm
    is_animal_expense: bool       = False              # cost allocation: 🐾 Animals bucket
    expense_category_id: Optional[int] = Field(None, ge=1)  # override auto category


class BatchReceiptItem(BaseModel):
    """One line inside a batch receive."""
    product_id: int             = Field(..., ge=1)
    qty:        float           = Field(..., gt=0)
    unit_cost:  Optional[float] = Field(None, ge=0)


class BatchReceiptCreate(BaseModel):
    """Multi-product receive submitted from the form.

    Payment fields apply to the whole batch:
      • supplier_id  — optional supplier link
      • amount_paid  — total cash paid (split proportionally across line items)

    Storage:
      • location_id  — destination storage for all items in the batch.
                       Defaults to Main Warehouse if not provided.

    Cost allocation (batch-wide — applies to every auto-created expense):
      • farm_id            — allocate to a specific farm
      • is_animal_expense  — allocate to the "🐾 Animals" bucket instead
    """
    product_type: Literal["products", "packaging_materials"] = "products"
    receive_date: date_type
    supplier_ref: Optional[str] = Field(None, max_length=150)
    supplier_id:  Optional[int] = Field(None, ge=1)
    amount_paid:  Optional[float] = Field(None, ge=0)
    notes:        Optional[str] = None
    location_id:  Optional[int] = Field(None, ge=1)
    farm_id:      Optional[int] = Field(None, ge=1)
    is_animal_expense: bool     = False
    expense_category_id: Optional[int] = Field(None, ge=1)
    items:        list[BatchReceiptItem] = Field(..., min_length=1)


class ReceiptUpdate(BaseModel):
    """Editable fields for an existing receipt.

    Note: supplier link and payment status are NOT editable here. Use the
    suppliers payment API to record additional payments. Changing total cost
    is only permitted when the receipt was a cash receipt (no supplier credit).
    """
    qty:          float           = Field(..., gt=0)
    unit_cost:    Optional[float] = Field(None, ge=0)
    product_type: Optional[Literal["products", "packaging_materials"]] = None
    receive_date: date_type
    supplier_ref: Optional[str]   = Field(None, max_length=150)
    notes:        Optional[str]   = None


# ── Private helpers ───────────────────────────────────────────────────────────

async def _next_receipt_ref(db: AsyncSession) -> str:
    result = await db.execute(select(func.max(ProductReceipt.id)))
    max_id = result.scalar() or 0
    return f"RCV-{str(max_id + 1).zfill(5)}"


async def _next_expense_ref(db: AsyncSession) -> str:
    result = await db.execute(select(func.max(Expense.id)))
    max_id = result.scalar() or 0
    return f"EXP-{str(max_id + 1).zfill(5)}"


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


async def _resolve_chosen_category(
    db: AsyncSession,
    category_id: int,
) -> ExpenseCategory:
    """Resolve a user-selected expense category override.

    Validates that the category exists and is active, and ensures the matching
    expense ledger account is present (created on demand using the category's
    account_code, mirroring the auto-derived path).
    """
    result = await db.execute(
        select(ExpenseCategory).where(ExpenseCategory.id == category_id)
    )
    category = result.scalar_one_or_none()
    if category is None:
        raise HTTPException(
            status_code=400,
            detail=f"Expense category {category_id} not found",
        )
    if category.is_active != "1":
        raise HTTPException(
            status_code=400,
            detail=f"Expense category '{category.name}' is archived — choose another one",
        )
    # Make sure the underlying expense account exists.
    await _ensure_account(
        db, category.account_code, category.name, "expense",
    )
    return category


async def _get_or_create_receipt_category(
    db: AsyncSession,
    *,
    product_type: str,
) -> ExpenseCategory:
    category_config = RECEIPT_PRODUCT_TYPE_TO_CATEGORY.get(product_type)
    if category_config is None:
        raise HTTPException(status_code=422, detail="Product Type is required")

    category_name, account_code = category_config
    result   = await db.execute(
        select(ExpenseCategory).where(
            ExpenseCategory.name == category_name,
            ExpenseCategory.is_active == "1",
        )
    )
    category = result.scalar_one_or_none()
    if category is not None:
        return category

    result = await db.execute(
        select(ExpenseCategory).where(
            ExpenseCategory.account_code == account_code,
            ExpenseCategory.is_active == "1",
        )
    )
    category = result.scalar_one_or_none()
    if category is not None:
        return category

    await _ensure_account(
        db, account_code, category_name, "expense"
    )
    category = ExpenseCategory(
        name=category_name,
        account_code=account_code,
        is_active="1",
    )
    db.add(category)
    await db.flush()
    return category


def _payment_method_label(paid: Decimal, total: Decimal) -> str:
    if total <= 0:
        return "cash"
    if paid >= total:
        return "cash"
    if paid <= 0:
        return "credit"
    return "partial"


async def _post_receipt_expense(
    db: AsyncSession,
    *,
    category: ExpenseCategory,
    product_name: str,
    receipt_ref: str,
    qty: Decimal,
    total_cost: Decimal,
    paid_cash: Decimal,
    receive_date: date_type,
    supplier: Optional[Supplier],
    supplier_ref: Optional[str],
    user_id: Optional[int],
    farm_id: Optional[int] = None,
    is_animal_expense: bool = False,
) -> Expense:
    """Create Expense + double-entry Journal.

    Journal layout:
      Debit  expense account        (total_cost)
      Credit cash                   (paid_cash)            ── if > 0
      Credit accounts_payable       (total_cost - paid_cash) ── if > 0

    Cost allocation:
      • farm_id           — tags the expense to a specific farm (shows in
                            the expense table's Farm column, and in farm
                            cost-allocation reports).
      • is_animal_expense — tags the expense as "🐾 Animals". Shows in the
                            expense table as Animals, and rolls into the
                            combined Animals → Analyze view.

    No commit — caller owns transaction.
    """
    exp_ref = await _next_expense_ref(db)
    unpaid  = (total_cost - paid_cash)
    if unpaid < 0:
        unpaid = Decimal("0")
    payment_method = _payment_method_label(paid_cash, total_cost)

    journal = Journal(
        ref_type="expense",
        description=f"{category.name} — {receipt_ref}",
        user_id=user_id,
    )
    db.add(journal)
    await db.flush()

    total_amount = float(total_cost)
    expense_acc = await _ensure_account(db, category.account_code, category.name, "expense")
    db.add(JournalEntry(
        journal_id=journal.id, account_id=expense_acc.id,
        debit=total_amount, credit=0,
    ))
    expense_acc.balance = Decimal(str(expense_acc.balance or 0)) + total_cost

    if paid_cash > 0:
        cash_acc = await _ensure_account(db, CASH_ACCOUNT_CODE, CASH_ACCOUNT_NAME, "asset")
        db.add(JournalEntry(
            journal_id=journal.id, account_id=cash_acc.id,
            debit=0, credit=float(paid_cash),
        ))
        cash_acc.balance = Decimal(str(cash_acc.balance or 0)) - paid_cash

    if unpaid > 0:
        ap_acc = await _ensure_account(db, AP_ACCOUNT_CODE, AP_ACCOUNT_NAME, "liability")
        db.add(JournalEntry(
            journal_id=journal.id, account_id=ap_acc.id,
            debit=0, credit=float(unpaid),
        ))
        ap_acc.balance = Decimal(str(ap_acc.balance or 0)) + unpaid

    # bump supplier balance with the unpaid amount
    if supplier is not None and unpaid > 0:
        supplier.balance = Decimal(str(supplier.balance or 0)) + unpaid

    vendor_label = supplier.name if supplier is not None else supplier_ref

    expense = Expense(
        ref_number=exp_ref,
        category_id=category.id,
        user_id=user_id,
        expense_date=receive_date,
        amount=total_amount,
        payment_method=payment_method,
        vendor=vendor_label,
        description=(
            f"Stock receipt {receipt_ref} — "
            f"{float(qty):.3f} \u00d7 {product_name}"
        ),
        journal_id=journal.id,
        farm_id=(farm_id if (farm_id and not is_animal_expense) else None),
        is_animal_expense=bool(is_animal_expense),
    )
    db.add(expense)
    return expense


async def _get_receipt_or_404(db: AsyncSession, receipt_id: int) -> ProductReceipt:
    result = await db.execute(
        select(ProductReceipt)
        .options(
            selectinload(ProductReceipt.product),
            selectinload(ProductReceipt.user),
            selectinload(ProductReceipt.expense),
            selectinload(ProductReceipt.supplier),
        )
        .where(ProductReceipt.id == receipt_id)
    )
    receipt = result.scalar_one_or_none()
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"Receipt {receipt_id} not found")
    return receipt


def _quantize_receipt_values(
    qty_value: float,
    unit_cost_value: Optional[float],
) -> tuple[Decimal, Optional[Decimal], Optional[Decimal]]:
    qty = Decimal(str(qty_value)).quantize(_QTY, rounding=ROUND_HALF_UP)
    unit_cost: Optional[Decimal] = None
    total_cost: Optional[Decimal] = None
    if unit_cost_value is not None and unit_cost_value > 0:
        unit_cost = Decimal(str(unit_cost_value)).quantize(_MONEY, rounding=ROUND_HALF_UP)
        total_cost = (qty * unit_cost).quantize(_MONEY, rounding=ROUND_HALF_UP)
    return qty, unit_cost, total_cost


async def _get_receipt_move(db: AsyncSession, receipt_id: int) -> StockMove | None:
    result = await db.execute(
        select(StockMove)
        .where(StockMove.ref_type == "receipt", StockMove.ref_id == receipt_id)
        .order_by(StockMove.id.desc())
    )
    return result.scalar_one_or_none()


async def _delete_expense_bundle(db: AsyncSession, expense: Expense | None) -> None:
    """Delete expense + reverse its journal account balances. No supplier reversal."""
    if expense is None:
        return

    journal = None
    if expense.journal_id:
        result = await db.execute(
            select(Journal)
            .options(selectinload(Journal.entries).selectinload(JournalEntry.account))
            .where(Journal.id == expense.journal_id)
        )
        journal = result.scalar_one_or_none()

    if journal is not None:
        for entry in journal.entries:
            if entry.account is not None and entry.account.balance is not None:
                entry.account.balance = (
                    Decimal(str(entry.account.balance))
                    - Decimal(str(entry.debit or 0))
                    + Decimal(str(entry.credit or 0))
                )
        await db.delete(journal)

    await db.delete(expense)


async def _sync_receipt_expense(
    db: AsyncSession,
    *,
    receipt: ProductReceipt,
    product_name: str,
    qty: Decimal,
    total_cost: Optional[Decimal],
    receive_date: date_type,
    supplier_ref: Optional[str],
    product_type: Optional[str] = None,
) -> Optional[str]:
    """Update path: only safe when receipt has NO outstanding supplier balance.

    If the receipt was on credit/partial, we refuse to mutate the journal here
    to keep supplier balance + A/P consistent. Caller should reject those edits
    upstream; this function is a defensive no-op for those cases.
    """
    # Refuse edits to receipts that carried any supplier credit.
    paid_cash = Decimal(str(receipt.amount_paid or 0))
    original_total = Decimal(str(receipt.total_cost or 0))
    had_credit = (
        receipt.supplier_id is not None
        and original_total > 0
        and paid_cash < original_total
    )
    if had_credit:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot edit a receipt that was on supplier credit. "
                "Record a payment from the Suppliers page, or delete and re-create."
            ),
        )

    if total_cost is None or total_cost <= 0:
        if receipt.expense_id:
            expense_result = await db.execute(
                select(Expense)
                .options(selectinload(Expense.category))
                .where(Expense.id == receipt.expense_id)
            )
            expense = expense_result.scalar_one_or_none()
            await _delete_expense_bundle(db, expense)
            receipt.expense_id = None
        receipt.amount_paid = Decimal("0")
        return None

    if receipt.expense_id is None:
        if not product_type:
            raise HTTPException(status_code=422, detail="Product Type is required")
        category = await _get_or_create_receipt_category(db, product_type=product_type)
        expense = await _post_receipt_expense(
            db,
            category=category,
            product_name=product_name,
            receipt_ref=receipt.ref_number,
            qty=qty,
            total_cost=total_cost,
            paid_cash=total_cost,             # cash on edit-recreate
            receive_date=receive_date,
            supplier=None,
            supplier_ref=supplier_ref,
            user_id=receipt.user_id,
        )
        await db.flush()
        receipt.expense_id = expense.id
        receipt.amount_paid = total_cost
        return expense.ref_number

    # Existing expense — straightforward cash receipt update path
    expense_result = await db.execute(
        select(Expense)
        .options(selectinload(Expense.category))
        .where(Expense.id == receipt.expense_id)
    )
    expense = expense_result.scalar_one_or_none()
    if expense is None:
        receipt.expense_id = None
        return await _sync_receipt_expense(
            db,
            receipt=receipt,
            product_name=product_name,
            qty=qty,
            total_cost=total_cost,
            receive_date=receive_date,
            supplier_ref=supplier_ref,
            product_type=product_type,
        )

    old_amount = Decimal(str(expense.amount or 0)).quantize(_MONEY, rounding=ROUND_HALF_UP)
    delta = total_cost - old_amount

    expense_category = expense.category
    if expense_category is None:
        category_result = await db.execute(
            select(ExpenseCategory).where(ExpenseCategory.id == expense.category_id)
        )
        expense_category = category_result.scalar_one_or_none()
    if expense_category is None:
        if not product_type:
            raise HTTPException(status_code=422, detail="Product Type is required")
        expense_category = await _get_or_create_receipt_category(db, product_type=product_type)
        expense.category_id = expense_category.id

    expense_account = await _ensure_account(db, expense_category.account_code, expense_category.name, "expense")
    cash_account = await _ensure_account(db, CASH_ACCOUNT_CODE, CASH_ACCOUNT_NAME, "asset")
    if delta:
        expense_account.balance = Decimal(str(expense_account.balance or 0)) + delta
        cash_account.balance = Decimal(str(cash_account.balance or 0)) - delta

    if expense.journal_id:
        journal_result = await db.execute(
            select(Journal)
            .options(selectinload(Journal.entries))
            .where(Journal.id == expense.journal_id)
        )
        journal = journal_result.scalar_one_or_none()
    else:
        journal = None

    if journal is None:
        journal = Journal(
            ref_type="expense",
            description=f"{expense_category.name} — {receipt.ref_number}",
            user_id=receipt.user_id,
        )
        db.add(journal)
        await db.flush()
        expense.journal_id = journal.id

    debit_entry = next((entry for entry in journal.entries if Decimal(str(entry.debit or 0)) > 0), None)
    credit_entry = next((entry for entry in journal.entries if Decimal(str(entry.credit or 0)) > 0), None)
    if debit_entry is None:
        debit_entry = JournalEntry(journal_id=journal.id, account_id=expense_account.id, debit=0, credit=0)
        db.add(debit_entry)
    debit_entry.account_id = expense_account.id
    debit_entry.debit = float(total_cost)
    debit_entry.credit = 0
    if credit_entry is None:
        credit_entry = JournalEntry(journal_id=journal.id, account_id=cash_account.id, debit=0, credit=0)
        db.add(credit_entry)
    credit_entry.account_id = cash_account.id
    credit_entry.debit = 0
    credit_entry.credit = float(total_cost)
    journal.description = f"{expense_category.name} — {receipt.ref_number}"

    expense.expense_date = receive_date
    expense.amount = float(total_cost)
    expense.vendor = supplier_ref
    expense.payment_method = "cash"
    expense.description = (
        f"Stock receipt {receipt.ref_number} — "
        f"{float(qty):.3f} × {product_name}"
    )
    receipt.amount_paid = total_cost
    return expense.ref_number


async def _resolve_supplier(
    db: AsyncSession,
    supplier_id: Optional[int],
) -> Optional[Supplier]:
    if supplier_id is None:
        return None
    result = await db.execute(select(Supplier).where(Supplier.id == supplier_id))
    supplier = result.scalar_one_or_none()
    if supplier is None:
        raise HTTPException(status_code=404, detail=f"Supplier {supplier_id} not found")
    return supplier


async def _create_receipt_core(
    db: AsyncSession,
    data: ReceiptCreate,
    current_user: User,
) -> dict[str, Any]:
    """
    All receipt logic for one product — no commit.
    Caller must call db.commit() after (possibly after processing more items).
    """
    result  = await db.execute(select(Product).where(Product.id == data.product_id))
    product = result.scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product {data.product_id} not found")

    qty = Decimal(str(data.qty)).quantize(_QTY, rounding=ROUND_HALF_UP)

    unit_cost:  Optional[Decimal] = None
    total_cost: Optional[Decimal] = None
    if data.unit_cost is not None and data.unit_cost > 0:
        unit_cost  = Decimal(str(data.unit_cost)).quantize(_MONEY, rounding=ROUND_HALF_UP)
        total_cost = (qty * unit_cost).quantize(_MONEY, rounding=ROUND_HALF_UP)

    # Resolve supplier + payment split
    supplier = await _resolve_supplier(db, data.supplier_id)

    # Determine cash paid for this line
    if total_cost is None or total_cost <= 0:
        paid_cash = Decimal("0")
    else:
        if data.amount_paid is None:
            # Default: no supplier ⇒ fully cash. With supplier ⇒ fully credit.
            paid_cash = Decimal("0") if supplier is not None else total_cost
        else:
            paid_cash = Decimal(str(data.amount_paid)).quantize(_MONEY, rounding=ROUND_HALF_UP)
            if paid_cash > total_cost:
                paid_cash = total_cost
            if paid_cash < 0:
                paid_cash = Decimal("0")

    # Credit (unpaid) is only meaningful when supplier is set
    if supplier is None and total_cost is not None and total_cost > 0:
        # No supplier => must be fully cash for clean accounting
        paid_cash = total_cost

    ref_number = await _next_receipt_ref(db)

    qty_before    = Decimal(str(product.stock or 0))
    qty_after     = qty_before + qty
    if data.affect_stock:
        product.stock = qty_after
    if unit_cost is not None:
        product.cost = unit_cost

    # ── Resolve destination storage ───────────────────────────────────
    # If caller didn't pick a location, fall back to the default
    # (Main Warehouse). The location is recorded on the receipt
    # regardless of `affect_stock` so we always know which storage was
    # intended.
    target_location: Optional[StockLocation] = None
    if data.location_id:
        loc_result = await db.execute(
            select(StockLocation).where(
                StockLocation.id == data.location_id,
                StockLocation.is_active.is_(True),
            )
        )
        target_location = loc_result.scalar_one_or_none()
        if target_location is None:
            raise HTTPException(
                status_code=400,
                detail=f"Storage location {data.location_id} not found or inactive",
            )
    else:
        target_location = await ensure_default_stock_location(db)

    # Increment per-location stock for the chosen storage.
    if data.affect_stock and target_location is not None:
        loc_stock = await get_or_create_location_stock(
            db, location_id=target_location.id, product_id=product.id
        )
        loc_stock.qty = quantize_qty((loc_stock.qty or 0)) + qty

    supplier_ref = (data.supplier_ref or "").strip() or None
    notes        = (data.notes or "").strip() or None

    receipt = ProductReceipt(
        ref_number=ref_number,
        product_id=product.id,
        user_id=current_user.id,
        receive_date=data.receive_date,
        qty=qty,
        unit_cost=unit_cost,
        total_cost=total_cost,
        supplier_ref=supplier_ref,
        supplier_id=supplier.id if supplier is not None else None,
        amount_paid=paid_cash,
        notes=notes,
        location_id=target_location.id if target_location else None,
    )
    db.add(receipt)

    if data.affect_stock:
        move_note = f"Receipt {ref_number}"
        if target_location is not None:
            move_note += f" → {target_location.name}"
        move = StockMove(
            product_id=product.id,
            type="in",
            qty=qty,
            qty_before=qty_before,
            qty_after=qty_after,
            ref_type="receipt",
            ref_id=0,
            note=move_note,
            user_id=current_user.id,
        )
        db.add(move)
        await db.flush()
        move.ref_id = receipt.id
    else:
        await db.flush()

    expense_ref: Optional[str] = None
    payment_method = "cash"
    unpaid_amount = Decimal("0")
    if total_cost and total_cost > 0:
        chosen_id = getattr(data, "expense_category_id", None)
        if chosen_id:
            category = await _resolve_chosen_category(db, chosen_id)
        else:
            category = await _get_or_create_receipt_category(db, product_type=data.product_type)
        expense  = await _post_receipt_expense(
            db,
            category=category,
            product_name=product.name,
            receipt_ref=ref_number,
            qty=qty,
            total_cost=total_cost,
            paid_cash=paid_cash,
            receive_date=data.receive_date,
            supplier=supplier,
            supplier_ref=supplier_ref,
            user_id=current_user.id,
            farm_id=getattr(data, "farm_id", None),
            is_animal_expense=bool(getattr(data, "is_animal_expense", False)),
        )
        await db.flush()
        receipt.expense_id = expense.id
        expense_ref = expense.ref_number
        payment_method = expense.payment_method
        unpaid_amount = (total_cost - paid_cash)

    record(
        db,
        "Receive",
        "receive_products",
        f"{product.name} — {float(qty):.3f} — {ref_number}",
        user=current_user,
        ref_type="receipt",
        ref_id=receipt.id,
    )

    return {
        "id":             receipt.id,
        "ref_number":     ref_number,
        "product_id":     product.id,
        "product_name":   product.name,
        "product_sku":    product.sku,
        "receive_date":   data.receive_date.isoformat(),
        "qty":            float(qty),
        "unit_cost":      float(unit_cost)  if unit_cost  else None,
        "total_cost":     float(total_cost) if total_cost else None,
        "supplier_ref":   supplier_ref,
        "supplier_id":    supplier.id   if supplier else None,
        "supplier_name":  supplier.name if supplier else None,
        "amount_paid":    float(paid_cash),
        "amount_unpaid":  float(unpaid_amount) if unpaid_amount > 0 else 0.0,
        "payment_method": payment_method,
        "notes":          notes,
        "product_type":   data.product_type,
        "expense_id":     receipt.expense_id,
        "expense_ref":    expense_ref,
        "location_id":    target_location.id   if target_location else None,
        "location_name":  target_location.name if target_location else None,
    }


# ── Public API ────────────────────────────────────────────────────────────────

async def create_receipt(
    db: AsyncSession,
    data: ReceiptCreate,
    current_user: User,
) -> dict[str, Any]:
    """Single-product receive, one transaction."""
    result = await _create_receipt_core(db, data, current_user)
    await db.commit()
    return result


async def update_receipt(
    db: AsyncSession,
    receipt_id: int,
    data: ReceiptUpdate,
    current_user: User,
) -> dict[str, Any]:
    receipt = await _get_receipt_or_404(db, receipt_id)
    product = receipt.product
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product {receipt.product_id} not found")

    old_qty = Decimal(str(receipt.qty or 0)).quantize(_QTY, rounding=ROUND_HALF_UP)
    new_qty, unit_cost, total_cost = _quantize_receipt_values(data.qty, data.unit_cost)
    qty_delta = new_qty - old_qty

    current_stock = Decimal(str(product.stock or 0)).quantize(_QTY, rounding=ROUND_HALF_UP)
    stock_before_receipt = current_stock - old_qty
    new_stock = stock_before_receipt + new_qty
    if new_stock < 0:
        raise HTTPException(status_code=400, detail="Cannot reduce receipt below current available stock")

    product.stock = new_stock
    if unit_cost is not None:
        product.cost = unit_cost

    supplier_ref = (data.supplier_ref or "").strip() or None
    notes = (data.notes or "").strip() or None

    receipt.qty = new_qty
    receipt.unit_cost = unit_cost
    receipt.total_cost = total_cost
    receipt.receive_date = data.receive_date
    receipt.supplier_ref = supplier_ref
    receipt.notes = notes

    move = await _get_receipt_move(db, receipt.id)
    if move is not None:
        move.qty_before = stock_before_receipt
        move.qty = new_qty
        move.qty_after = new_stock
        move.note = f"Receipt {receipt.ref_number}"

    expense_ref = await _sync_receipt_expense(
        db,
        receipt=receipt,
        product_name=product.name,
        qty=new_qty,
        total_cost=total_cost,
        receive_date=data.receive_date,
        supplier_ref=supplier_ref,
        product_type=data.product_type,
    )

    record(
        db,
        "Receive",
        "update_receipt",
        f"{receipt.ref_number} updated",
        user=current_user,
        ref_type="receipt",
        ref_id=receipt.id,
    )
    await db.commit()

    return {
        "id": receipt.id,
        "ref_number": receipt.ref_number,
        "product_id": product.id,
        "product_name": product.name,
        "product_sku": product.sku,
        "receive_date": receipt.receive_date.isoformat() if receipt.receive_date else None,
        "qty": float(new_qty),
        "unit_cost": float(unit_cost) if unit_cost is not None else None,
        "total_cost": float(total_cost) if total_cost is not None else None,
        "supplier_ref": supplier_ref,
        "notes": notes,
        "product_type": data.product_type,
        "expense_id": receipt.expense_id,
        "expense_ref": expense_ref,
        "received_by": receipt.user.name if receipt.user else None,
    }


async def delete_receipt(
    db: AsyncSession,
    receipt_id: int,
    current_user: User,
) -> dict[str, Any]:
    receipt = await _get_receipt_or_404(db, receipt_id)
    product = receipt.product
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product {receipt.product_id} not found")

    receipt_qty = Decimal(str(receipt.qty or 0)).quantize(_QTY, rounding=ROUND_HALF_UP)
    current_stock = Decimal(str(product.stock or 0)).quantize(_QTY, rounding=ROUND_HALF_UP)
    new_stock = current_stock - receipt_qty
    if new_stock < 0:
        raise HTTPException(status_code=400, detail="Cannot delete receipt because stock has already been consumed")

    product.stock = new_stock

    # Reverse supplier balance if there was unpaid credit on this receipt
    if receipt.supplier_id is not None:
        total_cost = Decimal(str(receipt.total_cost or 0))
        paid_cash  = Decimal(str(receipt.amount_paid or 0))
        unpaid     = total_cost - paid_cash
        if unpaid > 0:
            supplier = receipt.supplier
            if supplier is None:
                sup_result = await db.execute(
                    select(Supplier).where(Supplier.id == receipt.supplier_id)
                )
                supplier = sup_result.scalar_one_or_none()
            if supplier is not None:
                supplier.balance = Decimal(str(supplier.balance or 0)) - unpaid

    move = await _get_receipt_move(db, receipt.id)
    if move is not None:
        await db.delete(move)

    if receipt.expense_id:
        expense_result = await db.execute(
            select(Expense)
            .options(selectinload(Expense.category))
            .where(Expense.id == receipt.expense_id)
        )
        expense = expense_result.scalar_one_or_none()
        await _delete_expense_bundle(db, expense)

    deleted_ref = receipt.ref_number
    await db.delete(receipt)
    record(
        db,
        "Receive",
        "delete_receipt",
        f"{deleted_ref} deleted",
        user=current_user,
        ref_type="receipt",
        ref_id=receipt_id,
    )
    await db.commit()
    return {"ok": True, "id": receipt_id, "ref_number": deleted_ref}


async def create_receipt_batch(
    db: AsyncSession,
    data: BatchReceiptCreate,
    current_user: User,
) -> dict[str, Any]:
    """
    Multi-product receive submitted in one form.

    Payment split: amount_paid is distributed across line items proportionally
    to each line's total cost. All items share supplier_id.

    All items are processed inside a single transaction:
    one commit at the end — if any product is invalid the whole batch rolls back.
    """
    # First pass — compute per-line totals so we can distribute amount_paid proportionally.
    line_totals: list[Decimal] = []
    for item in data.items:
        if item.unit_cost is not None and item.unit_cost > 0:
            t = (Decimal(str(item.qty)) * Decimal(str(item.unit_cost))).quantize(
                _MONEY, rounding=ROUND_HALF_UP
            )
        else:
            t = Decimal("0")
        line_totals.append(t)
    grand_total = sum(line_totals, Decimal("0"))

    # Determine total amount_paid for the batch
    if data.amount_paid is None:
        # No supplier ⇒ implicit fully cash. With supplier ⇒ fully credit.
        if data.supplier_id is None:
            total_paid = grand_total
        else:
            total_paid = Decimal("0")
    else:
        total_paid = Decimal(str(data.amount_paid)).quantize(_MONEY, rounding=ROUND_HALF_UP)
        if total_paid > grand_total:
            total_paid = grand_total
        if total_paid < 0:
            total_paid = Decimal("0")

    # Distribute total_paid across lines proportionally; assign rounding remainder to last paid line.
    per_line_paid: list[Decimal] = []
    if grand_total > 0 and total_paid > 0:
        running = Decimal("0")
        for i, lt in enumerate(line_totals):
            if i == len(line_totals) - 1:
                share = total_paid - running
            else:
                share = (total_paid * lt / grand_total).quantize(_MONEY, rounding=ROUND_HALF_UP)
                running += share
            if share < 0:
                share = Decimal("0")
            if share > lt:
                share = lt
            per_line_paid.append(share)
    else:
        per_line_paid = [Decimal("0")] * len(line_totals)

    receipts: list[dict[str, Any]] = []
    for idx, item in enumerate(data.items):
        paid_for_line = float(per_line_paid[idx]) if line_totals[idx] > 0 else None
        line = ReceiptCreate(
            product_id=item.product_id,
            qty=item.qty,
            unit_cost=item.unit_cost,
            receive_date=data.receive_date,
            supplier_ref=data.supplier_ref,
            supplier_id=data.supplier_id,
            amount_paid=paid_for_line,
            notes=data.notes,
            product_type=data.product_type,
            location_id=data.location_id,
            farm_id=data.farm_id,
            is_animal_expense=data.is_animal_expense,
            expense_category_id=data.expense_category_id,
        )
        receipts.append(await _create_receipt_core(db, line, current_user))

    await db.commit()

    total_cost   = sum(r["total_cost"]    or 0 for r in receipts)
    total_paid_f = sum(r["amount_paid"]   or 0 for r in receipts)
    total_unpaid = sum(r["amount_unpaid"] or 0 for r in receipts)
    return {
        "count":         len(receipts),
        "total_cost":    round(total_cost, 2),
        "total_paid":    round(total_paid_f, 2),
        "total_unpaid":  round(total_unpaid, 2),
        "receipts":      receipts,
    }


async def list_receipts(
    db: AsyncSession,
    *,
    skip: int = 0,
    limit: int = 50,
    product_id: Optional[int] = None,
) -> dict[str, Any]:
    """Paginated receipt history with product, user, supplier, and expense refs."""
    base = select(ProductReceipt)
    if product_id is not None:
        base = base.where(ProductReceipt.product_id == product_id)

    count_result = await db.execute(
        select(func.count()).select_from(base.subquery())
    )
    total = count_result.scalar() or 0

    rows_stmt = (
        base.options(
            selectinload(ProductReceipt.product),
            selectinload(ProductReceipt.user),
            selectinload(ProductReceipt.supplier),
            selectinload(ProductReceipt.location),
            selectinload(ProductReceipt.expense).selectinload(Expense.category),
        )
        .order_by(ProductReceipt.receive_date.desc(), ProductReceipt.id.desc())
        .offset(skip)
        .limit(limit)
    )
    result   = await db.execute(rows_stmt)
    receipts = result.scalars().all()

    def _row(r: ProductReceipt) -> dict[str, Any]:
        total_cost = float(r.total_cost) if r.total_cost is not None else None
        paid       = float(r.amount_paid) if r.amount_paid is not None else 0.0
        unpaid     = (total_cost - paid) if (total_cost is not None) else None
        return {
            "id":             r.id,
            "ref_number":     r.ref_number,
            "product_id":     r.product_id,
            "product_name":   r.product.name if r.product else None,
            "product_sku":    r.product.sku  if r.product else None,
            "receive_date":   r.receive_date.isoformat() if r.receive_date else None,
            "qty":            float(r.qty),
            "unit_cost":      float(r.unit_cost) if r.unit_cost is not None else None,
            "total_cost":     total_cost,
            "supplier_ref":   r.supplier_ref,
            "supplier_id":    r.supplier_id,
            "supplier_name":  r.supplier.name if r.supplier else None,
            "amount_paid":    paid,
            "amount_unpaid":  unpaid if (unpaid and unpaid > 0) else 0.0,
            "payment_method": (r.expense.payment_method if r.expense else None),
            "notes":          r.notes,
            "expense_category_name": r.expense.category.name if r.expense and r.expense.category else None,
            "expense_id":     r.expense_id,
            "expense_ref":    r.expense.ref_number if r.expense else None,
            "received_by":    r.user.name if r.user else None,
            "created_at":     r.created_at.isoformat() if r.created_at else None,
            "location_id":    r.location_id,
            "location_name":  r.location.name if r.location else None,
        }

    return {
        "total": total,
        "items": [_row(r) for r in receipts],
    }