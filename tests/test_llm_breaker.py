"""A rejected API key pauses model calls (one error, not one per call) and the app falls back."""
import anthropic
import httpx2 as httpx

from finn.intel import llm


def test_rejected_key_pauses_calls(monkeypatch):
    calls = []

    class Msgs:
        def create(self, **kw):
            calls.append(1)
            req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.AuthenticationError("invalid x-api-key", response=httpx.Response(401, request=req), body=None)

    monkeypatch.setattr(llm, "LLM_ENABLED", True)
    monkeypatch.setattr(llm, "_auth_down_until", 0.0)
    monkeypatch.setattr(llm, "_get_client", lambda: type("C", (), {"messages": Msgs(), "beta": type("B", (), {"messages": Msgs()})()})())
    assert llm.complete("t1", "hello") is None
    assert llm.complete("t2", "hello again") is None and llm.call("t3", model="m", system="s", messages=[{"role": "user", "content": "x"}]) == (None, None)
    assert len(calls) == 1                          # paused after the first rejection
    assert not llm.enabled() and llm.status().startswith("key rejected")
