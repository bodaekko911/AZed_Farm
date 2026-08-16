"""
Unit names — canonical form and synonyms
========================================
Unit strings are free text on products and on delivery lines, entered over
years by different people and through Excel imports. "pcs", "piece" and "PCS"
all mean the same thing, but a plain string comparison says they don't.

That matters wherever two units are compared rather than displayed — notably
the season cost-apply check, which refuses to write a per-piece cost onto a
per-kilogram product. Without normalisation, renaming the unit on a product
would make it stop matching its own historical delivery lines.

`canonical_unit` is for comparison only. It never changes what is stored or
shown — display keeps whatever the user typed.
"""

from __future__ import annotations

# Canonical name → every spelling that means it.
_UNIT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "piece": ("piece", "pieces", "pcs", "pc", "pce", "each", "ea", "قطعة", "حبة"),
    "kg":    ("kg", "kgs", "kilo", "kilos", "kilogram", "kilograms", "كجم", "كيلو", "كيلوجرام"),
    "gram":  ("gram", "grams", "g", "gm", "gr", "جم", "جرام"),
    "litre": ("litre", "liter", "litres", "liters", "ltr", "l", "لتر"),
    "ml":    ("ml", "millilitre", "milliliter", "مل"),
    "box":   ("box", "boxes", "carton", "crate", "كرتونة", "صندوق"),
    "pack":  ("pack", "packet", "packs", "عبوة"),
    "bunch": ("bunch", "bunches", "حزمة"),
}

_LOOKUP: dict[str, str] = {
    spelling: canonical
    for canonical, spellings in _UNIT_SYNONYMS.items()
    for spelling in spellings
}

# What new records should use. Changing this changes the default on new
# products and the pre-selected option in the unit dropdown; it does not
# rewrite anything already stored.
DEFAULT_UNIT = "piece"


def canonical_unit(value: str | None) -> str:
    """Comparison form of a unit name. Unknown units pass through lowercased,
    so a unit this module has never heard of still compares consistently."""
    cleaned = (value or "").strip().lower()
    if not cleaned:
        return ""
    return _LOOKUP.get(cleaned, cleaned)


def units_match(left: str | None, right: str | None) -> bool:
    """True when two unit names mean the same thing. An empty unit matches
    anything — an unset unit is not evidence of a mismatch."""
    a, b = canonical_unit(left), canonical_unit(right)
    if not a or not b:
        return True
    return a == b
