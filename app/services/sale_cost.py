"""The cost saved on a sale line when it is recorded.

Every sale and refund line keeps the product's cost at that moment, so a past
month's margin stays what it was when costs are updated later. A product with
no cost yet saves nothing (NULL) rather than 0 — reports then fall back to its
current cost instead of showing that sale as 100% margin forever.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional


def cost_snapshot(product) -> Optional[Decimal]:
    cost = getattr(product, "cost", None)
    try:
        value = Decimal(str(cost)) if cost is not None else Decimal("0")
    except Exception:
        return None
    return value if value > 0 else None
