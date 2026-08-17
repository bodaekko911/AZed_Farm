"""Batch costing — material cost in, unit cost out.

Input cost is a lookup: each product carries a cost, so a batch's material
cost is a fact. The output side needs an assumption whenever a batch yields
more than one product, because nothing records which input became which
output — so the basis is chosen explicitly and reported.
"""

from types import SimpleNamespace

import pytest

from app.services.production_costing import cost_batch


def product(name, *, cost=0.0, price=0.0, unit="kg", weight=None, sku=""):
    return SimpleNamespace(
        name=name, sku=sku, unit=unit, cost=cost, price=price, unit_weight_kg=weight,
    )


def line(prod, qty, product_id=1):
    return SimpleNamespace(product=prod, product_id=product_id, qty=qty)


# ── Input cost ───────────────────────────────────────────────────────────────

def test_input_cost_is_qty_times_product_cost():
    tomato = product("Tomato", cost=17.78)
    jars = product("Glass Jar", cost=3.0, unit="piece")
    result = cost_batch(
        [line(tomato, 100, 1), line(jars, 40, 2)],
        [line(product("Sauce", price=60), 80, 3)],
    )

    assert [l["line_cost"] for l in result["input_lines"]] == [1778.0, 120.0]
    assert result["input_cost"] == 1898.0


def test_inputs_without_a_cost_are_named_not_silently_zeroed():
    result = cost_batch(
        [line(product("Tomato", cost=17.78), 100, 1),
         line(product("Salt", cost=0), 5, 2)],
        [line(product("Sauce", price=60), 80, 3)],
    )

    assert result["products_missing_cost"] == ["Salt"]
    assert result["cost_is_complete"] is False
    assert result["input_cost"] == 1778.0          # what is known, still counted


# ── Output cost ──────────────────────────────────────────────────────────────

def test_single_output_takes_the_whole_cost():
    result = cost_batch(
        [line(product("Tomato", cost=20), 100, 1)],
        [line(product("Sauce", price=60), 80, 2)],
    )

    assert result["allocation_basis"] == "single"
    out = result["output_lines"][0]
    assert out["allocated_cost"] == 2000.0
    assert out["unit_cost"] == 25.0                # 2,000 ÷ 80
    assert out["share_pct"] == 100.0


def test_two_outputs_split_by_weight():
    """Both outputs are in kg, so mass drives the split."""
    result = cost_batch(
        [line(product("Tomato", cost=20), 100, 1)],
        [line(product("Sauce", price=60), 60, 2),
         line(product("Puree", price=40), 20, 3)],
    )

    assert result["allocation_basis"] == "weight"
    by_name = {o["product"]: o for o in result["output_lines"]}
    assert by_name["Sauce"]["share_pct"] == 75.0
    assert by_name["Sauce"]["allocated_cost"] == 1500.0
    assert by_name["Sauce"]["unit_cost"] == 25.0
    assert by_name["Puree"]["allocated_cost"] == 500.0


def test_pieces_convert_to_kilograms_for_the_split():
    """A jar of 0.5 kg is half a kilogram, not one unit of anything."""
    result = cost_batch(
        [line(product("Tomato", cost=20), 100, 1)],
        [line(product("Sauce", price=60), 50, 2),
         line(product("Jarred sauce", price=80, unit="piece", weight=0.5), 100, 3)],
    )

    assert result["allocation_basis"] == "weight"
    by_name = {o["product"]: o for o in result["output_lines"]}
    # 50 kg vs 100 × 0.5 = 50 kg → an even split
    assert by_name["Sauce"]["share_pct"] == 50.0
    assert by_name["Jarred sauce"]["share_pct"] == 50.0
    assert by_name["Jarred sauce"]["unit_cost"] == 10.0     # 1,000 ÷ 100 jars


def test_falls_back_to_value_when_an_output_has_no_weight():
    result = cost_batch(
        [line(product("Tomato", cost=20), 100, 1)],
        [line(product("Sauce", price=60), 50, 2),
         line(product("Herb pack", price=50, unit="piece"), 50, 3)],
    )

    assert result["allocation_basis"] == "value"
    by_name = {o["product"]: o for o in result["output_lines"]}
    # 3,000 vs 2,500 of value
    assert by_name["Sauce"]["share_pct"] == 54.5
    assert by_name["Herb pack"]["share_pct"] == 45.5


def test_falls_back_to_quantity_when_there_is_neither_weight_nor_price():
    result = cost_batch(
        [line(product("Tomato", cost=20), 100, 1)],
        [line(product("A", unit="piece"), 30, 2),
         line(product("B", unit="piece"), 70, 3)],
    )

    assert result["allocation_basis"] == "quantity"
    by_name = {o["product"]: o for o in result["output_lines"]}
    assert by_name["A"]["share_pct"] == 30.0
    assert by_name["B"]["share_pct"] == 70.0


def test_allocated_costs_always_add_back_to_the_input_cost():
    for outputs in (
        [line(product("Sauce", price=60), 60, 2), line(product("Puree", price=40), 20, 3)],
        [line(product("Sauce", price=60), 50, 2), line(product("Pack", price=50, unit="piece"), 50, 3)],
        [line(product("A", unit="piece"), 30, 2), line(product("B", unit="piece"), 70, 3)],
    ):
        result = cost_batch([line(product("Tomato", cost=20), 100, 1)], outputs)
        assert result["output_cost"] == result["input_cost"], result["allocation_basis"]
        assert round(sum(o["share_pct"] for o in result["output_lines"]), 1) == 100.0


# ── Derived figures ──────────────────────────────────────────────────────────

def test_yield_compares_input_weight_to_output_weight():
    result = cost_batch(
        [line(product("Tomato", cost=20), 100, 1)],
        [line(product("Sauce", price=60), 80, 2)],
    )

    assert result["input_kg"] == 100.0
    assert result["output_kg"] == 80.0
    assert result["yield_pct"] == 80.0             # 20% lost in processing


def test_margin_and_change_against_the_products_current_cost():
    result = cost_batch(
        [line(product("Tomato", cost=20), 100, 1)],
        [line(product("Sauce", cost=18.0, price=60), 80, 2)],
    )

    out = result["output_lines"][0]
    assert out["unit_cost"] == 25.0
    assert out["current_cost"] == 18.0
    assert out["cost_change"] == 7.0               # costing more than recorded
    assert out["margin_pct"] == 58.3               # (60 − 25) ÷ 60


def test_output_with_no_price_reports_no_margin_rather_than_zero():
    result = cost_batch(
        [line(product("Tomato", cost=20), 100, 1)],
        [line(product("Sauce"), 80, 2)],
    )

    assert result["output_lines"][0]["margin_pct"] is None


def test_empty_batch_does_not_blow_up():
    result = cost_batch([], [])

    assert result["input_cost"] == 0.0
    assert result["output_lines"] == []
    assert result["yield_pct"] is None
