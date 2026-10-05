"""
Combined product cost — one average over everything that came in
================================================================
A product's stock can arrive three ways in the same period, and each way has
its own cost:

    grown   farm deliveries × the Season Analysis cost per unit
    bought  receipts × what was paid per unit
    made    batch outputs × what the batch cost to make (production_costing)

Writing whichever was computed last onto the product made the others vanish
— Mejdool A grown at 0.14 and bought at 0.19 ended up at one or the other. The
cost here is the quantity-weighted average over all of them:

    cost = Σ(qty × unit cost) ÷ Σ qty      over every source in the period

Made products follow the chain. A batch is costed with the costs known so
far; each pass can settle one more level, so

    fresh (grown + bought)  ──drying──▶  powder (1g)  ──packing──▶  powder (50g)
    level 1                              level 2                    level 3

and a pack made from a powder uses the powder's combined cost, not a stale
one. More passes than batches means a loop (a product feeding its own batch),
so it stops there.

A product is not costed when part of its supply cannot be valued — a batch
input with no cost, or a harvest recorded in a different unit from the
product — because averaging only the known part would misstate it; it is
reported with what is missing. A cost more than 3× the selling price is
flagged: it is almost always a quantity or cost in the wrong unit.
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


# Packing and raw materials are not sold, so their "price" is a placeholder
# and comparing a cost with it says nothing.
NOT_SOLD = {"packing", "raw"}


def _price_to_check(product) -> float:
    if (getattr(product, "item_type", None) or "").lower() in NOT_SOLD:
        return 0.0
    return _num(getattr(product, "price", 0))


def _proxy(product, cost: float):
    """The product as cost_batch sees it, with a cost not yet saved."""
    return SimpleNamespace(
        name=getattr(product, "name", None), sku=getattr(product, "sku", None),
        unit=getattr(product, "unit", None), unit_weight_kg=getattr(product, "unit_weight_kg", None),
        price=getattr(product, "price", None), cost=cost,
    )


def _line(product_id, qty, product, cost):
    return SimpleNamespace(product_id=product_id, qty=qty, product=_proxy(product, cost))


def roll_up(batches: Iterable[dict], products: dict, supplies: Optional[dict] = None) -> list[dict]:
    """Combined cost per product.

    ``batches``:  [{"batch_number", "inputs": [(product_id, qty)], "outputs": [(product_id, qty)]}]
    ``products``: {product_id: Product} for every product involved.
    ``supplies``: {product_id: [{"source": "grown"|"bought", "qty", "unit_cost", "label"}]};
                  an entry with ``unit_cost`` None is supply that could not be
                  valued, and ``label`` says why.
    Returns one row per product that had any supply in the period.
    """
    batches = list(batches)
    supplies = supplies or {}
    costs = {pid: _num(getattr(p, "cost", 0)) for pid, p in products.items()}
    original = dict(costs)

    by_output: dict = {}
    for b in batches:
        for pid, _qty in b["outputs"]:
            if b not in by_output.setdefault(pid, []):
                by_output[pid].append(b)
    product_ids = list(dict.fromkeys([*supplies, *by_output]))

    resolved_on: dict = {}
    state: dict = {}
    for pass_no in range(1, len(batches) + 2):
        changed = False
        for pid in product_ids:
            sources, missing = [], set()
            for s in supplies.get(pid, []):
                if s.get("unit_cost") is None:
                    missing.add(s.get("label") or f"{s['source']} supply has no cost")
                    continue
                sources.append({"source": s["source"], "qty": _num(s["qty"]),
                                "unit_cost": _num(s["unit_cost"]), "label": s.get("label", "")})
            for b in by_output.get(pid, []):
                result = cost_batch(
                    [_line(i, q, products.get(i), costs.get(i, 0.0)) for i, q in b["inputs"]],
                    [_line(o, q, products.get(o), costs.get(o, 0.0)) for o, q in b["outputs"]],
                )
                missing.update(f"no cost on batch input {n}" for n in result["products_missing_cost"])
                if not result["cost_is_complete"] and not result["products_missing_cost"]:
                    missing.add(f"{b['batch_number']}: inputs have no cost")
                for out in result["output_lines"]:
                    if out["product_id"] == pid and out["qty"] > 0:
                        sources.append({"source": "made", "qty": out["qty"], "unit_cost": out["unit_cost"],
                                        "label": f"{b['batch_number']} ({result['allocation_basis_label']})"})
            total_qty = sum(s["qty"] for s in sources if s["qty"] > 0)
            total_value = sum(s["qty"] * s["unit_cost"] for s in sources if s["qty"] > 0)
            new_cost = round(total_value / total_qty, 3) if total_qty > 0 and not missing else None
            state[pid] = {"missing": sorted(missing), "sources": sources, "qty": total_qty}
            if new_cost is not None and abs(costs.get(pid, 0.0) - new_cost) > 1e-9:
                costs[pid] = new_cost
                resolved_on[pid] = pass_no
                changed = True
            elif new_cost is not None:
                resolved_on.setdefault(pid, pass_no)
        if not changed:
            break

    # Chain depth for display: supply from outside, or made only from raw
    # materials, is step 1; made from a product costed here is one step more.
    depth: dict = {}

    def depth_of(pid, seen=()):
        if pid in depth:
            return depth[pid]
        if pid in seen:
            return 1
        inputs = {i for b in by_output.get(pid, []) for i, _q in b["inputs"] if i in resolved_on and i != pid}
        depth[pid] = 1 + max((depth_of(i, (*seen, pid)) for i in inputs), default=0)
        return depth[pid]

    rows = []
    for pid in product_ids:
        product = products.get(pid)
        old = original.get(pid, 0.0)
        info = state.get(pid, {"missing": [], "sources": [], "qty": 0.0})
        new: Optional[float] = costs[pid] if pid in resolved_on else None
        price = _num(getattr(product, "price", 0))
        check_price = _price_to_check(product)
        # One mistyped receipt can hide inside a reasonable-looking average
        # (0.14 g bought for 9 800), so each line is checked on its own.
        bad_lines = [s for s in info["sources"]
                     if check_price > 0 and s["qty"] > 0 and s["unit_cost"] > 3 * check_price]
        if new is None:
            status = "incomplete" if info["missing"] else "no_output"
        elif check_price > 0 and (new > 3 * check_price or bad_lines):
            status = "suspect"
        elif abs(new - old) < 0.0005:
            status = "unchanged"
        else:
            status = "ok"
        by_source = {}
        for s in info["sources"]:
            agg = by_source.setdefault(s["source"], {"source": s["source"], "qty": 0.0, "value": 0.0})
            agg["qty"] += s["qty"]
            agg["value"] += s["qty"] * s["unit_cost"]
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
            "qty_in": round(info["qty"], 3),
            "level": depth_of(pid) if pid in resolved_on else None,
            "status": status,
            "missing_cost": info["missing"],
            "suspect_lines": [f"{s['label']}: {round(s['qty'], 3):g} {getattr(product, 'unit', '') or ''} at "
                              f"{round(s['unit_cost'], 3):g} — over 3× the selling price" for s in bad_lines],
            # Per source: how much came in and at what average cost.
            "sources": [
                {"source": a["source"], "qty": round(a["qty"], 3),
                 "unit_cost": round(a["value"] / a["qty"], 3) if a["qty"] > 0 else None}
                for a in sorted(by_source.values(), key=lambda a: ["grown", "bought", "made"].index(a["source"]))
            ],
            "lines": [{**s, "qty": round(s["qty"], 3), "unit_cost": round(s["unit_cost"], 3)}
                      for s in info["sources"]],
        })
    order = {"suspect": 0, "incomplete": 1, "ok": 2, "unchanged": 3, "no_output": 4}
    rows.sort(key=lambda r: (order[r["status"]], r["level"] or 99, r["product"].lower()))
    return rows
