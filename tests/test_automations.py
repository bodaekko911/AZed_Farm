"""Automations (phase 1): the engine, the run log, the admin section and the morning brief.

A real async SQLite database; e-mail and the model are mocked. A small test-only event rule (fed from activity-log rows
the test writes) drives the parts of the engine the phase-2 rules will use: the watermark, digests, the daily cap,
Gmail's daily limit and holding alerts for the morning brief.
"""
import asyncio
import json
import shutil
import subprocess
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

import app.models  # noqa: F401  every table
from app.core.config import settings
from app.core.log import ActivityLog
from app.core.time_utils import app_tz, today_local
from app.database import Base
from app.models.automation import AutomationEvent, AutomationRule, AutomationRun, AutomationSettings
from app.models.b2b import B2BClient
from app.models.brief import WeeklyBriefSettings
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm
from app.models.hr import Employee, Payroll
from app.models.product import Product
from app.models.user import User
from app.services import automation_rules as ar
from app.services import automation_service as auto
from app.services import mail_service

TODAY = today_local()
YESTERDAY = TODAY - timedelta(days=1)
MORNING_AT = datetime.combine(TODAY, time(7, 5), tzinfo=app_tz())


class Mailbox:
    def __init__(self):
        self.sent, self.fail_with = [], None

    async def send(self, recipients, subject, html, text, transport=None):
        if self.fail_with:
            error, self.fail_with = self.fail_with, (self.fail_with if getattr(self, "keep_failing", False) else None)
            raise RuntimeError(error)
        self.sent.append({"to": list(recipients), "subject": subject, "html": html, "text": text})


def model(text="• Sales were steady\n• Two clients owe money", status=200):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": "down"})
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}],
                                         "usage": {"prompt_tokens": 900, "completion_tokens": 40}})
    transport = httpx.MockTransport(handler)
    transport.calls = calls
    return transport


# A test-only event rule: activity-log rows of module "Test" are its "events".
async def _test_detect(db, after_id, cfg):
    rows = (await db.execute(select(ActivityLog).where(ActivityLog.id > after_id, ActivityLog.module == "Test")
                             .order_by(ActivityLog.id))).scalars().all()
    latest = (await db.execute(select(func.max(ActivityLog.id)))).scalar() or after_id
    return ([{"ref": f"log:{r.id}", "urgent": "URGENT" in (r.description or ""),
              "payload": {"cells": [r.description]}} for r in rows], max(latest, after_id))


async def _test_latest(db):
    return (await db.execute(select(func.max(ActivityLog.id)))).scalar() or 0


async def _test_digest(db, ctx):
    rows = [json.loads(e.payload)["cells"] for e in ctx["events"]]
    return auto.Report(subject="Azed Farm — Test alerts", intro=f"{len(rows)} new", items=len(rows),
                       sections=[auto.Section("Alerts", ["What"], rows)])


TEST_RULE = ar.RuleSpec(key="test_events", names={"en": "Test alerts", "ar": "تنبيهات تجربة"},
                        descriptions={"en": "test", "ar": "test"}, trigger="events", detect=_test_detect,
                        latest_id=_test_latest, digest=_test_digest, head=lambda lang: ["What"])


@pytest.fixture
def env(monkeypatch):
    box = Mailbox()
    monkeypatch.setattr(mail_service, "send", box.send)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://farm.test")
    monkeypatch.setattr(settings, "ASSISTANT_API_KEY", "k")
    monkeypatch.setattr(settings, "ASSISTANT_MODEL", "m")
    monkeypatch.setattr(settings, "ASSISTANT_FAST_MODEL", None)
    monkeypatch.setattr(settings, "ASSISTANT_BASE_URL", "https://assistant.test/v1")
    from app.services import assistant_service
    monkeypatch.setattr(assistant_service, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(assistant_service, "_mode_from_db", False)
    monkeypatch.setitem(ar.RULES, "test_events", TEST_RULE)
    yield box
    asyncio.set_event_loop(asyncio.new_event_loop())


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def setup():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    db.add_all([
        User(id=1, name="Boss", email="boss@farm.test", password="x", role="admin"),
        WeeklyBriefSettings(id=1, recipients="owner@farm.test\nmanager@farm.test"),
        Farm(id=1, name="North Farm", is_active=1),
        Product(id=1, sku="TOM", name="Tomatoes", unit="kg", price=Decimal("20"), cost=Decimal("8"),
                stock=Decimal("2"), min_stock=Decimal("10"), is_active=True),
        Product(id=2, sku="HON", name="Raw Honey", unit="jar", price=Decimal("150"), cost=Decimal("90"),
                stock=Decimal("80"), min_stock=Decimal("5"), is_active=True),
        B2BClient(id=1, name="Joud Market", outstanding=Decimal("4200"), payment_terms="net30"),
        ExpenseCategory(id=1, name="Fuel", account_code="5300", is_active="1"),
        ExpenseCategory(id=2, name="Salaries", account_code="5100", is_active="1"),
        Employee(id=1, name="Saeed", is_active=True),
    ])
    await db.flush()
    db.add(Payroll(id=1, employee_id=1, period=YESTERDAY.strftime("%Y-%m"), base_salary=Decimal("6000"),
                   net_salary=Decimal("6000"), paid=True))
    await db.flush()
    db.add_all([
        Expense(id=1, ref_number="EXP-00001", category_id=1, farm_id=1, amount=Decimal("7500"), expense_date=YESTERDAY,
                vendor="Wataniya", user_id=1),
        Expense(id=2, ref_number="EXP-00002", category_id=2, amount=Decimal("6000"), expense_date=YESTERDAY,
                payroll_id=1),                                     # payroll's own salary expense: not listed
        Expense(id=3, ref_number="EXP-00003", category_id=1, amount=Decimal("300"), expense_date=YESTERDAY),
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


async def enable(db, key, **values):
    row = await auto.get_rule(db, key)
    row.enabled = True
    for k, v in values.items():
        setattr(row, k, v)
    await db.commit()
    return row


async def runs(db, key=None):
    q = select(AutomationRun).order_by(AutomationRun.id)
    if key:
        q = q.where(AutomationRun.rule_key == key)
    return (await db.execute(q)).scalars().all()


async def log(db, text):
    db.add(ActivityLog(module="Test", action="x", description=text, user_name="t"))
    await db.commit()


# ── The morning brief ────────────────────────────────────────────────────────

def test_the_morning_brief_is_built_from_the_apps_own_figures(env):
    async def go(db, factory):
        await enable(db, "morning_brief")
        assert await auto.tick(db, MORNING_AT, transport=model()) == ["morning_brief: sent"]
        [mail] = env.sent
        assert mail["to"] == ["owner@farm.test", "manager@farm.test"]                 # the brief's recipients
        html, text = mail["html"], mail["text"]
        assert "Morning brief" in mail["subject"] and "Sales yesterday" in html
        assert "Tomatoes" in html and "/inventory/?search=TOM" in html                  # low stock, linked
        assert "Joud Market" in html and "https://farm.test/b2b/#client/1" in html      # who owes us
        assert "EXP-00001" in html and "https://farm.test/expenses/?search=EXP-00001" in html
        assert "EXP-00002" not in html and "EXP-00003" not in html                      # payroll / small
        assert "• Sales were steady" in html                                            # the model's lines
        assert "<script" not in html and "<img" not in html
        [ran] = await runs(db, "morning_brief")
        assert (ran.status, ran.period) == ("sent", TODAY.isoformat())
        logged = (await db.execute(select(ActivityLog.description).where(ActivityLog.action == "ai_summary"))).scalar()
        assert "tokens 900+40" in logged
        counted = await auto.emails_today(db, MORNING_AT)
        assert counted == {"emails": 1, "recipients": 2}
    scenario(go)


def test_the_morning_brief_in_arabic(env):
    async def go(db, factory):
        await enable(db, "morning_brief", language="ar")
        subject, html = await auto.preview(db, "morning_brief", MORNING_AT)
        assert 'dir="rtl"' in html and "ملخص الصباح" in subject and "مبيعات أمس" in html
    scenario(go)


def test_a_failed_model_still_sends_the_brief(env):
    async def go(db, factory):
        await enable(db, "morning_brief")
        await auto.tick(db, MORNING_AT, transport=model(status=500))
        [mail] = env.sent
        assert "Highlights" not in mail["html"] and "Sales yesterday" in mail["html"]
    scenario(go)


def test_preview_sends_nothing_and_claims_nothing(env):
    async def go(db, factory):
        await enable(db, "morning_brief")
        db.add(AutomationEvent(rule_key="test_events", ref="held:1", payload=json.dumps({"cells": ["old alert"]}),
                               held=True))
        await db.commit()
        subject, html = await auto.preview(db, "morning_brief", MORNING_AT)
        assert "old alert" in html and env.sent == [] and await runs(db) == []
        assert (await db.execute(select(AutomationEvent.run_id))).scalar() is None
    scenario(go)


# ── Scheduling: once, retried, across workers ────────────────────────────────

def test_a_scheduled_run_goes_out_once_across_workers(env):
    async def go(db, factory):
        await enable(db, "morning_brief")
        other = factory()                                 # a second gunicorn worker, which read the rule earlier
        stale = await auto.get_rule(other, "morning_brief")
        await auto.tick(db, MORNING_AT, transport=model())
        assert await auto.run_scheduled(other, stale, TODAY.isoformat(), MORNING_AT, transport=model()) is None
        assert await auto.tick(db, MORNING_AT + timedelta(minutes=1), transport=model()) == []
        assert len(env.sent) == 1 and len(await runs(db)) == 1
        await other.close()
    scenario(go)


def test_a_failed_send_is_retried_without_duplicates_and_hides_secrets(env, monkeypatch):
    monkeypatch.setattr(settings, "MAIL_RELAY_SECRET", "relay-secret-123")

    async def go(db, factory):
        await enable(db, "morning_brief")
        env.fail_with = "Gmail relay: bad secret relay-secret-123"
        assert await auto.tick(db, MORNING_AT, transport=model()) == ["morning_brief: failed"]
        assert env.sent == []
        assert await auto.tick(db, MORNING_AT + timedelta(minutes=1), transport=model()) == ["morning_brief: sent"]
        assert len(env.sent) == 1
        failed, sent = await runs(db, "morning_brief")
        assert (failed.status, sent.status) == ("failed", "sent")
        assert "relay-secret-123" not in failed.error and "[hidden]" in failed.error
    scenario(go)


def test_nothing_runs_while_a_rule_is_off_or_outside_its_window(env):
    async def go(db, factory):
        assert await auto.tick(db, MORNING_AT, transport=model()) == []           # off by default
        await enable(db, "morning_brief")
        late = datetime.combine(TODAY, time(6, 0), tzinfo=app_tz()) + timedelta(days=1, hours=14)
        assert auto.due_period(await auto.get_rule(db, "morning_brief"), late) is None   # beyond the catch-up
        assert env.sent == []
    scenario(go)


# ── Events: watermark, digest, cap, Gmail's limit, holding for the morning ───

def test_events_start_from_now_and_are_reported_once(env):
    async def go(db, factory):
        await log(db, "before the rule was on")
        await enable(db, "test_events")
        await auto.tick(db, MORNING_AT)                                    # sets the watermark; no backlog
        assert (await db.execute(select(func.count(AutomationEvent.id)))).scalar() == 0
        await log(db, "first")
        await auto.tick(db, MORNING_AT)
        assert len(env.sent) == 1 and "first" in env.sent[0]["html"]
        await auto.tick(db, MORNING_AT + timedelta(minutes=1))
        row = await auto.get_rule(db, "test_events")
        await auto.poll(db, row)                                           # polling again finds nothing new
        assert len(env.sent) == 1
        assert (await db.execute(select(func.count(AutomationEvent.id)))).scalar() == 1
    scenario(go)


def test_events_are_batched_into_one_digest_per_window_urgent_ones_at_once(env):
    async def go(db, factory):
        await enable(db, "test_events", digest_minutes=60)
        await auto.tick(db, MORNING_AT)
        for n in range(3):
            await log(db, f"event {n}")
        await auto.tick(db, MORNING_AT)
        assert len(env.sent) == 1 and all(f"event {n}" in env.sent[0]["html"] for n in range(3))
        await log(db, "event 4")
        await auto.tick(db, MORNING_AT)
        assert len(env.sent) == 1                                          # waits for the window
        await log(db, "URGENT event 5")
        await auto.tick(db, MORNING_AT)
        assert len(env.sent) == 2 and "event 4" in env.sent[1]["html"] and "URGENT" in env.sent[1]["html"]
        await db.execute(update(AutomationRule).values(last_digest_at=datetime.now(timezone.utc) - timedelta(hours=2)))
        await db.commit()
        await log(db, "event 6")
        await auto.tick(db, MORNING_AT)
        assert len(env.sent) == 3 and "event 6" in env.sent[2]["html"] and "event 4" not in env.sent[2]["html"]
    scenario(go)


def test_the_cap_holds_alerts_for_the_morning_brief(env):
    async def go(db, factory):
        cfg = await auto.get_settings(db)
        cfg.daily_cap = 2                                  # digests may use 1; the morning brief keeps the last
        await db.commit()
        await enable(db, "morning_brief", send_time="08:00")   # not due at 07:05
        await enable(db, "test_events", digest_minutes=1)
        await auto.tick(db, MORNING_AT)
        await log(db, "URGENT a")
        await auto.tick(db, MORNING_AT)
        assert len(env.sent) == 1
        await log(db, "URGENT b")
        await auto.tick(db, MORNING_AT)
        await auto.tick(db, MORNING_AT)
        assert len(env.sent) == 1                          # capped
        capped = [r for r in await runs(db, "test_events") if r.status == "capped"]
        assert len(capped) == 1 and "held for the next morning brief" in capped[0].error
        assert (await db.execute(select(AutomationEvent.held).where(AutomationEvent.ref.like("log:%"))
                                 .order_by(AutomationEvent.id.desc()))).first()[0] is True
        # The next morning brief carries the held alert, once.
        tomorrow = MORNING_AT + timedelta(days=1, hours=1)                # 08:05 tomorrow
        await auto.tick(db, tomorrow, transport=model())
        assert len(env.sent) == 2 and "URGENT b" in env.sent[1]["html"] and "held back" in env.sent[1]["html"]
        await auto.tick(db, tomorrow + timedelta(minutes=5), transport=model())
        assert len(env.sent) == 2
    scenario(go)


def test_gmails_daily_limit_pauses_until_tomorrow_and_holds_alerts(env):
    async def go(db, factory):
        await enable(db, "morning_brief", send_time="08:00")   # not due at 07:05
        await enable(db, "test_events")
        await auto.tick(db, MORNING_AT)
        await log(db, "URGENT a")
        env.fail_with = "Gmail relay: Exception: Service invoked too many times for one day: email."
        await auto.tick(db, MORNING_AT)
        assert env.sent == []
        cfg = await auto.get_settings(db)
        assert cfg.gmail_limit_day == TODAY
        [failed] = [r for r in await runs(db, "test_events")]
        assert failed.status == "gmail_limit"
        await log(db, "URGENT b")
        await auto.tick(db, MORNING_AT + timedelta(minutes=5))               # no retry all day
        assert env.sent == []
        held = (await db.execute(select(func.count(AutomationEvent.id)).where(AutomationEvent.held.is_(True)))).scalar()
        assert held == 2
        out = await auto.blocked(db, MORNING_AT, "test_events")
        assert out == "gmail_limit"
    scenario(go)


# ── Admin only; settings validated ───────────────────────────────────────────

def test_only_admins_reach_the_automation_endpoints():
    from app.core.permissions import require_admin
    from app.routers import assistant
    paths = [r for r in assistant.router.routes if "automations" in getattr(r, "path", "")]
    assert len(paths) == 5
    for route in paths:
        calls = {d.call for d in route.dependant.dependencies}
        assert require_admin in calls, route.path
    with pytest.raises(HTTPException) as e:
        run(require_admin(SimpleNamespace(role="manager")))
    assert e.value.status_code == 403


def test_saving_validates_recipients_trigger_and_thresholds(env):
    async def go(db, factory):
        from app.routers import assistant
        admin = SimpleNamespace(id=1, name="Boss", role="admin", email="boss@farm.test")
        with pytest.raises(HTTPException) as e:
            await assistant.save_automation("morning_brief", assistant.AutomationIn(recipients="not-an-email"),
                                            db=db, admin=admin)
        assert "not-an-email" in e.value.detail
        with pytest.raises(HTTPException):
            await assistant.save_automation("morning_brief", assistant.AutomationIn(trigger="events"), db=db, admin=admin)
        out = await assistant.save_automation("morning_brief", assistant.AutomationIn(
            enabled=True, send_time="06:30", recipients="a@farm.test", settings={"expense_threshold": 2000}),
            db=db, admin=admin)
        rule = next(r for r in out["rules"] if r["key"] == "morning_brief")
        assert rule["enabled"] and rule["send_time"] == "06:30" and rule["settings"]["expense_threshold"] == 2000
        assert out["daily_cap"] == 40 and out["links_ready"]
    scenario(go)


def test_a_test_send_goes_to_the_admin_only_and_is_counted_but_not_capped(env):
    async def go(db, factory):
        from app.routers import assistant
        admin = SimpleNamespace(id=1, name="Boss", role="admin", email="boss@farm.test")
        out = await assistant.test_automation("morning_brief", db=db, admin=admin)
        assert out["ok"] and env.sent[0]["to"] == ["boss@farm.test"] and env.sent[0]["subject"].startswith("[Test]")
        assert await auto.sent_today(db, datetime.now(app_tz())) == 0
        assert (await auto.emails_today(db))["emails"] == 1
    scenario(go)


def test_every_email_counts_in_todays_total_including_the_briefs(env):
    async def go(db, factory):
        from app.services import weekly_brief_service as brief
        await brief.send(db, only_to=["x@farm.test"])
        assert (await auto.emails_today(db))["emails"] == 1
    scenario(go)


# ── URL filters on the pages the e-mails link to ─────────────────────────────

def _page(module, attr):
    from app.routers import expenses, inventory
    user = SimpleNamespace(id=1, name="B", email="b@x", role="admin", permissions=None)
    return {"expenses": expenses.expenses_ui, "inventory": inventory.inventory_ui}[module](current_user=user)


def _run_prefill(page_html, input_id, search):
    """Run the page's own prefill function in node with a stub DOM."""
    start = page_html.index("function prefillSearchFromUrl")
    end = page_html.index("\n}\n", start) + 3
    script = page_html[start:end] + f"""
const box = {{value: "before"}};
globalThis.window = {{location: {{search: {json.dumps(search)}}}}};
globalThis.document = {{getElementById: id => id === {json.dumps(input_id)} ? box : null}};
prefillSearchFromUrl({json.dumps(input_id)});
console.log(box.value);
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.mark.parametrize("page,input_id", [("expenses", "exp-search"), ("inventory", "stock-search")])
def test_a_url_filter_only_prefills_the_pages_search(page, input_id):
    html = _page(page, None)
    boot = html.index("prefillSearchFromUrl(\"" + input_id + "\")")
    first_load = html.index("loadExpenses()" if page == "expenses" else "await loadStock()", boot - 400)
    assert boot < html.index("await", boot)                       # filled before anything loads
    if not shutil.which("node"):
        pytest.skip("node not installed")
    assert _run_prefill(html, input_id, "?search=EXP-00001") == "EXP-00001"
    assert _run_prefill(html, input_id, "") == "before"                       # no filter: unchanged
    assert _run_prefill(html, input_id, "?search=%3Cscript%3E") == "before"   # odd: ignored
    assert _run_prefill(html, input_id, "?search=%E0%A4%A") == "before"       # malformed: ignored
    assert first_load
