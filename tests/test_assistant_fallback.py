"""Fallback provider: when the first AI provider doesn't answer, the same request goes to a second one.

Every model call (Ask, document reading, the field bot) goes through assistant_service.post_completion, so this is
tested there, with httpx MockTransports for both providers.
"""
import asyncio

import httpx
import pytest

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.core.config import settings
from app.services import assistant_service as svc

PRIMARY, FALLBACK = "https://primary.test/v1", "https://fallback.test/openai/v1"


@pytest.fixture(autouse=True)
def providers(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_BASE_URL", PRIMARY)
    monkeypatch.setattr(settings, "ASSISTANT_API_KEY", "primary-key")
    monkeypatch.setattr(settings, "ASSISTANT_TRANSCRIBE_BASE_URL", FALLBACK)
    monkeypatch.setattr(settings, "ASSISTANT_TRANSCRIBE_API_KEY", "groq-key")
    monkeypatch.setattr(settings, "ASSISTANT_FALLBACK_BASE_URL", None)
    monkeypatch.setattr(settings, "ASSISTANT_FALLBACK_API_KEY", None)
    monkeypatch.setattr(settings, "ASSISTANT_FALLBACK_MODEL", "fallback-text")
    monkeypatch.setattr(settings, "ASSISTANT_FALLBACK_VISION_MODEL", None)
    monkeypatch.setattr(settings, "ASSISTANT_FALLBACK_AFTER_SECONDS", 40.0)
    monkeypatch.setattr(svc, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(svc, "_primary_down_until", 0.0)
    yield


def provider(primary: str):
    """primary: "ok", "timeout" or "500". The fallback always answers. Returns (transport, calls)."""
    calls = []

    def handler(request: httpx.Request):
        import json
        body = json.loads(request.content)
        calls.append((request.url.host, request.headers["authorization"], body["model"]))
        if request.url.host == "primary.test":
            if primary == "timeout":
                raise httpx.ReadTimeout("no answer", request=request)
            if primary == "500":
                return httpx.Response(500, json={"error": "unexpected"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
    return httpx.MockTransport(handler), calls


def post(transport, body=None):
    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await svc.post_completion(client, body or {"model": "claude-fable-5", "messages": [
                {"role": "user", "content": "hello"}]})
    return asyncio.run(go())


def test_a_provider_that_answers_is_used_alone():
    transport, calls = provider("ok")
    assert post(transport).status_code == 200
    assert calls == [("primary.test", "Bearer primary-key", "claude-fable-5")]


def test_a_timeout_switches_to_the_fallback_and_stays_there_for_a_while():
    transport, calls = provider("timeout")
    assert post(transport).status_code == 200
    assert calls == [("primary.test", "Bearer primary-key", "claude-fable-5"),
                     ("fallback.test", "Bearer groq-key", "fallback-text")]       # the voice provider's URL and key
    calls.clear()
    post(transport)
    assert calls == [("fallback.test", "Bearer groq-key", "fallback-text")]       # no second wait on the first


def test_passing_errors_after_the_retries_switch_too():
    transport, calls = provider("500")
    assert post(transport).status_code == 200
    assert [c[0] for c in calls] == ["primary.test"] * 3 + ["fallback.test"]


def test_images_fall_back_only_to_a_vision_model(monkeypatch):
    image_body = {"model": "vision", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "read"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA"}}]}]}
    transport, calls = provider("500")
    assert post(transport, image_body).status_code == 500                         # no vision model: no fallback
    assert {c[0] for c in calls} == {"primary.test"}
    monkeypatch.setattr(settings, "ASSISTANT_FALLBACK_VISION_MODEL", "fallback-vision")
    calls.clear()
    assert post(transport, image_body).status_code == 200
    assert calls[-1] == ("fallback.test", "Bearer groq-key", "fallback-vision")


def test_without_a_fallback_nothing_changes(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_FALLBACK_MODEL", None)
    transport, calls = provider("timeout")
    with pytest.raises(httpx.ReadTimeout):
        post(transport)
    assert {c[0] for c in calls} == {"primary.test"}


def test_its_own_url_and_key_win_over_the_voice_providers(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_FALLBACK_BASE_URL", "https://other.test/v1")
    monkeypatch.setattr(settings, "ASSISTANT_FALLBACK_API_KEY", "other-key")
    transport, calls = provider("timeout")
    post(transport)
    assert calls[-1] == ("other.test", "Bearer other-key", "fallback-text")


def test_more_than_three_images_stay_with_the_first_provider(monkeypatch):
    monkeypatch.setattr(settings, "ASSISTANT_FALLBACK_VISION_MODEL", "fallback-vision")
    four = {"model": "vision", "messages": [{"role": "user", "content": [{"type": "text", "text": "read"}] + [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA"}}] * 4}]}
    transport, calls = provider("500")
    assert post(transport, four).status_code == 500
    assert {c[0] for c in calls} == {"primary.test"}
