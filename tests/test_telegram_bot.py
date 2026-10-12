"""Telegram field bot (phase 1): workers send photos and short texts; each becomes a DRAFT a manager confirms.

A real async SQLite database runs the app's own recording code. Telegram and the AI model are httpx MockTransports, so
nothing leaves the machine. What is protected:

* the webhook needs Telegram's secret header, stores each update once (by update_id) and does no work itself;
* an unlinked chat gets one polite reply a day and nothing else; a link code works once, within 24 hours, and five
  wrong codes in a day make the bot ignore /start from that chat until tomorrow;
* an album is one document, read once 10 seconds after its last part;
* Arabic text becomes a farm-delivery or spoilage draft; nothing is recorded until a manager confirms;
* confirming records through the same services as the web pages, with the CONFIRMING manager's permissions, never by
  the person who sent it; rejecting needs a reason; the worker is told either way;
* a daily limit per chat, counting only submissions that call the model;
* the bot token never reaches a log line, an exception message or the activity log;
* stored evidence is a small grayscale JPEG; retention deletes what is no longer needed.
"""
import asyncio
import io
import json
import logging
import time
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.models  # noqa: F401  every table
from app.core.config import settings
from app.core.log import ActivityLog
from app.database import Base
from app.models.accounting import Account
from app.models.document import DocumentFile, DocumentLink
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.hr import Employee
from app.models.product import Product
from app.models.spoilage import SpoilageRecord
from app.models.telegram import FieldDraft, TelegramChat, TelegramChatGuard, TelegramLinkCode, TelegramUpdate
from app.models.user import User
from app.routers import telegram as tg_router
from app.services import field_service as field
from app.services import telegram_api as tg

TOKEN = "123456789:AAH-secretTOKENvalue_xyz"
SECRET = "webhook-secret-1"
WORKER_CHAT = 5550001


# ── Fixtures: settings, a fake Telegram, a fake model ───────────────────────

class FakeTelegram:
    """Answers the Bot API calls the bot makes, and remembers what it sent."""

    def __init__(self):
        self.sent, self.calls, self.files, self.next_id = [], [], {}, 100
        self.fail_downloads = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/file/bot" in path:
            if self.fail_downloads:
                raise httpx.ConnectError(f"could not reach {request.url}", request=request)
            return httpx.Response(200, content=self.files[path.rsplit("/", 1)[-1]])
        method = path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "sendMessage":
            self.next_id += 1
            self.sent.append({**body, "message_id": self.next_id})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        if method == "getFile":
            data = self.files[body["file_id"]]
            return httpx.Response(200, json={"ok": True, "result": {"file_path": f"photos/{body['file_id']}",
                                                                   "file_size": len(data)}})
        if method == "setWebhook":
            return httpx.Response(400, json={"ok": False, "description": f"bad url for bot{TOKEN}"})
        return httpx.Response(200, json={"ok": True, "result": True})

    def texts(self, chat_id=WORKER_CHAT):
        return [m["text"] for m in self.sent if m["chat_id"] == chat_id]


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(settings, "ASSISTANT_API_KEY", "test-key")
    monkeypatch.setattr(settings, "ASSISTANT_MODEL", "test-model")
    monkeypatch.setattr(settings, "ASSISTANT_FAST_MODEL", None)
    monkeypatch.setattr(settings, "ASSISTANT_VISION_MODEL", "vision-model")
    monkeypatch.setattr(settings, "ASSISTANT_BASE_URL", "https://assistant.test/v1")
    monkeypatch.setattr(tg, "_transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(field, "kick", lambda *a, **k: None)          # tests drain the queue themselves
    from app.services import assistant_service
    monkeypatch.setattr(assistant_service, "RETRY_DELAYS", (0, 0))
    yield fake
    asyncio.set_event_loop(asyncio.new_event_loop())


def model(*payloads, seen=None):
    """The AI model: answers each call with the next payload."""
    queue = list(payloads)

    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(json.loads(request.content))
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}],
                                         "usage": {"prompt_tokens": 1800, "completion_tokens": 200}})
    return httpx.MockTransport(handler)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def admin(uid=1):
    return SimpleNamespace(id=uid, name="Boss", role="admin", permissions=None)


def reviewer(extra=""):
    """A farm manager who may use the inbox and record farm deliveries."""
    perms = "page_field_inbox,action_field_drafts_review,page_farm,action_farm_delivery_create" + extra
    return SimpleNamespace(id=3, name="Manager", role="viewer", permissions=perms)


async def setup():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    db.add_all([
        User(id=1, name="Boss", email="boss@x.test", password="x", role="admin"),
        User(id=2, name="Saeed", email="saeed@x.test", password="x", role="viewer"),
        User(id=3, name="Manager", email="m@x.test", password="x", role="viewer"),
        Employee(id=1, name="Am Saeed", is_active=True),
        Employee(id=2, name="Gone", is_active=False),
        Account(code="1000", name="Cash", type="asset", balance=0),
        Account(code="5500", name="Other Expenses", type="expense", balance=0),
        Farm(id=1, name="North Farm", is_active=1), Farm(id=2, name="South Farm", is_active=1),
        Product(id=1, sku="TOM", name="Tomatoes", unit="kg", price=Decimal("20"), cost=Decimal("8"),
                stock=Decimal("50"), is_active=True),
        Product(id=2, sku="CUC", name="Cucumber", unit="kg", price=Decimal("10"), cost=Decimal("4"),
                stock=Decimal("30"), is_active=True),
    ])
    await db.commit()
    return engine, factory, db


def scenario(fn):
    async def go():
        engine, factory, db = await setup()
        try:
            return await fn(db, factory)
        finally:
            await db.close()
            await engine.dispose()
    return run(go())


async def link(db, *, employee_id=1, user_id=2, kinds=("harvest_sheet", "spoilage", "expense_receipt"), limit=30,
               chat_id=WORKER_CHAT):
    if await count(db, TelegramChat, TelegramChat.chat_id == chat_id):
        return
    db.add(TelegramChat(chat_id=chat_id, username="saeed", employee_id=employee_id, user_id=user_id,
                        allowed_kinds=",".join(kinds), daily_limit=limit, language="ar", is_active=True))
    await db.commit()


_uid = iter(range(1000, 100000))


def msg(text=None, *, chat_id=WORKER_CHAT, photo=None, group=None, reply_to=None, update_id=None, **extra):
    m = {"message_id": next(_uid), "date": int(time.time()), "chat": {"id": chat_id, "type": "private"},
         "from": {"id": chat_id, "username": "saeed", "phone_number": "+201001112223"}}
    if text is not None:
        m["text"] = text
    if photo is not None:
        m["photo"] = photo
    if group:
        m["media_group_id"] = group
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    m.update(extra)
    return {"update_id": update_id or next(_uid), "message": m}


def tap(data, message_id, *, chat_id=WORKER_CHAT):
    return {"update_id": next(_uid), "callback_query": {"id": f"cb{next(_uid)}", "data": data, "from": {"id": chat_id},
                                                        "message": {"message_id": message_id, "chat": {"id": chat_id}}}}


def jpeg(w=3000, h=2000, color=(200, 40, 40)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def photo(fake, name, data=None):
    """A Telegram photo message's sizes; the bot should pick the 1280 px one (the largest ≤ about 1600)."""
    fake.files[name] = data or jpeg()
    fake.files[name + "_big"] = b"never downloaded"
    return [{"file_id": name + "_small", "width": 90, "height": 60, "file_size": 2000},
            {"file_id": name, "width": 1280, "height": 853, "file_size": len(fake.files[name])},
            {"file_id": name + "_big", "width": 2560, "height": 1706, "file_size": 900000}]


async def send(db, factory, *updates, transport=None, wait_album=True):
    for u in updates:
        await field.enqueue(db, u)
    if wait_album:   # the 10 s album wait, without sleeping
        await db.execute(update(TelegramUpdate).values(received_at=field.now() - timedelta(seconds=11)))
        await db.commit()
    return await field.drain(factory, transport=transport)


async def count(db, model_, *where):
    return (await db.execute(select(func.count()).select_from(model_).where(*where))).scalar()


async def drafts(db):
    db.expire_all()
    return (await db.execute(select(FieldDraft).order_by(FieldDraft.id))).scalars().all()


SHEET = {"type": "harvest_sheet", "date": None, "farm": "المزرعة الشمالية", "farm_match": "North Farm",
         "items": [{"farm": None, "farm_match": None, "product": "طماطم", "product_match": "Tomatoes", "qty": "١٢٠",
                    "unit": "كيلو"}], "received_by": None, "notes": None, "unclear": []}
TEXT_DELIVERY = {"type": "farm_delivery", "date": None, "farm": "المزرعة الشمالية", "farm_match": "North Farm",
                 "items": [{"product": "طماطم", "product_match": "Tomatoes", "qty": "٢٠", "unit": "كيلو"}],
                 "reason": None, "unclear": []}
TEXT_SPOILAGE = {"type": "spoilage", "date": None, "farm": None, "farm_match": None,
                 "items": [{"product": "خيار", "product_match": "Cucumber", "qty": "٣", "unit": "كيلو"}],
                 "reason": "حر شديد", "unclear": []}


def fake_request(body, secret=SECRET):
    raw = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json")]
    if secret is not None:
        headers.append((b"x-telegram-bot-api-secret-token", secret.encode()))

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/telegram/api/webhook", "headers": headers,
                    "query_string": b""}, receive)


# ── Webhook ─────────────────────────────────────────────────────────────────

def test_the_webhook_needs_the_secret_and_stores_each_update_once(telegram):
    async def go(db, factory):
        update_ = msg("hello", update_id=777)
        for bad in (None, "wrong"):
            with pytest.raises(HTTPException) as e:
                await tg_router.webhook(fake_request(update_, bad), BackgroundTasks(), db=db)
            assert e.value.status_code == 403
        assert await count(db, TelegramUpdate) == 0
        bg = BackgroundTasks()
        assert await tg_router.webhook(fake_request(update_), bg, db=db) == {"ok": True}
        assert len(bg.tasks) == 1                                             # processing is kicked, not done here
        bg2 = BackgroundTasks()
        await tg_router.webhook(fake_request(update_), bg2, db=db)           # Telegram retried it
        assert await count(db, TelegramUpdate) == 1 and not bg2.tasks
        stored = (await db.execute(select(TelegramUpdate))).scalar_one()
        assert "+20100" not in stored.payload and "phone" not in stored.payload   # no phone numbers kept
        assert telegram.sent == []                                            # nothing processed inside the webhook
    scenario(go)


def test_a_duplicate_update_id_is_processed_once(telegram):
    async def go(db, factory):
        u = msg("hello", update_id=900)
        await send(db, factory, u)
        await send(db, factory, u)
        assert len(telegram.texts()) == 1
    scenario(go)


# ── Linking ─────────────────────────────────────────────────────────────────

def test_an_unlinked_chat_gets_one_polite_reply_and_nothing_else(telegram):
    async def go(db, factory):
        await send(db, factory, msg("صباح الخير"), msg(photo=photo(telegram, "p1")), msg("تالف ٣ كيلو خيار"),
                   transport=model(TEXT_SPOILAGE))
        assert len(telegram.texts()) == 1 and "/start" in telegram.texts()[0]
        assert await count(db, FieldDraft) == 0 and await count(db, DocumentFile) == 0
    scenario(go)


def test_a_link_code_works_once_and_within_24_hours(telegram):
    async def go(db, factory):
        out = await field.create_link_code(db, admin(), employee_id=1, user_id=2, kinds=["harvest_sheet", "spoilage"])
        code = out["code"]
        assert await count(db, TelegramLinkCode, TelegramLinkCode.code_hash == code) == 0   # only the hash is kept
        await send(db, factory, msg(f"/start {code.lower()}"))
        chat = (await db.execute(select(TelegramChat))).scalar_one()
        assert (chat.chat_id, chat.employee_id, chat.user_id, chat.username) == (WORKER_CHAT, 1, 2, "saeed")
        assert chat.allowed_kinds == "harvest_sheet,spoilage" and "Am Saeed" in telegram.texts()[-1]
        # The same code from another chat: refused.
        await send(db, factory, msg(f"/start {code}", chat_id=6660002))
        assert await count(db, TelegramChat) == 1 and "غير صحيح" in telegram.texts(6660002)[-1]
        # An expired code: refused.
        old = await field.create_link_code(db, admin(), employee_id=1, kinds=["spoilage"])
        await db.execute(update(TelegramLinkCode).where(TelegramLinkCode.used_at.is_(None))
                         .values(expires_at=field.now() - timedelta(minutes=1)))
        await db.commit()
        await send(db, factory, msg(f"/start {old['code']}", chat_id=6660003))
        assert await count(db, TelegramChat) == 1
    scenario(go)


def test_five_wrong_codes_silence_start_until_tomorrow(telegram):
    async def go(db, factory):
        for n in range(5):
            await send(db, factory, msg(f"/start WRONG{n}", chat_id=7770001))
        assert len(telegram.texts(7770001)) == 5
        good = await field.create_link_code(db, admin(), employee_id=1, kinds=["spoilage"])
        await send(db, factory, msg(f"/start {good['code']}", chat_id=7770001))
        assert len(telegram.texts(7770001)) == 5 and await count(db, TelegramChat) == 0   # ignored
        # Tomorrow: the counter starts again.
        await db.execute(update(TelegramChatGuard).values(day=field.today_local() - timedelta(days=1)))
        await db.commit()
        await send(db, factory, msg(f"/start {good['code']}", chat_id=7770001))
        assert await count(db, TelegramChat) == 1
    scenario(go)


def test_a_deactivated_employee_or_disabled_user_unlinks_the_chat(telegram):
    async def go(db, factory):
        await link(db)
        await field.deactivate_linked(db, employee_id=1)
        await db.commit()
        assert not (await db.execute(select(TelegramChat.is_active))).scalar()
        # And a chat whose person was deactivated some other way stops at the next message.
        await db.execute(update(TelegramChat).values(is_active=True))
        await db.execute(update(Employee).where(Employee.id == 1).values(is_active=False))
        await db.commit()
        await send(db, factory, msg("تالف ٣ كيلو خيار"), transport=model(TEXT_SPOILAGE))
        db.expire_all()
        assert not (await db.execute(select(TelegramChat.is_active))).scalar() and await count(db, FieldDraft) == 0
    scenario(go)


# ── Submissions ─────────────────────────────────────────────────────────────

def test_an_album_is_one_document_read_once_after_its_last_part(telegram):
    async def go(db, factory):
        await link(db)
        seen = []
        parts = [msg(photo=photo(telegram, "a1"), group="G1"), msg(photo=photo(telegram, "a2"), group="G1", caption="كشف اليوم"),
                 msg(photo=photo(telegram, "a3"), group="G1")]
        # Within 10 s of the last part, nothing is read yet.
        assert await send(db, factory, *parts, transport=model(SHEET, seen=seen), wait_album=False) == 0
        assert seen == []
        assert await send(db, factory, transport=model(SHEET, seen=seen)) == 1
        assert len(seen) == 1 and len(seen[0]["messages"][1]["content"]) == 4      # one call, three images
        [d] = await drafts(db)
        assert (d.kind, d.status, d.source_text) == ("harvest_sheet", "awaiting_worker", "كشف اليوم")
        files = (await db.execute(select(DocumentFile.page_no, DocumentFile.size_bytes, DocumentFile.doc_key))).all()
        assert [f[0] for f in files] == [1, 2, 3] and {f[2] for f in files} == {d.doc_key}
        assert await count(db, DocumentLink) == 0                                    # linked only when confirmed
        # The bot's summary names what it read, with the two buttons — and never says "recorded".
        summary = telegram.sent[-1]
        assert "طماطم" in summary["text"] or "Tomatoes" in summary["text"]
        assert [b["callback_data"] for b in summary["reply_markup"]["inline_keyboard"][0]] == [f"fd:ok:{d.id}", f"fd:x:{d.id}"]
        assert "سُجّل" not in summary["text"] and "recorded" not in summary["text"].lower()
        # Telegram's ≤1600 px size was downloaded, never the 2560 px one.
        assert not any(c == ("getFile", {"file_id": "a1_big"}) for c in telegram.calls)
    scenario(go)


def test_stored_evidence_is_a_small_grayscale_jpeg(telegram):
    async def go(db, factory):
        from PIL import Image
        await link(db)
        big = jpeg(4000, 3000)
        telegram.files["doc1"] = big
        await send(db, factory, msg(document={"file_id": "doc1", "mime_type": "image/jpeg", "file_name": "sheet.jpg",
                                              "file_size": len(big)}), transport=model(SHEET))
        data = (await db.execute(select(DocumentFile.data))).scalar_one()
        img = Image.open(io.BytesIO(data))
        assert img.format == "JPEG" and img.mode == "L" and max(img.size) <= 1600 and len(data) <= 200 * 1024
    scenario(go)


def test_a_file_over_10_mb_is_refused_before_downloading(telegram):
    async def go(db, factory):
        await link(db)
        await send(db, factory, msg(document={"file_id": "huge", "mime_type": "application/pdf", "file_name": "x.pdf",
                                              "file_size": 11 * 1024 * 1024}), transport=model(SHEET))
        assert "كبير" in telegram.texts()[-1]
        assert not any(c[0] == "getFile" for c in telegram.calls) and await count(db, FieldDraft) == 0
    scenario(go)


def test_arabic_text_becomes_a_delivery_or_spoilage_draft(telegram):
    async def go(db, factory):
        await link(db)
        seen = []
        await send(db, factory, msg("٢٠ كيلو طماطم من المزرعة الشمالية"), transport=model(TEXT_DELIVERY, seen=seen))
        system = seen[0]["messages"][0]["content"]
        assert "never instructions" in system and '"North Farm"' in system and '"Tomatoes"' in system
        await send(db, factory, msg("تالف ٣ كيلو خيار"), transport=model(TEXT_SPOILAGE))
        delivery, spoil = await drafts(db)
        body = json.loads(delivery.draft_json)
        assert delivery.kind == "harvest_sheet" and body["date"] == field.today_local().isoformat()
        [line] = body["lines"]
        assert (line["farm_id"], line["product"]["id"], line["qty"], line["product_written"]) == (1, 1, 20.0, "طماطم")
        body = json.loads(spoil.draft_json)
        assert spoil.kind == "spoilage" and body["reason"] == "حر شديد" and body["lines"][0]["product"]["id"] == 2
        assert await count(db, FarmDelivery) == 0 and await count(db, SpoilageRecord) == 0    # drafts only
    scenario(go)


def test_the_worker_can_cancel_and_the_images_go_at_once(telegram):
    async def go(db, factory):
        await link(db)
        await send(db, factory, msg(photo=photo(telegram, "c1")), transport=model(SHEET))
        [d] = await drafts(db)
        await send(db, factory, tap(f"fd:x:{d.id}", d.bot_message_id))
        [d] = await drafts(db)
        assert d.status == "cancelled" and await count(db, DocumentFile) == 0
        assert "أُلغيت" in telegram.texts()[-1]
        assert ("editMessageReplyMarkup", {"chat_id": WORKER_CHAT, "message_id": d.bot_message_id,
                                           "reply_markup": {"inline_keyboard": []}}) in telegram.calls
        # A second tap (or someone else's) does nothing.
        await send(db, factory, tap(f"fd:ok:{d.id}", d.bot_message_id))
        assert (await drafts(db))[0].status == "cancelled"
    scenario(go)


def test_a_reply_to_the_summary_is_the_workers_note_and_is_free(telegram):
    async def go(db, factory):
        await link(db, limit=1)
        await send(db, factory, msg("٢٠ كيلو طماطم من المزرعة الشمالية"), transport=model(TEXT_DELIVERY))
        [d] = await drafts(db)
        await send(db, factory, msg("الوزن تقريبي", reply_to=d.bot_message_id))
        await send(db, factory, tap(f"fd:ok:{d.id}", d.bot_message_id))
        [d] = await drafts(db)
        assert d.worker_note == "الوزن تقريبي" and d.status == "in_review"
        assert "أُرسلت للمراجعة" in telegram.texts()[-1]
    scenario(go)


def test_the_daily_limit_counts_only_model_calls(telegram):
    async def go(db, factory):
        await link(db, limit=2)
        await send(db, factory, msg("٢٠ كيلو طماطم من المزرعة الشمالية"), transport=model(TEXT_DELIVERY))
        await send(db, factory, msg(photo=photo(telegram, "l1"), group="G2"), msg(photo=photo(telegram, "l2"), group="G2"),
                   transport=model(SHEET))                                   # an album counts once
        first = (await drafts(db))[0]
        await send(db, factory, tap(f"fd:ok:{first.id}", first.bot_message_id))   # a tap is free
        seen = []
        await send(db, factory, msg("تالف ٣ كيلو خيار"), transport=model(TEXT_SPOILAGE, seen=seen))
        assert seen == [] and "للحد اليومي" in telegram.texts()[-1] and await count(db, FieldDraft) == 2
    scenario(go)


def test_voice_notes_are_not_supported_yet(telegram):
    async def go(db, factory):
        await link(db)
        await send(db, factory, msg(voice={"file_id": "v1", "duration": 3, "file_size": 5000}))
        assert "الصوتية" in telegram.texts()[-1] and await count(db, FieldDraft) == 0
    scenario(go)


# ── Review in the Field inbox ───────────────────────────────────────────────

async def in_review(db, factory, telegram, text=None, payload=TEXT_DELIVERY, kinds=None, user_id=2):
    await link(db, user_id=user_id, **({"kinds": kinds} if kinds else {}))
    if text is None:
        await send(db, factory, msg(photo=photo(telegram, f"r{next(_uid)}")), transport=model(payload))
    else:
        await send(db, factory, msg(text), transport=model(payload))
    d = (await drafts(db))[-1]
    await send(db, factory, tap(f"fd:ok:{d.id}", d.bot_message_id))
    return (await drafts(db))[-1]


def test_a_manager_confirms_a_photo_draft_through_the_real_services(telegram):
    async def go(db, factory):
        d = await in_review(db, factory, telegram, text=None, payload=SHEET)
        assert await field.pending_count(db) == 1
        out = await field.confirm(db, reviewer(), d.id, {
            "date": field.today_local().isoformat(), "received_by": "", "notes": "",
            "items": [{"farm_id": 1, "product_id": 1, "qty": 120, "written": "طماطم · ١٢٠ · كيلو"}]})
        delivery = (await db.execute(select(FarmDelivery))).scalar_one()
        assert out["refs"] == delivery.delivery_number and delivery.user_id == 3          # the confirming manager
        tom = await db.get(Product, 1)
        await db.refresh(tom)
        assert float(tom.stock) == 170.0                                                 # stock went up
        link_ = (await db.execute(select(DocumentLink))).scalar_one()
        assert (link_.ref_type, link_.ref_id, link_.doc_key) == ("farm_delivery", delivery.id, d.doc_key)
        [d] = await drafts(db)
        assert (d.status, d.reviewed_by_id) == ("confirmed", 3) and await field.pending_count(db) == 0
        assert "قبولها" in telegram.texts()[-1]
        with pytest.raises(HTTPException) as e:                                          # never twice
            await field.confirm(db, reviewer(), d.id, {"date": field.today_local().isoformat(), "items": []})
        assert e.value.status_code == 409
    scenario(go)


def test_a_spoilage_draft_is_confirmed_through_the_production_code(telegram):
    async def go(db, factory):
        d = await in_review(db, factory, telegram, text="تالف ٣ كيلو خيار", payload=TEXT_SPOILAGE)
        boss = admin()
        out = await field.confirm(db, boss, d.id, {"date": field.today_local().isoformat(), "reason": "حر شديد",
                                                   "items": [{"product_id": 2, "qty": 3, "farm_id": None}]})
        rec = (await db.execute(select(SpoilageRecord))).scalar_one()
        assert (float(rec.qty), rec.reason) == (3.0, "حر شديد") and out["refs"] == rec.ref_number
        cuc = await db.get(Product, 2)
        await db.refresh(cuc)
        assert float(cuc.stock) == 27.0
    scenario(go)


def test_rejecting_needs_a_reason_and_tells_the_worker(telegram):
    async def go(db, factory):
        d = await in_review(db, factory, telegram, text="٢٠ كيلو طماطم من المزرعة الشمالية")
        with pytest.raises(HTTPException) as e:
            await field.reject(db, reviewer(), d.id, "   ")
        assert e.value.status_code == 400
        await field.reject(db, reviewer(), d.id, "الكمية غير صحيحة")
        [d] = await drafts(db)
        assert (d.status, d.reject_reason) == ("rejected", "الكمية غير صحيحة")
        assert "الكمية غير صحيحة" in telegram.texts()[-1] and await count(db, FarmDelivery) == 0
    scenario(go)


def test_permissions_are_the_confirming_managers_and_nobody_reviews_their_own(telegram):
    async def go(db, factory):
        d = await in_review(db, factory, telegram, text="٢٠ كيلو طماطم من المزرعة الشمالية")
        body = {"date": field.today_local().isoformat(), "items": [{"farm_id": 1, "product_id": 1, "qty": 20}]}
        no_review = SimpleNamespace(id=3, name="M", role="viewer",
                                    permissions="page_field_inbox,page_farm,action_farm_delivery_create")
        no_record = SimpleNamespace(id=3, name="M", role="viewer", permissions="page_field_inbox,action_field_drafts_review")
        for user in (no_review, no_record):
            with pytest.raises(HTTPException) as e:
                await field.confirm(db, user, d.id, body)
            assert e.value.status_code == 403
            with pytest.raises(HTTPException):
                await field.reject(db, user, d.id, "no")
        # The worker's own AZed login (user 2) can't confirm or reject it — unless that login is an admin.
        own = SimpleNamespace(id=2, name="Saeed", role="viewer",
                              permissions=reviewer().permissions)
        with pytest.raises(HTTPException) as e:
            await field.confirm(db, own, d.id, body)
        assert e.value.status_code == 403 and "yourself" in e.value.detail
        with pytest.raises(HTTPException):
            await field.reject(db, own, d.id, "mine")
        assert (await drafts(db))[0].status == "in_review" and await count(db, FarmDelivery) == 0
        # A failed recording leaves the draft in review, ready to try again.
        with pytest.raises(HTTPException):
            await field.confirm(db, reviewer(), d.id, {"date": field.today_local().isoformat(),
                                                       "items": [{"farm_id": 1, "product_id": 99, "qty": 20}]})
        assert (await drafts(db))[0].status == "in_review"
        # An admin may record what they sent themselves.
        await field.confirm(db, admin(uid=2), d.id, body)
        assert (await drafts(db))[0].status == "confirmed" and await count(db, FarmDelivery) == 1
    scenario(go)


def test_the_inbox_router_marks_own_drafts_and_streams_draft_photos(telegram):
    async def go(db, factory):
        from app.routers import documents as doc_router
        from app.routers import field_inbox
        d = await in_review(db, factory, telegram, text=None, payload=SHEET)
        own = SimpleNamespace(id=2, name="Saeed", role="viewer", permissions=reviewer().permissions)
        mine = await field_inbox.draft(d.id, db=db, user=own)
        assert mine["own"] and not mine["can_review"]
        mine_admin = await field_inbox.draft(d.id, db=db, user=admin(uid=2))
        assert not mine_admin["own"] and mine_admin["own_admin"] and mine_admin["can_review"]
        theirs = await field_inbox.draft(d.id, db=db, user=reviewer())
        assert not theirs["own"] and theirs["can_review"] and theirs["images"]
        listed = (await field_inbox.drafts("in_review", db=db, user=reviewer()))["drafts"]
        assert [x["id"] for x in listed] == [d.id] and listed[0]["sender"]["name"] == "Am Saeed"
        # Whoever may open the inbox sees a pending draft's photo; someone who can't, doesn't.
        resp = await doc_router.document_file(theirs["images"][0], db=db, user=reviewer())
        assert resp.media_type == "image/jpeg"
        cashier = SimpleNamespace(id=9, name="Till", role="cashier", permissions=None)
        with pytest.raises(HTTPException):
            await doc_router.document_file(theirs["images"][0], db=db, user=cashier)
    scenario(go)


# ── Housekeeping ────────────────────────────────────────────────────────────

def test_the_sweep_retries_stuck_work_and_deletes_what_is_no_longer_needed(telegram):
    async def go(db, factory):
        await link(db)
        await send(db, factory, msg(photo=photo(telegram, "s1")), transport=model(SHEET))           # abandoned
        d_reject = await in_review(db, factory, telegram, text=None, payload=SHEET)
        rejected_id = d_reject.id
        await field.reject(db, reviewer(), d_reject.id, "صورة غير واضحة")
        abandoned = (await drafts(db))[0]
        long_ago = field.now() - timedelta(days=91)
        await db.execute(update(FieldDraft).where(FieldDraft.id == abandoned.id)
                         .values(created_at=field.now() - timedelta(hours=49)))
        await db.execute(update(FieldDraft).where(FieldDraft.id == d_reject.id).values(reviewed_at=long_ago))
        db.add(TelegramUpdate(update_id=1, status="done", received_at=field.now() - timedelta(days=31)))
        db.add(TelegramUpdate(update_id=2, status="processing", claim="x", payload=json.dumps(msg("hi")["message"] | {}),
                              received_at=field.now(), claimed_at=field.now() - timedelta(minutes=10)))
        await db.commit()
        out = await field.sweep(db)
        assert out["retried"] == 1 and out["expired"] == 1 and out["rejected_cleared"] == 1 and out["updates_deleted"] == 1
        assert await count(db, DocumentFile) == 0
        db.expire_all()
        assert (await db.execute(select(FieldDraft.status).where(FieldDraft.id == rejected_id))).scalar() == "rejected"          # the row stays 90 days+, images go
        assert (await db.execute(select(TelegramUpdate.status).where(TelegramUpdate.update_id == 2))).scalar() == "pending"
    scenario(go)


def test_the_bot_token_never_reaches_logs_errors_or_the_activity_log(telegram, caplog):
    async def go(db, factory):
        caplog.set_level(logging.DEBUG)
        await link(db)
        telegram.fail_downloads = True                              # the download URL (with the token) fails
        for _ in range(field.MAX_ATTEMPTS):
            await send(db, factory, *([msg(photo=photo(telegram, "t1"))] if _ == 0 else []), transport=model(SHEET))
        assert "مشكلة" in telegram.texts()[-1]                      # the worker is told after the last attempt
        with pytest.raises(tg.TelegramError) as e:
            await tg.download(f"photos/x.jpg")
        assert TOKEN not in str(e.value) and "api.telegram.org" not in str(e.value)
        with pytest.raises(tg.TelegramError) as e:
            await tg.set_webhook("https://farm.test/telegram/api/webhook", SECRET)
        assert TOKEN not in str(e.value)
        logging.getLogger("httpx").info("HTTP Request: GET https://api.telegram.org/file/bot%s/photos/a.jpg", TOKEN)
        activity = " ".join(d for (d,) in (await db.execute(select(ActivityLog.description))).all())
        for text in (caplog.text, activity, " ".join(str(r.args) for r in caplog.records)):
            assert TOKEN not in text and "AAH-secretTOKEN" not in text and "/file/bot1" not in text
    scenario(go)


def test_the_admin_can_unlink_relink_and_delete_a_chat(telegram):
    async def go(db, factory):
        await link(db)
        chat_id = (await db.execute(select(TelegramChat.id))).scalar()
        boss = admin()
        await tg_router.update_chat(chat_id, tg_router.ChatIn(is_active=False), db=db, admin=boss)
        await send(db, factory, msg("تالف ٣ كيلو خيار"), transport=model(TEXT_SPOILAGE))
        assert await count(db, FieldDraft) == 0                                   # unlinked: ignored
        await tg_router.update_chat(chat_id, tg_router.ChatIn(is_active=True), db=db, admin=boss)
        await send(db, factory, msg("تالف ٣ كيلو خيار"), transport=model(TEXT_SPOILAGE))
        assert await count(db, FieldDraft) == 1                                   # relinked: works again
        # Relinking someone deactivated in HR is refused.
        await tg_router.update_chat(chat_id, tg_router.ChatIn(is_active=False), db=db, admin=boss)
        await db.execute(update(Employee).where(Employee.id == 1).values(is_active=False))
        await db.commit()
        with pytest.raises(HTTPException):
            await tg_router.update_chat(chat_id, tg_router.ChatIn(is_active=True), db=db, admin=boss)
        await tg_router.delete_chat(chat_id, db=db, admin=boss)
        assert await count(db, TelegramChat) == 0 and await count(db, FieldDraft) == 1    # its drafts stay
    scenario(go)


# ══ Phase 2: managers approve from Telegram ══════════════════════════════════

MANAGER_CHAT, OTHER_CHAT = 8880001, 8880002
REVIEW_PERMS = "page_field_inbox,action_field_drafts_review,page_farm,action_farm_delivery_create,page_production," \
               "action_production_log_spoilage"


async def approver(db, *, chat_id=MANAGER_CHAT, user_id=3, perms=REVIEW_PERMS, approves=True):
    await db.execute(update(User).where(User.id == user_id).values(permissions=perms))
    db.add(TelegramChat(chat_id=chat_id, username="boss", user_id=user_id, allowed_kinds="spoilage", daily_limit=30,
                        language="ar", is_active=True, approver=approves))
    await db.commit()


async def sent_for_review(db, factory, telegram, text=None, payload=TEXT_DELIVERY):
    """A worker's submission, sent for review — which is when approvers are told."""
    await link(db)
    if text is None:
        await send(db, factory, msg(photo=photo(telegram, f"q{next(_uid)}")), transport=model(payload))
    else:
        await send(db, factory, msg(text), transport=model(payload))
    d = (await drafts(db))[-1]
    await send(db, factory, tap(f"fd:ok:{d.id}", d.bot_message_id))
    return (await drafts(db))[-1]


def buttons_to(telegram, chat_id):
    last = [m for m in telegram.sent if m["chat_id"] == chat_id][-1]
    return [b.get("callback_data") for row in last.get("reply_markup", {}).get("inline_keyboard", []) for b in row]


def test_only_chosen_approvers_with_the_permissions_are_told(telegram):
    async def go(db, factory):
        await approver(db)                                                      # chosen, may review
        await approver(db, chat_id=OTHER_CHAT, user_id=1, approves=False)      # an admin, but not chosen
        await approver(db, chat_id=8880003, user_id=2, perms="page_field_inbox")   # chosen, can't review
        d = await sent_for_review(db, factory, telegram, text="٢٠ كيلو طماطم من المزرعة الشمالية")
        assert telegram.texts(MANAGER_CHAT) and not telegram.texts(OTHER_CHAT) and not telegram.texts(8880003)
        text = telegram.texts(MANAGER_CHAT)[-1]
        assert f"#{d.id}" in text and "Am Saeed" in text and "North Farm" in text and "Tomatoes" in text
        assert buttons_to(telegram, MANAGER_CHAT) == [f"fa:ok:{d.id}", f"fa:no:{d.id}"]
        assert json.loads((await drafts(db))[-1].notices)[0][0] == MANAGER_CHAT
    scenario(go)


def test_a_clean_draft_is_approved_from_telegram_through_the_real_services(telegram):
    async def go(db, factory):
        await approver(db)
        d = await sent_for_review(db, factory, telegram, text="٢٠ كيلو طماطم من المزرعة الشمالية")
        notice = json.loads((await drafts(db))[-1].notices)[0][1]
        await send(db, factory, tap(f"fa:ok:{d.id}", notice, chat_id=MANAGER_CHAT))
        delivery = (await db.execute(select(FarmDelivery))).scalar_one()
        item = (await db.execute(select(FarmDeliveryItem))).scalar_one()
        assert delivery.user_id == 3 and (item.product_id, float(item.qty)) == (1, 20.0)      # the approving manager
        [d] = await drafts(db)
        assert (d.status, d.reviewed_by_id) == ("confirmed", 3)
        assert "قبولها" in telegram.texts()[-1]                                                # the worker is told
        edits = [b for m, b in telegram.calls if m == "editMessageText"]
        assert edits[-1]["message_id"] == notice and "سُجّل" in edits[-1]["text"]           # the notice is closed
        # A second tap does nothing.
        await send(db, factory, tap(f"fa:ok:{d.id}", notice, chat_id=MANAGER_CHAT))
        assert await count(db, FarmDelivery) == 1
    scenario(go)


def test_approval_from_telegram_is_blocked_while_a_draft_has_warnings(telegram):
    async def go(db, factory):
        await approver(db)
        unclear = {**SHEET, "date": field.today_local().isoformat(), "unclear": ["items.0.qty"]}
        d = await sent_for_review(db, factory, telegram, text=None, payload=unclear)
        assert buttons_to(telegram, MANAGER_CHAT) == [f"fa:no:{d.id}"]                       # reject only
        assert "يحتاج مراجعة في AZed" in telegram.texts(MANAGER_CHAT)[-1]
        assert ("sendPhoto", {"chat_id": MANAGER_CHAT, "photo": json.loads(d.draft_json)["_tg_files"][0][1]}) \
            in telegram.calls                                                                 # the worker's photo, re-sent
        # Tapping an old or forged approve button changes nothing.
        await send(db, factory, tap(f"fa:ok:{d.id}", 1, chat_id=MANAGER_CHAT))
        assert await count(db, FarmDelivery) == 0 and (await drafts(db))[-1].status == "in_review"
        # A unit that can't be converted is a warning too.
        body = {"lines": [{"product": {"id": 1, "unit": "kg", "pack": {"amount": 1000.0, "base": "g"}}, "qty": 3,
                           "unit": "box", "pack": None, "farm_id": 1, "match": "suggested"}], "missing": [], "unclear": []}
        assert "unit" in field.warnings_for("harvest_sheet", body)
        body["lines"][0].update(unit="جرام", pack={"amount": 1.0, "base": "g"}, qty=500)
        assert field.warnings_for("harvest_sheet", body) == [] and field.product_qty(body["lines"][0]) == (0.5, True)
    scenario(go)


def test_a_manager_rejects_from_telegram_with_a_reason(telegram):
    async def go(db, factory):
        await approver(db)
        d = await sent_for_review(db, factory, telegram, text="٢٠ كيلو طماطم من المزرعة الشمالية")
        await send(db, factory, tap(f"fa:no:{d.id}", 1, chat_id=MANAGER_CHAT))
        assert "سبب الرفض" in telegram.texts(MANAGER_CHAT)[-1]
        await send(db, factory, msg("/cancel", chat_id=MANAGER_CHAT))
        assert (await drafts(db))[-1].status == "in_review"
        await send(db, factory, tap(f"fa:no:{d.id}", 1, chat_id=MANAGER_CHAT))
        await send(db, factory, msg("الكمية أكثر من المعتاد", chat_id=MANAGER_CHAT))
        [d] = await drafts(db)
        assert (d.status, d.reject_reason, d.reviewed_by_id) == ("rejected", "الكمية أكثر من المعتاد", 3)
        assert "الكمية أكثر من المعتاد" in telegram.texts()[-1]                                # the worker is told
        assert await count(db, FieldDraft) == 1                                                # no model call, no draft
    scenario(go)


def test_nobody_approves_their_own_from_telegram_and_a_failed_record_says_why(telegram):
    async def go(db, factory):
        await approver(db, user_id=2)            # the worker's own login (user 2), not an admin
        d = await sent_for_review(db, factory, telegram, text="تالف ٣ كيلو خيار", payload=TEXT_SPOILAGE)
        assert not telegram.texts(MANAGER_CHAT)
        await send(db, factory, tap(f"fa:ok:{d.id}", 1, chat_id=MANAGER_CHAT))
        assert (await drafts(db))[-1].status == "in_review"
        # Another manager approves — but there isn't that much cucumber in stock: nothing recorded, they're told why.
        await approver(db, chat_id=OTHER_CHAT)
        await db.execute(update(Product).where(Product.id == 2).values(stock=Decimal("1")))
        await db.commit()
        await send(db, factory, tap(f"fa:ok:{d.id}", 1, chat_id=OTHER_CHAT))
        assert "لم يُسجَّل" in telegram.texts(OTHER_CHAT)[-1] and "in stock" in telegram.texts(OTHER_CHAT)[-1]
        assert (await drafts(db))[-1].status == "in_review" and await count(db, SpoilageRecord) == 0
    scenario(go)
