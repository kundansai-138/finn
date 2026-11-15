"""Every model call goes through here: routing, caching, structured output, cost ledger.

Routing
  tier="fast" -> MODEL_FAST (Haiku 5.5, low effort): classification fallbacks, one-liners,
                 bio extraction, guidance extraction per transcript, ask-bar intent parsing.
  tier="deep" -> MODEL_DEEP (Opus 5.5, high effort): promised-vs-delivered judgement, bull/bear.

Caching
  1. Response cache: sha256(model, system, prompt, schema) -> stored output. Filings and transcripts
     are immutable, so each document is processed exactly once, ever.
  2. Prompt caching: long stable prefixes (system + document) are marked cache_control so follow-up
     questions over the same transcript are billed at ~10% of the input price.

If no API key is configured every call returns None and callers use their deterministic fallback.
The app is fully usable without a key; the model makes it better, not possible.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from .. import db, store
from ..config import LLM_API_KEY, LLM_BASE_URL, LLM_BYOK, LLM_PROVIDER, MODELS
from ..config import LLM_ENABLED as _ENV_ON  # the server has a key and FINN_LLM isn't off
from . import gemini, providers

log = logging.getLogger(__name__)

# Why the last model call in this request produced nothing (rate limit, API error, refusal, bad JSON...).
# Captured from this module's own warnings, so every failure path is covered without threading it through.
_last_err: contextvars.ContextVar[str] = contextvars.ContextVar("finn_model_err", default="")


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING:
            _last_err.set(record.getMessage())


log.addHandler(_Capture())
LLM_ENABLED = _ENV_ON   # the server key; enabled() is the live answer, including a visitor's own key

# Response cache (sha256 of the full request -> output). On by default; evals that measure model
# latency switch it off so a rerun isn't served from a previous run's answers.
RESPONSE_CACHE = True

# USD per 1M tokens: (input, output, cache_read)
WEB_SEARCH_USD = 0.01
MIN_MAX_TOKENS = 1024
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "gemini-2.5-flash-lite": (0.10, 0.40, 0.025),
    "gemini-2.5-flash": (0.30, 2.50, 0.075),
    "gemini-2.5-pro": (1.25, 10.00, 0.31),
}
GEMINI_SEARCH_USD = 0.035   # Google Search grounding, per grounded request beyond the free daily allowance

# ---- whose model ---------------------------------------------------------------------------------
# FINN is model-agnostic. A visitor picks a provider, pastes a key and picks models in the app; the
# browser keeps that on the device and sends it with each request (X-FINN-Model-* headers). The API
# middleware binds it here for that request only; it is never written to the database or logs.
# Without one, the server's own key (if configured) is used; without either, deterministic fallbacks.
@dataclass(frozen=True)
class Creds:
    provider: str
    key: str
    fast: str
    deep: str
    web: str
    base: str = ""
    visitor: bool = False

    @property
    def kind(self) -> str:
        return providers.PROVIDERS.get(self.provider, providers.PROVIDERS["custom"])["kind"]

    @property
    def search(self) -> bool:
        return bool(providers.PROVIDERS.get(self.provider, {}).get("search"))


_req: contextvars.ContextVar[Creds | None] = contextvars.ContextVar("finn_model", default=None)


def provider_of(key: str) -> str | None:
    return providers.guess(key)


def creds_from(key: str | None, provider: str | None = None, fast: str | None = None, deep: str | None = None,
               base: str | None = None) -> Creds | None:
    """Build a visitor's model config from what the app sent; None if it's unusable."""
    key = (key or "").strip()
    prov = (provider or "").strip().lower() or providers.guess(key)
    if not key or prov not in providers.PROVIDERS or len(key) > 400:
        return None
    p = providers.PROVIDERS[prov]
    if prov == "custom":
        try:
            base = providers.safe_base(base or "")
        except providers.ProviderError:
            return None
    else:
        base = p.get("base", "")
    clean = lambda m: m if m and re.fullmatch(r"[\w.:/@+-]{1,120}", m) else ""   # noqa: E731
    f, d = clean((fast or "").strip()) or p.get("fast", ""), clean((deep or "").strip()) or p.get("deep", "")
    if not f or not d:
        return None
    return Creds(prov, key, f, d, p.get("web") or f, base, visitor=True)


def use_key(key: str | None, provider: str | None = None, fast: str | None = None, deep: str | None = None,
            base: str | None = None) -> contextvars.Token:
    return _req.set(creds_from(key, provider, fast, deep, base) if LLM_BYOK else None)


def reset_key(token: contextvars.Token) -> None:
    _req.reset(token)


def _server() -> Creds | None:
    if not (LLM_ENABLED and LLM_API_KEY):
        return None
    m = MODELS.get(LLM_PROVIDER) or providers.PROVIDERS.get(LLM_PROVIDER, {})
    base = LLM_BASE_URL or providers.PROVIDERS.get(LLM_PROVIDER, {}).get("base", "")
    return Creds(LLM_PROVIDER, LLM_API_KEY, m.get("fast", ""), m.get("deep", ""), m.get("web") or m.get("fast", ""), base)


def _creds() -> Creds | None:
    return _req.get() or _server()


def source() -> str:
    """'visitor' (a model config from this request), 'server', or 'none'."""
    return "visitor" if _req.get() else "server" if _server() else "none"


def _route(model: str, c: Creds) -> str:
    """Callers name a model by role (MODEL_WEB, MODEL_AGENT, MODEL_DEEP); map it to the active config."""
    if model in (c.fast, c.deep, c.web):
        return model
    for prov in list(MODELS.values()) + [providers.PROVIDERS["anthropic"], providers.PROVIDERS["gemini"]]:
        for role in ("deep", "web", "fast"):
            if prov.get(role) == model:
                return {"deep": c.deep, "web": c.web, "fast": c.fast}[role]
    return c.fast


# Circuit breaker, per key: a rejected key (401/403) fails every call the same way, so stop calling
# with it for a while instead of retrying each one-liner, enrichment and extraction. Another visitor's
# key, or the server's, is unaffected. The app keeps running on its deterministic fallbacks.
AUTH_PAUSE_S = 900
_down: dict[str, tuple[float, str]] = {}


def _kid(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def available() -> bool:
    c = _creds()
    if not c or os.getenv("FINN_LLM", "on") == "off":
        return False
    return time.time() >= _down.get(_kid(c.key), (0.0, ""))[0]


def web_search_available() -> bool:
    c = _creds()
    return bool(c and c.search and available())


def status() -> str:
    c = _creds()
    if not c:
        return "off"
    until, err = _down.get(_kid(c.key), (0.0, ""))
    return f"ok ({c.provider}, {source()} key)" if time.time() >= until else f"key rejected: {err}"


def _key_rejected(purpose: str, e: Exception) -> None:
    c = _creds()
    kid = _kid(c.key if c else "")
    first = time.time() >= _down.get(kid, (0.0, ""))[0]
    err = str(getattr(e, "message", e))[:160]
    _down[kid] = (time.time() + AUTH_PAUSE_S, err)
    if first:
        log.error("%s rejected the %s API key (%s): %s. Calls with it paused for %d min; running on fallbacks.",
                  (c.provider if c else "?").title(), source(), purpose, err, AUTH_PAUSE_S // 60)


def enabled() -> bool:
    return available()


def clear_error() -> None:
    _last_err.set("")


def failure() -> dict[str, Any]:
    """Why no model answer is available for this request, in words a visitor can act on."""
    c = _creds()
    if not c:
        return {"reason": "no_key", "provider": None, "visitor": False, "detail": ""}
    label = providers.PROVIDERS.get(c.provider, {}).get("label", c.provider)
    until, err = _down.get(_kid(c.key), (0.0, ""))
    if time.time() < until:
        return {"reason": "rejected", "provider": label, "visitor": c.visitor, "detail": err}
    return {"reason": "error", "provider": label, "visitor": c.visitor, "detail": _last_err.get() or "the model returned no answer"}


def models() -> dict[str, str | None]:
    """The models this request would use (the visitor's choice, else the server's), for display."""
    c = _creds()
    return {"provider": c.provider if c else None, "fast": c.fast if c else None, "deep": c.deep if c else None}


_clients: dict[str, Any] = {}


def _get_client(key: str = ""):
    kid = _kid(key)
    if kid not in _clients:
        import anthropic

        _clients[kid] = anthropic.Anthropic(api_key=key, max_retries=3, timeout=120.0)
    return _clients[kid]


def check_key(key: str, provider: str | None = None, base: str | None = None) -> dict[str, Any]:
    """Validate a key without spending anything (list the models it can use) and return those models."""
    key = (key or "").strip()
    prov = (provider or "").strip().lower() or providers.guess(key)
    if not key or prov not in providers.PROVIDERS:
        return {"ok": False, "error": "Pick a provider for this key."}
    p = providers.PROVIDERS[prov]
    try:
        if prov == "custom":
            base = providers.safe_base(base or "")
        else:
            base = p.get("base", "")
        if p["kind"] == "gemini":
            names = sorted(m for m in gemini.list_models(key) if "gemini" in m and "embedding" not in m)
        elif p["kind"] == "anthropic":
            import anthropic

            names = sorted(m.id for m in anthropic.Anthropic(api_key=key, timeout=15.0).models.list(limit=100))
        else:
            names = providers.list_models(base, key)
        fast = p.get("fast") if p.get("fast") in names else (names[0] if names else "")
        deep = p.get("deep") if p.get("deep") in names else fast
        return {"ok": True, "provider": prov, "label": p["label"], "models": names[:400], "fast": fast, "deep": deep,
                "search": bool(p.get("search"))}
    except (gemini.GeminiError, providers.ProviderError) as e:
        return {"ok": False, "provider": prov, "error": "The key was rejected." if e.auth else e.message[:200]}
    except Exception as e:  # anthropic auth / connection errors
        msg = str(getattr(e, "message", e))
        bad = "401" in msg or "auth" in msg.lower() or "invalid" in msg.lower()
        return {"ok": False, "provider": prov, "error": "The key was rejected." if bad else msg[:200]}


def _key(model: str, system: str, prompt: str, schema: dict | None) -> str:
    blob = json.dumps([model, system, prompt, schema], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _ledger(model: str, purpose: str, key: str, usage: Any | None, cached: bool) -> None:
    it = ot = cr = 0
    cost = 0.0
    if usage is not None:
        it = getattr(usage, "input_tokens", 0) or 0
        ot = getattr(usage, "output_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        pin, pout, pcr = PRICES.get(model, (4.0, 20.0, 0.2))
        cost = (it * pin + cw * pin * 1.25 + cr * pcr + ot * pout) / 1e6
        # Server-side web search is billed per search on top of tokens ($10 per 1,000).
        searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
        cost += (GEMINI_SEARCH_USD if searches else 0) if model.startswith("gemini") else searches * WEB_SEARCH_USD
    store.ledger_add({"at": time.time(), "model": model, "purpose": purpose, "prompt_hash": key[:16], "input_tokens": it,
                      "output_tokens": ot, "cache_read_tokens": cr, "cost_usd": cost, "cached": int(cached)})


def complete(
    purpose: str,
    prompt: str,
    *,
    tier: str = "fast",
    system: str = "",
    schema: dict | None = None,
    max_tokens: int = 4000,
    effort: str | None = None,
    cache_prefix: str | None = None,
) -> Any | None:
    """Return parsed JSON (when schema is given) or text; None when the model is unavailable.

    cache_prefix: a long, stable block (e.g. a transcript) placed before `prompt` and marked for
    prompt caching, so several questions over the same document reuse it cheaply.
    """
    if not available():
        return None
    # Current models think adaptively before answering, and thinking counts against max_tokens: a
    # 120-token cap can be spent entirely on thinking and return nothing. Only generated tokens are
    # billed, so a floor costs nothing and removes a silent failure (found by the live tests).
    max_tokens = max(max_tokens, MIN_MAX_TOKENS)
    c = _creds()
    model = c.deep if tier == "deep" else c.fast
    key = _key(model, system, (cache_prefix or "") + "\n\n" + prompt, schema)
    hit = db.kv_get("llm", key) if RESPONSE_CACHE else None
    if hit is not None:
        _ledger(model, purpose, key, None, cached=True)
        return hit

    if c.kind != "anthropic":
        resp = _other(purpose, c, model=model, system=system, max_tokens=max_tokens, effort=effort or ("high" if tier == "deep" else "low"),
                      schema=schema, messages=[{"role": "user", "content": ((cache_prefix + "\n\n") if cache_prefix else "") + prompt}])
        if resp is None:
            return None
        return _finish(resp, model, purpose, key, schema, store_it=True)

    content: list[dict[str, Any]] = []
    if cache_prefix:
        content.append({"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}})
    content.append({"type": "text", "text": prompt})

    output_config: dict[str, Any] = {"effort": effort or ("high" if tier == "deep" else "low")}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}

    kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
        output_config=output_config,
    )
    if system:
        kwargs["system"] = system

    import anthropic

    try:
        client = _get_client(c.key)
        if model == providers.PROVIDERS["anthropic"]["deep"]:
            # Opus: opt into server-side refusal fallback so a rare false-positive decline still
            # returns an answer instead of an empty ledger.
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            resp = client.messages.create(**kwargs)
    except anthropic.RateLimitError as e:
        log.warning("LLM rate limited (%s): %s", purpose, e)
        return None
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        _key_rejected(purpose, e)
        return None
    except anthropic.APIStatusError as e:
        log.warning("LLM API error %s (%s): %s", e.status_code, purpose, e.message)
        return None
    except anthropic.APIConnectionError as e:
        log.warning("LLM connection error (%s): %s", purpose, e)
        return None

    if resp.stop_reason == "refusal":
        log.warning("LLM refused (%s)", purpose)
        _ledger(model, purpose, key, resp.usage, cached=False)
        return None
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    _ledger(model, purpose, key, resp.usage, cached=False)
    if resp.stop_reason == "max_tokens":
        log.warning("LLM hit max_tokens (%s): %d chars of answer", purpose, len(text))
    if not text:
        return None
    if schema:
        try:
            out: Any = json.loads(text)
        except json.JSONDecodeError:
            log.warning("LLM returned invalid JSON (%s), stop_reason=%s", purpose, resp.stop_reason)
            return None
    else:
        out = text
    db.kv_put("llm", key, out)
    return out


def call(
    purpose: str,
    *,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    schema: dict | None = None,
    effort: str = "medium",
    max_tokens: int = 4000,
    tools: list[dict] | None = None,
    cache_key: str | None = None,
) -> tuple[Any | None, Any | None]:
    """Agent-grade call: multi-turn messages, a cached system prompt, optional server tools.

    The system prompt carries a cache breakpoint, and so does the last history message, so a
    growing conversation re-reads its prefix at ~10% of the input price. `cache_key` enables the
    response cache (skipped for web search, which must stay fresh).
    Returns (parsed_or_text, raw_response); (None, None) when the model is unavailable.
    """
    if not available():
        return None, None
    c = _creds()
    model = _route(model, c)
    if tools and not c.search:   # web search needs Anthropic or Gemini; callers fall back without it
        return None, None
    key = _key(model, system, json.dumps(messages, sort_keys=True, default=str) + (cache_key or ""), schema)
    if cache_key and not tools and RESPONSE_CACHE:
        hit = db.kv_get("llm", key)
        if hit is not None:
            _ledger(model, purpose, key, None, cached=True)
            return hit, None
    msgs = [dict(m) for m in messages]
    if len(msgs) >= 2 and isinstance(msgs[-2].get("content"), str):
        msgs[-2] = {**msgs[-2], "content": [{"type": "text", "text": msgs[-2]["content"], "cache_control": {"type": "ephemeral"}}]}
    output_config: dict[str, Any] = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    kwargs: dict[str, Any] = dict(
        model=model, max_tokens=max_tokens, messages=msgs, output_config=output_config,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
    )
    if tools:
        kwargs["tools"] = tools
    if c.kind != "anthropic":
        resp = _other(purpose, c, model=model, system=system, messages=messages, max_tokens=max_tokens, effort=effort,
                      schema=schema, tools=tools)
        if resp is None:
            return None, None
        out = _finish(resp, model, purpose, key, schema, store_it=bool(cache_key and not tools))
        return out, resp
    import anthropic

    client = _get_client(c.key)
    try:
        resp = None
        for _ in range(4):  # server tools may pause a long turn; resume it
            if model == providers.PROVIDERS["anthropic"]["deep"]:
                resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = client.messages.create(**kwargs)
            if resp.stop_reason != "pause_turn":
                break
            kwargs["messages"] = kwargs["messages"] + [{"role": "assistant", "content": resp.content}]
    except anthropic.RateLimitError as e:
        log.warning("LLM rate limited (%s): %s", purpose, e)
        return None, None
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        _key_rejected(purpose, e)
        return None, None
    except anthropic.APIStatusError as e:
        log.warning("LLM API error %s (%s): %s", e.status_code, purpose, e.message)
        return None, None
    except anthropic.APIConnectionError as e:
        log.warning("LLM connection error (%s): %s", purpose, e)
        return None, None
    _ledger(model, purpose, key, resp.usage, cached=False)
    if resp.stop_reason == "refusal":
        return None, resp
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if not schema:
        return text, resp
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        log.warning("LLM returned invalid JSON (%s), stop_reason=%s", purpose, resp.stop_reason)
        return None, resp
    if cache_key and not tools:
        db.kv_put("llm", key, out)
    return out, resp


def _other(purpose: str, c: Creds, **kw) -> Any | None:
    """Gemini natively; every other non-Anthropic provider through the OpenAI-compatible adapter."""
    try:
        if c.kind == "gemini":
            return gemini.generate(c.key, **kw)
        kw.pop("tools", None)
        return providers.generate(c.provider, c.base, c.key, **kw)
    except (gemini.GeminiError, providers.ProviderError) as e:
        if e.auth:
            _key_rejected(purpose, e)
        elif e.status_code == 429:
            log.warning("LLM rate limited (%s): %s", purpose, e.message)
        else:
            log.warning("LLM API error %s (%s): %s", e.status_code, purpose, e.message)
    except Exception as e:   # network trouble: the caller falls back
        log.warning("LLM connection error (%s): %s", purpose, e)
    return None


def _finish(resp: Any, model: str, purpose: str, key: str, schema: dict | None, store_it: bool) -> Any | None:
    _ledger(model, purpose, key, resp.usage, cached=False)
    if resp.stop_reason == "refusal":
        log.warning("LLM refused (%s)", purpose)
        return None
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if resp.stop_reason == "max_tokens":
        log.warning("LLM hit max_tokens (%s): %d chars of answer", purpose, len(text))
    if not text:
        return None
    out: Any = text
    if schema:
        try:
            out = json.loads(text)
        except json.JSONDecodeError:
            log.warning("LLM returned invalid JSON (%s), stop_reason=%s", purpose, resp.stop_reason)
            return None
    if store_it:
        db.kv_put("llm", key, out)
    return out


def spend_summary() -> dict[str, Any]:
    items = store.ledger_summary()
    c = _creds()
    return {"enabled": enabled(), "provider": c.provider if c else None, "key": source(), "fast": c.fast if c else None,
            "deep": c.deep if c else None,
            "total_usd": round(sum((r["cost"] or 0) for r in items), 4), "by_purpose": items}
