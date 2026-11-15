"""Gemini backend for llm.py, speaking the same shape the rest of FINN reads from Anthropic responses.

Callers read `resp.content` (text blocks with `.citations`, plus a `web_search_tool_result` block listing
what the search returned), `resp.usage` and `resp.stop_reason`. This module builds exactly that from a
Gemini `generateContent` reply, so enrichment, people discovery and the agent's web search run unchanged.

Web search maps to Gemini's Google Search grounding:
  - grounding chunks become the search results; their redirect links are resolved to the real page URL
    (one request each, no body read), because FINN verifies a claim by fetching the page itself;
  - grounding supports become citations on the line they support;
  - `allowed_domains` has no Gemini equivalent, so it becomes an instruction plus a hard filter on the
    results: a line citing anything else fails the existing "URL must be among the results" check.
"""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace as NS
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
UA = "FINN/1.0 (research prototype; resolves search citations)"


class GeminiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status_code, self.message = status, message

    @property
    def auth(self) -> bool:
        m = self.message.lower()
        return self.status_code in (401, 403) or (self.status_code == 400 and ("api key" in m or "api_key" in m))


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _thinking(model: str, effort: str) -> dict:
    if "gemini-3" in model:
        return {"thinkingLevel": "high" if effort in ("high", "max", "xhigh") else "low"}
    if "flash" in model:
        return {"thinkingBudget": {"low": 0, "medium": 1024}.get(effort, -1)}
    return {"thinkingBudget": {"low": 256, "medium": 2048}.get(effort, -1)}   # pro can't switch thinking off


def _resolve(uris: list[str]) -> list[str]:
    """Grounding links are redirects through Google; read the Location header to get the real page."""
    out = []
    with httpx.Client(timeout=6.0, headers={"User-Agent": UA}) as c:
        for u in uris:
            real = u
            if "grounding-api-redirect" in u:
                try:
                    r = c.get(u, follow_redirects=False)
                    real = r.headers.get("location") or u
                except httpx.HTTPError:
                    pass
            out.append(real)
    return out


def _host_ok(url: str, domains: list[str] | None) -> bool:
    if not domains:
        return True
    host = urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def generate(api_key: str, *, model: str, system: str, messages: list[dict], max_tokens: int, effort: str,
             schema: dict | None = None, tools: list[dict] | None = None) -> NS:
    search = next((t for t in (tools or []) if "web_search" in str(t.get("type", ""))), None)
    domains = (search or {}).get("allowed_domains")
    sys_text = system or ""
    if domains:
        sys_text += f"\n\nSearch and cite only these sites: {', '.join(domains)}."
    contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": _text(m["content"])}]}
                for m in messages if _text(m.get("content", ""))]
    cfg: dict[str, Any] = {"maxOutputTokens": max_tokens + 2048, "thinkingConfig": _thinking(model, effort)}
    if schema and not search:
        cfg["responseMimeType"] = "application/json"
        cfg["responseJsonSchema"] = schema
    body: dict[str, Any] = {"contents": contents, "generationConfig": cfg}
    if sys_text.strip():
        body["systemInstruction"] = {"parts": [{"text": sys_text}]}
    if search:
        body["tools"] = [{"google_search": {}}]

    r = httpx.post(API.format(model=model), json=body, timeout=120.0,
                   headers={"x-goog-api-key": api_key, "Content-Type": "application/json"})
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except ValueError:
            msg = r.text
        raise GeminiError(r.status_code, msg[:300])
    j = r.json()
    um = j.get("usageMetadata", {})
    cand = (j.get("candidates") or [{}])[0]
    fr = cand.get("finishReason", "STOP")
    stop = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens"}.get(fr, "refusal" if fr in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION") else "end_turn")
    if j.get("promptFeedback", {}).get("blockReason"):
        stop = "refusal"
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
    gm = cand.get("groundingMetadata") or {}
    queries = len(gm.get("webSearchQueries") or [])
    usage = NS(input_tokens=um.get("promptTokenCount", 0) - um.get("cachedContentTokenCount", 0),
               output_tokens=um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0),
               cache_read_input_tokens=um.get("cachedContentTokenCount", 0), cache_creation_input_tokens=0,
               server_tool_use=NS(web_search_requests=queries))
    if not search:
        return NS(content=[NS(type="text", text=text, citations=[])], usage=usage, stop_reason=stop)
    return NS(content=_grounded_blocks(text, gm, domains), usage=usage, stop_reason=stop)


def _grounded_blocks(text: str, gm: dict, domains: list[str] | None) -> list[NS]:
    chunks = [c.get("web", {}) for c in gm.get("groundingChunks") or []]
    urls = _resolve([c.get("uri", "") for c in chunks])
    results = [NS(url=u, title=c.get("title") or urlparse(u).netloc, page_age=None)
               for u, c in zip(urls, chunks) if u.startswith("http") and _host_ok(u, domains)]
    allowed = {r.url for r in results}
    lines = text.split("\n")
    cites: list[list[NS]] = [[] for _ in lines]
    for s in gm.get("groundingSupports") or []:
        seg = (s.get("segment") or {}).get("text", "").strip()
        if not seg:
            continue
        i = next((k for k, ln in enumerate(lines) if seg[:80] in ln), None)
        if i is None:
            continue
        for ci in s.get("groundingChunkIndices") or []:
            if ci < len(urls) and urls[ci] in allowed:
                cites[i].append(NS(url=urls[ci], title=chunks[ci].get("title") or "", cited_text=seg))
    blocks = [NS(type="web_search_tool_result", content=results)]
    for k, ln in enumerate(lines):
        # The model never sees the pages' real URLs, so a URL it writes may be a guess. Where the line is
        # grounded, point it at the page that grounds it; FINN still fetches that page and checks the claim.
        if cites[k]:
            for u in re.findall(r"https?://\S+", ln):
                if u.rstrip(").,;") not in allowed:
                    ln = ln.replace(u, cites[k][0].url)
        blocks.append(NS(type="text", text=ln + ("\n" if k < len(lines) - 1 else ""), citations=cites[k]))
    return blocks
