"""The Gemini backend returns the response shape FINN's callers read from Anthropic (mocked HTTP)."""
import httpx

from finn.intel import gemini


def _reply(payload, status=200):
    return lambda *a, **k: httpx.Response(status, json=payload, request=httpx.Request("POST", "https://x"))


def test_plain_json(monkeypatch):
    monkeypatch.setattr(gemini.httpx, "post", _reply({
        "candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True}, {"text": '{"a": 1}'}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "thoughtsTokenCount": 3}}))
    r = gemini.generate("k", model="gemini-2.5-flash", system="s", messages=[{"role": "user", "content": "q"}],
                        max_tokens=100, effort="low", schema={"type": "object"})
    assert r.stop_reason == "end_turn" and r.content[0].text == '{"a": 1}' and r.usage.output_tokens == 8


def test_grounded_search(monkeypatch):
    text = "FACT | Ravi Kumar named CEO | https://guess.example/x\nnothing here"
    monkeypatch.setattr(gemini.httpx, "post", _reply({
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP", "groundingMetadata": {
            "webSearchQueries": ["q1", "q2"],
            "groundingChunks": [{"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc", "title": "bseindia.com"}},
                                {"web": {"uri": "https://blog.example/p", "title": "blog.example"}}],
            "groundingSupports": [{"segment": {"text": "Ravi Kumar named CEO"}, "groundingChunkIndices": [0, 1]}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}))
    monkeypatch.setattr(gemini, "_resolve", lambda uris: ["https://www.bseindia.com/ann/1" if "redirect" in u else u for u in uris])
    r = gemini.generate("k", model="gemini-2.5-flash", system="", messages=[{"role": "user", "content": "q"}], max_tokens=100,
                        effort="low", tools=[{"type": "web_search_20250305", "name": "web_search", "allowed_domains": ["bseindia.com"]}])
    results = r.content[0]
    assert results.type == "web_search_tool_result" and [x.url for x in results.content] == ["https://www.bseindia.com/ann/1"]
    line = r.content[1]
    assert "https://www.bseindia.com/ann/1" in line.text and "guess.example" not in line.text   # pointed at the grounding page
    assert [c.url for c in line.citations] == ["https://www.bseindia.com/ann/1"]                 # off-list domain dropped
    assert r.usage.server_tool_use.web_search_requests == 2


def test_bad_key_is_auth_error(monkeypatch):
    monkeypatch.setattr(gemini.httpx, "post", _reply({"error": {"message": "API key not valid. Please pass a valid API key."}}, 400))
    try:
        gemini.generate("k", model="m", system="", messages=[{"role": "user", "content": "q"}], max_tokens=10, effort="low")
    except gemini.GeminiError as e:
        assert e.auth
    else:
        raise AssertionError("expected GeminiError")
