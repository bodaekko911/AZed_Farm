"""B2B quotations (price offers): the Quotations tab's API, the printed copy, and e-mailing it.

Permissions: tab_b2b_quotations to see them; action_b2b_quotations_create to create / edit drafts / duplicate / renew;
action_b2b_quotations_send to e-mail them and mark them sent, accepted or rejected; approving a below-cost price needs
action_b2b_quotations_below_cost (checked in the service). Quotations never change stock, accounting or balances.
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.log import record
from app.core.permissions import get_current_user, has_permission, require_permission
from app.core.templates import templates
from app.database import get_async_session
from app.models.quotation import B2BQuotation
from app.models.user import User
from app.services import quotation_service as qs

router = APIRouter(prefix="/b2b", tags=["B2B quotations"],
                   dependencies=[Depends(require_permission("page_b2b")),
                                 Depends(require_permission("tab_b2b_quotations"))])

CREATE = Depends(require_permission("action_b2b_quotations_create"))
SEND = Depends(require_permission("action_b2b_quotations_send"))


class StatusIn(BaseModel):
    status: str = Field(..., pattern="^(sent|accepted|rejected)$")
    reason: Optional[str] = Field(None, max_length=300)


class RenewIn(BaseModel):
    valid_until: Optional[str] = None
    approve_below_cost: bool = False


class SettingsIn(BaseModel):
    terms_en: Optional[str] = Field(None, max_length=4000)
    terms_ar: Optional[str] = Field(None, max_length=4000)
    target_margin_pct: float = Field(30, ge=0, le=95)
    validity_days: int = Field(14, ge=1, le=180)


async def _out(db, q: B2BQuotation) -> dict:
    cfg = await qs.get_settings(db)
    out = qs.serialize(q, Decimal(str(cfg.target_margin_pct)), for_staff=True)
    out["invoices"] = await qs.invoices_of(db, q.id)
    out["has_recurring"] = out["totals"]["deliveries_per_week"] > 0
    return out


class ConvertIn(BaseModel):
    skip_delivery: bool = False


class MakeClientIn(BaseModel):
    link_client_id: Optional[int] = None


@router.get("/api/quotations/options")
async def options(db: AsyncSession = Depends(get_async_session), user: User = Depends(get_current_user)):
    """What the quotation form needs: clients (with their discount and terms), active products (with today's cost,
    for the margin shown on screen) and the settings."""
    from app.models.b2b import B2BClient
    from app.models.product import Product
    cfg = await qs.get_settings(db)
    await db.commit()
    clients = (await db.execute(select(B2BClient).where(or_(B2BClient.is_active.is_(True), B2BClient.is_active.is_(None)))
                                .order_by(B2BClient.name))).scalars().all()
    products = (await db.execute(select(Product).where(or_(Product.is_active.is_(True), Product.is_active.is_(None)))
                                 .order_by(Product.name))).scalars().all()
    return {
        "clients": [{"id": c.id, "name": c.name, "discount_pct": float(c.discount_pct or 0),
                     "payment_terms": c.payment_terms, "email": c.email} for c in clients],
        "products": [{"id": p.id, "name": p.name, "sku": p.sku, "unit": p.unit or "", "price": float(p.price or 0),
                      "cost": float(p.cost or 0), "category": p.category} for p in products],
        "settings": {"terms_en": cfg.terms_en, "terms_ar": cfg.terms_ar,
                     "target_margin_pct": float(cfg.target_margin_pct), "validity_days": cfg.validity_days},
        "can": {"create": has_permission(user, "action_b2b_quotations_create"),
                "send": has_permission(user, "action_b2b_quotations_send"),
                "below_cost": has_permission(user, qs.BELOW_COST_PERMISSION),
                "convert": has_permission(user, "action_b2b_quotations_convert")
                           and has_permission(user, "action_b2b_invoices_create"),
                "create_client": has_permission(user, "action_b2b_clients_create")},
    }


@router.get("/api/quotations")
async def list_quotations(status: Optional[str] = None, q: Optional[str] = None, date_from: Optional[str] = None,
                          date_to: Optional[str] = None, db: AsyncSession = Depends(get_async_session)):
    from app.models.b2b import B2BClient
    await qs.expire_due(db)
    stmt = select(B2BQuotation).options(selectinload(B2BQuotation.client)).outerjoin(
        B2BClient, B2BClient.id == B2BQuotation.client_id)
    if status in qs.STATUSES:
        stmt = stmt.where(B2BQuotation.status == status)
    if q:
        like = f"%{q.strip()[:80]}%"
        stmt = stmt.where(or_(B2BClient.name.ilike(like), B2BQuotation.prospect_name.ilike(like),
                              B2BQuotation.number.ilike(like)))
    for value, op in ((date_from, ">="), (date_to, "<=")):
        if value:
            try:
                day = date.fromisoformat(value[:10])
            except ValueError:
                raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD.")
            stmt = stmt.where(B2BQuotation.quote_date >= day if op == ">=" else B2BQuotation.quote_date <= day)
    rows = (await db.execute(stmt.order_by(B2BQuotation.quote_date.desc(), B2BQuotation.id.desc()).limit(300)
                             .execution_options(populate_existing=True))).scalars().all()
    return {"quotations": [{"id": r.id, "number": r.number, "recipient": qs.recipient_name(r),
                            "is_prospect": r.client_id is None, "quote_date": r.quote_date.isoformat(),
                            "valid_until": r.valid_until.isoformat(), "status": r.status, "total": float(r.total),
                            "weekly_value": float(r.weekly_value), "language": r.language} for r in rows]}


@router.get("/api/quotations/price")
async def price(product_id: int, client_id: Optional[int] = None, unit_price: Optional[float] = None,
                line_discount_pct: float = 0, db: AsyncSession = Depends(get_async_session)):
    """The default price for a product and client, and the margin check of a price — for the form, as you type."""
    from app.models.product import Product
    product = (await db.execute(select(Product).where(Product.id == product_id))).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    client = await qs._client(db, client_id) if client_id else None
    cfg = await qs.get_settings(db)
    await db.commit()
    default = qs.default_price(product, client)
    chosen = Decimal(str(unit_price)) if unit_price is not None else default
    c = qs.check_line(product, chosen, Decimal(str(line_discount_pct or 0)), Decimal(str(cfg.target_margin_pct)))
    return {"default_price": float(default), "catalogue_price": float(product.price or 0),
            "client_discount_pct": float(qs.client_discount(client)), "net_unit": float(c.net_unit),
            "effective_discount_pct": c.effective_discount_pct, "cost": float(c.cost) if c.cost is not None else None,
            "margin_pct": c.margin_pct, "below_cost": c.below_cost, "below_target": c.below_target,
            "no_cost": c.no_cost, "active": product.is_active is not False}


@router.get("/api/quotations/settings")
async def get_quote_settings(db: AsyncSession = Depends(get_async_session)):
    cfg = await qs.get_settings(db)
    await db.commit()
    return {"terms_en": cfg.terms_en, "terms_ar": cfg.terms_ar, "target_margin_pct": float(cfg.target_margin_pct),
            "validity_days": cfg.validity_days}


@router.post("/api/quotations/settings", dependencies=[CREATE])
async def save_quote_settings(data: SettingsIn, db: AsyncSession = Depends(get_async_session),
                              user: User = Depends(get_current_user)):
    cfg = await qs.get_settings(db)
    cfg.terms_en, cfg.terms_ar = data.terms_en, data.terms_ar
    cfg.target_margin_pct, cfg.validity_days = Decimal(str(data.target_margin_pct)), data.validity_days
    record(db, "B2B", "quotation_settings", f"Quotation settings: target margin {data.target_margin_pct:g}%, "
                                             f"valid {data.validity_days} days", user=user)
    await db.commit()
    return await get_quote_settings(db)


@router.get("/api/quotations/{quotation_id}")
async def get_quotation(quotation_id: int, db: AsyncSession = Depends(get_async_session)):
    return await _out(db, await qs.load(db, quotation_id))


@router.post("/api/quotations", dependencies=[CREATE])
async def create_quotation(data: dict, db: AsyncSession = Depends(get_async_session),
                           user: User = Depends(get_current_user)):
    q, checks = await qs.save(db, user, data)
    return {**await _out(db, await qs.load(db, q.id)), "checks": checks}


@router.put("/api/quotations/{quotation_id}", dependencies=[CREATE])
async def update_quotation(quotation_id: int, data: dict, db: AsyncSession = Depends(get_async_session),
                           user: User = Depends(get_current_user)):
    q, checks = await qs.save(db, user, data, await qs.load(db, quotation_id))
    return {**await _out(db, await qs.load(db, q.id)), "checks": checks}


@router.post("/api/quotations/{quotation_id}/duplicate", dependencies=[CREATE])
async def duplicate_quotation(quotation_id: int, db: AsyncSession = Depends(get_async_session),
                              user: User = Depends(get_current_user)):
    q, checks = await qs.duplicate(db, user, await qs.load(db, quotation_id))
    return {**await _out(db, await qs.load(db, q.id)), "checks": checks}


@router.post("/api/quotations/{quotation_id}/renew", dependencies=[CREATE])
async def renew_quotation(quotation_id: int, data: RenewIn, db: AsyncSession = Depends(get_async_session),
                          user: User = Depends(get_current_user)):
    q, checks = await qs.renew(db, user, await qs.load(db, quotation_id), data.valid_until, data.approve_below_cost)
    return {**await _out(db, await qs.load(db, q.id)), "checks": checks}


@router.post("/api/quotations/{quotation_id}/status", dependencies=[SEND])
async def quotation_status(quotation_id: int, data: StatusIn, db: AsyncSession = Depends(get_async_session),
                           user: User = Depends(get_current_user)):
    q = await qs.set_status(db, user, await qs.load(db, quotation_id), data.status, data.reason)
    return await _out(db, await qs.load(db, q.id))


@router.post("/api/quotations/{quotation_id}/email", dependencies=[SEND])
async def email_quotation(quotation_id: int, db: AsyncSession = Depends(get_async_session),
                          user: User = Depends(get_current_user)):
    """E-mail the quotation to the client's or prospect's address. On success it's logged and marked sent."""
    from app.services import mail_service
    q = await qs.load(db, quotation_id)
    if q.status not in ("draft", "sent"):
        raise HTTPException(status_code=409, detail=f"A quotation that is {q.status} can't be sent.")
    to = qs.recipient_email(q)
    if not to:
        raise HTTPException(status_code=400, detail="The recipient has no e-mail address. Add one to the client or "
                                                    "the prospect first.")
    subject, html, text = qs.email_html(q, templates.env.globals["BRAND"])
    try:
        await mail_service.send([to], subject, html, text)
    except Exception as exc:  # noqa: BLE001
        from app.services.automation_service import clean_error
        raise HTTPException(status_code=502, detail=f"Not sent: {clean_error(exc)}")
    try:
        from app.services.automation_service import note_email
        await note_email(db, f"Quotation {q.number}", [to])
    except Exception:  # noqa: BLE001  counting never undoes a sent e-mail
        pass
    q.status, q.sent_at, q.sent_to = "sent", qs.now_utc(), to
    record(db, "B2B", "email_quotation", f"{q.number} e-mailed to {to}", user=user, ref_type="b2b_quotation",
           ref_id=q.id)
    await db.commit()
    return {"ok": True, "message": f"{q.number} sent to {to}", "quotation": await _out(db, await qs.load(db, q.id))}


@router.post("/api/quotations/{quotation_id}/invoice",
             dependencies=[Depends(require_permission("action_b2b_quotations_convert")),
                           Depends(require_permission("action_b2b_invoices_create"))])
async def convert_quotation(quotation_id: int, data: ConvertIn, db: AsyncSession = Depends(get_async_session),
                            user: User = Depends(get_current_user)):
    """Accepted quotation → a B2B invoice, through the existing invoice code (stock checked now)."""
    out = await qs.convert_to_invoice(db, user, await qs.load(db, quotation_id), skip_delivery=data.skip_delivery)
    return {**out, "quotation": await _out(db, await qs.load(db, quotation_id))}


@router.post("/api/quotations/{quotation_id}/client", dependencies=[CREATE])
async def quotation_client(quotation_id: int, data: MakeClientIn, db: AsyncSession = Depends(get_async_session),
                           user: User = Depends(get_current_user)):
    """Make the prospect a B2B client (needs the B2B create-client right), or link an existing client."""
    out = await qs.make_client(db, user, await qs.load(db, quotation_id), data.link_client_id)
    return {**out, "quotation": await _out(db, await qs.load(db, quotation_id))}


@router.get("/quotation/{quotation_id}/print", response_class=HTMLResponse)
async def print_quotation(quotation_id: int, request: Request, db: AsyncSession = Depends(get_async_session)):
    """The printed quotation, in its own language. Built from qs.printable(), which carries no cost or margin."""
    q = await qs.load(db, quotation_id)
    return templates.TemplateResponse(request, "b2b_quotation_print.html", {"q": qs.printable(q)})
