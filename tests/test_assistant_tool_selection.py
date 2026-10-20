"""Ask offers only the tools a question needs.

Every round re-sends the tool list, and all of it is most of a round's prompt. The question is matched by
keyword to the tools it needs; a question that matches nothing still gets them all, and the model can ask for
the rest (more_tools) when what it was offered doesn't fit. The endpoint is faked: nothing is spent.
"""

import asyncio
import json

import httpx

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.services import assistant_service
from app.services.assistant_service import MORE_TOOLS, select_tools
from tests.test_assistant import configured, make_db, user  # noqa: F401  (the fixture configures the endpoint)


def every_tool():
    return assistant_service.allowed_tools(user()) + assistant_service.allowed_actions(user())


def test_a_question_gets_its_topic_and_nothing_else():
    names = every_tool()
    assert select_tools("كام مبيعات النهارده؟", None, names)[:2] == ["sales_summary", "product_profitability"]
    assert "payroll" not in select_tools("كام مبيعات النهارده؟", None, names)
    assert select_tools("What's running low?", None, names) == ["products", "stock", "stock_by_location"]
    assert select_tools("هالك الطماطم الشهر ده", None, names) == ["spoilage"]
    assert set(select_tools("مين اشتغل كام يوم الشهر ده؟", None, names)) == {"payroll", "employee_balances"}


def test_actions_are_offered_only_when_something_is_to_be_recorded():
    names = every_tool()
    assert "propose_expense" not in select_tools("how much did we spend on diesel?", None, names)
    assert "propose_expense" in select_tools("add 500 diesel", None, names)
    assert "propose_pos_sale" in select_tools("sold 2 kg tomatoes, cash", None, names)


def test_a_follow_up_keeps_the_earlier_topic():
    history = [{"role": "user", "content": "How much did we spend this month?"},
               {"role": "assistant", "content": "12,000 EGP"}]
    assert "expenses" in select_tools("and last month?", history, every_tool())


def test_a_question_that_matches_nothing_gets_every_tool():
    names = every_tool()
    assert select_tools("Hello", None, names) == names


def test_only_tools_the_user_may_use_are_offered():
    viewer = user(role="viewer", permissions="page_products")
    names = assistant_service.allowed_tools(viewer) + assistant_service.allowed_actions(viewer)
    assert set(select_tools("كام مبيعات النهارده؟ والمخزون", None, names)) <= set(names)


def test_the_model_can_ask_for_the_rest_and_gets_it_next_round():
    session, db = make_db()
    rounds = []

    def handler(request):
        body = json.loads(request.content)
        offered = [t["function"]["name"] for t in body["tools"]]
        rounds.append(offered)
        if len(rounds) == 1:
            call = {"id": "c1", "type": "function", "function": {"name": MORE_TOOLS, "arguments": "{}"}}
        elif len(rounds) == 2:
            call = {"id": "c2", "type": "function", "function": {"name": "suppliers", "arguments": "{}"}}
        else:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Done."}}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                                                                  "tool_calls": [call]}}]})

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(assistant_service.ask(
            db, user(), "What's running low?", None, transport=httpx.MockTransport(handler)))
    finally:
        loop.close()

    assert result["answer"] == "Done."
    assert "suppliers" not in rounds[0] and MORE_TOOLS in rounds[0]
    assert "suppliers" in rounds[1] and MORE_TOOLS not in rounds[1]
    assert [lk["tool"] for lk in result["lookups"]] == ["suppliers"]
