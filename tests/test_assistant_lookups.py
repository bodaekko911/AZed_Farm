"""Ask — the read-only lookups added in two phases. Each one wraps the code its page already uses, so the figures are
checked against that code on the same seeded data. Phase 1: season_cost, consignment_stock, employee_balances,
activity_log. Phase 2: production_batches, stock_by_location, livestock, carbon_footprint.

A real async SQLite database; no model is called (the tools are run directly, as the model would through run_tool).
"""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.models  # noqa: F401  every table
from app.core.log import ActivityLog
from app.core.time_utils import today_local
from app.database import Base
from app.models.b2b import (B2BClient, B2BInvoice, B2BInvoiceItem, Consignment, ConsignmentItem, ConsignmentSale,
                            ConsignmentSaleItem, ConsignmentStockCount, ConsignmentStockCountItem)
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.hr import (Employee, EmployeeAllowanceAdvance, EmployeeLoan, EmployeeLoanRepayment,
                           EmployeePayrollDeduction)
from app.models.product import Product
from app.models.user import User
from app.services import assistant_lookups as lookups
from app.services import assistant_service as svc

TODAY = today_local()
MONTH = TODAY.strftime("%Y-%m")
SEASON = {"date_from": (TODAY - timedelta(days=60)).isoformat(), "date_to": TODAY.isoformat()}


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def admin():
    return SimpleNamespace(id=1, name="Boss", role="admin", permissions=None)


def viewer(perms: str):
    return SimpleNamespace(id=5, name="V", role="viewer", permissions=perms)


def utc(day: date, hour=9):
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


async def seeded():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()
    d1, d2 = TODAY - timedelta(days=40), TODAY - timedelta(days=10)
    db.add_all([
        User(id=1, name="Boss", email="b@x.test", password="x", role="admin"),
        User(id=2, name="سعيد المخزن", email="s@x.test", password="x", role="viewer"),
        Farm(id=1, name="North Farm", is_active=1), Farm(id=2, name="Habiba/SPC", is_active=1),
        Product(id=1, sku="TOM", name="Tomatoes", unit="kg", price=Decimal("20"), cost=Decimal("8"), stock=Decimal("100")),
        Product(id=2, sku="CUC", name="خيار بلدي", unit="kg", price=Decimal("12"), cost=Decimal("5"), stock=Decimal("50")),
        Product(id=3, sku="HON", name="Raw Honey", unit="jar", price=Decimal("150"), cost=Decimal("90"), stock=Decimal("40")),
        ExpenseCategory(id=1, name="Fertiliser", account_code="5001", is_active="1"),
        ExpenseCategory(id=2, name="Labour", account_code="5002", is_active="1"),
        Employee(id=1, name="عم سعيد", base_salary=Decimal("6000"), is_active=True),
        Employee(id=2, name="Mona Hassan", base_salary=Decimal("5000"), is_active=True),
        Employee(id=3, name="No Balance", base_salary=Decimal("4000"), is_active=True),
        B2BClient(id=1, name="Joud Market", payment_terms="consignment", discount_pct=Decimal("10"),
                  outstanding=Decimal("0")),
        B2BClient(id=2, name="Habiba Kitchen", payment_terms="consignment", discount_pct=Decimal("0"),
                  outstanding=Decimal("0")),
    ])
    await db.flush()
    db.add_all([
        Expense(id=1, category_id=1, farm_id=1, amount=Decimal("6000"), expense_date=d1, description="Fertiliser"),
        Expense(id=2, category_id=2, farm_id=1, amount=Decimal("4000"), expense_date=d2, description="Harvest"),
        FarmDelivery(id=1, delivery_number="FD-1", farm_id=1, delivery_date=d2),
    ])
    await db.flush()
    db.add_all([FarmDeliveryItem(delivery_id=1, product_id=1, qty=Decimal("800"), unit="kg"),
                FarmDeliveryItem(delivery_id=1, product_id=2, qty=Decimal("200"), unit="kg")])
    # Consignment: Joud got 10 kg tomatoes (old) and 6 jars of honey (recent); reported 3 kg sold, not settled.
    for n, (cid, pid, qty, price, when) in enumerate([(1, 1, 10, 20, TODAY - timedelta(days=50)),
                                                       (1, 3, 6, 150, TODAY - timedelta(days=5)),
                                                       (2, 2, 20, 12, TODAY - timedelta(days=30))], start=1):
        sub = qty * price
        disc = sub * (10 if cid == 1 else 0) / 100
        db.add(B2BInvoice(id=n, invoice_number=f"B2B-{n}", client_id=cid, invoice_type="consignment", status="unpaid",
                          subtotal=Decimal(str(sub)), discount=Decimal(str(disc)), total=Decimal(str(sub - disc)),
                          amount_paid=Decimal("0"), created_at=utc(when)))
        await db.flush()
        db.add(B2BInvoiceItem(invoice_id=n, product_id=pid, qty=Decimal(str(qty)), unit_price=Decimal(str(price)),
                              total=Decimal(str(sub))))
        db.add(Consignment(id=n, ref_number=f"CON-{n}", client_id=cid, invoice_id=n, status="active",
                           created_at=utc(when)))
        await db.flush()
        db.add(ConsignmentItem(consignment_id=n, product_id=pid, qty_sent=Decimal(str(qty)), qty_sold=0,
                               qty_returned=0, unit_price=Decimal(str(price))))
    db.add(ConsignmentSale(id=1, client_id=1, subtotal=0, discount=0, amount=0, created_at=utc(TODAY - timedelta(days=2))))
    await db.flush()
    db.add(ConsignmentSaleItem(sale_id=1, product_id=1, qty=Decimal("3"), unit_price=Decimal("20"), total=Decimal("60")))
    # The kitchen's shelf was counted after its consignment: 4 kg left.
    db.add(ConsignmentStockCount(id=1, client_id=2, counted_at=utc(TODAY - timedelta(days=3))))
    await db.flush()
    db.add(ConsignmentStockCountItem(count_id=1, product_id=2, qty=Decimal("4")))
    # HR: a loan this month half repaid, an older loan, an open allowance advance, a day deduction this month.
    first = TODAY.replace(day=1)
    db.add_all([
        EmployeeLoan(id=1, employee_id=1, loan_date=first, amount=Decimal("2000"), status="open"),
        EmployeeLoan(id=2, employee_id=1, loan_date=first - timedelta(days=40), amount=Decimal("1000"), status="open"),
        EmployeeLoan(id=3, employee_id=2, loan_date=first - timedelta(days=40), amount=Decimal("500"), status="cancelled"),
        EmployeeAllowanceAdvance(id=1, employee_id=2, advance_date=first, amount=Decimal("300"), status="open"),
        EmployeePayrollDeduction(employee_id=1, period=MONTH, type="day_deduction", days=Decimal("1"),
                                 amount=Decimal("200")),
    ])
    await db.flush()
    db.add(EmployeeLoanRepayment(loan_id=1, employee_id=1, repayment_date=first, amount=Decimal("500")))
    # Activity log, including entries with secrets in them.
    db.add_all([
        ActivityLog(user_id=1, user_name="Boss", user_role="admin", module="Products", action="edit_product",
                    description="Edited product: [TOM] Tomatoes — price 18 → 20", created_at=utc(TODAY)),
        ActivityLog(user_id=2, user_name="سعيد المخزن", user_role="viewer", module="Expenses", action="create_expense",
                    description="Added expense EXP-1 password=hunter2 token: 123456789:AAH-secretTOKENvalue_xyz",
                    created_at=utc(TODAY, 10)),
        ActivityLog(user_id=1, user_name="Boss", user_role="admin", module="Expenses", action="delete_expense",
                    description="Deleted EXP-9 api_key=sk-live-abcdefghijklmnop1234 Bearer eyJhbGciOi.abc",
                    created_at=utc(TODAY - timedelta(days=90))),
    ])
    await db.commit()
    return engine, db


def scenario(fn):
    async def go():
        engine, db = await seeded()
        try:
            return await fn(db)
        finally:
            await db.close()
            await engine.dispose()
    return run(go())


async def ask(db, who, name, **args):
    return json.loads(await svc.run_tool(db, who, name, json.dumps(args)))


# ── Who gets which tool ──────────────────────────────────────────────────────

def test_each_new_lookup_is_offered_only_with_its_permissions():
    names = ("season_cost", "consignment_stock", "employee_balances", "activity_log")
    assert set(names) <= set(svc.allowed_tools(admin()))
    assert "activity_log" not in svc.allowed_tools(SimpleNamespace(id=2, role="manager", permissions=None))
    assert "activity_log" not in svc.allowed_tools(viewer("page_farm,tab_farm_season,page_expenses,page_hr"))
    allowed = lambda perms: set(svc.allowed_tools(viewer(perms))) & set(names)
    assert allowed("page_farm,tab_farm_season,page_expenses") == {"season_cost"}
    assert allowed("page_farm,tab_farm_season") == set()                         # the tab's data needs page_expenses
    assert allowed("page_b2b,tab_b2b_consignment") == {"consignment_stock"}
    assert allowed("page_hr,tab_hr_payroll,action_hr_view_loans") == {"employee_balances"}
    assert allowed("page_hr,tab_hr_payroll") == set()                            # the loans need their own right
    names_in_schema = {s["function"]["name"] for s in svc._tool_schemas(svc.allowed_tools(admin()))}
    assert {"season_cost", "activity_log"} <= names_in_schema


def test_a_tool_the_user_may_not_use_is_refused_when_called():
    async def go(db):
        out = await ask(db, viewer("page_b2b,tab_b2b_consignment"), "activity_log")
        assert "not available" in out["error"]
    scenario(go)


# ── season_cost ──────────────────────────────────────────────────────────────

def test_season_cost_matches_season_analysis():
    async def go(db):
        from app.services.expense_service import get_cost_allocation
        out = await ask(db, admin(), "season_cost", farm="north", **SEASON)
        page = await get_cost_allocation(db, farm_id="1", date_from=SEASON["date_from"], date_to=SEASON["date_to"],
                                         allocation_method="quantity")
        assert out["farms"] == "North Farm" and out["total_cost"] == page["total_cost"] == 10000.0
        mine = {c["crop"]: c for c in out["crops"]}
        for p in page["products"]:
            c = mine[p["product_name"]]
            assert (c["cost"], c["cost_per_kg"], c["cost_per_unit"], c["kg_delivered"], c["selling_price_per_unit"]) == \
                (p["allocated_cost"], p["cost_per_kg"], p["cost_per_unit"], p["total_kg"], p["sale_price"])
        assert mine["Tomatoes"]["cost_per_kg"] == 10.0                    # 10,000 EGP over 1,000 kg
        assert mine["Tomatoes"]["selling_price_per_kg"] == 20.0           # kg products: per unit = per kg
        assert out["period"] == f"{SEASON['date_from']} to {SEASON['date_to']}" and "Season Analysis" in out["note"]
    scenario(go)


def test_season_cost_finds_an_arabic_crop_and_handles_an_empty_period():
    async def go(db):
        out = await ask(db, admin(), "season_cost", product="خيار", **SEASON)
        assert [c["crop"] for c in out["crops"]] == ["خيار بلدي"]
        empty = await ask(db, admin(), "season_cost", farm="Habiba/SPC", date_from="2020-01-01", date_to="2020-01-31")
        assert empty["crops"] == [] and empty["total_cost"] == 0
        bad = await ask(db, admin(), "season_cost", farm="Sunset Valley")
        assert "farm" in bad["error"]                                  # asks back instead of guessing
    scenario(go)


# ── consignment_stock ────────────────────────────────────────────────────────

def test_consignment_stock_matches_the_client_page_and_counts_reported_sales():
    async def go(db):
        from app.routers.b2b import _build_client_consignment_stock_payload
        out = await ask(db, admin(), "consignment_stock")
        page = await _build_client_consignment_stock_payload(1, db)
        joud = next(c for c in out["clients"] if c["client"] == "Joud Market")
        held = {i["product"]: i for i in joud["items"]}
        on_page = {i["name"]: i for i in page["items"] if i["qty_on_hand"] > 0}
        assert {k: v["qty_held"] for k, v in held.items()} == {k: v["qty_on_hand"] for k, v in on_page.items()}
        assert held["Tomatoes"]["qty_held"] == 7.0 and held["Tomatoes"]["reported_sold"] == 3.0   # not yet settled
        assert joud["value_at_consignment_price"] == page["totals"]["value_on_hand"] == 7 * 20 + 6 * 150
        assert joud["value_client_will_pay"] == page["totals"]["net_value_on_hand"]               # after the 10%
        assert joud["value_at_current_cost"] == 7 * 8 + 6 * 90
        assert joud["oldest_active_consignment"] == (TODAY - timedelta(days=50)).isoformat()
        assert joud["days_since_oldest_active"] == 50 and joud["days_since_latest"] == 5
        kitchen = next(c for c in out["clients"] if c["client"] == "Habiba Kitchen")
        assert kitchen["items"][0]["qty_held"] == 4.0 and kitchen["stock_count_date"]          # the count wins
        assert [c["client"] for c in out["clients"]] == ["Joud Market", "Habiba Kitchen"]      # oldest first
        assert "older stock may have been held longer" in out["note"] and "today's product cost" in out["note"]
    scenario(go)


def test_consignment_stock_for_one_client_by_name():
    async def go(db):
        out = await ask(db, admin(), "consignment_stock", client="المطبخ habiba kitchen")
        assert [c["client"] for c in out["clients"]] == ["Habiba Kitchen"]
        none = await ask(db, admin(), "consignment_stock", client="Unknown Shop")
        assert "error" in none
    scenario(go)


# ── employee_balances ────────────────────────────────────────────────────────

def test_employee_balances_match_hr_and_name_arabic_employees():
    async def go(db):
        from app.routers.hr import get_employee_loans
        out = await ask(db, admin(), "employee_balances", employee="سعيد", month=MONTH)
        [saeed] = out["employees"]
        hr = await get_employee_loans(1, db=db)
        assert saeed["employee"] == "عم سعيد"
        assert saeed["loans_outstanding"] == sum(l["balance"] for l in hr) == 2500.0
        assert saeed["loans_repaid_so_far"] == 500.0
        due = saeed[f"to_deduct_in_{MONTH}"]
        assert due == {"loans_due": 1500.0, "allowance_advances": 0.0, "day_deductions": 200.0,
                       "other_deductions": 0.0, "total": 1700.0}
        everyone = await ask(db, admin(), "employee_balances", month=MONTH)
        names = [e["employee"] for e in everyone["employees"]]
        assert names == ["عم سعيد", "Mona Hassan"] and "No Balance" not in names    # the cancelled loan isn't owed
        mona = everyone["employees"][1]
        assert mona["loans_outstanding"] == 0 and mona["open_allowance_advances"] == 300.0
        assert everyone["total_loans_outstanding"] == 2500.0 and "recovered in the month it is dated" in everyone["note"]
    scenario(go)


def test_employee_balances_for_a_month_with_nothing_due():
    async def go(db):
        out = await ask(db, admin(), "employee_balances", employee="mona", month="2020-01")
        assert out["employees"][0][f"to_deduct_in_2020-01"]["loans_due"] == 0.0
        assert "error" in await ask(db, admin(), "employee_balances", month="January")
    scenario(go)


# ── activity_log ─────────────────────────────────────────────────────────────

def test_activity_log_matches_the_audit_page_and_hides_secrets():
    async def go(db):
        from app.routers.audit_log import audit_log_data
        out = await ask(db, admin(), "activity_log", module="expenses")
        page = await audit_log_data(module="Expenses", action=None, user_name=None, date_from=None, date_to=None,
                                    search=None, page=1, page_size=50, db=db, _=None)
        assert [r["action"] for r in out["rows"]] == [r["action"] for r in page["rows"]] == \
            ["create_expense", "delete_expense"]
        text = json.dumps(out, ensure_ascii=False)
        for secret in ("hunter2", "AAH-secretTOKEN", "sk-live-abcdefghijklmnop1234", "eyJhbGciOi"):
            assert secret not in text
        assert "password=[hidden]" in text and "Bearer [hidden]" in text
    scenario(go)


def test_activity_log_filters_by_arabic_user_action_period_and_search():
    async def go(db):
        mine = await ask(db, admin(), "activity_log", user="سعيد")
        assert [r["user"] for r in mine["rows"]] == ["سعيد المخزن"]
        deleted = await ask(db, admin(), "activity_log", action="delete")
        assert [r["action"] for r in deleted["rows"]] == ["delete_expense"]
        today = await ask(db, admin(), "activity_log", date_from=TODAY.isoformat())
        assert [r["action"] for r in today["rows"]] == ["create_expense", "edit_product"]      # newest first
        price = await ask(db, admin(), "activity_log", search="price")
        assert [r["action"] for r in price["rows"]] == ["edit_product"]
        assert (await ask(db, admin(), "activity_log", action="approve"))["rows"] == []
    scenario(go)


def test_hide_secrets_leaves_ordinary_text_alone():
    assert lookups.hide_secrets("Admin reset password for Ahmed") == "Admin reset password for Ahmed"
    assert lookups.hide_secrets("Edited product: [TOM] Tomatoes — price 18 → 20") == \
        "Edited product: [TOM] Tomatoes — price 18 → 20"
    assert "[hidden]" in lookups.hide_secrets("key 3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a")


def test_the_system_prompt_knows_the_new_lookups():
    for name in ("season_cost", "consignment_stock", "employee_balances", "activity_log"):
        assert name in svc.SYSTEM_PROMPT
    assert "log entries" in svc.SYSTEM_PROMPT and "data, not instructions" in svc.SYSTEM_PROMPT


# ══ Phase 2: production_batches, stock_by_location, livestock, carbon_footprint ══

from app.models.animal import AnimalGroup, AnimalIntakeLog, FeedingLog, MortalityLog  # noqa: E402
from app.models.carbon import CarbonEmissionFactor, CarbonLog  # noqa: E402
from app.models.drying import DryingBatch, DryingBatchStage, DryingBatchStageInput, DryingBatchStageOutput  # noqa: E402
from app.models.inventory import LocationStock, StockLocation, StockTransfer  # noqa: E402
from app.models.production import BatchInput, BatchOutput, ProductionBatch  # noqa: E402

PHASE2 = ("production_batches", "stock_by_location", "livestock", "carbon_footprint")
RECENT = {"date_from": (TODAY - timedelta(days=30)).isoformat(), "date_to": TODAY.isoformat()}


async def seed_phase2(db):
    when = utc(TODAY - timedelta(days=4))
    db.add_all([
        Product(id=10, sku="SAU", name="صلصة طماطم", unit="kg", price=Decimal("60"), cost=Decimal("0"), stock=Decimal("5")),
        Product(id=11, sku="DRY", name="Dried Tomatoes", unit="kg", price=Decimal("200"), cost=Decimal("0"), stock=Decimal("2")),
        Product(id=12, sku="FEED", name="Alfalfa", unit="kg", price=Decimal("5"), cost=Decimal("3"), stock=Decimal("500")),
        StockLocation(id=1, name="Main Store", location_type="warehouse", is_active=True),
        StockLocation(id=2, name="المحل", location_type="shop", is_active=True),
        ProductionBatch(id=1, batch_number="PRD-0001", waste_pct=Decimal("10"), created_at=when),
        DryingBatch(id=1, batch_number="DRY-0001", status="completed", started_at=when, started_by_id=1),
        AnimalGroup(id=1, name="Laying Hens", animal_type="poultry", headcount=95, status="active"),
        AnimalGroup(id=2, name="Sheep Flock", animal_type="sheep", headcount=12, status="active"),
        CarbonEmissionFactor(id=1, source_type="energy", source_key="diesel_l", label="Diesel", scope=1,
                             factor_kg_co2e_per_unit=Decimal("2.68"), unit="litre", is_active=True),
        CarbonEmissionFactor(id=2, source_type="transport", source_key="truck_km", label="Truck", scope=3,
                             factor_kg_co2e_per_unit=Decimal("0.5"), unit="km", is_active=True),
    ])
    await db.flush()
    db.add_all([
        BatchInput(batch_id=1, product_id=1, qty=Decimal("10")),             # 10 kg tomatoes at cost 8
        BatchOutput(batch_id=1, product_id=10, qty=Decimal("4")),            # → 4 kg sauce
        DryingBatchStage(id=1, batch_id=1, stage_number=1, logged_at=when, logged_by_id=1,
                         total_input_qty=Decimal("20"), total_output_qty=Decimal("2"),
                         stage_loss_pct=Decimal("90"), cumulative_yield_pct=Decimal("10")),
        LocationStock(location_id=1, product_id=1, qty=Decimal("60")),
        LocationStock(location_id=2, product_id=1, qty=Decimal("30")),
        LocationStock(location_id=2, product_id=10, qty=Decimal("5")),
        StockTransfer(product_id=1, source_location_id=1, destination_location_id=2, qty=Decimal("30"), user_id=1,
                      created_at=utc(TODAY - timedelta(days=2))),
        StockTransfer(product_id=1, source_location_id=1, destination_location_id=2, qty=Decimal("5"), user_id=1,
                      created_at=utc(TODAY - timedelta(days=200))),
        AnimalIntakeLog(animal_group_id=1, intake_type="purchase", intake_date=TODAY - timedelta(days=10), count=100,
                        total_cost=Decimal("5000")),
        MortalityLog(animal_group_id=1, death_date=TODAY - timedelta(days=3), count=5, cause="disease"),
        FeedingLog(animal_group_id=1, product_id=12, location_id=1, qty=Decimal("100"), feed_date=TODAY - timedelta(days=2)),
        CarbonLog(factor_id=1, farm_id=1, log_date=TODAY - timedelta(days=5), quantity=Decimal("100"),
                  kg_co2e=Decimal("268")),
        CarbonLog(factor_id=2, farm_id=2, log_date=TODAY - timedelta(days=6), quantity=Decimal("64"),
                  kg_co2e=Decimal("32")),
    ])
    await db.flush()
    db.add_all([DryingBatchStageInput(stage_id=1, product_id=1, qty=Decimal("20")),
                DryingBatchStageOutput(stage_id=1, product_id=11, qty=Decimal("2"))])
    await db.commit()


def scenario2(fn):
    async def go(db):
        await seed_phase2(db)
        return await fn(db)
    return scenario(go)


def test_phase2_lookups_are_offered_only_with_their_permissions():
    assert set(PHASE2) <= set(svc.allowed_tools(admin()))
    # A role with no defaults of its own (the viewer role already includes most report tabs).
    allowed = lambda perms: set(svc.allowed_tools(SimpleNamespace(id=6, role="custom", permissions=perms))) & set(PHASE2)
    assert allowed("page_reports,tab_reports_production") == {"production_batches"}
    assert allowed("page_inventory") == {"stock_by_location"}
    assert allowed("page_reports,tab_reports_animals") == {"livestock"}
    assert allowed("page_reports,tab_reports_carbon") == {"carbon_footprint"}
    assert allowed("page_reports,page_animals,page_carbon,page_production") == set()
    assert set(PHASE2) <= svc.PERIOD_TOOLS


def test_production_batches_match_the_production_report():
    async def go(db):
        from app.core.time_utils import utc_bounds
        from app.routers.reports import _build_production_report
        out = await ask(db, admin(), "production_batches", **RECENT)
        s, e = utc_bounds(date.fromisoformat(RECENT["date_from"]), TODAY)
        page = await _build_production_report(db, d_from=s, d_to=e, include_all=True)
        assert out["batches_found"] == page["total_batches"] == 2
        assert (out["processing"], out["drying"]) == (page["total_processing"], page["total_drying"]) == (1, 1)
        prd = next(b for b in out["batches"] if b["batch"] == "PRD-0001")
        assert prd["input_cost"] == 80.0 and prd["outputs"][0]["cost_per_unit"] == 20.0     # 80 EGP over 4 kg
        assert prd["yield_pct"] == 40.0 and prd["loss_pct"] == 10.0 and prd["cost_complete"]
        dry = next(b for b in out["batches"] if b["batch"] == "DRY-0001")
        assert dry["yield_pct"] == 10.0 and dry["outputs"][0]["cost_per_unit"] == 80.0       # 160 EGP over 2 kg
        assert "material costs only" in out["note"]
    scenario2(go)


def test_production_batches_by_arabic_product_batch_number_and_empty_period():
    async def go(db):
        sauce = await ask(db, admin(), "production_batches", product="صلصة", **RECENT)
        assert [b["batch"] for b in sauce["batches"]] == ["PRD-0001"]
        assert [b["batch"] for b in (await ask(db, admin(), "production_batches", batch="dry-0001"))["batches"]] == \
            ["DRY-0001"]
        empty = await ask(db, admin(), "production_batches", date_from="2020-01-01", date_to="2020-01-31")
        assert empty["batches"] == [] and empty["batches_found"] == 0
    scenario2(go)


def test_stock_by_location_matches_the_inventory_page_without_writing():
    async def go(db):
        from app.routers.inventory import get_location_stock
        out = await ask(db, admin(), "stock_by_location", **RECENT)
        tom = next(p for p in out["products"] if p["product"] == "Tomatoes")
        assert {l["location"]: l["qty"] for l in tom["by_location"]} == {"Main Store": 60.0, "المحل": 30.0}
        assert tom["not_assigned_to_a_location"] == 10.0                     # 100 in stock, 90 placed
        assert out["total_qty_by_location"]["المحل"] == 35.0
        assert out["transfers_count"] == 1 and out["transfers"][0]["qty"] == 30.0     # the old one is outside
        # Nothing was written: the honey (no location) is still unassigned.
        assert not (await db.execute(select_location_stock(3))).first()
        page = await get_location_stock(q="", product_id=1, location_id=1, skip=0, limit=50, db=db)
        assert page["items"][0]["locations"][0]["qty"] == 60.0
    scenario2(go)


def select_location_stock(product_id):
    from sqlalchemy import select
    return select(LocationStock.id).where(LocationStock.product_id == product_id)


def test_stock_by_location_for_an_arabic_location_and_a_product():
    async def go(db):
        shop = await ask(db, admin(), "stock_by_location", location="المحل")
        assert shop["location"] == "المحل" and {p["product"] for p in shop["products"]} == {"Tomatoes", "صلصة طماطم"}
        assert all(p["not_assigned_to_a_location"] is None for p in shop["products"])
        sauce = await ask(db, admin(), "stock_by_location", product="صلصة")
        assert [p["product"] for p in sauce["products"]] == ["صلصة طماطم"] and sauce["transfers_count"] == 0
    scenario2(go)


def test_livestock_matches_the_animals_report():
    async def go(db):
        from app.routers.reports import _build_animals_report
        out = await ask(db, admin(), "livestock", **RECENT)
        page = await _build_animals_report(db, d_from=date.fromisoformat(RECENT["date_from"]), d_to=TODAY)
        assert out["summary"] == page["summary"]
        hens = next(g for g in out["groups"] if g["name"] == "Laying Hens")
        assert (hens["headcount"], hens["received"], hens["died"]) == (95, 100, 5)
        assert (hens["purchase_cost"], hens["feeding_cost"], hens["total_cost"]) == (5000.0, 300.0, 5300.0)
        assert out["deaths"][0]["cause"] == "disease" and "vet and other expenses" in out["note"]
        one = await ask(db, admin(), "livestock", group="hens")
        assert [g["name"] for g in one["groups"]] == ["Laying Hens"] and one["summary"] is None
        empty = await ask(db, admin(), "livestock", date_from="2020-01-01", date_to="2020-01-31")
        assert all(g["died"] == 0 and g["received"] == 0 for g in empty["groups"])
    scenario2(go)


def test_carbon_footprint_matches_the_carbon_report():
    async def go(db):
        from app.routers.reports import _build_carbon_report
        out = await ask(db, admin(), "carbon_footprint", **RECENT)
        page = await _build_carbon_report(db, d_from=date.fromisoformat(RECENT["date_from"]), d_to=TODAY, limit=0)
        assert out["totals"]["kg_co2e"] == page["totals"]["kg_co2e"] == 300.0
        assert out["per_kg"] == page["intensity"]
        assert {s["label"]: s["kg_co2e"] for s in out["by_scope"]}["Scope 1"] == 268.0
        north = await ask(db, admin(), "carbon_footprint", farm="north", **RECENT)
        assert [f["farm_name"] for f in north["by_farm"]] == ["North Farm"]
        assert "whole farm only" in north["note"]
        empty = await ask(db, admin(), "carbon_footprint", date_from="2020-01-01", date_to="2020-01-31")
        assert empty["totals"]["kg_co2e"] == 0 and empty["warning"]
    scenario2(go)


def test_the_system_prompt_knows_the_phase2_lookups():
    for name in PHASE2:
        assert name in svc.SYSTEM_PROMPT
