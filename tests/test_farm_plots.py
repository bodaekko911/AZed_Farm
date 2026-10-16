"""Greenhouses & fields under a farm: seeds / fertilizer going in, harvest coming out.

A real async SQLite database. An input taken from inventory deducts stock at its storage (and deleting it puts the
stock back); a typed-in input just records its cost. Production is the delivery lines tagged with the plot, and a
line can only be tagged with a plot on the delivery's own farm.
"""
import asyncio
import importlib.util
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.models  # noqa: F401  every table
from app.core.permission_catalog import get_role_permissions, is_known_permission
from app.database import Base
from app.models.farm import Farm, FarmDeliveryItem
from app.models.inventory import LocationStock, StockLocation, StockMove
from app.models.product import Product
from app.models.user import User
from app.routers import farm as farm_router
from tests.test_permission_enforcement import _make_client

PLOT_PERMISSIONS = ("tab_farm_plots", "action_farm_plot_manage", "action_farm_plot_input")


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _setup():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()
    db.add_all([
        User(id=1, name="Boss", email="boss@farm.test", password="x", role="admin"),
        Farm(id=1, name="North Farm", is_active=1),
        Farm(id=2, name="South Farm", is_active=1),
        Product(id=1, sku="NPK", name="NPK 20-20-20", unit="kg", price=Decimal("0"), cost=Decimal("40"),
                stock=Decimal("100"), is_active=True, item_type="raw"),
        Product(id=2, sku="TOM", name="Tomatoes", unit="kg", price=Decimal("20"), cost=Decimal("8"),
                stock=Decimal("0"), is_active=True),
        StockLocation(id=1, name="Main Store", is_active=True),
    ])
    await db.flush()
    db.add(LocationStock(location_id=1, product_id=1, qty=Decimal("100")))
    await db.commit()
    user = (await db.execute(select(User).where(User.id == 1))).scalar_one()
    return db, user


async def _plot(db, user, farm_id=1, name="Greenhouse 1", plot_type="greenhouse"):
    out = await farm_router.create_plot(farm_router.PlotIn(farm_id=farm_id, name=name, plot_type=plot_type),
                                        db=db, current_user=user)
    return out["id"]


def test_fertilizer_from_inventory_deducts_stock_and_delete_puts_it_back():
    async def go():
        db, user = await _setup()
        plot_id = await _plot(db, user)
        made = await farm_router.create_plot_input(farm_router.PlotInputIn(
            plot_id=plot_id, input_type="fertilizer", product_id=1, location_id=1, qty=12.5,
            applied_on="2026-10-01"), db=db, current_user=user)

        product = (await db.execute(select(Product).where(Product.id == 1))).scalar_one()
        loc = (await db.execute(select(LocationStock).where(LocationStock.product_id == 1))).scalar_one()
        await db.refresh(product); await db.refresh(loc)
        assert float(product.stock) == 87.5 and float(loc.qty) == 87.5
        moves = (await db.execute(select(StockMove).where(StockMove.ref_type == "plot_input"))).scalars().all()
        assert len(moves) == 1 and moves[0].type == "out"

        rows = await farm_router.list_plot_inputs(plot_id=plot_id, db=db)
        assert rows[0]["item_name"] == "NPK 20-20-20" and rows[0]["unit"] == "kg"
        assert rows[0]["total_cost"] == 500.0                  # valued at product cost × qty
        assert rows[0]["location_name"] == "Main Store"

        await farm_router.delete_plot_input(made["id"], db=db, current_user=user)
        await db.refresh(product); await db.refresh(loc)
        assert float(product.stock) == 100 and float(loc.qty) == 100
        return True
    assert run(go())


def test_cannot_take_more_than_the_storage_holds():
    async def go():
        db, user = await _setup()
        plot_id = await _plot(db, user)
        with pytest.raises(HTTPException) as e:
            await farm_router.create_plot_input(farm_router.PlotInputIn(
                plot_id=plot_id, input_type="fertilizer", product_id=1, location_id=1, qty=150,
                applied_on="2026-10-01"), db=db, current_user=user)
        return e.value
    err = run(go())
    assert err.status_code == 400 and "Not enough" in err.detail


def test_summary_adds_up_inputs_and_tagged_harvest():
    async def go():
        db, user = await _setup()
        gh = await _plot(db, user)
        field = await _plot(db, user, name="East Field", plot_type="field")
        await farm_router.create_plot_input(farm_router.PlotInputIn(
            plot_id=gh, input_type="seed", item_name="Tomato seeds F1", unit="pack", qty=3, total_cost=450,
            crop="Tomato", applied_on="2026-09-01"), db=db, current_user=user)
        await farm_router.create_plot_input(farm_router.PlotInputIn(
            plot_id=gh, input_type="fertilizer", product_id=1, location_id=1, qty=10, total_cost=380,
            applied_on="2026-09-15"), db=db, current_user=user)
        await farm_router.create_delivery(farm_router.DeliveryCreate(
            farm_id=1, delivery_date="2026-10-10",
            items=[farm_router.DeliveryItemIn(product_id=2, qty=200, plot_id=gh),
                   farm_router.DeliveryItemIn(product_id=2, qty=50)]),            # untagged line
            db=db, current_user=user)

        summary = {s["id"]: s for s in await farm_router.plots_summary(db=db)}
        s = summary[gh]
        assert s["input_cost"] == 830.0
        assert s["cost_by_type"] == {"seed": 450.0, "fertilizer": 380.0}
        assert s["production"] == [{"product_id": 2, "product": "Tomatoes", "unit": "kg", "qty": 200.0, "value": 4000.0}]
        assert s["production_value"] == 4000.0
        assert s["last_input"] == "2026-09-15" and s["last_harvest"] == "2026-10-10"
        assert summary[field]["inputs"] == [] and summary[field]["production"] == []

        # A date range leaves out what falls outside it.
        ranged = {x["id"]: x for x in await farm_router.plots_summary(date_from="2026-09-10", db=db)}
        assert ranged[gh]["input_cost"] == 380.0

        # The delivery list shows which plot each line came from.
        listed = await farm_router.get_deliveries(db=db)
        names = sorted(str(i["plot_name"]) for i in listed["deliveries"][0]["items"])
        assert names == ["Greenhouse 1", "None"]
        return True
    assert run(go())


def test_a_delivery_line_cannot_use_another_farms_plot():
    async def go():
        db, user = await _setup()
        south = await _plot(db, user, farm_id=2, name="South GH")
        with pytest.raises(HTTPException) as e:
            await farm_router.create_delivery(farm_router.DeliveryCreate(
                farm_id=1, delivery_date="2026-10-10",
                items=[farm_router.DeliveryItemIn(product_id=2, qty=5, plot_id=south)]),
                db=db, current_user=user)
        count = len((await db.execute(select(FarmDeliveryItem))).scalars().all())
        return e.value, count
    err, count = run(go())
    assert err.status_code == 400 and count == 0


def test_plot_names_are_unique_per_farm_and_archive_keeps_history():
    async def go():
        db, user = await _setup()
        gh = await _plot(db, user)
        with pytest.raises(HTTPException):
            await _plot(db, user, name="greenhouse 1")
        await _plot(db, user, farm_id=2, name="Greenhouse 1")       # same name on another farm is fine
        await farm_router.archive_plot(gh, db=db, current_user=user)
        active = await farm_router.list_plots(db=db)
        everything = await farm_router.list_plots(include_archived=True, db=db)
        with pytest.raises(HTTPException):                          # nothing new goes onto an archived plot
            await farm_router.create_plot_input(farm_router.PlotInputIn(
                plot_id=gh, item_name="x", qty=1, applied_on="2026-10-01"), db=db, current_user=user)
        return len(active), len(everything)
    assert run(go()) == (1, 2)


# ── permissions ──────────────────────────────────────────────────────

def _user(perms: str, role: str = "viewer"):
    return SimpleNamespace(id=5, name="Farm Hand", email="f@x.com", role=role, permissions=perms, is_active=True)


def test_the_permissions_exist_and_the_manager_has_them():
    for key in PLOT_PERMISSIONS:
        assert is_known_permission(key)
        assert key in get_role_permissions("manager")


def test_plot_data_needs_the_tab_and_changes_need_the_actions():
    client, _ = _make_client(_user("page_farm"))
    assert client.get("/farm/api/plots/summary").status_code == 403
    assert client.get("/farm/api/plot-inputs").status_code == 403
    assert client.post("/farm/api/plots", json={"farm_id": 1, "name": "x"}).status_code == 403
    assert client.post("/farm/api/plot-inputs", json={"plot_id": 1, "qty": 1, "applied_on": "2026-10-01"}).status_code == 403
    assert client.delete("/farm/api/plot-inputs/1").status_code == 403


def test_the_page_has_the_tab():
    client, _ = _make_client(_user("", role="admin"))
    html = client.get("/farm/").text
    assert 'id="tab-plots"' in html and 'id="section-plots"' in html and 'class="plot-sel"' in html


def _migration():
    path = next(Path("alembic/versions").glob("*_0060_farm_plots.py"))
    spec = importlib.util.spec_from_file_location("m0060", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_gives_existing_farm_users_the_new_permissions():
    m = _migration()
    out = set(m._with_plot_permissions("page_farm,tab_farm_deliveries").split(","))
    assert set(PLOT_PERMISSIONS) <= out
    out = set(m._with_plot_permissions("page_farm,-action_farm_plot_input").split(","))
    assert "action_farm_plot_input" not in out and "tab_farm_plots" in out
    assert m._with_plot_permissions("page_pos") == "page_pos"
    assert m._with_plot_permissions(None) is None
