from datetime import date as date_type
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.log import record
from app.models.accounting import Account, Journal, JournalEntry
from app.models.animal import AnimalGroup
from app.models.carbon import CarbonEmissionFactor, CarbonLog
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.user import User
from app.schemas.expense import (
    ExpenseCategoryCreate,
    ExpenseCategoryUpdate,
    ExpenseCreate,
    ExpenseUpdate,
)

SALARY_CATEGORY_NAME = "Salaries & Wages"
SALARY_ACCOUNT_CODE = "5006"


def _clean_text(value: Optional[str]) -> Optional[str]:
    return (value or "").strip() or None


def _payment_account_code(payment_method: str) -> str:
    return "1000" if payment_method in {"cash", "card"} else "1200"


def _parse_filter_date(value: Optional[str]) -> Optional[date_type]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date filter '{value}' - use YYYY-MM-DD",
        ) from None


async def _next_expense_reference(db: AsyncSession) -> str:
    result = await db.execute(select(func.max(Expense.id)))
    max_id = result.scalar() or 0
    return f"EXP-{str(max_id + 1).zfill(5)}"


async def _get_or_create_account(
    db: AsyncSession,
    account_code: str,
    *,
    account_name: Optional[str] = None,
) -> Account:
    result = await db.execute(select(Account).where(Account.code == account_code))
    account = result.scalar_one_or_none()
    if account:
        return account

    account = Account(
        code=account_code,
        name=account_name or f"Account {account_code}",
        type="expense" if account_code.startswith("5") else "asset",
        balance=0,
    )
    db.add(account)
    await db.flush()
    return account


async def _post_expense_journal(
    db: AsyncSession,
    *,
    description: str,
    amount: float,
    expense_account_code: str,
    payment_method: str,
    user_id: Optional[int],
) -> Journal:
    journal = Journal(ref_type="expense", description=description, user_id=user_id)
    db.add(journal)
    await db.flush()

    entries = [
        (expense_account_code, amount, 0),
        (_payment_account_code(payment_method), 0, amount),
    ]
    for account_code, debit, credit in entries:
        account = await _get_or_create_account(db, account_code)
        db.add(
            JournalEntry(
                journal_id=journal.id,
                account_id=account.id,
                debit=debit,
                credit=credit,
            )
        )
        account.balance += Decimal(str(debit)) - Decimal(str(credit))

    return journal


async def _reverse_expense_journal(db: AsyncSession, expense: Expense) -> None:
    if not expense.category:
        return

    journal = Journal(
        ref_type="expense_reversal",
        description=f"Reversal - {expense.ref_number}",
        user_id=expense.user_id,
    )
    db.add(journal)
    await db.flush()

    entries = [
        (_payment_account_code(expense.payment_method), float(expense.amount), 0),
        (expense.category.account_code, 0, float(expense.amount)),
    ]
    for account_code, debit, credit in entries:
        result = await db.execute(select(Account).where(Account.code == account_code))
        account = result.scalar_one_or_none()
        if not account:
            continue

        db.add(
            JournalEntry(
                journal_id=journal.id,
                account_id=account.id,
                debit=debit,
                credit=credit,
            )
        )
        account.balance += Decimal(str(debit)) - Decimal(str(credit))


async def _get_active_category(db: AsyncSession, category_id: int) -> ExpenseCategory:
    result = await db.execute(
        select(ExpenseCategory).where(
            ExpenseCategory.id == category_id,
            ExpenseCategory.is_active == "1",
        )
    )
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    return category


async def _validate_animal_group(
    db: AsyncSession, animal_group_id: Optional[int]
) -> Optional[int]:
    """Ensures the referenced animal group exists (returns the id or None).
    A 0 / falsy value clears the link."""
    if not animal_group_id:
        return None
    group = (
        await db.execute(
            select(AnimalGroup).where(AnimalGroup.id == animal_group_id)
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=400, detail="Animal group not found")
    return group.id


async def _get_or_create_salary_category(db: AsyncSession) -> ExpenseCategory:
    account = await _get_or_create_account(
        db,
        SALARY_ACCOUNT_CODE,
        account_name=SALARY_CATEGORY_NAME,
    )
    account.type = "expense"
    account.name = account.name or SALARY_CATEGORY_NAME

    result = await db.execute(
        select(ExpenseCategory).where(ExpenseCategory.name == SALARY_CATEGORY_NAME)
    )
    category = result.scalar_one_or_none()
    if category:
        category.account_code = SALARY_ACCOUNT_CODE
        category.is_active = "1"
        return category

    category = ExpenseCategory(
        name=SALARY_CATEGORY_NAME,
        account_code=SALARY_ACCOUNT_CODE,
        description="Payroll salary payments",
        is_active="1",
    )
    db.add(category)
    await db.flush()
    return category


async def list_categories(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(ExpenseCategory)
        .options(selectinload(ExpenseCategory.expenses))
        .where(ExpenseCategory.is_active == "1")
        .order_by(ExpenseCategory.account_code)
    )
    categories = result.scalars().all()
    return [
        {
            "id": category.id,
            "name": category.name,
            "account_code": category.account_code,
            "description": category.description or "",
            "count": len(category.expenses),
            "total": float(sum(expense.amount for expense in category.expenses)),
            "unit_price": float(category.unit_price) if category.unit_price is not None else None,
            "unit_name": category.unit_name,
            "carbon_factor_key": category.carbon_factor_key or "",
        }
        for category in categories
    ]


async def create_category(db: AsyncSession, data: ExpenseCategoryCreate) -> dict:
    category_name = data.name.strip()
    result = await db.execute(select(ExpenseCategory).where(ExpenseCategory.name == category_name))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Category name already exists")

    if data.account_code and data.account_code.strip():
        account_code = data.account_code.strip()
    else:
        categories_result = await db.execute(select(ExpenseCategory))
        existing_codes = [
            int(category.account_code)
            for category in categories_result.scalars().all()
            if category.account_code
            and category.account_code.isdigit()
            and 5000 <= int(category.account_code) <= 5999
        ]
        account_code = str(max(existing_codes) + 1) if existing_codes else "5001"

    await _get_or_create_account(db, account_code, account_name=category_name)

    category = ExpenseCategory(
        name=category_name,
        account_code=account_code,
        description=_clean_text(data.description),
        unit_price=(data.unit_price if data.unit_price and data.unit_price > 0 else None),
        unit_name=_clean_text(data.unit_name),
        carbon_factor_key=_clean_text(data.carbon_factor_key),
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return {"id": category.id, "name": category.name, "account_code": category.account_code}


async def update_category(
    db: AsyncSession,
    category_id: int,
    data: ExpenseCategoryUpdate,
    current_user: User,
) -> dict:
    """Edit an existing expense category (name / unit pricing / carbon factor)."""
    result = await db.execute(select(ExpenseCategory).where(ExpenseCategory.id == category_id))
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")

    if data.name is not None:
        new_name = data.name.strip()
        if new_name and new_name != category.name:
            # uniqueness check
            existing = await db.execute(
                select(ExpenseCategory).where(
                    ExpenseCategory.name == new_name,
                    ExpenseCategory.id != category.id,
                )
            )
            if existing.scalar_one_or_none():
                raise HTTPException(status_code=400, detail="Category name already exists")
            category.name = new_name
    if data.description is not None:
        category.description = _clean_text(data.description)
    if data.unit_price is not None:
        category.unit_price = data.unit_price if data.unit_price > 0 else None
    if data.unit_name is not None:
        category.unit_name = _clean_text(data.unit_name)
    if data.carbon_factor_key is not None:
        # Accept "" to clear; validate non-empty keys against the catalog.
        key = data.carbon_factor_key.strip()
        if key:
            check = await db.execute(
                select(CarbonEmissionFactor).where(
                    CarbonEmissionFactor.source_key == key,
                    CarbonEmissionFactor.is_active == True,
                )
            )
            if not check.scalar_one_or_none():
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown or inactive carbon factor: {key}",
                )
            category.carbon_factor_key = key
        else:
            category.carbon_factor_key = None

    record(
        db,
        "Expenses",
        "update_category",
        f"Updated category {category.name}",
        user=current_user,
        ref_type="expense_category",
        ref_id=category.id,
    )
    await db.commit()
    await db.refresh(category)
    return {
        "ok": True,
        "id": category.id,
        "name": category.name,
        "unit_price": float(category.unit_price) if category.unit_price is not None else None,
        "unit_name": category.unit_name,
        "carbon_factor_key": category.carbon_factor_key,
    }


async def list_carbon_factors(db: AsyncSession) -> list[dict]:
    """Active carbon emission factors for the category-edit dropdown."""
    result = await db.execute(
        select(CarbonEmissionFactor)
        .where(CarbonEmissionFactor.is_active == True)
        .order_by(CarbonEmissionFactor.source_type, CarbonEmissionFactor.label)
    )
    return [
        {
            "source_key":  f.source_key,
            "label":       f.label,
            "unit":        f.unit,
            "factor":      float(f.factor_kg_co2e_per_unit),
            "source_type": f.source_type,
        }
        for f in result.scalars().all()
    ]


async def _create_carbon_log_for_expense(
    db: AsyncSession,
    expense: Expense,
    category: ExpenseCategory,
    consumption: float,
    current_user: User,
) -> None:
    """Auto-create a CarbonLog when an expense's category has a mapped factor."""
    if not category.carbon_factor_key or consumption <= 0:
        return
    result = await db.execute(
        select(CarbonEmissionFactor).where(
            CarbonEmissionFactor.source_key == category.carbon_factor_key,
            CarbonEmissionFactor.is_active == True,
        )
    )
    factor = result.scalar_one_or_none()
    if not factor:
        return
    qty = Decimal(str(consumption))
    kg_co2e = (qty * factor.factor_kg_co2e_per_unit).quantize(Decimal("0.0001"))
    db.add(CarbonLog(
        factor_id=factor.id,
        farm_id=expense.farm_id,
        user_id=current_user.id,
        log_date=expense.expense_date,
        quantity=qty,
        kg_co2e=kg_co2e,
        ref_type="expense",
        ref_id=expense.id,
        notes=f"Auto-logged from expense {expense.ref_number} ({category.name})",
    ))


async def archive_category(db: AsyncSession, category_id: int) -> dict:
    result = await db.execute(
        select(ExpenseCategory)
        .options(selectinload(ExpenseCategory.expenses))
        .where(ExpenseCategory.id == category_id)
    )
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    if category.expenses:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a category that has expenses. Archive it instead.",
        )

    category.is_active = "0"
    await db.commit()
    return {"ok": True}


async def list_expenses(
    db: AsyncSession,
    *,
    category_id: Optional[int] = None,
    month: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict]:
    statement = select(Expense).options(
        selectinload(Expense.category),
        selectinload(Expense.user),
        selectinload(Expense.farm),
        selectinload(Expense.animal_group),
    )
    if category_id:
        statement = statement.where(Expense.category_id == category_id)
    start_date = _parse_filter_date(date_from)
    end_date = _parse_filter_date(date_to)
    if start_date:
        statement = statement.where(Expense.expense_date >= start_date)
    if end_date:
        statement = statement.where(Expense.expense_date <= end_date)
    if month and not start_date and not end_date:
        try:
            year, month_number = int(month[:4]), int(month[5:7])
            statement = statement.where(
                func.extract("year", Expense.expense_date) == year,
                func.extract("month", Expense.expense_date) == month_number,
            )
        except (ValueError, IndexError):
            raise HTTPException(status_code=400, detail="Invalid month filter - use YYYY-MM") from None

    statement = statement.order_by(Expense.expense_date.desc(), Expense.id.desc())
    result = await db.execute(statement)
    expenses = result.scalars().all()
    return [
        {
            "id": expense.id,
            "ref_number": expense.ref_number,
            "category": expense.category.name if expense.category else "—",
            "category_id": expense.category_id,
            "account_code": expense.category.account_code if expense.category else "—",
            "expense_date": str(expense.expense_date),
            "amount": float(expense.amount),
            "payment_method": expense.payment_method,
            "vendor": expense.vendor or "",
            "description": expense.description or "",
            "created_by": expense.user.name if expense.user else "—",
            "farm_id": expense.farm_id,
            "farm_name": expense.farm.name if expense.farm else None,
            "animal_group_id":   expense.animal_group_id,
            "animal_group_name": expense.animal_group.name if expense.animal_group else None,
            "is_animal_expense": bool(getattr(expense, "is_animal_expense", False)),
        }
        for expense in expenses
    ]


async def get_summary(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    this_month_result = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            func.extract("year", Expense.expense_date) == now.year,
            func.extract("month", Expense.expense_date) == now.month,
        )
    )
    this_month = this_month_result.scalar() or 0

    last_month_year = now.year if now.month > 1 else now.year - 1
    last_month_number = now.month - 1 if now.month > 1 else 12
    last_month_result = await db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            func.extract("year", Expense.expense_date) == last_month_year,
            func.extract("month", Expense.expense_date) == last_month_number,
        )
    )
    last_month = last_month_result.scalar() or 0

    total_result = await db.execute(select(func.coalesce(func.sum(Expense.amount), 0)))
    total_all = total_result.scalar() or 0

    categories_result = await db.execute(
        select(ExpenseCategory).where(ExpenseCategory.is_active == "1")
    )
    breakdown = []
    for category in categories_result.scalars().all():
        category_total_result = await db.execute(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.category_id == category.id,
                func.extract("year", Expense.expense_date) == now.year,
                func.extract("month", Expense.expense_date) == now.month,
            )
        )
        category_total = category_total_result.scalar() or 0
        if float(category_total) > 0:
            breakdown.append({"name": category.name, "total": float(category_total)})

    breakdown.sort(key=lambda item: item["total"], reverse=True)
    return {
        "this_month": float(this_month),
        "last_month": float(last_month),
        "total_all": float(total_all),
        "breakdown": breakdown,
    }


async def create_expense_entry(db: AsyncSession, data: ExpenseCreate, current_user: User) -> dict:
    category = await _get_active_category(db, data.category_id)
    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")

    try:
        expense_date = date_type.fromisoformat(data.expense_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format - use YYYY-MM-DD") from exc

    # Validate the animal group up-front (if provided)
    animal_group_id = await _validate_animal_group(db, data.animal_group_id)

    reference_number = await _next_expense_reference(db)
    amount = round(float(data.amount), 2)
    vendor = _clean_text(data.vendor)
    description = _clean_text(data.description)

    # ── consumption + unit price snapshot ──
    # If unit_price_used is provided on the entry, use it; otherwise fall back to
    # the category's default unit_price. Then derive consumption from amount if the
    # caller didn't supply one explicitly.
    unit_price_used: Optional[float] = None
    consumption: Optional[float] = None
    if data.unit_price_used is not None and data.unit_price_used > 0:
        unit_price_used = round(float(data.unit_price_used), 4)
    elif category.unit_price is not None and float(category.unit_price) > 0:
        unit_price_used = float(category.unit_price)

    if data.consumption is not None and data.consumption > 0:
        consumption = round(float(data.consumption), 4)
    elif unit_price_used and unit_price_used > 0:
        consumption = round(amount / unit_price_used, 4)

    journal = await _post_expense_journal(
        db,
        description=f"{category.name} expense - {reference_number}" + (f" - {vendor}" if vendor else ""),
        amount=amount,
        expense_account_code=category.account_code,
        payment_method=data.payment_method,
        user_id=current_user.id,
    )

    expense = Expense(
        ref_number=reference_number,
        category_id=category.id,
        user_id=current_user.id,
        expense_date=expense_date,
        amount=amount,
        payment_method=data.payment_method,
        vendor=vendor,
        description=description,
        journal_id=journal.id,
        farm_id=data.farm_id or None,
        animal_group_id=animal_group_id,
        is_animal_expense=bool(getattr(data, "is_animal_expense", False)),
        consumption=consumption,
        unit_price_used=unit_price_used,
    )
    db.add(expense)
    await db.flush()  # need expense.id for the carbon log

    # Auto-log to carbon footprint when the category has an emission factor
    # mapped and we have a positive consumption quantity to apply it to.
    if consumption and consumption > 0 and category.carbon_factor_key:
        await _create_carbon_log_for_expense(db, expense, category, consumption, current_user)

    record(
        db,
        "Expenses",
        "add_expense",
        f"{category.name} - {reference_number} - {amount:.2f} - {data.payment_method}"
        + (f" - {consumption} {category.unit_name}" if consumption and category.unit_name else ""),
        user=current_user,
        ref_type="expense",
        ref_id=0,
    )
    await db.commit()
    await db.refresh(expense)
    return {
        "id": expense.id,
        "ref_number": expense.ref_number,
        "amount": float(expense.amount),
        "category": category.name,
        "consumption": float(expense.consumption) if expense.consumption is not None else None,
        "unit_name": category.unit_name,
        "carbon_logged": bool(consumption and consumption > 0 and category.carbon_factor_key),
    }


async def create_payroll_expense(
    db: AsyncSession,
    payroll,
    current_user: User,
    *,
    payment_method: str = "cash",
    paid_date: Optional[date_type] = None,
    amount_override: Optional[float] = None,
) -> Expense:
    if payment_method not in {"cash", "bank_transfer", "card"}:
        raise HTTPException(status_code=400, detail="Invalid payment method")

    # For a partial payment the recorded expense is the cash actually paid out,
    # not the full net salary (the unpaid remainder is settled as days off).
    if amount_override is not None:
        amount = round(float(amount_override), 2)
    else:
        amount = round(float(payroll.net_salary or 0), 2)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Payroll net salary must be greater than 0")

    existing_result = await db.execute(
        select(Expense)
        .options(selectinload(Expense.category), selectinload(Expense.farm))
        .where(Expense.payroll_id == payroll.id)
    )
    existing = existing_result.scalar_one_or_none()
    if existing:
        return existing

    category = await _get_or_create_salary_category(db)
    employee = getattr(payroll, "employee", None)
    employee_name = getattr(employee, "name", None) or f"Employee #{payroll.employee_id}"
    farm_id = getattr(employee, "farm_id", None) or None
    is_animal = bool(getattr(employee, "works_with_animals", False))
    payment_date = paid_date or date_type.today()
    reference_number = await _next_expense_reference(db)
    description = f"Salary payment - {employee_name} - {payroll.period} - payroll #{payroll.id}"

    journal = await _post_expense_journal(
        db,
        description=f"Salaries & Wages expense - {reference_number} - {employee_name}",
        amount=amount,
        expense_account_code=category.account_code,
        payment_method=payment_method,
        user_id=current_user.id,
    )

    expense = Expense(
        ref_number=reference_number,
        category_id=category.id,
        category=category,
        user_id=current_user.id,
        expense_date=payment_date,
        amount=amount,
        payment_method=payment_method,
        vendor=employee_name,
        description=description,
        journal_id=journal.id,
        payroll_id=payroll.id,
        farm_id=farm_id,
        is_animal_expense=is_animal,
    )
    db.add(expense)
    await db.flush()
    record(
        db,
        "Expenses",
        "add_payroll_expense",
        f"{SALARY_CATEGORY_NAME} - {reference_number} - {amount:.2f} - payroll #{payroll.id}",
        user=current_user,
        ref_type="expense",
        ref_id=expense.id or 0,
    )
    return expense


async def update_expense_entry(
    db: AsyncSession,
    expense_id: int,
    data: ExpenseUpdate,
    current_user: User,
) -> dict:
    result = await db.execute(
        select(Expense)
        .options(selectinload(Expense.category))
        .where(Expense.id == expense_id)
    )
    expense = result.scalar_one_or_none()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")

    await _reverse_expense_journal(db, expense)

    if data.category_id is not None:
        expense.category = await _get_active_category(db, data.category_id)
        expense.category_id = expense.category.id
    if data.expense_date is not None:
        try:
            expense.expense_date = date_type.fromisoformat(data.expense_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid date format") from exc
    if data.amount is not None:
        if data.amount <= 0:
            raise HTTPException(status_code=400, detail="Amount must be greater than 0")
        expense.amount = round(float(data.amount), 2)
    if data.payment_method is not None:
        expense.payment_method = data.payment_method
    if data.vendor is not None:
        expense.vendor = _clean_text(data.vendor)
    if data.description is not None:
        expense.description = _clean_text(data.description)
    if data.farm_id is not None:
        expense.farm_id = data.farm_id or None
    if data.animal_group_id is not None:
        expense.animal_group_id = await _validate_animal_group(db, data.animal_group_id)
    if getattr(data, "is_animal_expense", None) is not None:
        expense.is_animal_expense = bool(data.is_animal_expense)

    if expense.category is None:
        category_result = await db.execute(
            select(ExpenseCategory).where(ExpenseCategory.id == expense.category_id)
        )
        expense.category = category_result.scalar_one_or_none()

    journal = await _post_expense_journal(
        db,
        description=f"{expense.category.name} expense (edited) - {expense.ref_number}",
        amount=float(expense.amount),
        expense_account_code=expense.category.account_code,
        payment_method=expense.payment_method,
        user_id=current_user.id,
    )
    expense.journal_id = journal.id

    record(
        db,
        "Expenses",
        "edit_expense",
        f"Edited {expense.ref_number} - {float(expense.amount):.2f}",
        user=current_user,
        ref_type="expense",
        ref_id=expense.id,
    )
    await db.commit()
    return {"ok": True}


async def delete_expense_entry(
    db: AsyncSession,
    expense_id: int,
    current_user: User,
) -> dict:
    result = await db.execute(
        select(Expense)
        .options(selectinload(Expense.category))
        .where(Expense.id == expense_id)
    )
    expense = result.scalar_one_or_none()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")

    reference_number = expense.ref_number
    await _reverse_expense_journal(db, expense)
    await db.delete(expense)
    record(
        db,
        "Expenses",
        "delete_expense",
        f"Deleted {reference_number} - journal reversed",
        user=current_user,
        ref_type="expense",
        ref_id=expense_id,
    )
    await db.commit()
    return {"ok": True}


async def get_cost_allocation(
    db: AsyncSession,
    *,
    farm_id: int | str,
    date_from: str,
    date_to: str,
) -> dict:
    try:
        start_date = date_type.fromisoformat(date_from)
        end_date = date_type.fromisoformat(date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format - use YYYY-MM-DD") from exc

    farm_selector = str(farm_id).strip().lower()
    selected_farm_ids: list[int]
    farm_scope_label: str
    include_unassigned_salary = False
    if farm_selector == "both":
        farms_result = await db.execute(
            select(Farm).where(Farm.is_active == 1).order_by(Farm.name)
        )
        farms = farms_result.scalars().all()
        if not farms:
            raise HTTPException(status_code=404, detail="No active farms found")
        selected_farm_ids = [farm.id for farm in farms]
        farm_scope_label = "Both Farms"
        include_unassigned_salary = True
    else:
        try:
            single_farm_id = int(farm_selector)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid farm selection") from exc

        farm_result = await db.execute(select(Farm).where(Farm.id == single_farm_id))
        farm = farm_result.scalar_one_or_none()
        if not farm:
            raise HTTPException(status_code=404, detail="Farm not found")
        selected_farm_ids = [farm.id]
        farm_scope_label = farm.name

    expense_scope = Expense.farm_id.in_(selected_farm_ids)
    if include_unassigned_salary:
        expense_scope = or_(
            expense_scope,
            (Expense.farm_id.is_(None)) & (ExpenseCategory.name == SALARY_CATEGORY_NAME),
        )

    expenses_result = await db.execute(
        select(Expense)
        .join(ExpenseCategory, Expense.category_id == ExpenseCategory.id)
        .options(selectinload(Expense.category))
        .where(
            expense_scope,
            Expense.expense_date >= start_date,
            Expense.expense_date <= end_date,
        )
    )
    expenses = expenses_result.scalars().all()
    total_cost = sum(float(expense.amount) for expense in expenses)

    cost_by_category: dict[str, float] = {}
    salary_cost = 0.0
    unassigned_salary_cost = 0.0
    for expense in expenses:
        category_name = expense.category.name if expense.category else "Other"
        amount = float(expense.amount)
        cost_by_category[category_name] = cost_by_category.get(category_name, 0) + amount
        if category_name == SALARY_CATEGORY_NAME:
            salary_cost += amount
            if expense.farm_id is None:
                unassigned_salary_cost += amount

    deliveries_result = await db.execute(
        select(FarmDelivery)
        .options(selectinload(FarmDelivery.items).selectinload(FarmDeliveryItem.product))
        .where(
            FarmDelivery.farm_id.in_(selected_farm_ids),
            FarmDelivery.delivery_date >= start_date,
            FarmDelivery.delivery_date <= end_date,
        )
    )
    deliveries = deliveries_result.scalars().all()

    quantity_by_product: dict[int, dict] = {}
    for delivery in deliveries:
        for item in delivery.items:
            product = item.product
            if item.product_id not in quantity_by_product:
                quantity_by_product[item.product_id] = {
                    "product_id": item.product_id,
                    "product_name": product.name if product else f"#{item.product_id}",
                    "unit": item.unit or (product.unit if product else "kg"),
                    "sale_price": float(product.price) if product else 0,
                    "total_qty": 0,
                }
            quantity_by_product[item.product_id]["total_qty"] += float(item.qty)

    total_quantity = sum(item["total_qty"] for item in quantity_by_product.values())
    estimated_revenue = sum(
        info["total_qty"] * info["sale_price"] for info in quantity_by_product.values()
    )
    products = []
    for product_id, info in quantity_by_product.items():
        share = info["total_qty"] / total_quantity if total_quantity > 0 else 0
        # Salary & Wages expenses are already included in total_cost above; do not add them again.
        allocated_cost = total_cost * share
        cost_per_unit = allocated_cost / info["total_qty"] if info["total_qty"] > 0 else 0
        profit_per_unit = info["sale_price"] - cost_per_unit
        products.append(
            {
                "product_id": product_id,
                "product_name": info["product_name"],
                "unit": info["unit"],
                "total_qty": round(info["total_qty"], 3),
                "share_pct": round(share * 100, 1),
                "allocated_cost": round(allocated_cost, 2),
                "cost_per_unit": round(cost_per_unit, 2),
                "sale_price": round(info["sale_price"], 2),
                "profit_per_unit": round(profit_per_unit, 2),
                "profit_margin_pct": round(
                    (profit_per_unit / info["sale_price"] * 100) if info["sale_price"] > 0 else 0,
                    1,
                ),
            }
        )

    products.sort(key=lambda item: item["allocated_cost"], reverse=True)
    return {
        "farm_id": farm_id,
        "farm_ids": selected_farm_ids,
        "farm_name": farm_scope_label,
        "farm_scope_label": farm_scope_label,
        "date_from": date_from,
        "date_to": date_to,
        "total_cost": round(total_cost, 2),
        "total_expenses": round(total_cost, 2),
        "salary_cost": round(salary_cost, 2),
        "labor_cost": round(salary_cost, 2),
        "unassigned_salary_cost": round(unassigned_salary_cost, 2),
        "estimated_revenue": round(estimated_revenue, 2),
        "net_profit": round(estimated_revenue - total_cost, 2),
        "total_qty": round(total_quantity, 3),
        "cost_by_category": [
            {"name": name, "amount": round(amount, 2)}
            for name, amount in sorted(cost_by_category.items(), key=lambda item: -item[1])
        ],
        "products": products,
        "expense_count": len(expenses),
        "delivery_count": len(deliveries),
    }