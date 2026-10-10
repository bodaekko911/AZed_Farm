"""Outgoing e-mail — through a Gmail relay over HTTPS, or straight over SMTP.

Railway's Hobby plan blocks outgoing SMTP (ports 465/587), so the default way is the **Gmail relay**: a small Google
Apps Script deployed as a web app inside the sending Gmail account (code in scripts/gmail_relay.gs). The app POSTs the
message to it over HTTPS — never blocked — and Gmail sends it from that account. Settings:
    MAIL_RELAY_URL     the web app's /exec URL
    MAIL_RELAY_SECRET  a long random string, the same one written in the script

SMTP is still supported for hosts that allow it (e.g. Railway Pro): SMTP_HOST, SMTP_PORT, SMTP_USERNAME,
SMTP_PASSWORD, SMTP_FROM, SMTP_SECURITY ("starttls" on 587, "ssl" on 465, or "none"). When both are set, the relay
is used. smtplib blocks, so an SMTP send runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Optional

import httpx

from app.core.config import settings


def uses_relay() -> bool:
    return bool(settings.MAIL_RELAY_URL and settings.MAIL_RELAY_SECRET)


def is_configured() -> bool:
    return uses_relay() or bool(settings.SMTP_HOST and (settings.SMTP_FROM or settings.SMTP_USERNAME))


def _send(recipients: list[str], subject: str, html: str, text: str) -> None:
    sender = settings.SMTP_FROM or settings.SMTP_USERNAME
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.APP_NAME, sender)) if "<" not in sender else sender
    msg["To"] = sender                     # stakeholders are Bcc'd, so they don't see each other's addresses
    msg["Message-ID"] = make_msgid()
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    security = (settings.SMTP_SECURITY or "starttls").lower()
    context = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30, context=context)
    else:
        server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if settings.SMTP_USERNAME:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD or "")
        server.send_message(msg, to_addrs=recipients)


async def _send_relay(recipients: list[str], subject: str, html: str, text: str,
                      transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
    payload = {"secret": settings.MAIL_RELAY_SECRET, "bcc": recipients, "subject": subject, "html": html,
               "text": text, "name": settings.APP_NAME}
    # Apps Script answers a POST with a redirect to the result; follow it.
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True, transport=transport) as client:
        response = await client.post(settings.MAIL_RELAY_URL, json=payload)
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or not data.get("ok"):
        reason = data.get("error") or f"HTTP {response.status_code}"
        if not data:
            # Google answered with a page, not the script's JSON — usually an error the script threw. Show its text.
            page = re.sub(r"(?s)<(script|style).*?</\1>", "", response.text)
            page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)).strip()
            if "MailApp" in page or "send_mail" in page:
                reason = page[:240] + " — open the script, run the 'authorize' function once and allow sending mail"
            elif page:
                reason = page[:240]
            else:
                reason = ("the relay didn't answer as expected — check MAIL_RELAY_URL is the web app's /exec URL "
                          "and that it is deployed with access 'Anyone'")
        raise RuntimeError(f"Gmail relay: {reason}")


async def send(recipients: list[str], subject: str, html: str, text: str,
               transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
    """Send one message to everyone in `recipients` (Bcc). Raises on any error, with a reason a person can act on."""
    if not is_configured():
        raise RuntimeError("E-mail isn't set up: add MAIL_RELAY_URL and MAIL_RELAY_SECRET (Gmail relay).")
    if not recipients:
        raise RuntimeError("No recipients.")
    if uses_relay():
        await _send_relay(recipients, subject, html, text, transport=transport)
        return
    try:
        await asyncio.to_thread(_send, recipients, subject, html, text)
    except OSError as exc:
        if getattr(exc, "errno", None) in (101, 110, 111, 113) or isinstance(exc, TimeoutError):
            raise RuntimeError(f"{exc} — the server can't reach the mail server. Railway's Hobby plan blocks "
                               "SMTP; use the Gmail relay (MAIL_RELAY_URL) instead.") from exc
        raise
