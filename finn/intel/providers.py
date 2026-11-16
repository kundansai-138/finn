"""Model providers FINN can run on, and the adapter for every OpenAI-compatible API.

FINN is model-agnostic: a visitor picks a provider, pastes a key, and picks two models from the ones
that key can use (an everyday model for one-liners and extraction, a deep model for judgement and
answers). Anthropic and Gemini run natively, because they also give FINN server-side web search with
citations; everything else speaks the OpenAI chat-completions format, so one adapter covers OpenAI,
OpenRouter (hundreds of models), Groq, DeepSeek, Mistral, Together, and any compatible endpoint.

Web search: only Anthropic and Gemini. On other providers the web features (enrichment, web people
discovery, the agent's web tool) are skipped and the rest of FINN runs on the chosen models.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from types import SimpleNamespace as NS
from typing import Any
from urllib.parse import urlparse

import httpx

PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {"label": "Anthropic (Claude)", "kind": "anthropic", "hint": "sk-ant-…", "search": True,
                  "fast": "claude-haiku-5-5", "deep": "claude-opus-5-5", "web": "claude-sonnet-5-5"},
    "gemini": {"label": "Google Gemini", "kind": "gemini", "hint": "AIza…", "search": True,
               "fast": "gemini-2.5-flash", "deep": "gemini-3.1-pro-preview", "web": "gemini-2.5-flash"},
    "openai": {"label": "OpenAI", "kind": "openai", "hint": "sk-…", "base": "https://api.openai.com/v1",
               "fast": "gpt-5-mini", "deep": "gpt-5", "schema": True},
    "openrouter": {"label": "OpenRouter (any model)", "kind": "openai", "hint": "sk-or-…", "base": "https://openrouter.ai/api/v1",
                   "fast": "openai/gpt-5-mini", "deep": "anthropic/claude-opus-5-5", "schema": True},
    "groq": {"label": "Groq", "kind": "openai", "hint": "gsk_…", "base": "https://api.groq.com/openai/v1",
             "fast": "llama-3.1-8b-instant", "deep": "llama-3.3-70b-versatile"},
    "deepseek": {"label": "DeepSeek", "kind": "openai", "hint": "sk-…", "base": "https://api.deepseek.com/v1",
                 "fast": "deepseek-chat", "deep": "deepseek-reasoner"},
    "mistral": {"label": "Mistral", "kind": "openai", "hint": "…", "base": "https://api.mistral.ai/v1",
                "fast": "mistral-small-latest", "deep": "mistral-large-latest"},
    "together": {"label": "Together AI", "kind": "openai", "hint": "…", "base": "https://api.together.xyz/v1",
                 "fast": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "deep": "deepseek-ai/DeepSeek-V3"},
    "custom": {"label": "Other OpenAI-compatible API", "kind": "openai", "hint": "your key", "base": "",
               "fast": "", "deep": ""},
}


_NOT_CHAT = ("embedding", "image", "tts", "live", "audio", "aqa", "computer-use", "robotics", "nano", "gemma", "-exp", "learnlm", "veo", "imagen")


def newest_gemini(names: list[str] | set[str], role: str) -> str:
    """The newest chat model of a role ("deep" = pro, "fast" = flash, not lite) in what this key can list.
    Google retires models for new keys (gemini-2.5-pro went first), so pick from the key's own list
    instead of trusting a fixed name. Newest version wins; at equal version, stable beats preview."""
    want = "-pro" if role == "deep" else "-flash"
    cands = [n for n in names if n.startswith("gemini-") and want in n and "lite" not in n and not any(x in n for x in _NOT_CHAT)
             and "latest" not in n]

    def key(n: str):
        m = re.match(r"gemini-(\d+(?:\.\d+)?)", n)
        return (float(m.group(1)) if m else 0.0, "preview" not in n, -len(n))

    return max(cands, key=key) if cands else ""


def guess(key: str) -> str | None:
    """Best guess of the provider from the key's shape (only used when the visitor didn't say)."""
    for pre, prov in (("sk-ant-", "anthropic"), ("AIza", "gemini"), ("sk-or-", "openrouter"), ("gsk_", "groq"), ("sk-", "openai")):
        if key.startswith(pre):
            return prov
    return None


def public() -> list[dict]:
    """The provider list for the app's picker."""
    return [{"id": k, "label": v["label"], "hint": v["hint"], "search": bool(v.get("search")), "fast": v.get("fast", ""),
             "deep": v.get("deep", ""), "custom": k == "custom"} for k, v in PROVIDERS.items()]


class ProviderError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status_code, self.message = status, message

    @property
    def auth(self) -> bool:
        m = self.message.lower()
        return self.status_code in (401, 403) or (self.status_code == 400 and ("api key" in m or "api_key" in m))


def safe_base(url: str) -> str:
    """A custom endpoint must be public HTTPS: FINN's server makes the request, so no internal addresses."""
    u = urlparse((url or "").strip().rstrip("/"))
    if u.scheme != "https" or not u.hostname:
        raise ProviderError(400, "The endpoint must be an https:// URL.")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ProviderError(400, f"Can't resolve {u.hostname}.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ProviderError(400, "That endpoint points at a private address.")
    return u.geturl()


def _err(r: httpx.Response) -> ProviderError:
    try:
        j = r.json()
        e = j.get("error", j)
        msg = e.get("message", r.text) if isinstance(e, dict) else str(e)
    except ValueError:
        msg = r.text
    return ProviderError(r.status_code, str(msg)[:300])


def list_models(base: str, key: str) -> list[str]:
    r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=15.0)
    if r.status_code >= 400:
        raise _err(r)
    data = r.json()
    items = data.get("data", data) if isinstance(data, dict) else data
    return sorted({m.get("id") for m in items if isinstance(m, dict) and m.get("id")})


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _strip_fences(t: str) -> str:
    m = re.match(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", t, re.S)
    return m.group(1) if m else t


def generate(provider: str, base: str, key: str, *, model: str, system: str, messages: list[dict], max_tokens: int,
             effort: str, schema: dict | None = None) -> NS:
    """One chat-completions call, returned in the response shape FINN reads (content/usage/stop_reason)."""
    p = PROVIDERS.get(provider, PROVIDERS["custom"])
    msgs = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "assistant" if m["role"] == "assistant" else "user", "content": _text(m["content"])} for m in messages]
    body: dict[str, Any] = {"model": model, "messages": msgs}
    body["max_completion_tokens" if provider == "openai" else "max_tokens"] = max_tokens + (2048 if provider == "openai" else 0)
    if schema:
        if p.get("schema"):
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": False}}
        else:
            body["response_format"] = {"type": "json_object"}
            body["messages"] = [{"role": "system", "content": (system + "\n\n" if system else "") +
                                 "Reply with one JSON object only, matching this JSON Schema:\n" + json.dumps(schema)}] + msgs[1 if system else 0:]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if provider == "openrouter":
        headers["X-Title"] = "FINN"
    r = httpx.post(f"{base}/chat/completions", json=body, headers=headers, timeout=120.0)
    if r.status_code == 400 and schema and "response_format" in body:   # some models don't take a response format
        body.pop("response_format")
        r = httpx.post(f"{base}/chat/completions", json=body, headers=headers, timeout=120.0)
    if r.status_code >= 400:
        raise _err(r)
    j = r.json()
    ch = (j.get("choices") or [{}])[0]
    text = (ch.get("message") or {}).get("content") or ""
    if schema:
        text = _strip_fences(text)
    fr = ch.get("finish_reason") or "stop"
    stop = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}.get(fr, "end_turn")
    u = j.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    usage = NS(input_tokens=(u.get("prompt_tokens", 0) or 0) - cached, output_tokens=u.get("completion_tokens", 0) or 0,
               cache_read_input_tokens=cached, cache_creation_input_tokens=0, server_tool_use=NS(web_search_requests=0))
    return NS(content=[NS(type="text", text=text, citations=[])], usage=usage, stop_reason=stop)
