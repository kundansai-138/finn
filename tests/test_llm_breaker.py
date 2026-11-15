"""Model configs: a visitor's provider/key/models travel with their request only; a rejected key pauses
calls made with it (one error, not one per call) without affecting other keys; any OpenAI-compatible
provider works through one adapter; web search runs only where the provider has it."""
from types import SimpleNamespace as NS

import anthropic
import httpx2 as httpx
import pytest

from finn.intel import gemini, llm, providers


@pytest.fixture(autouse=True)
def _llm_on(monkeypatch):
    monkeypatch.setenv("FINN_LLM", "on")
    monkeypatch.setattr(llm, "_down", {})
    monkeypatch.setattr(llm, "RESPONSE_CACHE", False)


def _resp(text="ok"):
    return NS(content=[NS(type="text", text=text, citations=[])], stop_reason="end_turn",
              usage=NS(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                       server_tool_use=NS(web_search_requests=0)))


def test_rejected_key_pauses_calls(monkeypatch):
    calls = []

    class Msgs:
        def create(self, **kw):
            calls.append(1)
            req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.AuthenticationError("invalid x-api-key", response=httpx.Response(401, request=req), body=None)

    monkeypatch.setattr(llm, "_get_client", lambda *a: type("C", (), {"messages": Msgs(), "beta": type("B", (), {"messages": Msgs()})()})())
    t = llm.use_key("sk-ant-bad-key", "anthropic")
    try:
        assert llm.complete("t1", "hello") is None
        assert llm.complete("t2", "hello again") is None
        assert llm.call("t3", model="m", system="s", messages=[{"role": "user", "content": "x"}]) == (None, None)
        assert len(calls) == 1                          # paused after the first rejection
        assert not llm.enabled() and llm.status().startswith("key rejected")
    finally:
        llm.reset_key(t)
    t = llm.use_key("sk-ant-another-key", "anthropic")  # a different visitor's key is not paused
    try:
        assert llm.enabled()
    finally:
        llm.reset_key(t)


def test_visitor_models_are_used_by_role(monkeypatch):
    seen = {}
    monkeypatch.setattr(gemini, "generate", lambda key, **kw: seen.update(key=key, model=kw["model"]) or _resp())
    t = llm.use_key("AIzaTestKey123", "gemini", fast="gemini-x-fast", deep="gemini-x-deep")
    try:
        assert llm.source() == "visitor"
        assert llm.complete("t", "hi", tier="deep") == "ok" and seen["model"] == "gemini-x-deep"
        assert llm.complete("t", "hi") == "ok" and seen["model"] == "gemini-x-fast"
        text, _ = llm.call("t", model="claude-opus-5-5", system="s", messages=[{"role": "user", "content": "q"}])
        assert text == "ok" and seen["model"] == "gemini-x-deep" and seen["key"] == "AIzaTestKey123"
    finally:
        llm.reset_key(t)


def test_openai_compatible_provider(monkeypatch):
    sent = {}

    def post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, body=json, auth=headers["Authorization"])
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"message": {"content": '```json\n{"a": 1}\n```'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4}})

    monkeypatch.setattr(providers.httpx, "post", post)
    t = llm.use_key("gsk_test", "groq", fast="llama-small", deep="llama-big")
    try:
        assert llm.complete("t", "hi", tier="deep", schema={"type": "object"}) == {"a": 1}
        assert sent["url"] == "https://api.groq.com/openai/v1/chat/completions" and sent["body"]["model"] == "llama-big"
        assert sent["auth"] == "Bearer gsk_test" and sent["body"]["response_format"] == {"type": "json_object"}
        # no web search on this provider: tool calls fall back instead of calling
        assert not llm.web_search_available()
        assert llm.call("w", model="m", system="s", messages=[{"role": "user", "content": "q"}],
                        tools=[{"type": "web_search_20250305", "name": "web_search"}]) == (None, None)
    finally:
        llm.reset_key(t)


def test_unusable_configs_are_ignored():
    for args in (("not-a-key",), ("sk-xyz", "nonsense-provider"), ("sk-xyz", "custom", None, None, "http://10.0.0.1/v1")):
        t = llm.use_key(*args)
        try:
            assert llm.source() != "visitor"
        finally:
            llm.reset_key(t)
    assert llm.check_key("hello")["ok"] is False


def test_custom_endpoint_must_be_public_https():
    for bad in ("http://api.example.com/v1", "https://localhost/v1", "https://127.0.0.1/v1"):
        with pytest.raises(providers.ProviderError):
            providers.safe_base(bad)


def test_headers_reach_the_request():
    """The API binds the X-FINN-Model-* headers for the request, and only that request."""
    from fastapi.testclient import TestClient

    from finn.api import app

    c = TestClient(app)
    r = c.get("/api/llm", headers={"X-FINN-Model-Key": "sk-test", "X-FINN-Model-Provider": "openai"}).json()
    assert r["source"] == "visitor" and r["web_search"] is False and any(p["id"] == "openrouter" for p in r["providers"])
    assert c.get("/api/llm").json()["source"] != "visitor"
