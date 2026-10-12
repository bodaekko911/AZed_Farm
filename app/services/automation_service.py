"""
Automations — rules that run on their own and send what they find by e-mail
============================================================================
Managed by an admin on the Ask page. Automations only READ business data (through the same builders the Reports
pages and Ask use) and send e-mail; the only rows they write are their own bookkeeping (automation_* tables) and
activity-log entries for the e-mails and model calls.

Two kinds of rule (see automation_rules.RULES):

* scheduled — daily / weekly / monthly at a time. Each period is claimed with one conditional UPDATE on the rule's
  ``last_period`` (the weekly brief's pattern), so with several gunicorn workers it runs exactly once. A failed send
  hands the period back and is retried on the next tick.
* events — polled: each tick looks only at rows with an id above the rule's ``watermark`` (a primary-key range), stores
  what it finds as automation_events (unique per rule and record, so nothing is reported twice) and moves the
  watermark with a conditional UPDATE. Events go out as ONE digest per rule at most every ``digest_minutes`` — or at
  once when one is urgent. A run claims its events by setting their run_id; if the send fails they are released and
  the next tick sends the same items, never twice.

Gmail's relay has a daily quota, so automation e-mails stop at a daily cap (the morning brief keeps the last slot);
alerts held back by the cap — or by Gmail saying its daily limit was reached — go out in the next morning brief.

The model only writes a few lines of text from figures already computed; if it fails, the e-mail goes without them.
Text from the database (names, notes, descriptions) is data, never instructions; error text is scrubbed of secrets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.log import ActivityLog, record
from app.core.time_utils import app_tz, utc_bounds
from app.models.automation import AutomationEvent, AutomationRule, AutomationRun, AutomationSettings

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
CATCH_UP = timedelta(hours=12)        # how late a scheduled run may still go out
KEEP = timedelta(days=90)
TRIGGERS = ("daily", "weekly", "monthly", "events")
MORNING = "morning_brief"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ── Reports a rule hands to the engine ───────────────────────────────────────

@dataclass
class Section:
    title: str
    head: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)      # a cell may be (text, url) for a link
    note: Optional[str] = None


@dataclass
class Report:
    subject: str
    intro: str
    sections: list[Section]
    items: int                                  # 0 = nothing to send (event digests)
    summary_data: Optional[dict] = None         # figures for the model's few lines (morning brief)
    summary: Optional[str] = None
    usage: Optional[tuple] = None               # (prompt tokens, completion tokens, seconds) of the summary call


# ── Fixed text ───────────────────────────────────────────────────────────────

TEXT = {
    "en": {"open": "Open in AZed", "footer": "Sent by AZed Farm automations. Figures come from the same reports as "
                                              "AZed; open the links to check them.", "test": "[Test] ",
           "highlights": "Highlights"},
    "ar": {"open": "افتح في AZed", "footer": "أرسلتها أتمتة AZed Farm. الأرقام من نفس تقارير AZed؛ افتح الروابط "
                                             "للتحقق منها.", "test": "[تجربة] ", "highlights": "أهم النقاط"},
}


def tr(lang: str, key: str) -> str:
    return (TEXT.get(lang) or TEXT["en"])[key]


def link(path: str) -> Optional[str]:
    """A full address for an e-mail link, or None when the app's public address isn't set."""
    base = (settings.PUBLIC_BASE_URL or "").rstrip("/")
    return f"{base}{path}" if base.startswith("http") else None


# ── The e-mail: tables, inline styles, no images or scripts, readable on a phone ──

def render(report: Report, lang: str) -> tuple[str, str]:
    rtl = lang == "ar"
    align = "right" if rtl else "left"
    def cell(value: Any) -> tuple[str, str]:
        if isinstance(value, tuple):
            text, url = value
            if url:
                return (f'<a href="{escape(url)}" style="color:#0f8a43">{escape(str(text))}</a>',
                        f"{text} ({url})")
            value = text
        return escape(str(value if value is not None else "—")), str(value if value is not None else "—")

    parts = [f'<div dir="{"rtl" if rtl else "ltr"}" style="font-family:Arial,Helvetica,sans-serif;color:#1a1e14;'
             f'max-width:640px;margin:0 auto;padding:12px;text-align:{align}">',
             f'<h2 style="margin:0 0 6px;font-size:20px;color:#0f8a43">{escape(report.subject)}</h2>',
             f'<p style="margin:0 0 14px;font-size:14px;color:#4a5040">{escape(report.intro)}</p>']
    text = [report.subject, report.intro, ""]
    if report.summary:
        parts.append(f'<div style="background:#eef6f0;border-radius:8px;padding:10px 12px;margin:0 0 14px;'
                     f'font-size:14px;line-height:1.6"><b>{escape(tr(lang, "highlights"))}</b><br>'
                     + "<br>".join(escape(l) for l in report.summary.splitlines()) + "</div>")
        text += [tr(lang, "highlights"), report.summary, ""]
    for s in report.sections:
        parts.append(f'<h3 style="margin:16px 0 6px;font-size:15px">{escape(s.title)}</h3>')
        text.append(s.title)
        if s.note:
            parts.append(f'<p style="margin:0 0 6px;font-size:12px;color:#6b7363">{escape(s.note)}</p>')
            text.append(s.note)
        if s.rows:
            parts.append(f'<table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;'
                         f'font-size:13px">')
            if s.head:
                parts.append("<tr>" + "".join(
                    f'<th style="text-align:{align};padding:5px 6px;border-bottom:2px solid #cfe5d6;color:#4a5040;'
                    f'font-weight:600">{escape(h)}</th>' for h in s.head) + "</tr>")
            for row in s.rows:
                cells = [cell(v) for v in row]
                parts.append("<tr>" + "".join(
                    f'<td style="padding:5px 6px;border-bottom:1px solid #e6e8e0;vertical-align:top">{h}</td>'
                    for h, _t in cells) + "</tr>")
                text.append(" · ".join(t for _h, t in cells))
            parts.append("</table>")
        text.append("")
    parts.append(f'<p style="margin:18px 0 0;font-size:11px;color:#8a9080">{escape(tr(lang, "footer"))}</p></div>')
    text.append(tr(lang, "footer"))
    return "".join(parts), "\n".join(text)


# ── Secrets never reach a log or the run table ───────────────────────────────

def clean_error(exc: Any) -> str:
    from app.services.assistant_lookups import hide_secrets
    from app.services.telegram_api import redact
    text = redact(hide_secrets(str(exc)))
    for secret in (settings.MAIL_RELAY_SECRET, settings.SMTP_PASSWORD, settings.ASSISTANT_API_KEY):
        if secret:
            text = text.replace(secret, "[hidden]")
    return text[:300]


def is_gmail_limit(exc: Any) -> bool:
    """The relay (Apps Script MailApp) refusing because the day's sending quota is used up."""
    t = str(exc).lower()
    return "too many times" in t or ("quota" in t and "mail" in t) or "daily limit" in t or \
        ("limit" in t and "email" in t and "day" in t)


# ── Settings, cap, e-mail count ──────────────────────────────────────────────

async def get_settings(db) -> AutomationSettings:
    row = (await db.execute(select(AutomationSettings).where(AutomationSettings.id == 1))).scalar_one_or_none()
    if row is None:
        row = AutomationSettings(id=1, daily_cap=40)
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            row = (await db.execute(select(AutomationSettings).where(AutomationSettings.id == 1))).scalar_one()
    return row


def _today_bounds(now_local: datetime) -> tuple[datetime, datetime]:
    return utc_bounds(now_local.date(), now_local.date())


async def sent_today(db, now_local: datetime) -> int:
    """Automation e-mails sent today (what the cap counts — tests and the briefs aren't)."""
    s, e = _today_bounds(now_local)
    return (await db.execute(select(func.count(AutomationRun.id)).where(
        AutomationRun.status == "sent", AutomationRun.started_at >= s, AutomationRun.started_at <= e))).scalar() or 0


async def note_email(db, source: str, recipients: list[str]) -> None:
    """Every e-mail the app sends (briefs, automations, tests) — they share one Gmail quota, which counts recipients."""
    record(db, "Email", "sent", f"{source} — {len(recipients)} recipient(s)", ref_type="recipients",
           ref_id=str(len(recipients)))
    await db.commit()


async def emails_today(db, now_local: Optional[datetime] = None) -> dict:
    now_local = now_local or datetime.now(app_tz())
    s, e = _today_bounds(now_local)
    rows = (await db.execute(select(ActivityLog.ref_id).where(
        ActivityLog.module == "Email", ActivityLog.action == "sent",
        ActivityLog.created_at >= s, ActivityLog.created_at <= e))).all()
    return {"emails": len(rows), "recipients": sum(int(r) if str(r or "").isdigit() else 0 for (r,) in rows)}


async def blocked(db, now_local: datetime, rule_key: str) -> Optional[str]:
    """Why an automation e-mail can't go now: "gmail_limit", "capped", or None. The morning brief keeps the last
    slot under the cap, so alerts held back always have a way out."""
    cfg = await get_settings(db)
    if cfg.gmail_limit_day == now_local.date():
        return "gmail_limit"
    cap = max(1, int(cfg.daily_cap or 40))
    used = await sent_today(db, now_local)
    limit = cap if rule_key == MORNING else cap - 1
    return "capped" if used >= limit else None


# ── Rules: the stored row, created from the rule's defaults ──────────────────

def rules() -> dict:
    from app.services.automation_rules import RULES
    return RULES


async def get_rule(db, key: str) -> AutomationRule:
    spec = rules()[key]
    row = (await db.execute(select(AutomationRule).where(AutomationRule.key == key))).scalar_one_or_none()
    if row is None:
        row = AutomationRule(key=key, enabled=False, trigger=spec.trigger, send_time=spec.send_time,
                             weekday=spec.weekday, month_day=spec.month_day, language="en",
                             digest_minutes=60, settings=json.dumps(spec.default_settings()))
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            row = (await db.execute(select(AutomationRule).where(AutomationRule.key == key))).scalar_one()
    return row


def rule_settings(row: AutomationRule) -> dict:
    spec = rules()[row.key]
    out = spec.default_settings()
    try:
        stored = json.loads(row.settings or "{}")
    except ValueError:
        stored = {}
    for s in spec.settings:
        if s.key in stored:
            out[s.key] = stored[s.key]
    return out


async def recipients_for(db, row: AutomationRule) -> list[str]:
    from app.services import weekly_brief_service as brief
    good, _bad = brief.parse_recipients(row.recipients or "")
    if good:
        return good
    cfg = await brief.get_settings(db)
    return brief.parse_recipients(cfg.recipients or "")[0]


# ── Scheduling ───────────────────────────────────────────────────────────────

def last_moment(row: AutomationRule, now_local: datetime) -> Optional[datetime]:
    """The most recent moment this scheduled rule was due, in local time."""
    try:
        hh, mm = (int(x) for x in (row.send_time or "07:00").split(":"))
    except ValueError:
        hh, mm = 7, 0
    at = lambda d: datetime(d.year, d.month, d.day, hh, mm, tzinfo=now_local.tzinfo)
    today = now_local.date()
    if row.trigger == "daily":
        m = at(today)
        return m if m <= now_local else at(today - timedelta(days=1))
    if row.trigger == "weekly":
        back = (today.weekday() - int(row.weekday or 0)) % 7
        m = at(today - timedelta(days=back))
        return m if m <= now_local else m - timedelta(days=7)
    if row.trigger == "monthly":
        day = max(1, min(28, int(row.month_day or 1)))
        m = at(today.replace(day=day))
        if m > now_local:
            prev = (today.replace(day=1) - timedelta(days=1)).replace(day=day)
            m = at(prev)
        return m
    return None


def due_period(row: AutomationRule, now_local: datetime) -> Optional[str]:
    m = last_moment(row, now_local)
    if m is None or now_local - m > CATCH_UP:
        return None
    key = m.date().isoformat()
    return None if row.last_period == key else key


async def _claim_period(db, key: str, period: str) -> bool:
    result = await db.execute(update(AutomationRule).where(
        AutomationRule.key == key, (AutomationRule.last_period.is_(None)) | (AutomationRule.last_period != period))
        .values(last_period=period).execution_options(synchronize_session=False))
    await db.commit()
    return bool(result.rowcount)


async def _hand_back(db, key: str, period: str, previous: Optional[str]) -> None:
    await db.execute(update(AutomationRule).where(AutomationRule.key == key, AutomationRule.last_period == period)
                     .values(last_period=previous).execution_options(synchronize_session=False))
    await db.commit()


# ── Runs ─────────────────────────────────────────────────────────────────────

async def _start_run(db, key: str, period: Optional[str] = None) -> int:
    run = AutomationRun(rule_key=key, period=period, started_at=now_utc(), status="running")
    db.add(run)
    await db.commit()
    return run.id


async def _finish_run(db, run_id: int, status: str, items: int = 0, recipients: Optional[list[str]] = None,
                      error: Any = None) -> None:
    await db.execute(update(AutomationRun).where(AutomationRun.id == run_id).values(
        status=status, finished_at=now_utc(), items=items,
        recipients=", ".join(recipients or []) or None, error=clean_error(error) if error else None)
        .execution_options(synchronize_session=False))
    await db.commit()


async def _set_status(db, key: str, text: str) -> None:
    await db.execute(update(AutomationRule).where(AutomationRule.key == key).values(last_status=text[:300])
                     .execution_options(synchronize_session=False))
    await db.commit()


async def _already_logged(db, key: str, status: str, now_local: datetime, period: Optional[str] = None) -> bool:
    """One 'capped' / 'gmail_limit' line per rule and day (or period) — not one every tick."""
    s, e = _today_bounds(now_local)
    q = select(AutomationRun.id).where(AutomationRun.rule_key == key, AutomationRun.status == status,
                                       AutomationRun.started_at >= s, AutomationRun.started_at <= e)
    if period:
        q = q.where(AutomationRun.period == period)
    return (await db.execute(q.limit(1))).first() is not None


async def _log_blocked(db, key: str, why: str, now_local: datetime, items: int, period: Optional[str] = None) -> None:
    if await _already_logged(db, key, why, now_local, period):
        return
    run_id = await _start_run(db, key, period)
    message = "Gmail daily limit reached — held for the next morning brief" if why == "gmail_limit" else \
        "Daily e-mail cap reached — held for the next morning brief"
    await _finish_run(db, run_id, why, items=items, error=message)
    await _set_status(db, key, f"{message} ({now_local:%d %b %H:%M})")


async def _mark_gmail_limit(db, now_local: datetime) -> None:
    cfg = await get_settings(db)
    cfg.gmail_limit_day = now_local.date()
    await db.commit()
    logger.warning("automations: Gmail daily limit reached — e-mails paused until tomorrow")


# ── Sending one report ───────────────────────────────────────────────────────

async def summarize(report: Report, lang: str, transport=None) -> Optional[str]:
    """3–4 lines written by the model from the report's figures only. None when it can't — the e-mail still goes."""
    from app.services import assistant_service as a
    if not report.summary_data or not a.is_configured():
        return None
    from app.services.automation_rules import SUMMARY_PROMPT
    import httpx
    import time
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=60.0, transport=transport) as client:
            out = await a._chat(client, [
                {"role": "system", "content": SUMMARY_PROMPT[lang if lang in SUMMARY_PROMPT else "en"]},
                {"role": "user", "content": json.dumps(report.summary_data, ensure_ascii=False, default=str)}],
                [], allow_tools=False, model=settings.ASSISTANT_FAST_MODEL or settings.ASSISTANT_MODEL)
    except Exception:
        logger.warning("automations: summary failed", exc_info=True)
        return None
    usage = out.get("usage") or {}
    report.usage = (int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0),
                    time.perf_counter() - started)
    text = (((out.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()][:4]
    return "\n".join(l if l.startswith("•") else f"• {l.lstrip('-* ')}" for l in lines) or None


async def deliver(db, row: AutomationRule, report: Report, recipients: list[str], *, test: bool = False,
                  transport=None) -> None:
    """Add the model's lines, render and send. Raises on a failed send."""
    from app.services import mail_service
    lang = row.language if row.language in TEXT else "en"
    if report.summary_data and report.summary is None:
        report.summary = await summarize(report, lang, transport=transport)
        usage = report.usage
        if usage:
            record(db, "Automations", "ai_summary",
                   f"{row.key}: summary written | tokens {usage[0]}+{usage[1]} | {usage[2]:.1f}s")
            await db.commit()
    html, text = render(report, lang)
    subject = (tr(lang, "test") if test else "") + report.subject
    await mail_service.send(recipients, subject, html, text)
    await note_email(db, ("Automation test: " if test else "Automation: ") + row.key, recipients)


# ── Scheduled rules ──────────────────────────────────────────────────────────

async def _claim_held(db, run_id: int) -> list[AutomationEvent]:
    """The morning brief takes every alert held back by the cap or Gmail's limit."""
    await db.execute(update(AutomationEvent).where(AutomationEvent.held.is_(True), AutomationEvent.run_id.is_(None))
                     .values(run_id=run_id).execution_options(synchronize_session=False))
    await db.commit()
    return (await db.execute(select(AutomationEvent).where(AutomationEvent.run_id == run_id)
                             .order_by(AutomationEvent.id))).scalars().all()


async def _release(db, run_id: int, held: bool = False) -> None:
    await db.execute(update(AutomationEvent).where(AutomationEvent.run_id == run_id)
                     .values(run_id=None, **({"held": True} if held else {}))
                     .execution_options(synchronize_session=False))
    await db.commit()


async def build_report(db, row: AutomationRule, now_local: datetime, held: Optional[list] = None,
                       events: Optional[list] = None) -> Optional[Report]:
    spec = rules()[row.key]
    ctx = {"now_local": now_local, "settings": rule_settings(row), "lang": row.language or "en",
           "held": held or [], "events": events or []}
    return await (spec.digest(db, ctx) if spec.trigger == "events" else spec.build(db, ctx))


async def run_scheduled(db, row: AutomationRule, period: str, now_local: datetime, transport=None) -> Optional[str]:
    key, previous = row.key, row.last_period
    if not await _claim_period(db, key, period):
        return None
    why = await blocked(db, now_local, key)
    if why:
        await _hand_back(db, key, period, previous)
        await _log_blocked(db, key, why, now_local, 0, period)
        return f"{key}: {why}"
    recipients = await recipients_for(db, row)
    run_id = await _start_run(db, key, period)
    if not recipients:
        await _finish_run(db, run_id, "failed", error="No recipients — add them, or set the brief's recipients.")
        await _set_status(db, key, "Not sent: no recipients")
        return f"{key}: no recipients"
    held = await _claim_held(db, run_id) if key == MORNING else []
    try:
        report = await build_report(db, row, now_local, held=held)
        if report is None:
            await _finish_run(db, run_id, "nothing")
            await _set_status(db, key, f"Nothing to send ({now_local:%d %b %H:%M})")
            return f"{key}: nothing"
        await deliver(db, row, report, recipients, transport=transport)
    except Exception as exc:  # noqa: BLE001  handed back, retried next tick (or tomorrow after Gmail's limit)
        await db.rollback()
        gmail = is_gmail_limit(exc)
        if gmail:
            await _mark_gmail_limit(db, now_local)
        await _release(db, run_id, held=True)
        await _hand_back(db, key, period, previous)
        await _finish_run(db, run_id, "gmail_limit" if gmail else "failed", error=exc)
        await _set_status(db, key, ("Gmail daily limit reached" if gmail else f"Failed: {clean_error(exc)}"))
        logger.warning("automation %s not sent: %s", key, clean_error(exc))
        return f"{key}: failed"
    await _finish_run(db, run_id, "sent", items=report.items + len(held), recipients=recipients)
    await _set_status(db, key, f"Sent to {len(recipients)} on {now_local:%a %d %b %H:%M}")
    return f"{key}: sent"


# ── Event rules: poll, store, digest ─────────────────────────────────────────

async def poll(db, row: AutomationRule) -> int:
    """New events since the watermark. Returns how many were stored. Exactly one worker moves the watermark."""
    spec = rules()[row.key]
    old = row.watermark
    if old is None:
        # First run: start from now — the rule reports what happens after it was turned on, not the backlog.
        start = await spec.latest_id(db)
        await db.execute(update(AutomationRule).where(AutomationRule.key == row.key, AutomationRule.watermark.is_(None))
                         .values(watermark=start).execution_options(synchronize_session=False))
        await db.commit()
        return 0
    found, new_mark = await spec.detect(db, old, rule_settings(row))
    if new_mark == old:
        return 0
    stored = 0
    for ev in found:
        try:
            async with db.begin_nested():
                db.add(AutomationEvent(rule_key=row.key, ref=ev["ref"][:80], urgent=bool(ev.get("urgent")),
                                       payload=json.dumps(ev["payload"], ensure_ascii=False, default=str)))
            stored += 1
        except IntegrityError:
            pass                                            # already stored (another worker, or a retry)
    moved = await db.execute(update(AutomationRule).where(AutomationRule.key == row.key,
                                                          AutomationRule.watermark == old)
                             .values(watermark=new_mark).execution_options(synchronize_session=False))
    if not moved.rowcount:
        await db.rollback()                                 # another worker got there first
        return 0
    await db.commit()
    return stored


async def digest_due(db, row: AutomationRule, now_local: datetime) -> bool:
    pending = AutomationEvent.rule_key == row.key, AutomationEvent.run_id.is_(None), AutomationEvent.held.is_(False)
    if not (await db.execute(select(AutomationEvent.id).where(*pending).limit(1))).first():
        return False
    if (await db.execute(select(AutomationEvent.id).where(*pending, AutomationEvent.urgent.is_(True)).limit(1))).first():
        return True
    last = _aware(row.last_digest_at)
    return last is None or now_utc() - last >= timedelta(minutes=max(1, int(row.digest_minutes or 60)))


async def send_digest(db, row: AutomationRule, now_local: datetime, transport=None) -> Optional[str]:
    key = row.key
    why = await blocked(db, now_local, key)
    pending = (AutomationEvent.rule_key == key, AutomationEvent.run_id.is_(None), AutomationEvent.held.is_(False))
    if why:
        morning = await get_rule(db, MORNING)
        if morning.enabled:                                # held for the next morning brief
            count = (await db.execute(select(func.count(AutomationEvent.id)).where(*pending))).scalar() or 0
            await db.execute(update(AutomationEvent).where(*pending).values(held=True)
                             .execution_options(synchronize_session=False))
            await db.commit()
            await _log_blocked(db, key, why, now_local, count)
        return f"{key}: {why}"
    run_id = await _start_run(db, key)
    claimed = await db.execute(update(AutomationEvent).where(*pending).values(run_id=run_id)
                               .execution_options(synchronize_session=False))
    await db.commit()
    if not claimed.rowcount:                               # another worker took them
        await db.execute(delete(AutomationRun).where(AutomationRun.id == run_id))
        await db.commit()
        return None
    events = (await db.execute(select(AutomationEvent).where(AutomationEvent.run_id == run_id)
                               .order_by(AutomationEvent.id))).scalars().all()
    recipients = await recipients_for(db, row)
    try:
        if not recipients:
            raise RuntimeError("No recipients — add them, or set the brief's recipients.")
        report = await build_report(db, row, now_local, events=events)
        await deliver(db, row, report, recipients, transport=transport)
    except Exception as exc:  # noqa: BLE001  released: the same items go out on a later tick, never twice
        await db.rollback()
        gmail = is_gmail_limit(exc)
        if gmail:
            await _mark_gmail_limit(db, now_local)
        await _release(db, run_id, held=gmail)
        await _finish_run(db, run_id, "gmail_limit" if gmail else "failed", items=len(events), error=exc)
        await _set_status(db, key, "Gmail daily limit reached" if gmail else f"Failed: {clean_error(exc)}")
        return f"{key}: failed"
    await db.execute(update(AutomationRule).where(AutomationRule.key == key).values(last_digest_at=now_utc())
                     .execution_options(synchronize_session=False))
    await _finish_run(db, run_id, "sent", items=len(events), recipients=recipients)
    await _set_status(db, key, f"Sent {len(events)} item(s) to {len(recipients)} on {now_local:%a %d %b %H:%M}")
    return f"{key}: sent {len(events)}"


# ── The tick and the loop ────────────────────────────────────────────────────

async def cleanup(db) -> None:
    cutoff = now_utc() - KEEP
    await db.execute(delete(AutomationRun).where(AutomationRun.started_at < cutoff))
    await db.execute(delete(AutomationEvent).where(AutomationEvent.run_id.is_not(None),
                                                   AutomationEvent.created_at < cutoff))
    await db.commit()


async def tick(db, now_local: Optional[datetime] = None, transport=None) -> list[str]:
    now_local = now_local or datetime.now(app_tz())
    done = []
    for key, spec in rules().items():
        try:
            row = await get_rule(db, key)
            if not row.enabled:
                continue
            if row.trigger == "events":
                await poll(db, row)
                row = await get_rule(db, key)
                if await digest_due(db, row, now_local):
                    done.append(await send_digest(db, row, now_local, transport=transport))
            else:
                period = due_period(row, now_local)
                if period:
                    done.append(await run_scheduled(db, row, period, now_local, transport=transport))
        except Exception as exc:  # noqa: BLE001  one rule never stops the others
            await db.rollback()
            logger.warning("automation %s tick failed: %s", key, clean_error(exc))
    if random.random() < 0.02:
        await cleanup(db)
    return [d for d in done if d]


async def loop() -> None:
    await asyncio.sleep(45 + random.random() * 30)
    while True:
        try:
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                for line in await tick(db):
                    logger.info("automations: %s", line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001  never fatal
            logger.warning("automations tick failed: %s", clean_error(exc))
        await asyncio.sleep(TICK_SECONDS)


def start_loop():
    if not settings.AUTOMATIONS_LOOP:
        return None
    try:
        return asyncio.get_running_loop().create_task(loop())
    except RuntimeError:
        return None


# ── Preview and test (the admin's buttons) ───────────────────────────────────

async def report_now(db, key: str, now_local: datetime) -> Report:
    """The e-mail the rule would send now, built from what is waiting — nothing is claimed, sent or counted."""
    row = await get_rule(db, key)
    spec = rules()[key]
    lang = row.language if row.language in TEXT else "en"
    if spec.trigger == "events":
        events = (await db.execute(select(AutomationEvent).where(AutomationEvent.rule_key == key,
                                                                 AutomationEvent.run_id.is_(None))
                                   .order_by(AutomationEvent.id))).scalars().all()
        return await build_report(db, row, now_local, events=events) if events else spec.empty(lang)
    held = (await db.execute(select(AutomationEvent).where(AutomationEvent.held.is_(True),
                                                           AutomationEvent.run_id.is_(None)))).scalars().all()         if key == MORNING else []
    return await build_report(db, row, now_local, held=held) or spec.empty(lang)


async def preview(db, key: str, now_local: Optional[datetime] = None) -> tuple[str, str]:
    row = await get_rule(db, key)
    report = await report_now(db, key, now_local or datetime.now(app_tz()))
    return report.subject, render(report, row.language if row.language in TEXT else "en")[0]


async def send_test(db, key: str, to: list[str], now_local: Optional[datetime] = None, transport=None) -> dict:
    """Send the rule's e-mail now to ``to``, marked [Test]. Nothing is claimed; it doesn't count toward the cap
    (it does count in today's e-mail total — Gmail's quota sees it)."""
    row = await get_rule(db, key)
    report = await report_now(db, key, now_local or datetime.now(app_tz()))
    run_id = await _start_run(db, key, "test")
    try:
        await deliver(db, row, report, to, test=True, transport=transport)
    except Exception as exc:  # noqa: BLE001
        await _finish_run(db, run_id, "failed", error=exc)
        raise RuntimeError(clean_error(exc))
    await _finish_run(db, run_id, "test", recipients=to)
    return {"ok": True, "sent_to": to}
