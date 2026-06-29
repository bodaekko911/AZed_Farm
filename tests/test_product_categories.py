import asyncio

import pytest
from fastapi import HTTPException

from app.models.product import Product, ProductCategory
from app.routers.products import add_category, delete_category, get_categories
from app.schemas.product import ProductCategoryCreate


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class _CategorySession:
    def __init__(self):
        self.categories = []
        self.products = []
        self.deleted = []
        self.commits = 0

    async def execute(self, stmt):
        sql = str(stmt)
        if "count(*)" in sql:
            name = _lower_bound_value(stmt)
            count = sum(
                1
                for product in self.products
                if (product.category or "").lower() == name
                and (product.is_active is True or product.is_active is None)
            )
            return _Result(scalar=count)
        if "product_categories" in sql and "WHERE" in sql:
            name = _lower_bound_value(stmt)
            category = next(
                (cat for cat in self.categories if cat.name.lower() == name),
                None,
            )
            return _Result(scalar=category)
        if "product_categories.name" in sql:
            return _Result(rows=[(cat.name,) for cat in self.categories])
        if "products.category" in sql and "WHERE" in sql and "lower" in sql:
            name = _lower_bound_value(stmt)
            product = next(
                (product for product in self.products if (product.category or "").lower() == name),
                None,
            )
            return _Result(scalar=product.category if product else None)
        if "products.category" in sql:
            return _Result(rows=[(product.category,) for product in self.products if product.category])
        raise AssertionError(f"Unexpected statement: {sql}")

    def add(self, obj):
        if isinstance(obj, ProductCategory):
            obj.id = len(self.categories) + 1
            self.categories.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)
        self.categories.remove(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        return None


def _lower_bound_value(stmt) -> str:
    params = stmt.compile().params
    return str(next(iter(params.values()))).lower()


def test_saved_product_category_survives_reload_and_merges_with_product_categories():
    db = _CategorySession()
    db.products.append(Product(category="Fresh", is_active=True))

    created = asyncio.run(add_category(ProductCategoryCreate(name="Herbs"), db, object()))
    categories = asyncio.run(get_categories(db))

    assert created == {"id": 1, "name": "Herbs", "created": True}
    assert categories == ["Fresh", "Herbs"]


def test_duplicate_product_category_is_not_created_again():
    db = _CategorySession()
    db.products.append(Product(category="Fresh", is_active=True))

    created = asyncio.run(add_category(ProductCategoryCreate(name="fresh"), db, object()))

    assert created == {"name": "Fresh", "created": False}
    assert db.categories == []


def test_delete_saved_category_leaves_product_category_visible():
    db = _CategorySession()
    db.categories.append(ProductCategory(id=1, name="Fresh"))
    db.products.append(Product(category="Fresh", is_active=True))

    deleted = asyncio.run(delete_category("Fresh", db, object()))
    categories = asyncio.run(get_categories(db))

    assert deleted == {"ok": True, "product_count": 1}
    assert categories == ["Fresh"]


def test_delete_missing_category_returns_404():
    db = _CategorySession()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(delete_category("Missing", db, object()))

    assert exc.value.status_code == 404
