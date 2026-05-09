from __future__ import annotations

from sqlalchemy import or_

SERVICE_ITEM_TYPE = "service"

ITEM_TYPE_OPTIONS = [
    ("finished", "Finished Product"),
    ("raw", "Raw Material"),
    ("fresh", "Fresh"),
    ("packing", "Packing"),
    ("ingredient", "Ingredient"),
    (SERVICE_ITEM_TYPE, "Service"),
]

ITEM_TYPE_LABELS = {value: label for value, label in ITEM_TYPE_OPTIONS}

ITEM_TYPE_ALIASES = {
    "finished": "finished",
    "finished product": "finished",
    "product": "finished",
    "raw": "raw",
    "raw material": "raw",
    "fresh": "fresh",
    "packing": "packing",
    "packaging": "packing",
    "ingredient": "ingredient",
    "ingredients": "ingredient",
    "service": SERVICE_ITEM_TYPE,
    "services": SERVICE_ITEM_TYPE,
    "labor": SERVICE_ITEM_TYPE,
    "labour": SERVICE_ITEM_TYPE,
}


def normalize_item_type(value: object | None, default: str = "finished") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return ITEM_TYPE_ALIASES.get(text, text)


def is_service_item_type(value: object | None) -> bool:
    return normalize_item_type(value, default="") == SERVICE_ITEM_TYPE


def is_stock_tracked_product(product: object) -> bool:
    return not is_service_item_type(getattr(product, "item_type", None))


def stock_tracked_product_condition(product_model):
    return or_(
        product_model.item_type.is_(None),
        product_model.item_type != SERVICE_ITEM_TYPE,
    )
