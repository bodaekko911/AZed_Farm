"""
Cost a batch's outputs the moment the batch is saved
====================================================
Receiving blends each receipt into the cost of the stock on hand; a batch
does the same for what it makes. When a production, packaging or drying stage
is saved, each output's cost becomes

    (stock already on hand × its cost + qty made × what this batch cost to make)
    ÷ (stock on hand + qty made)

with "what this batch cost to make" from production_costing.cost_batch — the
same costing the Production report and Update Costs use. Powders and packs are
then never left at cost 0 until someone runs Update Costs; that monthly run
stays the exact recalculation and corrects anything this could not.

Two cases leave the cost alone and say why, rather than write a wrong number:

  • an input has no cost — the batch cost would be understated
  • the new cost is over 3× the product's selling price — almost always an
    input cost or quantity in the wrong unit (the Mejdool 190-per-gram case)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from app.services.production_costing import cost_batch
from app.services.receive_service import blended_cost

NOT_SOLD = {"packing", "raw"}


def _d(value) -> Decimal:
    return Decimal(str(value or 0))


def cost_outputs(inputs: Iterable, outputs: Iterable, stock_before: dict) -> list[dict]:
    """Update each output product's cost in place; return what happened.

    ``inputs`` / ``outputs``: objects with ``product``, ``product_id`` and ``qty``.
    ``stock_before``: {product_id: stock on hand before this batch added to it}.
    """
    inputs, outputs = list(inputs), list(outputs)
    costing = cost_batch(inputs, outputs)
    products = {o.product_id: o.product for o in outputs}
    updates = []

    if not costing["cost_is_complete"]:
        missing = ", ".join(costing["products_missing_cost"]) or "the inputs"
        for pid, product in products.items():
            updates.append({"product": getattr(product, "name", None) or f"Product #{pid}", "updated": False,
                            "reason": f"no cost on {missing}"})
        return updates

    for line in costing["output_lines"]:
        product = products.get(line["product_id"])
        if product is None or line["qty"] <= 0:
            continue
        unit_cost = _d(line["unit_cost"])
        name = getattr(product, "name", None) or f"Product #{line['product_id']}"
        unit = getattr(product, "unit", None) or "unit"
        price = _d(getattr(product, "price", 0))
        sold = (getattr(product, "item_type", None) or "").lower() not in NOT_SOLD
        if sold and price > 0 and unit_cost > price * 3:
            updates.append({"product": name, "updated": False,
                            "reason": f"this batch costs {unit_cost:f} per {unit}, over 3× "
                                      f"its selling price — check the input costs and quantities"})
            continue
        old = _d(getattr(product, "cost", 0))
        new = blended_cost(_d(stock_before.get(line["product_id"], 0)), old, _d(line["qty"]), unit_cost)
        product.cost = new
        updates.append({"product": name, "updated": True,
                        "old_cost": float(old), "new_cost": float(new), "batch_unit_cost": float(unit_cost)})
    return updates


def summary(updates: list[dict]) -> str:
    """One line for the toast after saving."""
    parts = []
    for u in updates:
        if u["updated"]:
            if abs(u["new_cost"] - u["old_cost"]) >= 0.0005:
                parts.append(f"{u['product']} cost {u['old_cost']:g} → {u['new_cost']:g}")
        else:
            parts.append(f"{u['product']} cost not updated — {u['reason']}")
    return "; ".join(parts)
