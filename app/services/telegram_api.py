"""The few Telegram Bot API calls the field bot needs, over httpx (already a dependency — no Telegram library).

The bot token is part of every Bot API URL, including file download URLs, so it must never reach a log line, an
exception message or the activity log:

* every httpx error is re-raised as TelegramError carrying only the method name and an HTTP status or error type —
  never str(exc), which includes the URL;
* a logging filter on the httpx/httpcore loggers rewrites any message that contains the token (httpx logs each
  request's URL at INFO);
* ``redact`` scrubs the token from any text before it is logged.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)
API = "https://api.telegram.org"
MAX_DOWNLOAD = 10 * 1024 * 1024
_transport: Optional[httpx.AsyncBaseTransport] = None     # tests swap in a MockTransport


class TelegramError(Exception):
    """A Bot API call failed. The message never contains the token or a URL."""


_BOT_PATH = re.compile(r"/(?:file/)?bot\d+:[A-Za-z0-9_-]+")


def redact(text: Any) -> str:
    text = str(text)
    token = settings.TELEGRAM_BOT_TOKEN
    if token:
        text = text.replace(token, "<token>")
    return _BOT_PATH.sub("/bot<token>", text)


class _RedactToken(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        return True


for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_RedactToken())


def configured() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_WEBHOOK_SECRET)


async def call(method: str, payload: Optional[dict] = None, timeout: float = 20.0) -> Any:
    if not settings.TELEGRAM_BOT_TOKEN:
        raise TelegramError("The bot token is not set.")
    url = f"{API}/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=_transport) as client:
            response = await client.post(url, json=payload or {})
    except httpx.HTTPError as exc:
        raise TelegramError(f"{method}: {type(exc).__name__}") from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or not data.get("ok"):
        description = redact(str(data.get("description") or ""))[:160]
        raise TelegramError(f"{method}: HTTP {response.status_code} {description}".strip())
    return data.get("result")


async def send_message(chat_id: int, text: str, buttons: Optional[list] = None,
                       reply_to: Optional[int] = None) -> Optional[int]:
    """Plain text (no parse mode, so nothing a worker typed can be read as markup). Returns the message id."""
    payload: dict = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    result = await call("sendMessage", payload)
    return (result or {}).get("message_id")


async def clear_buttons(chat_id: int, message_id: int) -> None:
    await call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                          "reply_markup": {"inline_keyboard": []}})


async def answer_callback(callback_id: str, text: str = "") -> None:
    await call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})


async def file_info(file_id: str) -> dict:
    return await call("getFile", {"file_id": file_id}) or {}


async def download(file_path: str, max_bytes: int = MAX_DOWNLOAD) -> bytes:
    """A file's bytes, refused past ``max_bytes`` while streaming (the size Telegram reported is checked first)."""
    url = f"{API}/file/bot{settings.TELEGRAM_BOT_TOKEN}/{file_path}"
    try:
        async with httpx.AsyncClient(timeout=60.0, transport=_transport) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise TelegramError(f"download: HTTP {response.status_code}")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise TelegramError("download: file too large")
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise TelegramError(f"download: {type(exc).__name__}") from None


async def set_webhook(url: str, secret: str) -> Any:
    return await call("setWebhook", {"url": url, "secret_token": secret, "drop_pending_updates": False,
                                     "allowed_updates": ["message", "callback_query"], "max_connections": 10})


async def webhook_info() -> dict:
    return await call("getWebhookInfo") or {}


async def me() -> dict:
    return await call("getMe") or {}
