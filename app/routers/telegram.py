"""Telegram field bot: the webhook Telegram calls, and the admin's bot settings (status, webhook, link codes, chats).

The webhook only checks Telegram's secret header, stores the update and answers — processing happens afterwards in
the background (field_service), so Telegram never waits on a model call and never retries a slow request.
"""
import hmac
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.log import record
from app.core.permissions import require_admin
from app.database import get_async_session
from app.models.telegram import TelegramChat
from app.models.user import User
from app.services import field_service as field
from app.services import telegram_api as tg

router = APIRouter(prefix="/telegram", tags=["Telegram"])


@router.post("/api/webhook")
async def webhook(request: Request, background: BackgroundTasks, db: AsyncSession = Depends(get_async_session)):
    secret = settings.TELEGRAM_WEBHOOK_SECRET or ""
    given = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
    if not secret or not settings.TELEGRAM_BOT_TOKEN or not hmac.compare_digest(given.encode(), secret.encode()):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        update = await request.json()
    except ValueError:
        return {"ok": True}
    if isinstance(update, dict) and await field.enqueue(db, update):
        # An album part waits 10 s for the rest; anything else is processed right after this answer.
        background.add_task(field.kick, field.ALBUM_WAIT.total_seconds() + 1 if _is_album(update) else 0.0)
    return {"ok": True}


def _is_album(update: dict) -> bool:
    return bool((update.get("message") or {}).get("media_group_id"))


# ── Admin ────────────────────────────────────────────────────────────────────

class LinkCodeIn(BaseModel):
    employee_id: Optional[int] = None
    user_id: Optional[int] = None
    kinds: list[str] = Field(default_factory=list)
    daily_limit: int = Field(30, ge=1, le=500)
    language: str = "ar"


class ChatIn(BaseModel):
    kinds: Optional[list[str]] = None
    daily_limit: Optional[int] = Field(None, ge=1, le=500)
    language: Optional[str] = None
    is_active: Optional[bool] = None


def _base_url(request: Request) -> str:
    return (settings.PUBLIC_BASE_URL or str(request.base_url)).rstrip("/")


@router.get("/api/admin/status")
async def status(request: Request, db: AsyncSession = Depends(get_async_session), _admin: User = Depends(require_admin)):
    out = {"token_set": bool(settings.TELEGRAM_BOT_TOKEN), "secret_set": bool(settings.TELEGRAM_WEBHOOK_SECRET),
           "webhook_url": _base_url(request) + "/telegram/api/webhook", "bot": None, "webhook": None, "error": None}
    if settings.TELEGRAM_BOT_TOKEN:
        try:
            me = await tg.me()
            out["bot"] = {"username": me.get("username"), "name": me.get("first_name")}
            info = await tg.webhook_info()
            # Only the parts worth showing — getWebhookInfo's own url is ours, never the token.
            out["webhook"] = {"url": info.get("url") or "", "pending": info.get("pending_update_count", 0),
                              "last_error": tg.redact(info.get("last_error_message") or "")[:200] or None}
        except tg.TelegramError as exc:
            out["error"] = tg.redact(exc)
    return out


@router.post("/api/admin/webhook")
async def register_webhook(request: Request, db: AsyncSession = Depends(get_async_session),
                           admin: User = Depends(require_admin)):
    if not tg.configured():
        raise HTTPException(status_code=400, detail="Set TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET first.")
    url = _base_url(request) + "/telegram/api/webhook"
    if not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Telegram needs an https address. Set PUBLIC_BASE_URL.")
    try:
        await tg.set_webhook(url, settings.TELEGRAM_WEBHOOK_SECRET)
    except tg.TelegramError as exc:
        raise HTTPException(status_code=502, detail=f"Telegram refused: {tg.redact(exc)}")
    record(db, "Telegram", "webhook", f"Telegram webhook registered at {url}", user=admin)
    await db.commit()
    return {"ok": True, "url": url}


@router.get("/api/admin/people")
async def people(db: AsyncSession = Depends(get_async_session), _admin: User = Depends(require_admin)):
    from app.models.hr import Employee
    employees = [{"id": i, "name": n} for i, n in (await db.execute(
        select(Employee.id, Employee.name).where(Employee.is_active.is_(True)).order_by(Employee.name))).all()]
    users = [{"id": i, "name": n} for i, n in (await db.execute(
        select(User.id, User.name).where(User.is_active.is_(True)).order_by(User.name))).all()]
    return {"employees": employees, "users": users, "kinds": list(field.SUBMISSION_KINDS)}


@router.post("/api/admin/link-codes")
async def link_code(data: LinkCodeIn, db: AsyncSession = Depends(get_async_session), admin: User = Depends(require_admin)):
    return await field.create_link_code(db, admin, employee_id=data.employee_id, user_id=data.user_id, kinds=data.kinds,
                                        daily_limit=data.daily_limit, language=data.language)


@router.get("/api/admin/chats")
async def chats(db: AsyncSession = Depends(get_async_session), _admin: User = Depends(require_admin)):
    rows = (await db.execute(select(TelegramChat).order_by(TelegramChat.is_active.desc(), TelegramChat.linked_at.desc()))).scalars().all()
    out = []
    for c in rows:
        out.append({"id": c.id, "username": c.username, "person": await field._person_name(db, c),
                    "employee_id": c.employee_id, "user_id": c.user_id, "kinds": field._kinds_of(c),
                    "daily_limit": c.daily_limit, "language": c.language, "is_active": c.is_active,
                    "used_today": await field._used_today(db, c.chat_id),
                    "linked_at": c.linked_at.isoformat() if c.linked_at else None})
    return {"chats": out}


@router.post("/api/admin/chats/{chat_row_id}")
async def update_chat(chat_row_id: int, data: ChatIn, db: AsyncSession = Depends(get_async_session),
                      admin: User = Depends(require_admin)):
    chat = (await db.execute(select(TelegramChat).where(TelegramChat.id == chat_row_id))).scalar_one_or_none()
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    if data.kinds is not None:
        kinds = [k for k in data.kinds if k in field.SUBMISSION_KINDS]
        if not kinds:
            raise HTTPException(status_code=400, detail="Pick at least one thing they may send.")
        chat.allowed_kinds = ",".join(kinds)
    if data.daily_limit is not None:
        chat.daily_limit = data.daily_limit
    if data.language in ("ar", "en"):
        chat.language = data.language
    if data.is_active is not None:
        if data.is_active and not chat.is_active and not await field._still_active(db, chat):
            raise HTTPException(status_code=400, detail="The linked person is inactive — make a new code instead.")
        chat.is_active = data.is_active                  # unlink / relink
    record(db, "Telegram", "update_chat", f"Telegram chat #{chat.id} updated: {data.model_dump(exclude_none=True)}",
           user=admin)
    await db.commit()
    return {"ok": True}


@router.delete("/api/admin/chats/{chat_row_id}")
async def delete_chat(chat_row_id: int, db: AsyncSession = Depends(get_async_session), admin: User = Depends(require_admin)):
    """Delete a chat from the list. To use the bot again that phone needs a new code. Its drafts stay in the inbox,
    without a sender name, and the worker is no longer told the outcome."""
    chat = (await db.execute(select(TelegramChat).where(TelegramChat.id == chat_row_id))).scalar_one_or_none()
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    name = await field._person_name(db, chat)
    await db.delete(chat)
    record(db, "Telegram", "delete_chat", f"Telegram chat of {name or 'unknown'} deleted from the list", user=admin)
    await db.commit()
    return {"ok": True}
