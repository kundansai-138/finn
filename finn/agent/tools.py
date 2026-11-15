"""Agent tools. Each returns *evidence*: small, citable facts with a source type.

Source types drive the anti-hallucination rules downstream:
  primary   exchange filings, concall transcripts, annual reports, reported financials
  computed  arithmetic FINN did on primary numbers (deterministic, shows its inputs)
  market    end-of-day prices and volumes
  network   the investor's own network (synthetic in the demo)
  secondary web results: allowed for context, never as the basis of financial analysis

Tool results are cached by (tool, args) with a TTL suited to the source, so a follow-up
question in the same thread never refetches what it already has.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from .. import companies, db, portfolio, pull, store
from ..context import current_user
from ..config import LLM_ENABLED, MODEL_WEB
from ..http import FetchError, fetch
from ..intel import llm
from ..network import graph
from ..network import recommend as reco
from ..sources import documents, prices
from . import retrieval

log = logging.getLogger(__name__)
Evidence = dict[str, Any]


@dataclass
class Tool:
    name: str
    description: str
    args: dict[str, str]          # arg -> description (all optional strings for the planner schema)
    required: list[str]
    fn: Callable[..., list[Evidence]]
    ttl: float
    needs_model: bool = False


def _ticker(t: str | None) -> str:
    if not t:
        raise ValueError("ticker is required")
    return t.upper().strip().replace(".NS", "")


def _fmt(v: Any) -> str:
    return "–" if v is None else (f"{v:,.1f}" if isinstance(v, float) and abs(v) < 100 else f"{v:,.0f}" if isinstance(v, (int, float)) else str(v))


# ---------------------------------------------------------------------------
def company_overview(ticker: str, **_) -> list[Evidence]:
    t = _ticker(ticker)
    d = pull.dossier(t)
    s, tr = d["snapshot"], d["trajectory"]
    ev = [{
        "title": f"{d['name']}: snapshot", "source": "computed", "cite": f"{t} reported financials ({d['basis']})",
        "url": d["links"].get("screener"),
        "text": (f"Market cap ₹{_fmt(s['market_cap_cr'])} cr; P/E {_fmt(s['pe'])}; ROCE {_fmt(s['roce'])}%; ROE {_fmt(s['roe'])}%; "
                 f"TTM revenue ₹{_fmt(s['revenue_ttm'])} cr, PAT ₹{_fmt(s['pat_ttm'])} cr, margin {_fmt(s['margin_ttm'])}%. "
                 f"Revenue CAGR 3y {_fmt(tr['cagr']['revenue_3y'])}%, 5y {_fmt(tr['cagr']['revenue_5y'])}%; PAT CAGR 3y {_fmt(tr['cagr']['pat_3y'])}%."),
    }]
    flags = [f["text"] for f in tr["flags"] + d["balance_sheet"]["flags"] + d["cash"]["flags"]]
    if flags:
        ev.append({"title": f"{d['name']}: signals in the numbers", "source": "computed", "cite": f"{t} reported financials",
                   "url": d["links"].get("screener"), "text": "; ".join(flags) + "."})
    if d.get("latest_results"):
        r = d["latest_results"]
        rows = "; ".join(f"{m['label']} ₹{_fmt(m['value'])} cr (QoQ {_fmt(m['qoq'])}%, YoY {_fmt(m['yoy'])}%)" for m in r["metrics"])
        ev.append({"title": f"{d['name']}: {r['period']} quarter", "source": "primary", "cite": f"{t} results, {r['period']}",
                   "url": r.get("filing_pdf"), "text": f"{rows}. {r['take']}"})
    return ev


def financials(ticker: str, statement: str | None = None, **_) -> list[Evidence]:
    t = _ticker(ticker)
    c = companies.get(t)
    key = {"quarterly": "quarters", "annual": "annual", "balance_sheet": "balance_sheet", "cash_flow": "cash_flow",
           "ratios": "efficiency"}.get((statement or "quarterly").lower(), "quarters")
    tab = c.get(key)
    if not tab:
        return []
    n = 8 if key == "quarters" else 6
    periods = tab["periods"][-n:]
    lines = [f"{label}: " + ", ".join(f"{p} {_fmt(v)}" for p, v in zip(periods, vals[-n:]))
             for label, vals in tab["rows"].items() if not label.lower().startswith("yoy")]
    return [{"title": f"{c['name']}: {statement or 'quarterly'} figures (₹ cr)", "source": "primary",
             "cite": f"{t} {statement or 'quarterly'} statements", "url": c.get("source_url"), "text": "\n".join(lines)}]


def promises(ticker: str, **_) -> list[Evidence]:
    t = _ticker(ticker)
    L = pull.dossier(t)["ledger"]
    out = []
    for it in L.get("items", [])[:8]:
        status = it["verdict"] if L.get("method") == "model" else "unverified (pattern-matched)"
        out.append({"title": f"Guidance: {it['metric']} {it['promised']} for {it['horizon']}", "source": "primary",
                    "cite": f"{t} {it['promised_in']} call, p.{it['page']}", "url": it.get("url"),
                    "text": f"\"{it['quote'][:300]}\". Status: {status}" + (f"; actual {it['actual']} ({it['window']})" if it.get("actual") is not None else "")})
    return out


def search_transcripts(ticker: str, query: str | None = None, **_) -> list[Evidence]:
    t = _ticker(ticker)
    hits = retrieval.transcript_index(t).search(query or "outlook guidance", k=5)
    return [{"title": f"{t} {h['call']} call: {h.get('speaker') or ''}".strip(), "source": "primary",
             "cite": f"{t} {h['call']} call, p.{h['page']}", "url": h["url"], "text": h["text"][:1100]} for h in hits]


def search_annual_report(ticker: str, query: str | None = None, **_) -> list[Evidence]:
    t = _ticker(ticker)
    hits = retrieval.annual_report_index(t).search(query or "risks", k=4)
    return [{"title": f"{t} {h['call']}", "source": "primary", "cite": f"{t} {h['call']}, p.{h['page']}",
             "url": h["url"], "text": h["text"][:1100]} for h in hits]


def filings(ticker: str | None = None, query: str | None = None, days: str | None = None, **_) -> list[Evidence]:
    from ..intel import classify as C

    d = int(days or 30)
    rows = []
    if ticker:
        t = _ticker(ticker)
        rows = store.ann_for_ticker(t, 40)
        if not rows:  # not a holding: use the company's own recent filings list
            rows = [{"ticker": t, "ts": a["ts"], "title": a["title"], "summary": a["summary"], "url": a["url"]}
                    for a in companies.get(t).get("announcements", [])]
    else:
        rows = store.ann_recent(200)
    from datetime import datetime, timedelta

    from .. import clock

    cutoff = (clock.now() - timedelta(days=d)).isoformat()
    rows = [r for r in rows if (r.get("ts") or "") >= cutoff]
    qt = set(retrieval._tokens(query or ""))
    rows.sort(key=lambda r: (-len(qt & set(retrieval._tokens(f"{r['title']} {r.get('summary') or ''}"))),
                             -C.classify(r["title"], r.get("summary") or "")[2]))
    out = []
    for r in rows[:8]:
        cat, _, base = C.classify(r["title"], r.get("summary") or "")
        out.append({"title": f"{r['ticker']} filing: {C.clean_title(r['title'])}", "source": "primary",
                    "cite": f"{r['ticker']} exchange filing, {r['ts'][:10]}", "url": r.get("url"),
                    "text": f"{r['ts'][:10]} [{C.LABELS.get(cat)}; {C.tier_of(base)}] {r.get('summary') or C.clean_title(r['title'])}"
                            + (" (SIMULATED demo event)" if r.get("source") == "scenario" else "")})
    return out


def price(ticker: str, **_) -> list[Evidence]:
    t = _ticker(ticker)
    m = (prices.fetch_eod([t]).get(t) or {}).get("metrics")
    if not m:
        return []
    return [{"title": f"{t} end-of-day price", "source": "market", "cite": f"{t} EOD {m['date']}", "url": None,
             "text": (f"Close ₹{_fmt(m['close'])} on {m['date']} ({_fmt(m['change_pct'])}% on the day"
                      + (f", {m['sigma']}σ" if m.get("sigma") else "") + f"); volume {m['volume_x']}× its 20-day average; "
                      f"52-week range ₹{_fmt(m['low_52w'])}–₹{_fmt(m['high_52w'])}.")}]


def who_can_help(ticker: str | None = None, theme: str | None = None, event: str | None = None, query: str | None = None, **_) -> list[Evidence]:
    net = graph.get()
    org = net.org_for_ticker(_ticker(ticker)) if ticker else None
    recs = reco.recommend(org, theme, event, query or "", k=4, min_score=0.25)
    return [{"title": f"Network: {r['person']['name']} ({r['person']['headline']})", "source": "network",
             "cite": f"your network: {r['person']['name']}", "url": None, "person": r["person"], "graph": r["graph"],
             "text": f"Match {round(r['score'] * 100)}%. {'; '.join(r['why'])}. Relationship: {r['warmth'].get('type')}, "
                     f"{r['warmth'].get('label')} (last spoke {r['warmth'].get('last_contact')}). Would know: {r['would_know']}."}
            for r in recs]


def route_to_person(person: str, **_) -> list[Evidence]:
    net = graph.get()
    hit = net.resolve(person)
    pid = hit["id"] if hit else None
    if not pid:
        return [{"title": f"No profile for {person}", "source": "network", "cite": "your network", "url": None,
                 "text": f"{person} isn't in your network or the profiles FINN has resolved."}]
    p = net.people[pid]
    paths = reco.with_graphs(net, net.paths_to(pid), pid)
    out = [{"title": f"Profile: {p['name']}", "source": "network", "cite": f"profile: {p['name']}", "url": None,
            "text": f"{p['name']}, {p.get('headline')}. Career: " + "; ".join(
                f"{r['title'].split('→')[-1].strip()} at {net.org_name(r['org'])} ({r['from'][:4]}–{(r.get('to') or 'now')[:4]})" for r in p.get("roles", []))
            + (" [SIMULATED profile]" if p.get("simulated") else "")}]
    for pth in paths:
        via = pth["via"]["name"] if pth.get("via") else "direct"
        out.append({"title": f"Route to {p['name']} via {via}", "source": "network", "cite": f"your network: route via {via}",
                    "url": None, "graph": pth["graph"], "text": f"Strength {round(pth['score'] * 100)}. {'; '.join(pth['why'])}"})
    return out


def web_search(query: str, **_) -> list[Evidence]:
    """Server-side web search (runs on Anthropic's infrastructure). Secondary by definition."""
    if not LLM_ENABLED:
        return []
    text, resp = llm.call(
        "web_search", model=MODEL_WEB, effort="low", max_tokens=1500,
        system="You research for an investment analyst. Report only what the search results say, with dates. "
               "Prefer company, exchange and regulator sources. No opinions.",
        messages=[{"role": "user", "content": f"Search the web and report the 3-5 most relevant findings for: {query}"}],
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
    )
    if not resp:
        return []
    out = []
    for b in resp.content:
        if getattr(b, "type", "") != "text" or not b.text.strip():
            continue
        cites = getattr(b, "citations", None) or []
        url = next((getattr(c, "url", None) for c in cites if getattr(c, "url", None)), None)
        title = next((getattr(c, "title", None) for c in cites if getattr(c, "title", None)), "web result")
        out.append({"title": f"Web: {title}", "source": "secondary", "cite": f"web: {title}", "url": url, "text": b.text.strip()[:900]})
    return out[:5]


PRIMARY_HOSTS = ("bseindia.com", "nseindia.com", "sebi.gov.in", "rbi.org.in")


def read_url(url: str, **_) -> list[Evidence]:
    if not url or not url.startswith("http"):
        return []
    try:
        if url.lower().endswith(".pdf") or "AnnPdfOpen" in url:
            pages = documents.pdf_pages(url, max_pages=12) or []
            body = "\n".join(pages)
        else:
            from bs4 import BeautifulSoup

            html = fetch(url, 3600)
            soup = BeautifulSoup(html, "lxml")
            for x in soup(["script", "style", "nav", "footer", "header"]):
                x.decompose()
            body = re.sub(r"\s+", " ", soup.get_text(" "))
    except FetchError as e:
        return [{"title": f"Couldn't read {url}", "source": "secondary", "cite": url, "url": url, "text": str(e)}]
    primary = any(h in url for h in PRIMARY_HOSTS)
    return [{"title": f"Page: {url[:80]}", "source": "primary" if primary else "secondary", "cite": url, "url": url, "text": body[:2500]}]


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("company_overview", "Headline numbers, growth rates, latest quarter and red/green flags for a company. Start here for 'how is X doing'.",
         {"ticker": "NSE symbol"}, ["ticker"], company_overview, 6 * 3600),
    Tool("financials", "Reported figures for recent periods. statement = quarterly | annual | balance_sheet | cash_flow | ratios.",
         {"ticker": "NSE symbol", "statement": "which statement"}, ["ticker"], financials, 6 * 3600),
    Tool("promises", "What management guided on recent earnings calls and whether it was delivered.",
         {"ticker": "NSE symbol"}, ["ticker"], promises, 6 * 3600),
    Tool("search_transcripts", "Search the last four earnings-call transcripts (management answers paired with analyst questions). Use for 'why', outlook, commentary, guidance, segment colour.",
         {"ticker": "NSE symbol", "query": "what to look for"}, ["ticker", "query"], search_transcripts, 30 * 86400),
    Tool("search_annual_report", "Search the latest annual report (strategy, risks, segments, related parties, contingent liabilities). Slow the first time.",
         {"ticker": "NSE symbol", "query": "what to look for"}, ["ticker", "query"], search_annual_report, 30 * 86400),
    Tool("filings", "Recent exchange filings for a company (or the whole portfolio if no ticker).",
         {"ticker": "NSE symbol (optional)", "query": "filter words (optional)", "days": "look-back days (default 30)"}, [], filings, 600),
    Tool("price", "Latest end-of-day price, day move, volume vs average, 52-week range.",
         {"ticker": "NSE symbol"}, ["ticker"], price, 3600),
    Tool("who_can_help", "Rank people in the investor's network best placed to explain a company, theme or event.",
         {"ticker": "NSE symbol (optional)", "theme": "e.g. specialty chemicals (optional)", "event": "results | M&A | capex | management_change | litigation (optional)",
          "query": "the topic in words (optional)"}, [], who_can_help, 3600),
    Tool("route_to_person", "Profile of a named person and the investor's warmest routes to them.",
         {"person": "full name"}, ["person"], route_to_person, 3600),
    Tool("web_search", "Search the public web. SECONDARY source: use only for context primary sources can't give (very recent events, people's backgrounds, industry structure) or when the user asks.",
         {"query": "search query"}, ["query"], web_search, 6 * 3600, needs_model=True),
    Tool("read_url", "Read a specific web page or PDF (e.g. a filing link found earlier).",
         {"url": "http(s) URL"}, ["url"], read_url, 3600),
]}


def available() -> list[Tool]:
    return [t for t in TOOLS.values() if LLM_ENABLED or not t.needs_model]


def catalog() -> str:
    lines = []
    for t in available():
        args = ", ".join(f"{a}{'' if a in t.required else '?'}: {d}" for a, d in t.args.items())
        lines.append(f"- {t.name}({args}): {t.description}")
    return "\n".join(lines)


def run(name: str, args: dict[str, Any]) -> list[Evidence]:
    t = TOOLS[name]
    clean = {k: v for k, v in args.items() if v not in (None, "", "null") and k in t.args}
    missing = [r for r in t.required if r not in clean]
    if missing:
        raise ValueError(f"{name} needs {', '.join(missing)}")
    # Keyed per user: network tools return *this* investor's contacts.
    key = hashlib.sha1(json.dumps([current_user(), name, clean], sort_keys=True).encode()).hexdigest()
    hit = db.kv_get("toolcache", key, max_age=t.ttl)
    if hit is not None:
        return hit
    out = t.fn(**clean)
    db.kv_put("toolcache", key, out)
    return out
