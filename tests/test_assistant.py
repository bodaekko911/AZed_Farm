"""Ask — answered from read-only lookups, scoped to the user, with a daily cap.

The assistant endpoint is faked with an httpx MockTransport, so these tests
never call a real model or spend anything.
"""

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.core.config import settings
from app.core.log import ActivityLog
from app.database import Base
from app.models.product import Product
from app.services import assistant_service


class AsyncSessionAdapter:
    def __init__(self, session):
        self.s = session

    async def execute(self, statement, params=None):
        return self.s.execute(statement, params or {})

    async def commit(self):
        self.s.commit()

    def add(self, obj):
        self.s.add(obj)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_API_KEY", "test-key")
    monkeypatch.setattr(settings, "ASSISTANT_MODEL", "test-model")
    monkeypatch.setattr(settings, "ASSISTANT_BASE_URL", "https://assistant.test/v1")
    monkeypatch.setattr(settings, "ASSISTANT_DAILY_LIMIT", 3)
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all([
        Product(id=1, sku="BAS", name="Organic Italian Basil (1g) HOF", unit="gram", price=Decimal("0.8"),
                cost=Decimal("0.233"), stock=Decimal("1500"), min_stock=Decimal("5"), is_active=True),
        Product(id=2, sku="JAR", name="500g Jar", unit="piece", price=Decimal("1"), cost=Decimal("50"),
                stock=Decimal("2"), min_stock=Decimal("10"), is_active=True),
    ])
    session.commit()
    return session, AsyncSessionAdapter(session)


def user(role="admin", permissions=None):
    return SimpleNamespace(id=7, name="Abdallah", role=role, permissions=permissions)


def fake_endpoint(seen):
    """First reply asks for the products lookup; the second answers from it."""
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen.append({"auth": request.headers.get("authorization"), "url": str(request.url), "body": body})
        tool_messages = [m for m in body["messages"] if m["role"] == "tool"]
        if not tool_messages:
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "products", "arguments": json.dumps({"low_stock": True})}}]}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 20},
            })
        low = json.loads(tool_messages[-1]["content"])["products"]
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant",
                                     "content": f"Running low: {', '.join(p['name'] for p in low)}."}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 30},
        })
    return httpx.MockTransport(handler)


def ask(db, u, question, seen, history=None):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            assistant_service.ask(db, u, question, history, transport=fake_endpoint(seen)))
    finally:
        loop.close()


def test_a_question_is_answered_from_a_read_only_lookup_on_real_data():
    session, db = make_db()
    seen = []
    result = ask(db, user(), "What's running low?", seen)

    assert result["answer"] == "Running low: 500g Jar."
    assert result["lookups"] == [{"tool": "products", "args": '{"low_stock": true}'}]
    assert result["usage"] == {"prompt_tokens": 2100, "completion_tokens": 50}
    assert result["questions_left"] == 2
    # Sent to the configured endpoint with the key from settings, OpenAI format.
    assert seen[0]["url"] == "https://assistant.test/v1/chat/completions"
    assert seen[0]["auth"] == "Bearer test-key"
    assert seen[0]["body"]["model"] == "test-model"
    # The question is logged — that is what the daily limit counts.
    logs = session.query(ActivityLog).filter_by(module="Assistant", action="ask").all()
    assert len(logs) == 1 and "running low" in logs[0].description.lower()


def test_tools_are_only_offered_for_reports_the_user_may_open():
    everything = assistant_service.allowed_tools(user("admin"))
    assert "payroll" in everything and "sales_summary" in everything
    cashier = assistant_service.allowed_tools(user("cashier"))
    assert "payroll" not in cashier and "profit_and_loss" not in cashier


def test_a_tool_the_user_may_not_use_is_refused_even_if_the_model_asks_for_it():
    _session, db = make_db()
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(assistant_service.run_tool(db, user("cashier"), "payroll", '{"period":"2026-09"}'))
    finally:
        loop.close()
    assert json.loads(out) == {"error": "'payroll' is not available to this user"}


def test_the_daily_limit_stops_further_questions():
    session, db = make_db()
    for _ in range(3):
        session.add(ActivityLog(user_id=7, user_name="Abdallah", user_role="admin", module="Assistant",
                                action="ask", description="q", created_at=datetime.now(timezone.utc)))
    session.commit()
    with pytest.raises(HTTPException) as exc:
        ask(db, user(), "One more?", [])
    assert exc.value.status_code == 429


def test_not_set_up_says_so(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_API_KEY", None)
    _session, db = make_db()
    with pytest.raises(HTTPException) as exc:
        ask(db, user(), "Hello", [])
    assert exc.value.status_code == 503


def test_follow_ups_send_only_the_last_few_turns():
    _session, db = make_db()
    seen = []
    history = [{"role": "user", "content": f"q{i}"} if i % 2 == 0 else {"role": "assistant", "content": f"a{i}"}
               for i in range(20)]
    ask(db, user(), "And now?", seen, history=history)
    roles = [m["role"] for m in seen[0]["body"]["messages"]]
    assert roles[0] == "system" and roles.count("user") + roles.count("assistant") == 11   # 10 earlier + this one


@pytest.mark.parametrize("tool", sorted(assistant_service.TOOLS))
def test_every_lookup_runs_against_the_real_schema(tool):
    _session, db = make_db()
    args = {"date_from": "2026-09-01", "date_to": "2026-09-30"} if tool in assistant_service.PERIOD_TOOLS else {}
    if tool == "payroll":
        args = {"period": "2026-09"}
    loop = asyncio.new_event_loop()
    try:
        out = json.loads(loop.run_until_complete(assistant_service.run_tool(db, user(), tool, json.dumps(args))))
    finally:
        loop.close()
    assert "error" not in out, out


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def log_questions(session, n, user_id=7):
    for _ in range(n):
        session.add(ActivityLog(user_id=user_id, user_name="Abdallah", user_role="admin", module="Assistant",
                                action="ask", description="q | lookups: none | tokens 100+20",
                                created_at=datetime.now(timezone.utc)))
    session.commit()


def test_an_admin_reset_lets_the_user_ask_again_today():
    session, db = make_db()
    log_questions(session, 3)
    admin = SimpleNamespace(id=1, name="Boss", role="admin", permissions=None)
    run(assistant_service.reset_limit(db, admin, user()))

    assert run(assistant_service.limit_state(db, user())) == (0, 3)
    result = ask(db, user(), "What's running low?", [])
    assert result["questions_left"] == 2
    usage = run(assistant_service.usage_today(db))[7]
    assert usage["used"] == 1 and usage["asked_today"] == 4 and usage["tokens"] == 3 * 120 + 2150


def test_reset_everyone_and_a_reset_for_someone_else():
    session, db = make_db()
    log_questions(session, 3)
    log_questions(session, 2, user_id=8)
    admin = SimpleNamespace(id=1, name="Boss", role="admin", permissions=None)
    run(assistant_service.reset_limit(db, admin, SimpleNamespace(id=8, name="Other")))
    assert run(assistant_service.limit_state(db, user())) == (3, 3)
    run(assistant_service.reset_limit(db, admin, None))
    assert run(assistant_service.limit_state(db, user())) == (0, 3)


def test_a_user_can_be_given_their_own_limit_and_back_to_default():
    session, db = make_db()
    log_questions(session, 3)
    admin = SimpleNamespace(id=1, name="Boss", role="admin", permissions=None)
    run(assistant_service.set_limit(db, admin, user(), 5))
    assert run(assistant_service.limit_state(db, user())) == (3, 5)
    assert ask(db, user(), "What's running low?", [])["questions_left"] == 1
    run(assistant_service.set_limit(db, admin, user(), 0))      # no limit
    assert run(assistant_service.limit_state(db, user()))[1] == 0
    run(assistant_service.set_limit(db, admin, user(), None))   # default again
    assert run(assistant_service.limit_state(db, user())) == (4, 3)
    with pytest.raises(HTTPException) as exc:
        ask(db, user(), "One more?", [])
    assert exc.value.status_code == 429


def test_the_question_carries_the_usual_periods():
    from datetime import date
    hints = assistant_service.period_hints(date(2026, 3, 31))
    assert "Today is Tuesday 2026-03-31" in hints
    assert "Last month: 2026-02-01 to 2026-02-28 (same days: 2026-02-01 to 2026-02-28)" in hints
    assert "This quarter: 2026-01-01 to 2026-03-31" in hints


def test_top_pos_customers_are_ranked_by_spend():
    from app.models.customer import Customer
    from app.models.invoice import Invoice
    session, db = make_db()
    now = datetime.now(timezone.utc)
    session.add_all([Customer(id=1, name="Mona"), Customer(id=2, name="Karim"), Customer(id=3, name="Mona")])
    session.add_all([
        Invoice(invoice_number="P1", customer_id=1, status="paid", total=Decimal("100"), created_at=now),
        Invoice(invoice_number="P2", customer_id=2, status="paid", total=Decimal("500"), created_at=now),
        Invoice(invoice_number="P3", customer_id=2, status="unpaid", total=Decimal("50"), created_at=now),
        Invoice(invoice_number="P4", customer_id=3, status="paid", total=Decimal("70"), created_at=now),
        Invoice(invoice_number="P5", customer_id=1, status="void", total=Decimal("999"), created_at=now),
    ])
    session.commit()
    today = assistant_service.today_local().isoformat()
    out = json.loads(run(assistant_service.run_tool(db, user(), "pos_customers",
                                                    json.dumps({"date_from": today, "date_to": today}))))
    assert out["invoices"] == 4 and out["total"] == 720.0
    # Two different customers who share a name stay separate.
    assert out["top_customers"] == [
        {"customer": "Karim", "invoices": 2, "total": 550.0, "unpaid": 50.0},
        {"customer": "Mona", "invoices": 1, "total": 100.0, "unpaid": 0.0},
        {"customer": "Mona", "invoices": 1, "total": 70.0, "unpaid": 0.0},
    ]


def test_a_lookup_that_fails_in_the_database_does_not_break_the_question(monkeypatch):
    """With a real async session the lookup runs in a savepoint; a failing one is reported to the model and the
    question still gets answered and logged."""
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def broken(db, args):
        await db.execute(text("SELECT no_such_column FROM products"))

    monkeypatch.setitem(assistant_service.TOOLS, "products",
                        (*assistant_service.TOOLS["products"][:3], broken))

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            seen = []
            result = await assistant_service.ask(db, user(), "What's running low?", None,
                                                 transport=fake_endpoint_reporting_errors(seen))
            logged = (await db.execute(select(ActivityLog).where(ActivityLog.action == "ask"))).scalars().all()
        await engine.dispose()
        return result, logged, seen

    result, logged, seen = run(scenario())
    assert result["answer"] == "The lookup failed."
    assert len(logged) == 1
    assert json.loads([m for m in seen[-1]["body"]["messages"] if m["role"] == "tool"][0]["content"]) == \
        {"error": "lookup failed"}


def fake_endpoint_reporting_errors(seen):
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen.append({"body": body})
        if not [m for m in body["messages"] if m["role"] == "tool"]:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "products", "arguments": "{}"}}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "The lookup failed."}}]})
    return httpx.MockTransport(handler)


def test_no_period_named_means_all_time_from_the_first_record():
    from datetime import date
    from app.models.expense import Expense, ExpenseCategory
    session, db = make_db()
    session.add(ExpenseCategory(id=1, name="Seeds", account_code="5001"))
    session.add(Expense(category_id=1, expense_date=date(2024, 3, 5), amount=Decimal("10")))
    session.commit()
    today = assistant_service.today_local()
    assert run(assistant_service._dates(db, {})) == (date(2024, 3, 5), today)
    assert run(assistant_service._dates(db, {"date_to": "2025-01-31"})) == (date(2024, 3, 5), date(2025, 1, 31))
    # A period that is named is used as given — longer than a year is fine.
    assert run(assistant_service._dates(db, {"date_from": "2023-01-01", "date_to": "2025-12-31"})) == \
        (date(2023, 1, 1), date(2025, 12, 31))
    out = json.loads(run(assistant_service.run_tool(db, user(), "expenses", "{}")))
    assert out["period"] == f"2024-03-05 to {today}" and out["total"] == 10.0


def test_payroll_shows_attendance_now_next_to_the_payroll_snapshot():
    """Taha: payroll was run on the 3rd (3 days worked) and he was then left on the 'absent' auto mode."""
    from datetime import date
    from app.models.hr import Attendance, Employee, Payroll
    session, db = make_db()
    session.add_all([Employee(id=1, name="Taha Mahmoud", base_salary=Decimal("6000"), attendance_auto_status="absent"),
                     Employee(id=2, name="Other Person", base_salary=Decimal("5000"))])
    session.add(Payroll(employee_id=1, period="2026-09", base_salary=Decimal("600"), days_worked=3, working_days=30,
                        net_salary=Decimal("600"), paid=False, created_at=datetime(2026, 9, 3, 12, tzinfo=timezone.utc)))
    for day in range(1, 31):
        session.add(Attendance(employee_id=1, date=date(2026, 9, day), status="present" if day <= 3 else "absent"))
    session.commit()

    out = json.loads(run(assistant_service.run_tool(db, user(), "payroll",
                                                    json.dumps({"period": "2026-09", "employee": "taha"}))))
    [taha] = out["employees"]
    assert taha["employee"] == "Taha Mahmoud" and taha["days_worked"] == 3
    assert taha["payroll_run_on"] == "2026-09-03"
    now = taha["attendance_now"]
    assert (now["present"], now["day_off"], now["not_logged_yet"], now["auto_mode"]) == (3, 27, 0, "absent")
    assert now["day_off_dates"][0] == "2026-09-04"

    # Someone with no payroll row this month is still found by name.
    other = json.loads(run(assistant_service.run_tool(db, user(), "payroll",
                                                      json.dumps({"period": "2026-09", "employee": "other"}))))
    assert other["employees"] == [{"employee": "Other Person", "payroll": "not run for this month",
                                   "attendance_now": {"present": 0, "day_off": 0, "not_logged_yet": 30,
                                                      "auto_mode": "present", "day_off_dates": []}}]



def test_each_question_is_timed_and_the_admin_sees_averages():
    session, db = make_db()
    result = ask(db, user(), "What's running low?", [])
    t = result["timing"]
    assert t["seconds"] >= t["model_seconds"] >= 0 and t["lookup_seconds"] >= 0
    log = session.query(ActivityLog).filter_by(module="Assistant", action="ask").one()
    assert assistant_service.TIME_RE.search(log.description)
    session.add(ActivityLog(user_id=7, user_name="Abdallah", user_role="admin", module="Assistant", action="ask",
                            description="q | tokens 1+1 | time 9.0s model 7.0s lookups 2.0s",
                            created_at=datetime.now(timezone.utc)))
    session.commit()
    usage = run(assistant_service.usage_today(db))[7]
    assert usage["timed"] == 2 and usage["last_seconds"] == 9.0


def test_the_first_day_of_data_is_cached_per_database():
    from datetime import date
    from app.models.expense import Expense, ExpenseCategory
    session, db = make_db()
    session.add(ExpenseCategory(id=1, name="Seeds", account_code="5001"))
    session.add(Expense(category_id=1, expense_date=date(2024, 3, 5), amount=Decimal("10")))
    session.commit()
    assert run(assistant_service.first_day(db)) == date(2024, 3, 5)
    session.add(Expense(category_id=1, expense_date=date(2023, 1, 1), amount=Decimal("10")))
    session.commit()
    assert run(assistant_service.first_day(db)) == date(2024, 3, 5)          # cached
    assistant_service._FIRST_DAY_CACHE.clear()
    assert run(assistant_service.first_day(db)) == date(2023, 1, 1)


@pytest.mark.parametrize("question, tier", [
    ("How much did we sell this month?", "fast"),
    ("Which B2B clients owe us the most?", "fast"),
    ("ما المنتجات التي قاربت على النفاد؟", "fast"),
    ("add 500 EGP diesel expense today", "fast"),
    ("Compare sales this month vs last month", "main"),
    ("Why did profit drop in September?", "main"),
    ("ليه المصروفات زادت الشهر ده؟", "main"),
    ("قارن مبيعات سبتمبر بأغسطس", "main"),
    ("What is the trend of basil sales?", "main"),
], ids=lambda v: v if v in ("fast", "main") else None)
def test_simple_questions_go_to_the_fast_model_and_reasoning_to_the_main_one(monkeypatch, question, tier):
    monkeypatch.setattr(settings, "ASSISTANT_FAST_MODEL", "fast-model")
    model, picked = assistant_service.pick_model(question)
    assert picked == tier and model == ("fast-model" if tier == "fast" else "test-model")


def test_without_a_fast_model_everything_uses_the_main_one():
    assert assistant_service.pick_model("How much did we sell?") == ("test-model", "main")


def test_the_main_model_takes_over_when_the_fast_one_fails(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_FAST_MODEL", "fast-model")
    models = []

    def handler(request):
        body = json.loads(request.content)
        models.append(body["model"])
        if body["model"] == "fast-model":
            return httpx.Response(500, json={"error": "overloaded"})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Answer."}}]})

    session, db = make_db()
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(assistant_service.ask(db, user(), "How much did we sell?", None,
                                                               transport=httpx.MockTransport(handler)))
    finally:
        loop.close()
    assert models == ["fast-model", "test-model"] and result["answer"] == "Answer." and result["model"] == "fast→main"
    log = session.query(ActivityLog).filter_by(module="Assistant", action="ask").one()
    assert "model_used fast→main" in log.description


def test_an_empty_fast_answer_is_written_by_the_main_model_from_the_same_lookups(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_FAST_MODEL", "fast-model")
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((body["model"], len([m for m in body["messages"] if m["role"] == "tool"])))
        if body["model"] == "fast-model" and not any(m["role"] == "tool" for m in body["messages"]):
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "products", "arguments": "{}"}}]}}]})
        if body["model"] == "fast-model":
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": ""}}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "From the lookup."}}]})

    _session, db = make_db()
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(assistant_service.ask(db, user(), "What's in stock?", None,
                                                               transport=httpx.MockTransport(handler)))
    finally:
        loop.close()
    # The products lookup ran once; the main model got its result and wrote the answer.
    assert calls == [("fast-model", 0), ("fast-model", 1), ("test-model", 1)]
    assert result["answer"] == "From the lookup." and result["lookups"] == [{"tool": "products", "args": "{}"}]
