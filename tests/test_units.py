"""Unit-name canonicalisation.

Units are free text entered over years and through Excel imports, so the same
unit arrives spelled several ways. Anywhere two units are *compared* rather
than displayed, the comparison has to see through that — otherwise renaming a
product's unit makes it stop matching its own historical delivery lines.
"""

import pytest

from app.core.units import DEFAULT_UNIT, canonical_unit, units_match


@pytest.mark.parametrize("spelling", ["piece", "pieces", "pcs", "PCS", " Pcs ", "pc", "each", "ea"])
def test_piece_spellings_all_canonicalise_together(spelling):
    assert canonical_unit(spelling) == "piece"


@pytest.mark.parametrize("spelling,expected", [
    ("kg", "kg"), ("KG", "kg"), ("kilogram", "kg"), ("كجم", "kg"),
    ("g", "gram"), ("grams", "gram"),
    ("ltr", "litre"), ("liter", "litre"),
    ("carton", "box"), ("crate", "box"),
])
def test_other_families_canonicalise(spelling, expected):
    assert canonical_unit(spelling) == expected


def test_unknown_units_pass_through_lowercased():
    """An unrecognised unit must still compare consistently with itself."""
    assert canonical_unit("Punnet") == "punnet"
    assert units_match("Punnet", "punnet") is True
    assert units_match("punnet", "tray") is False


def test_pcs_and_piece_are_the_same_unit():
    """The rename must not orphan historical rows still spelled 'pcs'."""
    assert units_match("pcs", "piece") is True
    assert units_match("piece", "PCS") is True


def test_different_units_do_not_match():
    assert units_match("kg", "piece") is False
    assert units_match("gram", "kg") is False        # same dimension, different unit


def test_missing_unit_matches_anything():
    """An unset unit is not evidence of a mismatch — refusing on it would block
    perfectly good products whose unit was simply never filled in."""
    assert units_match("", "kg") is True
    assert units_match(None, "piece") is True
    assert units_match("kg", None) is True


def test_default_unit_is_piece():
    assert DEFAULT_UNIT == "piece"
    assert canonical_unit(DEFAULT_UNIT) == "piece"
