"""Weekly brief: which week it covers, sending once a week, the figures and the e-mail. No real mail or model."""

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.core.config import settings
from app.core.time_utils import app_tz
from app.database import Base
import app.models  # noqa: F401  every table, as production has
import app.models.drying  # noqa: F401
from app.models.b2b import B2BClient
from app.models.brief import WeeklyBriefSettings
from app.models.expense import Expense, ExpenseCategory
from app.models.farm import Farm, FarmDelivery, FarmDeliveryItem
from app.models.product import Product
from app.services import mail_service
from app.services import weekly_brief_service as brief


class AsyncSessionAdapter:
    """A plain SQLite session behind the async API the services use."""

    def __init__(self, session):
        self.s = session

    async def execute(self, statement, params=None):
        return self.s.execute(statement, params or {})

    async def commit(self):
        self.s.commit()

    async def rollback(self):
        self.s.rollback()

    async def flush(self):
        self.s.flush()

    def add(self, obj):
        self.s.add(obj)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_API_KEY", None)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(settings, "SMTP_FROM", "brief@farm.test")
    monkeypatch.setattr(settings, "MAIL_RELAY_URL", None)
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mail_service, "_send", lambda to, subject, html, text: sent.append(
        {"to": to, "subject": subject, "html": html, "text": text}))
    return sent


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    return session, AsyncSessionAdapter(session)


def settings_row(**over):
    base = dict(id=1, enabled=True, send_weekday=5, send_time="09:00", recipients="owner@farm.test",
                include_ai_summary=False, last_sent_week=None)
    base.update(over)
    return WeeklyBriefSettings(**base)


def local(*args):
    return datetime(*args, tzinfo=app_tz())


# --- which week, and when -----------------------------------------------------

def test_saturday_morning_covers_saturday_to_friday():
    cfg = settings_row()
    # Saturday 10 Oct 2026, 09:30 — past the 09:00 send time.
    assert brief.due_week(cfg, local(2026, 10, 10, 9, 30)) == date(2026, 10, 9)
    assert brief.week_for(date(2026, 10, 10)) == (date(2026, 10, 3), date(2026, 10, 9))


def test_not_before_the_send_time_and_not_twice():
    cfg = settings_row()
    assert brief.due_week(cfg, local(2026, 10, 10, 8, 59)) is None          # Saturday, too early
    cfg.last_sent_week = "2026-10-09"
    assert brief.due_week(cfg, local(2026, 10, 10, 9, 30)) is None          # already sent


def test_a_restart_after_the_send_time_still_sends_but_not_days_later():
    cfg = settings_row()
    assert brief.due_week(cfg, local(2026, 10, 11, 20, 0)) == date(2026, 10, 9)   # Sunday evening: catch up
    assert brief.due_week(cfg, local(2026, 10, 13, 9, 30)) is None                 # Tuesday: too late, wait


def test_off_or_misconfigured_never_sends():
    assert brief.due_week(settings_row(enabled=False), local(2026, 10, 10, 9, 30)) is None
    assert brief.due_week(settings_row(send_time="9am"), local(2026, 10, 10, 9, 30)) is None


def test_recipients_are_validated():
    good, bad = brief.parse_recipients("a@x.test, b@y.test\nnot-an-email ; a@x.test")
    assert good == ["a@x.test", "b@y.test"] and bad == ["not-an-email"]


# --- sending once a week --------------------------------------------------------

def test_it_sends_once_for_the_week_however_many_workers_tick(outbox):
    session, db = make_db()
    session.add(settings_row())
    session.commit()
    now = local(2026, 10, 10, 9, 30)
    assert run(brief.tick(db, now)) == "weekly sent to 1"
    assert run(brief.tick(db, now)) is None            # a second worker, same week
    assert len(outbox) == 1 and outbox[0]["to"] == ["owner@farm.test"]
    cfg = session.execute(select(WeeklyBriefSettings)).scalar_one()
    session.refresh(cfg)
    assert cfg.last_sent_week == "2026-10-09" and cfg.last_status.startswith("Weekly sent to 1")


def test_a_failed_send_hands_the_week_back_for_a_retry(monkeypatch):
    session, db = make_db()
    session.add(settings_row(last_sent_week="2026-10-02"))
    session.commit()

    def boom(*_a):
        raise OSError("connection refused")
    monkeypatch.setattr(mail_service, "_send", boom)
    out = run(brief.tick(db, local(2026, 10, 10, 9, 30)))
    assert out.startswith("weekly failed")
    cfg = session.execute(select(WeeklyBriefSettings)).scalar_one()
    session.refresh(cfg)
    assert cfg.last_sent_week == "2026-10-02"           # not marked sent
    assert "connection refused" in cfg.last_status


def test_a_test_send_is_marked_and_does_not_count_as_the_week(outbox):
    session, db = make_db()
    session.add(settings_row())
    session.commit()
    run(brief.send(db, only_to=["me@farm.test"]))
    assert outbox[0]["subject"].startswith("[Test] ") and outbox[0]["to"] == ["me@farm.test"]
    cfg = session.execute(select(WeeklyBriefSettings)).scalar_one()
    assert cfg.last_sent_week is None


# --- the figures --------------------------------------------------------------------

def seed(session, week_end: date):
    session.add_all([
        ExpenseCategory(id=1, name="Seeds", account_code="5001"),
        ExpenseCategory(id=2, name="Fuel", account_code="5002"),
        Farm(id=1, name="North Farm"),
        Product(id=1, sku="TOM", name="Tomatoes", unit="kg", price=Decimal("20"), cost=Decimal("8"),
                stock=Decimal("2"), min_stock=Decimal("10"), is_active=True),
    ])
    session.flush()
    in_week, before = week_end - timedelta(days=2), week_end - timedelta(days=9)
    session.add_all([
        Expense(category_id=1, expense_date=in_week, amount=Decimal("1500"), description="Tomato seed"),
        Expense(category_id=2, expense_date=in_week, amount=Decimal("500"), description="Diesel"),
        Expense(category_id=1, expense_date=before, amount=Decimal("999"), description="Last week"),
        FarmDelivery(id=1, delivery_number="FD-1", farm_id=1, delivery_date=in_week),
        FarmDelivery(id=2, delivery_number="FD-2", farm_id=1, delivery_date=before),
    ])
    session.flush()
    session.add_all([
        FarmDeliveryItem(delivery_id=1, product_id=1, qty=Decimal("120"), unit="kg"),
        FarmDeliveryItem(delivery_id=2, product_id=1, qty=Decimal("100"), unit="kg"),
    ])
    # 25 clients owing: more than the 20 the balances lookup lists.
    session.add_all([B2BClient(name=f"Client {i:02d}", outstanding=Decimal("100")) for i in range(25)])
    session.add(B2BClient(name="Paid up", outstanding=Decimal("0")))
    session.commit()


def test_the_week_figures_come_from_that_week_only():
    session, db = make_db()
    week_end = date(2026, 10, 9)
    seed(session, week_end)
    data = run(brief.build(db, week_end))
    assert (data["period"], data["start"], data["end"]) == ("week", "2026-10-03", "2026-10-09")
    assert data["expenses"]["total"] == 2000 and data["expenses"]["count"] == 2
    assert data["expenses"]["by_category"][0] == {"category": "Seeds", "amount": 1500.0}
    assert data["farm"]["same_unit_total"] == {"qty": 120.0, "unit": "kg"}
    assert data["farm"]["same_unit_total_prior"] == {"qty": 100.0, "unit": "kg"}


def test_everyone_owing_is_counted_not_just_the_listed_ones():
    session, db = make_db()
    seed(session, date(2026, 10, 9))
    data = run(brief.build(db, date(2026, 10, 9)))
    assert data["money"]["b2b_clients_owing"] == 25
    assert data["money"]["b2b_owed"] == 2500
    assert len(data["money"]["top_owing"]) == 5


def test_mixed_units_are_not_added_together():
    assert brief._unit_total([{"qty": 5, "unit": "kg"}, {"qty": 3, "unit": "crate"}]) is None
    assert brief._unit_total([{"qty": 5, "unit": "kg"}, {"qty": 3, "unit": "KG"}]) == {"qty": 8, "unit": "kg"}


# --- the e-mail ---------------------------------------------------------------------

def sample(**over):
    data = {
        "period": "week", "start": "2026-10-03", "end": "2026-10-09", "prior_start": "2026-09-26",
        "prior_end": "2026-10-02",
        "sales": {"net": 52000, "net_change_pct": 12.4, "gross": 53000, "refunds": 1000, "pos": 21000,
                  "pos_count": 140, "b2b": 32000, "b2b_count": 6, "cash_collected": 50000,
                  "top_products": [{"name": "Basil <b>HOF</b>", "qty": 300, "revenue": 9000}]},
        "profit": {"revenue": 52000, "expenses": 60000, "net": -8000, "net_prior": 4000, "gross_margin_pct": 41.5,
                   "losing_products": 2, "least_profitable": [{"name": "Jar", "profit": -1200}]},
        "expenses": {"total": 60000, "count": 14, "by_category": [{"category": "Salaries", "amount": 40000}],
                     "largest": []},
        "farm": {"delivered_lines": 2, "deliveries": [{"farm": "North", "product": "Tomatoes", "qty": 120, "unit": "kg"}],
                 "same_unit_total": {"qty": 120, "unit": "kg"}, "same_unit_total_prior": {"qty": 100, "unit": "kg"},
                 "any_prior": True, "spoilage_records": 0, "spoilage_cost": 0, "spoilage_kg": 0, "spoilage_pct": None,
                 "spoilage_cost_complete": True},
        "money": {"b2b_owed": 2500, "b2b_clients_owing": 25, "top_owing": [{"client": "Joud &amp;amp; Bahaa",
                                                                          "outstanding": 900}]},
        "stock": {"value": 80000, "low_count": 1, "dead_count": 0, "low": [{"name": "Tomatoes", "stock": 2, "unit": "kg"}]},
        "attention": ["3 B2B invoices are over 30 days old"],
    }
    data.update(over)
    return data


def test_the_email_reads_well_and_escapes_names():
    subject, html, text = brief.render(sample(), "• Sales up 12%\n• <i>Loss</i> on the week")
    assert subject == "Azed Farm weekly brief — Sat 03 Oct – Fri 09 Oct: 52,000 EGP sales, 8,000 EGP loss"
    assert "Azed Farm — weekly brief</h2>" in html and text.startswith("Azed Farm — weekly brief")
    assert "Basil &lt;b&gt;HOF&lt;/b&gt;" in html and "<b>HOF</b>" not in html
    assert "&lt;i&gt;Loss&lt;/i&gt;" in html
    assert "Joud &amp; Bahaa" in html and "&amp;amp;" not in html
    assert "Net sales: 52,000 EGP (+12% vs the week before)" in text
    assert "Net loss: 8,000 EGP (week before: profit 4,000)" in text
    assert "Delivered: 120 kg (+20% vs the week before)" in text
    assert "B2B clients: 25 clients · 2,500 EGP" in text
    assert "NEEDS ATTENTION" in text and "over 30 days old" in text


def test_an_empty_week_still_renders():
    empty = sample(sales={**sample()["sales"], "net": 0, "net_change_pct": None, "top_products": []},
                   attention=[])
    _s, _h, text = brief.render(empty, None)
    assert "Net sales: 0 EGP" in text and "NEEDS ATTENTION" not in text


def test_shop_sales_in_the_week_are_counted_and_last_weeks_are_compared():
    from app.models.customer import Customer
    from app.models.invoice import Invoice, InvoiceItem
    from datetime import timezone
    session, db = make_db()
    week_end = date(2026, 10, 9)
    seed(session, week_end)
    session.add(Customer(id=1, name="Walk-in"))
    session.flush()
    # Noon UTC, well inside a Cairo day: Thursday this week, and the Thursday before.
    this_week = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    last_week = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    session.add_all([
        Invoice(id=1, invoice_number="INV-1", customer_id=1, status="paid", total=Decimal("600"),
                subtotal=Decimal("600"), created_at=this_week),
        Invoice(id=2, invoice_number="INV-2", customer_id=1, status="paid", total=Decimal("400"),
                subtotal=Decimal("400"), created_at=last_week),
    ])
    session.flush()
    session.add_all([
        InvoiceItem(invoice_id=1, product_id=1, name="Tomatoes", qty=Decimal("30"), unit_price=Decimal("20"),
                    total=Decimal("600"), unit_cost=Decimal("8")),
        InvoiceItem(invoice_id=2, product_id=1, name="Tomatoes", qty=Decimal("20"), unit_price=Decimal("20"),
                    total=Decimal("400"), unit_cost=Decimal("8")),
    ])
    session.commit()
    data = run(brief.build(db, week_end))
    assert data["sales"]["net"] == 600 and data["sales"]["pos_count"] == 1
    assert data["sales"]["net_change_pct"] == 50.0
    assert data["sales"]["top_products"][0]["name"] == "Tomatoes"
    assert data["profit"]["gross_margin_pct"] == 60.0          # (600 − 30×8) / 600



# --- monthly ------------------------------------------------------------------------

def test_the_monthly_brief_covers_last_calendar_month():
    cfg = settings_row(enabled=False, monthly_enabled=True, monthly_day=1)
    assert brief.due_month(cfg, local(2026, 10, 1, 9, 30)) == date(2026, 9, 30)
    assert brief.due_month(cfg, local(2026, 10, 1, 8, 0)) is None             # before the time
    assert brief.due_month(cfg, local(2026, 10, 2, 20, 0)) == date(2026, 9, 30)  # a day late: catch up
    assert brief.due_month(cfg, local(2026, 10, 3, 10, 0)) is None            # over two days: too late
    cfg.last_sent_month = "2026-09"
    assert brief.due_month(cfg, local(2026, 10, 1, 9, 30)) is None            # already sent
    assert brief.period_bounds("month", date(2026, 9, 30)) == (
        date(2026, 9, 1), date(2026, 9, 30), date(2026, 8, 1), date(2026, 8, 31))
    assert brief.period_bounds("month", date(2026, 3, 31))[2:] == (date(2026, 2, 1), date(2026, 2, 28))


def test_a_monthly_day_before_now_in_the_month_still_finds_last_month():
    cfg = settings_row(enabled=False, monthly_enabled=True, monthly_day=15)
    # 15 Oct, after the time: the month covered is September.
    assert brief.due_month(cfg, local(2026, 10, 15, 10, 0)) == date(2026, 9, 30)


def test_weekly_and_monthly_each_send_once_when_both_fall_due(outbox):
    session, db = make_db()
    # Thursday 1 Oct 2026 09:30: weekly on Thursdays, monthly on the 1st.
    session.add(settings_row(send_weekday=3, monthly_enabled=True, monthly_day=1))
    session.commit()
    now = local(2026, 10, 1, 9, 30)
    assert run(brief.tick(db, now)) == "weekly sent to 1; monthly sent to 1"
    assert run(brief.tick(db, now)) is None
    subjects = sorted(m["subject"] for m in outbox)
    assert subjects[0].startswith("Azed Farm monthly brief — September 2026:")
    assert subjects[1].startswith("Azed Farm weekly brief — Thu 24 Sep – Wed 30 Sep:")
    cfg = session.execute(select(WeeklyBriefSettings)).scalar_one()
    session.refresh(cfg)
    assert (cfg.last_sent_week, cfg.last_sent_month) == ("2026-09-30", "2026-09")


def test_the_monthly_figures_come_from_the_whole_month():
    session, db = make_db()
    seed(session, date(2026, 10, 9))                 # expenses on 7 Oct (this "week") and 30 Sep
    data = run(brief.build_month(db, date(2026, 9, 30)))
    assert (data["period"], data["start"], data["end"]) == ("month", "2026-09-01", "2026-09-30")
    assert data["expenses"]["total"] == 999               # only September's
    assert len(data["sales"]["daily"]) == 30


def test_the_monthly_email_says_month_not_week():
    data = sample(period="month", start="2026-09-01", end="2026-09-30", prior_start="2026-08-01",
                  prior_end="2026-08-31")
    subject, html, text = brief.render(data, None)
    assert subject.startswith("Azed Farm monthly brief — September 2026:")
    assert "Azed Farm — monthly brief</h2>" in html and "compared with August 2026" in html
    assert "vs the month before" in text and "week before" not in text


# --- charts ----------------------------------------------------------------------------

def test_the_sales_chart_has_a_bar_per_day_and_skips_an_empty_period():
    daily = [{"date": f"2026-10-0{d}", "net": v} for d, v in zip(range(3, 10), (100, 0, 250, 50, 0, 400, 200))]
    html = brief.chart_daily(daily, "week")
    assert html.count("<td valign=\"bottom\"") == 7
    assert "height:110px" in html                          # the best day is full height
    assert ">Sat<" in html and ">400<" in html
    assert brief.chart_daily([{"date": "2026-10-03", "net": 0}], "week") == ""


def test_the_expense_chart_scales_to_the_largest_and_escapes_names():
    html = brief.chart_bars([("Salaries", 40000), ("Fuel <b>", 10000), ("Nothing", 0)], "Expenses by category")
    assert "width:100%;height:12px" in html and "width:25%;height:12px" in html
    assert "Fuel &lt;b&gt;" in html and "Nothing" not in html


def test_the_charts_are_in_the_email():
    daily = [{"date": f"2026-10-0{d}", "net": 100 * d} for d in range(3, 10)]
    data = sample()
    data["sales"]["daily"] = daily
    _s, html, _t = brief.render(data, None)
    assert "Net sales per day" in html and "Expenses by category (EGP)" in html
