"""
B2B quotations — pricing, the margin guard, totals and estimates, statuses, and the printed / e-mailed copy
==========================================================================================================
A quotation only describes an offer: it never changes stock, accounting or a client's balance.

Pricing
* Default unit price = the catalogue price, less the client's discount_pct (a prospect pays the catalogue price).
* Each line may carry its own discount %, on top. The screen shows the combined effective discount vs the catalogue.
* Line total = qty × unit price × (1 − line discount %). Total = lines + delivery fee.
* Weekly lines count once a week, twice-weekly lines twice; the weekly estimate adds the delivery fee per delivery,
  as its own figure; monthly = weekly × 52 ÷ 12. One-time lines are in neither estimate.

The margin guard (screen only, never printed) checks the FINAL net price of each line against its cost today:
below the target margin is a warning; below cost blocks saving unless someone with the below-cost permission ticks
"approve below cost" (logged with their name); a product with no cost is flagged; an inactive product can't be quoted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.core.log import record
from app.core.permissions import has_permission
from app.core.time_utils import today_local
from app.models.quotation import B2BQuotation, B2BQuotationItem, B2BQuotationSettings

STATUSES = ("draft", "sent", "accepted", "rejected", "expired")
FREQUENCIES = {"once": 0, "weekly": 1, "twice_weekly": 2}       # deliveries a week
WEEKS_PER_MONTH = Decimal(52) / Decimal(12)
BELOW_COST_PERMISSION = "action_b2b_quotations_below_cost"


def money(v: Any) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── Settings ─────────────────────────────────────────────────────────────────

DEFAULT_TERMS = {
    "en": "Prices are in Egyptian pounds and include delivery to the agreed area unless a delivery fee is shown.\n"
          "Produce is organic and seasonal; quantities are confirmed on each order.\n"
          "This offer is valid until the date shown above.",
    "ar": "الأسعار بالجنيه المصري وتشمل التوصيل إلى المنطقة المتفق عليها ما لم تُذكر رسوم توصيل.\n"
          "المنتجات عضوية وموسمية، وتُؤكَّد الكميات مع كل طلب.\n"
          "هذا العرض سارٍ حتى التاريخ المذكور أعلاه.",
}


async def get_settings(db) -> B2BQuotationSettings:
    row = (await db.execute(select(B2BQuotationSettings).where(B2BQuotationSettings.id == 1))).scalar_one_or_none()
    if row is None:
        row = B2BQuotationSettings(id=1, terms_en=DEFAULT_TERMS["en"], terms_ar=DEFAULT_TERMS["ar"],
                                   target_margin_pct=Decimal("30"), validity_days=14)
        db.add(row)
        await db.flush()
    return row


# ── Pricing ──────────────────────────────────────────────────────────────────

def client_discount(client) -> Decimal:
    pct = Decimal(str(getattr(client, "discount_pct", 0) or 0)) if client is not None else Decimal("0")
    return max(Decimal("0"), min(Decimal("100"), pct))


def default_price(product, client) -> Decimal:
    """The catalogue price less the client's discount — what a line starts at."""
    return money(Decimal(str(product.price or 0)) * (Decimal("1") - client_discount(client) / Decimal("100")))


@dataclass
class LineCheck:
    net_unit: Decimal              # the final price per unit, after both discounts
    effective_discount_pct: Optional[float]     # vs the catalogue price
    cost: Optional[Decimal]
    margin_pct: Optional[float]
    below_cost: bool
    below_target: bool
    no_cost: bool


def check_line(product, unit_price: Decimal, line_discount_pct: Decimal, target_margin: Decimal,
               cost: Optional[Decimal] = None) -> LineCheck:
    """The margin guard for one line, on the final net price and today's cost (or ``cost`` when given)."""
    net = Decimal(str(unit_price)) * (Decimal("1") - Decimal(str(line_discount_pct or 0)) / Decimal("100"))
    catalogue = Decimal(str(product.price or 0))
    effective = float(round((Decimal("1") - net / catalogue) * 100, 1)) if catalogue > 0 else None
    cost = Decimal(str(product.cost or 0)) if cost is None else Decimal(str(cost))
    no_cost = cost <= 0
    margin = float(round((net - cost) / net * 100, 1)) if net > 0 and not no_cost else None
    below_cost = not no_cost and net < cost - Decimal("0.0005")
    below_target = not no_cost and not below_cost and margin is not None and margin < float(target_margin)
    return LineCheck(net, effective, None if no_cost else cost, margin, below_cost, below_target, no_cost)


def totals(lines: list[dict], delivery_fee: Any) -> dict:
    """``lines``: dicts with qty, unit_price, line_discount_pct, frequency. Returns every total and estimate."""
    subtotal = Decimal("0")
    after = Decimal("0")
    weekly_produce = Decimal("0")
    deliveries = 0
    for l in lines:
        gross = Decimal(str(l["qty"])) * Decimal(str(l["unit_price"]))
        net = money(gross * (Decimal("1") - Decimal(str(l.get("line_discount_pct") or 0)) / Decimal("100")))
        subtotal += gross
        after += net
        per_week = FREQUENCIES.get(l.get("frequency") or "once", 0)
        weekly_produce += net * per_week
        deliveries = max(deliveries, per_week)
    fee = money(delivery_fee)
    weekly_delivery = fee * deliveries
    weekly = weekly_produce + weekly_delivery
    return {
        "subtotal": money(subtotal), "discount_total": money(subtotal - after), "lines_total": money(after),
        "delivery_fee": fee, "total": money(after + fee),
        "weekly_produce": money(weekly_produce), "weekly_delivery": money(weekly_delivery), "weekly_value": money(weekly),
        "monthly_produce": money(weekly_produce * WEEKS_PER_MONTH),
        "monthly_delivery": money(weekly_delivery * WEEKS_PER_MONTH), "monthly_value": money(weekly * WEEKS_PER_MONTH),
        "deliveries_per_week": deliveries,
    }


# ── Saving ───────────────────────────────────────────────────────────────────

async def _client(db, client_id):
    from app.models.b2b import B2BClient
    if not client_id:
        return None
    client = (await db.execute(select(B2BClient).where(B2BClient.id == int(client_id)))).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=400, detail="That B2B client doesn't exist.")
    return client


async def priced_lines(db, raw_lines: list, client, target: Decimal) -> tuple[list[dict], list[dict]]:
    """Validate and price the lines. Returns (lines to store, warnings per line). Never writes."""
    from app.models.product import Product
    raw = [l for l in raw_lines or [] if isinstance(l, dict)][:60]
    if not raw:
        raise HTTPException(status_code=400, detail="Add at least one product.")
    ids = {int(l["product_id"]) for l in raw if str(l.get("product_id") or "").isdigit()}
    products = {p.id: p for p in (await db.execute(select(Product).where(Product.id.in_(ids or {-1})))).scalars().all()}
    lines, checks = [], []
    for n, l in enumerate(raw, start=1):
        pid = int(l["product_id"]) if str(l.get("product_id") or "").isdigit() else None
        product = products.get(pid)
        if product is None:
            raise HTTPException(status_code=400, detail=f"Line {n}: pick a product.")
        if product.is_active is False:
            raise HTTPException(status_code=400, detail=f"Line {n}: {product.name} is inactive and can't be quoted.")
        try:
            qty = Decimal(str(l.get("qty")))
        except Exception:
            raise HTTPException(status_code=400, detail=f"Line {n}: enter the quantity.")
        if qty <= 0:
            raise HTTPException(status_code=400, detail=f"Line {n}: the quantity must be above 0.")
        price = money(l["unit_price"]) if l.get("unit_price") not in (None, "") else default_price(product, client)
        if price < 0:
            raise HTTPException(status_code=400, detail=f"Line {n}: the price can't be negative.")
        disc = Decimal(str(l.get("line_discount_pct") or 0))
        if not Decimal("0") <= disc <= Decimal("100"):
            raise HTTPException(status_code=400, detail=f"Line {n}: the discount must be between 0 and 100%.")
        freq = l.get("frequency") or "once"
        if freq not in FREQUENCIES:
            raise HTTPException(status_code=400, detail=f"Line {n}: frequency must be one time, weekly or twice weekly.")
        check = check_line(product, price, disc, target)
        line = {"product_id": product.id, "qty": qty, "unit": product.unit or "", "unit_price": price,
                "line_discount_pct": disc, "frequency": freq, "note": (str(l.get("note") or "").strip()[:200] or None),
                "unit_cost": check.cost, "sort": n}
        line["line_total"] = money(qty * price * (Decimal("1") - disc / Decimal("100")))
        lines.append(line)
        checks.append({"line": n, "product": product.name, "net_unit": float(check.net_unit),
                       "effective_discount_pct": check.effective_discount_pct,
                       "cost": float(check.cost) if check.cost is not None else None, "margin_pct": check.margin_pct,
                       "below_cost": check.below_cost, "below_target": check.below_target, "no_cost": check.no_cost})
    return lines, checks


def _guard(checks: list[dict], user, approve: bool) -> bool:
    """Below-cost lines block saving unless approved by someone allowed to. Returns whether an approval was used."""
    below = [c for c in checks if c["below_cost"]]
    if not below:
        return False
    names = ", ".join(c["product"] for c in below)
    if not approve:
        raise HTTPException(status_code=400, detail={
            "message": f"Priced below cost: {names}. Raise the price, or tick 'approve below cost'.",
            "below_cost": below})
    if not has_permission(user, BELOW_COST_PERMISSION):
        raise HTTPException(status_code=403, detail=f"Priced below cost: {names}. Only someone allowed to approve "
                                                    "below-cost prices can save this.")
    return True


def _recipient_fields(data: dict, client) -> dict:
    if client is not None:
        return {"client_id": client.id, "prospect_name": None, "prospect_contact": None, "prospect_phone": None,
                "prospect_email": None, "prospect_area": None}
    name = str(data.get("prospect_name") or "").strip()[:200]
    if not name:
        raise HTTPException(status_code=400, detail="Pick a B2B client, or enter the prospect's name.")
    email = str(data.get("prospect_email") or "").strip()[:150] or None
    if email:
        from app.services.weekly_brief_service import EMAIL_RE
        if not EMAIL_RE.match(email):
            raise HTTPException(status_code=400, detail="The prospect's e-mail isn't valid.")
    return {"client_id": None, "prospect_name": name,
            "prospect_contact": str(data.get("prospect_contact") or "").strip()[:150] or None,
            "prospect_phone": str(data.get("prospect_phone") or "").strip()[:50] or None,
            "prospect_email": email, "prospect_area": str(data.get("prospect_area") or "").strip()[:120] or None}


def _parse_day(value, default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD.")


async def save(db, user, data: dict, quotation: Optional[B2BQuotation] = None) -> tuple[B2BQuotation, list[dict]]:
    """Create a quotation, or update a draft. Returns (quotation, the margin checks of its lines)."""
    if quotation is not None and quotation.status != "draft":
        raise HTTPException(status_code=409, detail="Only a draft can be edited. Duplicate it to change it.")
    cfg = await get_settings(db)
    client = await _client(db, data.get("client_id"))
    who = _recipient_fields(data, client)
    lines, checks = await priced_lines(db, data.get("lines") or [], client, Decimal(str(cfg.target_margin_pct)))
    approved = _guard(checks, user, bool(data.get("approve_below_cost")))
    fee = money(data.get("delivery_fee") or 0)
    if fee < 0:
        raise HTTPException(status_code=400, detail="The delivery fee can't be negative.")
    t = totals([{**l, "qty": l["qty"], "unit_price": l["unit_price"]} for l in lines], fee)
    quote_day = _parse_day(data.get("quote_date"), today_local())
    valid = _parse_day(data.get("valid_until"), quote_day + timedelta(days=int(cfg.validity_days or 14)))
    if valid < quote_day:
        raise HTTPException(status_code=400, detail="'Valid until' can't be before the quotation date.")
    language = "ar" if data.get("language") == "ar" else "en"
    fields = {
        **who, "quote_date": quote_day, "valid_until": valid, "language": language,
        "delivery_area": str(data.get("delivery_area") or "").strip()[:120] or None,
        "delivery_day": str(data.get("delivery_day") or "").strip()[:40] or None, "delivery_fee": fee,
        "payment_terms": (str(data.get("payment_terms") or "").strip()[:50] or
                          (client.payment_terms if client is not None else None)),
        "notes": str(data.get("notes") or "").strip()[:2000] or None,
        "terms": (str(data["terms"]).strip()[:4000] if data.get("terms") is not None
                  else (cfg.terms_ar if language == "ar" else cfg.terms_en)) or None,
        "subtotal": t["subtotal"], "discount_total": t["discount_total"], "total": t["total"],
        "weekly_value": t["weekly_value"], "monthly_value": t["monthly_value"],
    }
    creating = quotation is None
    if creating:
        quotation = B2BQuotation(status="draft", created_by_id=getattr(user, "id", None), **fields)
        db.add(quotation)
        await db.flush()
        quotation.number = f"QUO-{quotation.id:05d}"
    else:
        for k, v in fields.items():
            setattr(quotation, k, v)
        await db.execute(B2BQuotationItem.__table__.delete().where(B2BQuotationItem.quotation_id == quotation.id))
    for l in lines:
        db.add(B2BQuotationItem(quotation_id=quotation.id, **l))
    if approved:
        quotation.below_cost_approved_by_id = getattr(user, "id", None)
        quotation.below_cost_approved_at = now_utc()
        record(db, "B2B", "quotation_below_cost",
               f"{quotation.number}: priced below cost approved by {getattr(user, 'name', '?')} — "
               + ", ".join(f"{c['product']} at {c['net_unit']:.2f} (cost {c['cost']:.2f})" for c in checks if c["below_cost"]),
               user=user, ref_type="b2b_quotation", ref_id=quotation.id)
    record(db, "B2B", "create_quotation" if creating else "edit_quotation",
           f"{quotation.number} — {recipient_name(quotation, client)} — {fields['total']:.2f}",
           user=user, ref_type="b2b_quotation", ref_id=quotation.id)
    await db.commit()
    return quotation, checks


# ── Statuses ─────────────────────────────────────────────────────────────────

async def expire_due(db) -> int:
    """Drafts and sent quotations past their valid-until date become expired — nothing else changes."""
    result = await db.execute(update(B2BQuotation).where(
        B2BQuotation.status.in_(("draft", "sent")), B2BQuotation.valid_until < today_local())
        .values(status="expired").execution_options(synchronize_session=False))
    if result.rowcount:
        await db.commit()
    return result.rowcount or 0


async def load(db, quotation_id: int) -> B2BQuotation:
    from app.models.b2b import B2BClient  # noqa: F401
    await expire_due(db)
    q = (await db.execute(select(B2BQuotation).where(B2BQuotation.id == quotation_id).options(
        selectinload(B2BQuotation.client), selectinload(B2BQuotation.items).selectinload(B2BQuotationItem.product))
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if q is None:
        raise HTTPException(status_code=404, detail="Quotation not found")
    return q


async def set_status(db, user, q: B2BQuotation, status: str, reason: Optional[str] = None) -> B2BQuotation:
    allowed = {"sent": ("draft", "sent"), "accepted": ("draft", "sent"), "rejected": ("draft", "sent", "expired")}
    if status not in allowed:
        raise HTTPException(status_code=400, detail="Status must be sent, accepted or rejected.")
    if q.status == "expired" and status == "accepted":
        raise HTTPException(status_code=409, detail="This quotation has expired. Renew it with a new valid until date "
                                                    "first; renewing checks the prices against today's costs.")
    if q.status not in allowed[status]:
        raise HTTPException(status_code=409, detail=f"A quotation that is {q.status} can't be marked {status}.")
    reason = " ".join((reason or "").split())[:300]
    if status == "rejected" and not reason:
        raise HTTPException(status_code=400, detail="Give the reason it was rejected.")
    q.status = status
    if status == "sent":
        q.sent_at = q.sent_at or now_utc()
    else:
        q.decided_at, q.decided_by_id = now_utc(), getattr(user, "id", None)
        q.reject_reason = reason if status == "rejected" else None
    record(db, "B2B", f"quotation_{status}", f"{q.number} marked {status}" + (f": {reason}" if reason else ""),
           user=user, ref_type="b2b_quotation", ref_id=q.id)
    await db.commit()
    return q


async def renew(db, user, q: B2BQuotation, valid_until, approve_below_cost: bool = False) -> tuple[B2BQuotation, list]:
    """An expired (or still open) quotation gets a new valid-until date and goes back to draft. Produce costs change,
    so every line is checked again against today's cost; prices themselves are kept."""
    if q.status not in ("expired", "draft", "sent"):
        raise HTTPException(status_code=409, detail=f"A quotation that is {q.status} can't be renewed.")
    new_until = _parse_day(valid_until, today_local() + timedelta(days=14))
    if new_until < today_local():
        raise HTTPException(status_code=400, detail="The new valid until date must be today or later.")
    cfg = await get_settings(db)
    checks = []
    for n, item in enumerate(q.items, start=1):
        product = item.product
        if product is None or product.is_active is False:
            raise HTTPException(status_code=400, detail=f"Line {n}: the product is no longer active; duplicate the "
                                                        "quotation and change it.")
        c = check_line(product, item.unit_price, item.line_discount_pct, Decimal(str(cfg.target_margin_pct)))
        item.unit_cost = c.cost
        checks.append({"line": n, "product": product.name, "net_unit": float(c.net_unit),
                       "effective_discount_pct": c.effective_discount_pct,
                       "cost": float(c.cost) if c.cost is not None else None, "margin_pct": c.margin_pct,
                       "below_cost": c.below_cost, "below_target": c.below_target, "no_cost": c.no_cost})
    try:
        approved = _guard(checks, user, approve_below_cost)
    except HTTPException:
        await db.rollback()
        raise
    q.valid_until, q.status = new_until, "draft"
    q.sent_at = q.decided_at = q.decided_by_id = q.reject_reason = None
    if approved:
        q.below_cost_approved_by_id, q.below_cost_approved_at = getattr(user, "id", None), now_utc()
        record(db, "B2B", "quotation_below_cost", f"{q.number}: renewed below cost, approved by "
                                                   f"{getattr(user, 'name', '?')}", user=user,
               ref_type="b2b_quotation", ref_id=q.id)
    record(db, "B2B", "renew_quotation", f"{q.number} renewed until {new_until}", user=user,
           ref_type="b2b_quotation", ref_id=q.id)
    await db.commit()
    return q, checks


async def duplicate(db, user, q: B2BQuotation) -> tuple[B2BQuotation, list]:
    """A new draft with the same recipient and lines, dated today, priced against today's costs."""
    data = {"client_id": q.client_id, "prospect_name": q.prospect_name, "prospect_contact": q.prospect_contact,
            "prospect_phone": q.prospect_phone, "prospect_email": q.prospect_email, "prospect_area": q.prospect_area,
            "language": q.language, "delivery_area": q.delivery_area, "delivery_day": q.delivery_day,
            "delivery_fee": q.delivery_fee, "payment_terms": q.payment_terms, "notes": q.notes, "terms": q.terms,
            "lines": [{"product_id": i.product_id, "qty": i.qty, "unit_price": i.unit_price,
                       "line_discount_pct": i.line_discount_pct, "frequency": i.frequency, "note": i.note}
                      for i in q.items]}
    return await save(db, user, data)


# ── Convert to invoice, prospect to client (phase 2) ─────────────────────────

async def invoices_of(db, quotation_id: int) -> list[dict]:
    from app.models.b2b import B2BInvoice
    rows = (await db.execute(select(B2BInvoice).where(B2BInvoice.quotation_id == quotation_id)
                             .order_by(B2BInvoice.id))).scalars().all()
    return [{"id": i.id, "invoice_number": i.invoice_number, "total": float(i.total or 0), "status": i.status,
             "created_at": i.created_at.isoformat() if i.created_at else None} for i in rows]


async def convert_to_invoice(db, user, q: B2BQuotation, *, skip_delivery: bool = False) -> dict:
    """An accepted quotation → a B2B invoice through the existing invoice code (its stock, journal and credit checks,
    with stock checked now). The first invoice carries every line; with weekly lines, later ones carry the recurring
    lines only — one delivery each, created by hand (no recurring invoices are scheduled).

    The invoice code takes the client's discount off the prices it is given, and the quotation's prices already
    have it, so each line goes in at its price before the client's discount: the invoice comes out at the quoted
    amount (a few piasters of rounding are reported, never hidden)."""
    from app.models.b2b import B2BInvoice
    from app.models.product import Product
    from app.routers.b2b import InvoiceCreate, InvoiceItemIn, create_invoice
    from app.services.assistant_actions import pick_delivery
    if q.status != "accepted":
        raise HTTPException(status_code=409, detail="Only an accepted quotation can be invoiced.")
    if q.client_id is None:
        raise HTTPException(status_code=409, detail="This quotation is for a prospect. Make them a B2B client first.")
    earlier = await invoices_of(db, q.id)
    first = not earlier
    lines = [i for i in q.items if first or FREQUENCIES.get(i.frequency, 0) > 0]
    if not lines:
        raise HTTPException(status_code=409, detail="This quotation is already invoiced and has no weekly lines left "
                                                    "to deliver again.")
    client_pct = client_discount(q.client)
    keep = Decimal("1") - client_pct / Decimal("100")

    def before_client_discount(net: Decimal, catalogue: Decimal) -> float:
        if keep <= 0:                                  # a 100% client: the invoice nets to zero whatever the price
            return float(catalogue)
        return float((net / keep).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    items, expected = [], Decimal("0")
    for i in lines:
        net = Decimal(str(i.unit_price)) * (Decimal("1") - Decimal(str(i.line_discount_pct or 0)) / Decimal("100"))
        items.append(InvoiceItemIn(product_id=i.product_id, qty=float(i.qty),
                                   unit_price=before_client_discount(net, Decimal(str(i.product.price or 0)))))
        expected += Decimal(str(i.line_total))
    fee = money(q.delivery_fee)
    delivery_note = None
    if fee > 0 and not skip_delivery:
        from app.services import vocabulary_service as vocab
        await vocab.ready(db)          # the area groups pick_delivery reads
        products = (await db.execute(select(Product))).scalars().all()
        delivery = pick_delivery(products, q.delivery_area, q.prospect_area)
        if delivery is None:
            raise HTTPException(status_code=400, detail={
                "message": f"There is no delivery item for {q.delivery_area or 'this area'} to charge the "
                           f"{fee:.2f} EGP delivery fee. Add a product such as '{(q.delivery_area or 'Area')} delivery', "
                           "or invoice without the fee.", "needs_delivery_item": True})
        items.append(InvoiceItemIn(product_id=delivery.id, qty=1, unit_price=before_client_discount(fee, fee)))
        expected += fee
    elif fee > 0:
        delivery_note = f"Delivery fee of {fee:.2f} EGP not invoiced (left out on purpose)."
    which = "first delivery" if first else "next delivery"
    out = await create_invoice(InvoiceCreate(client_id=q.client_id, items=items,
                                             notes=f"From quotation {q.number} ({which})"), db, current_user=user)
    await db.execute(update(B2BInvoice).where(B2BInvoice.id == out["id"]).values(quotation_id=q.id)
                     .execution_options(synchronize_session=False))
    record(db, "B2B", "quotation_invoiced", f"{q.number} → {out['invoice_number']} ({which}), {out['total']:.2f}"
           + (f"; {delivery_note}" if delivery_note else ""), user=user, ref_type="b2b_quotation", ref_id=q.id)
    await db.commit()
    expected_total = float(money(expected)) if keep > 0 else 0.0      # the quoted prices already have the discount
    gap = round(float(out["total"]) - expected_total, 2)
    return {"ok": True, "invoice_id": out["id"], "invoice_number": out["invoice_number"], "total": out["total"],
            "delivery": which, "rounding_difference": gap if abs(gap) >= 0.01 else 0.0, "note": delivery_note}


async def make_client(db, user, q: B2BQuotation, link_client_id: Optional[int] = None) -> dict:
    """A prospect becomes a B2B client in one step (through the B2B page's own client code), or is linked to a client
    that already exists. The quotation then belongs to that client; its prices stay as quoted."""
    from app.models.b2b import B2BClient
    from app.routers.b2b import ClientCreate, create_client
    from app.services.assistant_actions import _norm, phone_key
    if q.client_id is not None:
        raise HTTPException(status_code=409, detail="This quotation is already for a B2B client.")
    if link_client_id:
        client = await _client(db, link_client_id)
        q.client_id = client.id
        record(db, "B2B", "quotation_client", f"{q.number}: prospect {q.prospect_name} linked to client {client.name}",
               user=user, ref_type="b2b_quotation", ref_id=q.id)
        await db.commit()
        return {"ok": True, "client_id": client.id, "client": client.name, "created": False}
    if not has_permission(user, "action_b2b_clients_create"):
        raise HTTPException(status_code=403, detail="You don't have permission to create B2B clients.")
    from app.services import vocabulary_service as vocab
    await vocab.ready(db)
    clients = (await db.execute(select(B2BClient))).scalars().all()
    key = phone_key(q.prospect_phone)
    alias_id = vocab.record_for("b2b_client", q.prospect_name)    # "فندق الموجة" is the client's local name
    same = [c for c in clients if _norm(c.name) == _norm(q.prospect_name) or c.id == alias_id
            or (key and phone_key(c.phone) == key)
            or (q.prospect_email and (c.email or "").strip().lower() == q.prospect_email.strip().lower())]
    if same:
        raise HTTPException(status_code=409, detail={
            "message": f"{same[0].name} already looks like this prospect. Link the quotation to that client instead?",
            "existing": [{"id": c.id, "name": c.name} for c in same[:3]]})
    terms = q.payment_terms if q.payment_terms in ("cash", "immediate", "net15", "net30", "net60", "consignment",
                                                   "full_payment", "credit") else "cash"
    made = await create_client(ClientCreate(name=q.prospect_name, contact_person=q.prospect_contact,
                                            phone=q.prospect_phone, email=q.prospect_email, address=q.prospect_area,
                                            payment_terms=terms, discount_pct=0,
                                            notes=f"Created from quotation {q.number}"), db, current_user=user)
    q.client_id = made["id"]
    record(db, "B2B", "quotation_client", f"{q.number}: prospect {q.prospect_name} made a B2B client",
           user=user, ref_type="b2b_quotation", ref_id=q.id)
    await db.commit()
    return {"ok": True, "client_id": made["id"], "client": made["name"], "created": True}


# ── Reading it back ──────────────────────────────────────────────────────────

def recipient_name(q: B2BQuotation, client=None) -> str:
    client = client if client is not None else q.client
    return client.name if client is not None else (q.prospect_name or "—")


def recipient_email(q: B2BQuotation) -> Optional[str]:
    return (q.client.email if q.client is not None else q.prospect_email) or None


def serialize(q: B2BQuotation, target_margin: Decimal, *, for_staff: bool) -> dict:
    """``for_staff`` adds cost and margin per line. The printed and e-mailed copies are built without them."""
    lines = []
    for i in q.items:
        line = {"product_id": i.product_id, "product": i.product.name if i.product else "—", "unit": i.unit or "",
                "qty": float(i.qty), "unit_price": float(i.unit_price), "line_discount_pct": float(i.line_discount_pct),
                "line_total": float(i.line_total), "frequency": i.frequency, "note": i.note}
        if for_staff and i.product is not None:
            c = check_line(i.product, i.unit_price, i.line_discount_pct, target_margin,
                           cost=i.unit_cost if i.unit_cost is not None else None)
            line.update(net_unit=float(c.net_unit), effective_discount_pct=c.effective_discount_pct,
                        cost=float(c.cost) if c.cost is not None else None, margin_pct=c.margin_pct,
                        below_cost=c.below_cost, below_target=c.below_target, no_cost=c.no_cost)
        lines.append(line)
    t = totals([{"qty": i.qty, "unit_price": i.unit_price, "line_discount_pct": i.line_discount_pct,
                 "frequency": i.frequency} for i in q.items], q.delivery_fee)
    out = {
        "id": q.id, "number": q.number, "status": q.status, "language": q.language,
        "client_id": q.client_id, "recipient": recipient_name(q), "is_prospect": q.client_id is None,
        "prospect": {"name": q.prospect_name, "contact": q.prospect_contact, "phone": q.prospect_phone,
                     "email": q.prospect_email, "area": q.prospect_area},
        "email": recipient_email(q),
        "quote_date": q.quote_date.isoformat(), "valid_until": q.valid_until.isoformat(),
        "delivery_area": q.delivery_area, "delivery_day": q.delivery_day, "payment_terms": q.payment_terms,
        "notes": q.notes, "terms": q.terms, "lines": lines,
        "totals": {k: float(v) if isinstance(v, Decimal) else v for k, v in t.items()},
        "sent_at": q.sent_at.isoformat() if q.sent_at else None, "sent_to": q.sent_to,
        "decided_at": q.decided_at.isoformat() if q.decided_at else None, "reject_reason": q.reject_reason,
    }
    if for_staff:
        out["below_cost_approved"] = q.below_cost_approved_by_id is not None
        out["target_margin_pct"] = float(target_margin)
    return out


# ── The printed and e-mailed copy (no cost, no margin) ───────────────────────
# Formal, plain wording in both languages, with no dashes used as punctuation.

PRINT_TEXT = {
    "en": {
        "title": "Quotation", "number": "Quotation number", "date": "Date", "valid_until": "Valid until",
        "to": "Prepared for", "contact": "Contact", "phone": "Phone", "email": "Email", "area": "Area",
        "product": "Product", "qty": "Quantity", "unit_price": "Unit price", "discount": "Discount",
        "line_total": "Amount", "frequency": "Frequency", "once": "One time", "weekly": "Weekly",
        "twice_weekly": "Twice a week", "subtotal": "Subtotal", "discounts": "Discounts",
        "delivery_fee": "Delivery fee", "delivery_included": "Delivery included", "total": "Total",
        "estimates": "Estimated regular supply", "produce": "Produce", "delivery": "Delivery",
        "per_week": "Per week", "per_month": "Per month", "delivery_details": "Delivery",
        "delivery_area": "Delivery area", "delivery_day": "Delivery day", "payment_terms": "Payment terms",
        "notes": "Notes", "terms": "Terms and conditions", "currency": "EGP",
        "greeting": "Thank you for your interest. Please find our price offer below.",
        "closing": "We look forward to supplying you. Please contact us to confirm your order.",
        "subject": "Quotation {number} from {company}",
        "registry": "Commercial registry", "tax_id": "Tax ID", "status": "Status",
        "accepted_by": "Accepted on behalf of the client", "signed_date": "Date",
    },
    "ar": {
        "title": "عرض سعر", "number": "رقم العرض", "date": "التاريخ", "valid_until": "صالح حتى",
        "to": "مقدَّم إلى", "contact": "المسؤول", "phone": "الهاتف", "email": "البريد الإلكتروني", "area": "المنطقة",
        "product": "المنتج", "qty": "الكمية", "unit_price": "سعر الوحدة", "discount": "الخصم", "line_total": "القيمة",
        "frequency": "التكرار", "once": "مرة واحدة", "weekly": "أسبوعياً", "twice_weekly": "مرتين أسبوعياً",
        "subtotal": "الإجمالي قبل الخصم", "discounts": "الخصومات", "delivery_fee": "رسوم التوصيل",
        "delivery_included": "التوصيل مشمول", "total": "الإجمالي", "estimates": "تقدير التوريد المنتظم",
        "produce": "المنتجات", "delivery": "التوصيل", "per_week": "أسبوعياً", "per_month": "شهرياً",
        "delivery_details": "التوصيل", "delivery_area": "منطقة التوصيل", "delivery_day": "يوم التوصيل",
        "payment_terms": "شروط الدفع", "notes": "ملاحظات", "terms": "الشروط والأحكام", "currency": "ج.م",
        "greeting": "نشكركم على اهتمامكم، ونرفق لكم عرض الأسعار التالي.",
        "closing": "يسعدنا التوريد لكم. يرجى التواصل معنا لتأكيد الطلب.",
        "subject": "عرض سعر رقم {number} من {company}",
        "registry": "السجل التجاري", "tax_id": "الرقم الضريبي", "status": "الحالة",
        "accepted_by": "الموافقة نيابة عن العميل", "signed_date": "التاريخ",
    },
}
TERMS_LABELS = {"en": {"immediate": "Payment on delivery", "net15": "Within 15 days", "net30": "Within 30 days",
                       "net60": "Within 60 days", "consignment": "Consignment", "cash": "Cash",
                       "full_payment": "Payment on delivery", "credit": "Credit"},
                "ar": {"immediate": "الدفع عند التسليم", "net15": "خلال ١٥ يوماً", "net30": "خلال ٣٠ يوماً",
                       "net60": "خلال ٦٠ يوماً", "consignment": "أمانة", "cash": "نقداً",
                       "full_payment": "الدفع عند التسليم", "credit": "آجل"}}


def printable(q: B2BQuotation) -> dict:
    """Everything the printed and e-mailed copies show — and nothing else (no cost, no margin)."""
    lang = q.language if q.language in PRINT_TEXT else "en"
    data = serialize(q, Decimal("0"), for_staff=False)
    for key in ("below_cost_approved", "target_margin_pct"):
        data.pop(key, None)
    data["text"] = PRINT_TEXT[lang]
    data["rtl"] = lang == "ar"
    data["payment_terms_label"] = TERMS_LABELS[lang].get(q.payment_terms or "", q.payment_terms)
    data["has_recurring"] = data["totals"]["deliveries_per_week"] > 0
    return data


def email_html(q: B2BQuotation, brand: dict) -> tuple[str, str, str]:
    """(subject, html, text): the same content as the printed copy, as e-mail-safe HTML (tables, inline styles)."""
    d = printable(q)
    t = d["text"]
    align = "right" if d["rtl"] else "left"
    m = lambda v: f"{float(v or 0):,.2f}"
    subject = t["subject"].format(number=q.number, company=brand.get("company_name", ""))
    rows = "".join(
        f"<tr><td style='padding:6px;border-bottom:1px solid #e6e8e0'>{escape(l['product'])}"
        + (f"<div style='color:#6b7363;font-size:12px'>{escape(l['note'])}</div>" if l.get("note") else "") + "</td>"
        f"<td style='padding:6px;border-bottom:1px solid #e6e8e0'>{l['qty']:g} {escape(l['unit'])}</td>"
        f"<td style='padding:6px;border-bottom:1px solid #e6e8e0'>{m(l['unit_price'])}</td>"
        f"<td style='padding:6px;border-bottom:1px solid #e6e8e0'>{(str(round(l['line_discount_pct'], 2)).rstrip('0').rstrip('.') + '%') if l['line_discount_pct'] else ''}</td>"
        f"<td style='padding:6px;border-bottom:1px solid #e6e8e0'>{escape(t[l['frequency']])}</td>"
        f"<td style='padding:6px;border-bottom:1px solid #e6e8e0;text-align:{'left' if d['rtl'] else 'right'}'>{m(l['line_total'])}</td></tr>"
        for l in d["lines"])
    tot = d["totals"]
    fee = m(tot["delivery_fee"]) if tot["delivery_fee"] else t["delivery_included"]
    total_rows = [(t["subtotal"], m(tot["subtotal"]))]
    if tot["discount_total"]:
        total_rows.append((t["discounts"], m(tot["discount_total"])))
    total_rows += [(t["delivery_fee"], fee), (t["total"], f"{m(tot['total'])} {t['currency']}")]
    totals_html = "".join(f"<tr><td style='padding:4px 6px'>{escape(a)}</td><td style='padding:4px 6px;"
                          f"font-weight:600'>{escape(b)}</td></tr>" for a, b in total_rows)
    est = ""
    if d["has_recurring"]:
        est = (f"<h3 style='font-size:15px;margin:16px 0 6px'>{escape(t['estimates'])}</h3>"
               f"<table style='border-collapse:collapse;font-size:13px'><tr><th></th><th style='padding:4px 8px'>"
               f"{escape(t['per_week'])}</th><th style='padding:4px 8px'>{escape(t['per_month'])}</th></tr>"
               f"<tr><td style='padding:4px 8px'>{escape(t['produce'])}</td><td style='padding:4px 8px'>"
               f"{m(tot['weekly_produce'])}</td><td style='padding:4px 8px'>{m(tot['monthly_produce'])}</td></tr>"
               f"<tr><td style='padding:4px 8px'>{escape(t['delivery'])}</td><td style='padding:4px 8px'>"
               + (f"{m(tot['weekly_delivery'])}</td><td style='padding:4px 8px'>{m(tot['monthly_delivery'])}"
                  if tot["weekly_delivery"] else f"{escape(t['delivery_included'])}</td><td style='padding:4px 8px'>"
                  f"{escape(t['delivery_included'])}")
               + f"</td></tr><tr><td style='padding:4px 8px;font-weight:600'>{escape(t['total'])}</td>"
               f"<td style='padding:4px 8px;font-weight:600'>{m(tot['weekly_value'])}</td><td style='padding:4px 8px;"
               f"font-weight:600'>{m(tot['monthly_value'])}</td></tr></table>")
    details = [(t["delivery_area"], d["delivery_area"]), (t["delivery_day"], d["delivery_day"]),
               (t["payment_terms"], d["payment_terms_label"])]
    details_html = "".join(f"<div><b>{escape(a)}:</b> {escape(str(b))}</div>" for a, b in details if b)
    html = (
        f"<div dir='{'rtl' if d['rtl'] else 'ltr'}' style='font-family:Arial,Helvetica,sans-serif;color:#1a1e14;"
        f"max-width:680px;margin:0 auto;padding:12px;text-align:{align}'>"
        f"<div style='font-size:20px;font-weight:700;color:{escape(brand.get('primary_color', '#2a7a2a'))}'>"
        f"{escape(brand.get('company_name', ''))}</div>"
        f"<h2 style='margin:8px 0;font-size:18px'>{escape(t['title'])} {escape(q.number)}</h2>"
        f"<p style='font-size:14px'>{escape(t['greeting'])}</p>"
        f"<div style='font-size:13px;line-height:1.7'><b>{escape(t['to'])}:</b> {escape(d['recipient'])}<br>"
        f"<b>{escape(t['date'])}:</b> {d['quote_date']} &nbsp; <b>{escape(t['valid_until'])}:</b> {d['valid_until']}</div>"
        f"<table cellpadding='0' cellspacing='0' style='width:100%;border-collapse:collapse;font-size:13px;margin:12px 0'>"
        f"<tr style='background:#eef6f0'>" + "".join(
            f"<th style='padding:6px;text-align:{align}'>{escape(t[k])}</th>"
            for k in ("product", "qty", "unit_price", "discount", "frequency", "line_total")) + f"</tr>{rows}</table>"
        f"<table style='border-collapse:collapse;font-size:13px;margin-{'right' if d['rtl'] else 'left'}:auto'>{totals_html}</table>"
        f"{est}<div style='font-size:13px;margin-top:12px;line-height:1.7'>{details_html}</div>"
        + (f"<h3 style='font-size:15px;margin:16px 0 6px'>{escape(t['notes'])}</h3><p style='font-size:13px;"
           f"white-space:pre-line'>{escape(d['notes'])}</p>" if d.get("notes") else "")
        + (f"<h3 style='font-size:15px;margin:16px 0 6px'>{escape(t['terms'])}</h3><p style='font-size:12px;"
           f"white-space:pre-line;color:#4a5040'>{escape(d['terms'])}</p>" if d.get("terms") else "")
        + f"<p style='font-size:14px;margin-top:16px'>{escape(t['closing'])}</p>"
        f"<p style='font-size:11px;color:#8a9080'>{escape(brand.get('company_name', ''))} · "
        f"{escape(brand.get('website', ''))}</p></div>")
    text = "\n".join([subject, t["greeting"], f"{t['to']}: {d['recipient']}",
                      f"{t['date']}: {d['quote_date']}   {t['valid_until']}: {d['valid_until']}", ""]
                     + [f"{l['product']}: {l['qty']:g} {l['unit']} × {m(l['unit_price'])} = {m(l['line_total'])}"
                        for l in d["lines"]]
                     + ["", f"{t['delivery_fee']}: {fee}", f"{t['total']}: {m(tot['total'])} {t['currency']}", "",
                        t["closing"]])
    return subject, html, text
