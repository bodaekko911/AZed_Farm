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
