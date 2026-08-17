"""
Batch costing — what went in, what it cost, what the output is worth
====================================================================
A processing or packaging batch consumes raw materials and yields finished
goods. This turns that into money:

    input cost  = Σ (input qty × that product's cost)
    output cost = the input cost, divided among the outputs
    unit cost   = each output's share ÷ how much of it came out

The input side is a straight lookup — each product carries a cost, so the
batch's material cost is a fact once those costs are right (the season report
can write them for farm produce).

The output side needs an assumption whenever a batch yields more than one
product, because nothing records that *this* input became *that* output. It is
the same joint-cost problem the season report has, so it is solved the same
way and reports which basis it used:

    weight → sale value → quantity

Weight first: in processing, cost genuinely follows mass. It is used only when
every output converts to kilograms, so pieces are never silently added to
kilograms. A single-output batch skips the question entirely — that output
takes the whole cost.

Only material cost is counted. Labour, energy and overhead are not attached to
batches anywhere in the system, so this is a *material* cost, not a full
production cost, and every caller says so.
"""

from __future__ import annotations

from typing import Iterable, Optional


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _output_kg(qty: float, product) -> Optional[float]:
    """Output quantity in kilograms, or None when it has no mass basis."""
    from app.core.units import canonical_unit

    unit = canonical_unit(getattr(product, "unit", None))
    if unit == "kg":
        return qty
    if unit == "gram":
        return qty / 1000.0
    piece_kg = getattr(product, "unit_weight_kg", None)
    if piece_kg and _num(piece_kg) > 0:
        return qty * _num(piece_kg)
    return None


def _line(product, product_id, qty, unit_cost):
    return {
        "product_id": product_id,
        "product": getattr(product, "name", None) or f"Product #{product_id}",
        "sku": getattr(product, "sku", None) or "",
        "unit": getattr(product, "unit", None) or "",
        "qty": round(qty, 3),
        "unit_cost": round(unit_cost, 3),
        "line_cost": round(qty * unit_cost, 2),
    }


def cost_batch(inputs: Iterable, outputs: Iterable) -> dict:
    """Cost one batch from its input and output rows.

    ``inputs``/``outputs`` are anything with ``product_id``, ``qty`` and a
    loaded ``product`` — BatchInput/BatchOutput, or the drying equivalents.
    """
    input_lines = []
    for item in inputs:
        product = getattr(item, "product", None)
        qty = _num(getattr(item, "qty", 0))
        input_lines.append(_line(product, getattr(item, "product_id", None), qty,
                                 _num(getattr(product, "cost", 0))))
    total_input_cost = round(sum(line["line_cost"] for line in input_lines), 2)

    raw_outputs = []
    for item in outputs:
        product = getattr(item, "product", None)
        qty = _num(getattr(item, "qty", 0))
        raw_outputs.append({
            "product": product,
            "product_id": getattr(item, "product_id", None),
            "qty": qty,
            "kg": _output_kg(qty, product),
            "price": _num(getattr(product, "price", 0)),
            "current_cost": _num(getattr(product, "cost", 0)),
        })

    positive = [o for o in raw_outputs if o["qty"] > 0]
    total_qty = sum(o["qty"] for o in positive)
    total_kg = sum(o["kg"] for o in positive if o["kg"] is not None)
    total_value = sum(o["qty"] * o["price"] for o in positive)

    if len(positive) <= 1:
        basis = "single"
    elif all(o["kg"] is not None for o in positive) and total_kg > 0:
        basis = "weight"
    elif total_value > 0:
        basis = "value"
    else:
        basis = "quantity"

    def share_of(entry) -> float:
        if entry["qty"] <= 0:
            return 0.0
        if basis == "single":
            return 1.0
        if basis == "weight":
            return entry["kg"] / total_kg if total_kg > 0 else 0.0
        if basis == "value":
            return (entry["qty"] * entry["price"]) / total_value if total_value > 0 else 0.0
        return entry["qty"] / total_qty if total_qty > 0 else 0.0

    output_lines = []
    for entry in raw_outputs:
        share = share_of(entry)
        allocated = total_input_cost * share
        unit_cost = allocated / entry["qty"] if entry["qty"] > 0 else 0.0
        output_lines.append({
            "product_id": entry["product_id"],
            "product": getattr(entry["product"], "name", None) or f"Product #{entry['product_id']}",
            "sku": getattr(entry["product"], "sku", None) or "",
            "unit": getattr(entry["product"], "unit", None) or "",
            "qty": round(entry["qty"], 3),
            "share_pct": round(share * 100, 1),
            "allocated_cost": round(allocated, 2),
            "unit_cost": round(unit_cost, 3),
            "current_cost": round(entry["current_cost"], 3),
            "cost_change": round(unit_cost - entry["current_cost"], 3),
            "sale_price": round(entry["price"], 2),
            "margin_pct": round(
                (entry["price"] - unit_cost) / entry["price"] * 100, 1
            ) if entry["price"] > 0 else None,
        })

    input_kg = sum(
        kg for kg in (
            _output_kg(_num(getattr(i, "qty", 0)), getattr(i, "product", None))
            for i in inputs
        ) if kg is not None
    )
    yield_pct = round(total_kg / input_kg * 100, 1) if input_kg > 0 and total_kg > 0 else None

    missing_cost = [
        line["product"] for line in input_lines
        if line["qty"] > 0 and line["unit_cost"] <= 0
    ]

    return {
        "input_lines": input_lines,
        "output_lines": output_lines,
        "input_cost": total_input_cost,
        "output_cost": round(sum(o["allocated_cost"] for o in output_lines), 2),
        "allocation_basis": basis,
        "allocation_basis_label": {
            "single": "Single output — takes the full cost",
            "weight": "Split by output weight",
            "value": "Split by output sale value",
            "quantity": "Split by output quantity",
        }[basis],
        "input_kg": round(input_kg, 3) if input_kg else 0.0,
        "output_kg": round(total_kg, 3) if total_kg else 0.0,
        "yield_pct": yield_pct,
        "products_missing_cost": sorted(set(missing_cost)),
        "cost_is_complete": not missing_cost and total_input_cost > 0,
    }
