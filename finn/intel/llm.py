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

import hashlib
import json
import logging
import time
from typing import Any

from .. import db, store
from ..config import ANTHROPIC_API_KEY, LLM_ENABLED, MODEL_DEEP, MODEL_FAST

log = logging.getLogger(__name__)

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
}

_client = None


# Circuit breaker: a rejected key (401/403) fails every call the same way, so stop calling for a while
# instead of retrying each one-liner, enrichment and extraction. The app keeps running on its
# deterministic fallbacks; a corrected key takes effect on redeploy, or after the pause here.
AUTH_PAUSE_S = 900
_auth_down_until = 0.0
_auth_error = ""


def available() -> bool:
    return LLM_ENABLED and time.time() >= _auth_down_until


def status() -> str:
    if LLM_ENABLED is False:
        return "off"
    return "ok" if time.time() >= _auth_down_until else f"key rejected: {_auth_error}"


def _key_rejected(purpose: str, e: Exception) -> None:
    global _auth_down_until, _auth_error
    first = time.time() >= _auth_down_until
    _auth_down_until = time.time() + AUTH_PAUSE_S
    _auth_error = str(getattr(e, "message", e))[:160]
    if first:
        log.error("Anthropic rejected the API key (%s): %s. Model calls paused for %d min; running on fallbacks. "
                  "Set a valid ANTHROPIC_API_KEY.", purpose, _auth_error, AUTH_PAUSE_S // 60)


def enabled() -> bool:
    return available()


def _get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=3, timeout=120.0)
    return _client


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
        cost += searches * WEB_SEARCH_USD
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
    model = MODEL_DEEP if tier == "deep" else MODEL_FAST
    key = _key(model, system, (cache_prefix or "") + "\n\n" + prompt, schema)
    hit = db.kv_get("llm", key) if RESPONSE_CACHE else None
    if hit is not None:
        _ledger(model, purpose, key, None, cached=True)
        return hit

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
        client = _get_client()
        if tier == "deep":
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
    import anthropic

    client = _get_client()
    try:
        resp = None
        for _ in range(4):  # server tools may pause a long turn; resume it
            if model == MODEL_DEEP:
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


def spend_summary() -> dict[str, Any]:
    items = store.ledger_summary()
    return {"enabled": LLM_ENABLED, "fast": MODEL_FAST, "deep": MODEL_DEEP,
            "total_usd": round(sum((r["cost"] or 0) for r in items), 4), "by_purpose": items}
