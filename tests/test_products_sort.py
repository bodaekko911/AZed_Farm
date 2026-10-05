"""Products list sorting — on the server, so it covers every page, not the 50 on screen."""

import asyncio
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.database import Base
from app.models.product import Product
from app.routers.products import get_products


class AsyncSessionAdapter:
    def __init__(self, session):
        self.s = session

    async def execute(self, statement, params=None):
        return self.s.execute(statement, params or {})


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all([
        Product(id=1, sku="B-2", name="Basil", price=Decimal("0.8"), cost=Decimal("0.23"), stock=Decimal("500"),
                unit="gram", category="Herbs", item_type="finished"),
        Product(id=2, sku="A-9", name="Olive Oil", price=Decimal("550"), cost=Decimal("341.7"), stock=Decimal("12"),
                unit="piece", category=None, item_type="finished"),
        Product(id=3, sku="C-1", name="Jar", price=Decimal("1"), cost=Decimal("50"), stock=Decimal("80"),
                unit="piece", category="Packing", item_type="packing"),
        Product(id=4, sku="D-4", name="Apple", price=Decimal("0.8"), cost=Decimal("0.1"), stock=Decimal("80"),
                unit="gram", category="Fruit", item_type="fresh"),
    ])
    session.commit()
    return AsyncSessionAdapter(session)


def names(db, **kw):
    params = dict(q="", low_stock=False, category="", item_type="", skip=0, limit=50)
    params.update(kw)
    return [p["name"] for p in asyncio.run(get_products(db=db, **params))["items"]]


def test_default_is_name_a_to_z():
    assert names(make_db()) == ["Apple", "Basil", "Jar", "Olive Oil"]


def test_each_column_sorts_both_ways():
    db = make_db()
    assert names(db, sort="price", dir="desc") == ["Olive Oil", "Jar", "Apple", "Basil"]  # ties by name
    assert names(db, sort="cost", dir="asc") == ["Apple", "Basil", "Jar", "Olive Oil"]
    assert names(db, sort="stock", dir="desc") == ["Basil", "Apple", "Jar", "Olive Oil"]
    assert names(db, sort="sku", dir="asc") == ["Olive Oil", "Basil", "Jar", "Apple"]
    assert names(db, sort="type", dir="asc") == ["Basil", "Olive Oil", "Apple", "Jar"]


def test_products_without_a_category_go_last_either_way():
    db = make_db()
    assert names(db, sort="category", dir="asc")[-1] == "Olive Oil"
    assert names(db, sort="category", dir="desc")[-1] == "Olive Oil"


def test_paging_through_a_sort_never_repeats_or_skips_a_product():
    db = make_db()
    pages = [names(db, sort="price", dir="asc", skip=i, limit=1)[0] for i in range(4)]
    assert sorted(pages) == ["Apple", "Basil", "Jar", "Olive Oil"]


def test_an_unknown_sort_column_falls_back_to_name():
    assert names(make_db(), sort="password; drop table", dir="desc") == ["Olive Oil", "Jar", "Basil", "Apple"]
