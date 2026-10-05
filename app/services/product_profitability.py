"""
Product profitability — which products actually make money
==========================================================
Joins what each product sold for, what it cost to make or buy, and what was
lost on the way, into one line per product:

    revenue          = net sales of the product (discounts and refunds off)
    cost of sales    = net qty sold × unit cost
    gross profit     = revenue − cost of sales
    losses           = spoiled qty × unit cost (spoilage + drying-batch spoilage)
    profit           = gross profit − losses

Revenue follows the Sales report's definition — paid POS invoices on their
date, B2B on the collection date, refunds on their date — so the column total
reconciles to the Sales report's net sales. The one difference is discounts:
the Sales report's product list uses line totals before the invoice discount,
here the discount is spread over the lines so a product's revenue is what was
really taken for it. Collections that cannot be traced to products are kept as
``unattributed_revenue`` so the reconciliation still closes.

Unit cost, in order of preference:

    batch    — the product was an output of production or drying batches in
               the period whose inputs were all costed: the quantity-weighted
               material cost from batch costing. This is where drying and
               processing yield loss lands — 10 kg of tomato drying to 1 kg
               puts ten kilograms of cost on that one kilogram.
    product  — the cost on the product card.
    missing  — neither; the product is reported, its cost is not invented.

Sales lines do not store the cost at the time of sale, so the cost is the one
known now, applied to the whole period. Batch cost is material only — labour,
energy and overhead are not recorded against batches.

Items typed as a Service (delivery, tours) have no cost of goods; they are
reported apart from the products so they neither show as 100% margin goods
nor raise "no cost set" warnings, and still count toward net sales.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.product_types import is_service_item_type


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class ProfitabilityLedger:
    """Collects per-product sales, refunds, losses and batch costs, then
    turns them into the report. Pure — the caller does the querying."""

    def __init__(self) -> None:
        self._rows: dict[Any, dict] = {}
        self._products: dict[Any, Any] = {}
        self.unattributed_revenue = 0.0

    def _row(self, product_id, product=None, name: Optional[str] = None) -> dict:
        if product is not None:
            self._products.setdefault(product_id, product)
        row = self._rows.get(product_id)
        if row is None:
            row = self._rows[product_id] = {
                "product_id": product_id,
                "name": getattr(product, "name", None) or name or f"Product #{product_id}",
                "qty_sold": 0.0, "qty_refunded": 0.0,
                "revenue_pos": 0.0, "revenue_b2b": 0.0, "refunds": 0.0,
                "loss_qty": 0.0,
                "batch_qty": 0.0, "batch_cost": 0.0, "batch_incomplete": False,
                "batches": [],
            }
        return row

    def add_sale(self, product_id, product, qty, revenue, channel: str) -> None:
        row = self._row(product_id, product)
        row["qty_sold"] += _num(qty)
        row["revenue_b2b" if channel == "b2b" else "revenue_pos"] += _num(revenue)

    def add_refund(self, product_id, product, qty, amount) -> None:
        row = self._row(product_id, product)
        row["qty_refunded"] += _num(qty)
        row["refunds"] += _num(amount)

    def add_loss(self, product_id, product, qty) -> None:
        self._row(product_id, product)["loss_qty"] += _num(qty)

    def add_unattributed(self, amount) -> None:
        self.unattributed_revenue += _num(amount)

    def add_batch_costing(self, costing: dict, products_by_id: dict, batch_number: str = "") -> None:
        """Fold one batch's ``cost_batch`` result into its outputs' unit cost."""
        complete = bool(costing.get("cost_is_complete"))
        for line in costing.get("output_lines", []):
            qty = _num(line.get("qty"))
            if qty <= 0:
                continue
            pid = line.get("product_id")
            row = self._row(pid, products_by_id.get(pid), line.get("product"))
            # Kept so the report can show where a batch cost came from —
            # one mistyped input quantity or cost moves the whole product.
            row["batches"].append({
                "batch_number": batch_number,
                "complete": complete,
                "allocation_basis": costing.get("allocation_basis"),
                "inputs": [
                    {k: i.get(k) for k in ("product", "qty", "unit", "unit_cost", "line_cost")}
                    for i in costing.get("input_lines", [])
                ],
                "input_cost": costing.get("input_cost"),
                "output_qty": line.get("qty"),
                "share_pct": line.get("share_pct"),
                "allocated_cost": line.get("allocated_cost"),
                "unit_cost": line.get("unit_cost"),
            })
            if not complete:
                # One batch with uncosted inputs would drag the average down,
                # so it disqualifies the batch basis for this product.
                row["batch_incomplete"] = True
                continue
            row["batch_qty"] += qty
            row["batch_cost"] += _num(line.get("allocated_cost"))

    # ── Result ───────────────────────────────────────────────────────────

    def _unit_cost(self, row: dict) -> tuple[float, str]:
        if row["batch_qty"] > 0 and not row["batch_incomplete"] and row["batch_cost"] > 0:
            return row["batch_cost"] / row["batch_qty"], "batch"
        card = _num(getattr(self._products.get(row["product_id"]), "cost", 0))
        if card > 0:
            return card, "product"
        return 0.0, "missing"

    def result(self) -> dict:
        lines, services = [], []
        for row in self._rows.values():
            product = self._products.get(row["product_id"])
            net_qty = row["qty_sold"] - row["qty_refunded"]
            revenue = row["revenue_pos"] + row["revenue_b2b"] - row["refunds"]
            # A product only produced in the window, never sold or lost,
            # has nothing to say about profit.
            if abs(net_qty) < 1e-9 and abs(revenue) < 0.005 and row["loss_qty"] <= 0:
                continue
            if is_service_item_type(getattr(product, "item_type", None)):
                services.append({
                    "product_id": row["product_id"],
                    "name": row["name"],
                    "category": getattr(product, "category", None) or "",
                    "unit": getattr(product, "unit", None) or "",
                    "qty_sold": round(net_qty, 3),
                    "revenue": round(revenue, 2),
                })
                continue
            unit_cost, source = self._unit_cost(row)
            card_cost = _num(getattr(product, "cost", 0))
            cogs = net_qty * unit_cost
            loss_cost = row["loss_qty"] * unit_cost
            gross = revenue - cogs
            profit = gross - loss_cost
            lines.append({
                "product_id": row["product_id"],
                "name": row["name"],
                "sku": getattr(product, "sku", None) or "",
                "category": getattr(product, "category", None) or "",
                "unit": getattr(product, "unit", None) or "",
                "qty_sold": round(net_qty, 3),
                "revenue_pos": round(row["revenue_pos"], 2),
                "revenue_b2b": round(row["revenue_b2b"], 2),
                "refunds": round(row["refunds"], 2),
                "revenue": round(revenue, 2),
                "avg_price": round(revenue / net_qty, 3) if net_qty > 0 else None,
                "unit_cost": round(unit_cost, 3),
                "cost_source": source,
                "card_cost": round(card_cost, 3),
                "cogs": round(cogs, 2),
                "gross_profit": round(gross, 2),
                "gross_margin_pct": round(gross / revenue * 100, 1) if revenue > 0 else None,
                "loss_qty": round(row["loss_qty"], 3),
                "loss_cost": round(loss_cost, 2),
                "profit": round(profit, 2),
                "margin_pct": round(profit / revenue * 100, 1) if revenue > 0 else None,
                "batches": row["batches"],
            })

        lines.sort(key=lambda r: (-r["profit"], r["name"].lower()))

        revenue = sum(r["revenue"] for r in lines)
        cogs = sum(r["cogs"] for r in lines)
        loss_cost = sum(r["loss_cost"] for r in lines)
        gross = revenue - cogs
        profit = gross - loss_cost
        for r in lines:
            r["profit_share_pct"] = round(r["profit"] / profit * 100, 1) if profit > 0 else None

        missing = sorted(r["name"] for r in lines if r["cost_source"] == "missing")
        losing = [r for r in lines if r["profit"] < 0]
        # Product card cost far from what batches say it costs to make — the
        # card is probably stale, and anything else using it (spoilage cost,
        # stock value) is off too.
        stale = sorted(
            r["name"] for r in lines
            if r["cost_source"] == "batch" and r["card_cost"] > 0
            and abs(r["card_cost"] - r["unit_cost"]) / r["unit_cost"] > 0.10
        )

        # A cost several times the selling price is almost always a unit
        # mix-up — a per-kg cost on a per-gram product, or grams typed into a
        # kg batch input — not a real cost.
        suspect = sorted(
            ({"name": r["name"], "source": r["cost_source"], "unit": r["unit"],
              "unit_cost": r["unit_cost"], "avg_price": r["avg_price"]}
             for r in lines
             if r["cost_source"] != "missing" and r["avg_price"] and r["unit_cost"] > 3 * r["avg_price"]),
            key=lambda s: s["name"],
        )
        services.sort(key=lambda r: -r["revenue"])
        services_revenue = sum(r["revenue"] for r in services)

        return {
            "products": lines,
            "services": services,
            "products_suspect_cost": suspect,
            "totals": {
                "revenue": round(revenue, 2),
                "cogs": round(cogs, 2),
                "gross_profit": round(gross, 2),
                "gross_margin_pct": round(gross / revenue * 100, 1) if revenue > 0 else None,
                "loss_cost": round(loss_cost, 2),
                "profit": round(profit, 2),
                "margin_pct": round(profit / revenue * 100, 1) if revenue > 0 else None,
                "services_revenue": round(services_revenue, 2),
                "unattributed_revenue": round(self.unattributed_revenue, 2),
                "net_sales": round(revenue + services_revenue + self.unattributed_revenue, 2),
            },
            "product_count": len(lines),
            "losing_count": len(losing),
            "top_earner": lines[0] if lines and lines[0]["profit"] > 0 else None,
            "biggest_drain": min(losing, key=lambda r: r["profit"]) if losing else None,
            "products_missing_cost": missing,
            "products_stale_cost": stale,
            "cost_is_complete": not missing,
        }
