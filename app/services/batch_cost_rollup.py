"""
Batch cost roll-up — carry production costs onto the products made
==================================================================
Batch costing (production_costing.cost_batch) says what each output of a batch
cost to make, but nothing wrote that back onto the product. So a drying batch
costed Beetroot Powder correctly while the packaging run that turned it into
50 g packs still saw the powder at cost 0, and every product past the first
stage of the chain had no cost.

This rolls costs down the chain for the batches in a period:

    farm produce  ──drying──▶  powder (1g)  ──packaging──▶  powder (50g)
    cost from Season           cost from                    cost from
    Analysis / receipts        this roll-up, pass 1         this roll-up, pass 2

Each pass costs every batch with the costs known so far; an output's new cost
is the quantity-weighted average over its batches in the period. A product
made from another made product gets its cost on a later pass, so the chain is
followed to any depth. Only products that come out of batches are changed —
raw inputs keep the cost Season Analysis or receiving gave them.

An output is not costed when any of its batches has an input with no cost —
a partial input cost would understate it — and is reported with what is
missing. A cost more than 3× the selling price is flagged: it is almost always
a quantity or cost in the wrong unit on an input.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Iterable, Optional

from app.services.production_costing import cost_batch


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _proxy(product, cost: float):
    """The product as cost_batch sees it, with a cost not yet saved."""
    return SimpleNamespace(
        name=getattr(product, "name", None), sku=getattr(product, "sku", None),
        unit=getattr(product, "unit", None), unit_weight_kg=getattr(product, "unit_weight_kg", None),
        price=getattr(product, "price", None), cost=cost,
    )


def _line(product_id, qty, product, cost):
    return SimpleNamespace(product_id=product_id, qty=qty, product=_proxy(product, cost))


def roll_up(batches: Iterable[dict], products: dict) -> list[dict]:
    """Cost the outputs of ``batches``.

    ``batches``: [{"batch_number", "inputs": [(product_id, qty)], "outputs": [(product_id, qty)]}]
    ``products``: {product_id: Product} for every product the batches touch.
    Returns one row per output product.
    """
    batches = list(batches)
    costs = {pid: _num(getattr(p, "cost", 0)) for pid, p in products.items()}
    original = dict(costs)
    by_output: dict = {}
    for b in batches:
        for pid, _qty in b["outputs"]:
            by_output.setdefault(pid, [])
            if b not in by_output[pid]:
                by_output[pid].append(b)

    resolved_on: dict = {}
    state: dict = {}
    # Each pass can resolve one more level of the chain; more passes than
    # batches means a loop (a product feeding its own batch), so stop there.
    for pass_no in range(1, len(batches) + 2):
        changed = False
        for pid, its_batches in by_output.items():
            total_qty = total_cost = 0.0
            missing: set = set()
            detail = []
            for b in its_batches:
                result = cost_batch(
                    [_line(i, q, products.get(i), costs.get(i, 0.0)) for i, q in b["inputs"]],
                    [_line(o, q, products.get(o), costs.get(o, 0.0)) for o, q in b["outputs"]],
                )
                missing.update(result["products_missing_cost"])
                if not result["cost_is_complete"] and not result["products_missing_cost"]:
                    missing.add("(inputs have no cost)")
                for out in result["output_lines"]:
                    if out["product_id"] == pid and out["qty"] > 0:
                        total_qty += out["qty"]
                        total_cost += out["allocated_cost"]
                        detail.append({
                            "batch_number": b["batch_number"],
                            "qty": out["qty"],
                            "allocated_cost": out["allocated_cost"],
                            "unit_cost": out["unit_cost"],
                            "basis": result["allocation_basis_label"],
                        })
            new_cost = round(total_cost / total_qty, 3) if total_qty > 0 and not missing else None
            state[pid] = {"missing": sorted(missing), "batches": detail, "qty": total_qty}
            if new_cost is not None and abs(costs.get(pid, 0.0) - new_cost) > 1e-9:
                costs[pid] = new_cost
                resolved_on[pid] = pass_no
                changed = True
            elif new_cost is not None:
                resolved_on.setdefault(pid, pass_no)
        if not changed:
            break

    rows = []
    for pid in by_output:
        product = products.get(pid)
        old = original.get(pid, 0.0)
        info = state.get(pid, {"missing": [], "batches": [], "qty": 0.0})
        new: Optional[float] = costs[pid] if pid in resolved_on else None
        price = _num(getattr(product, "price", 0))
        if new is None:
            status = "incomplete" if info["missing"] else "no_output"
        elif price > 0 and new > 3 * price:
            status = "suspect"
        elif abs(new - old) < 0.0005:
            status = "unchanged"
        else:
            status = "ok"
        rows.append({
            "product_id": pid,
            "product": getattr(product, "name", None) or f"Product #{pid}",
            "unit": getattr(product, "unit", None) or "",
            "item_type": getattr(product, "item_type", None) or "",
            "old_cost": round(old, 3),
            "new_cost": new,
            "change_pct": round((new - old) / old * 100, 1) if new is not None and old > 0 else None,
            "sale_price": round(price, 3),
            "margin_pct": round((price - new) / price * 100, 1) if new is not None and price > 0 else None,
            "qty_produced": round(info["qty"], 3),
            "level": resolved_on.get(pid),
            "status": status,
            "missing_cost": info["missing"],
            "batches": info["batches"],
        })
    order = {"suspect": 0, "incomplete": 1, "ok": 2, "unchanged": 3, "no_output": 4}
    rows.sort(key=lambda r: (order[r["status"]], r["level"] or 99, r["product"].lower()))
    return rows
