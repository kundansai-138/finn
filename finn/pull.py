"""Pull: an on-demand research dossier for any NSE ticker, grounded in primary sources.

Order of the dossier is the order of what an investor should look at first:
  1. Promises ledger (guidance vs delivery)    <- the hero
  2. Snapshot, trajectory, balance sheet, cash  <- deterministic from reported financials
  3. Bull vs bear                               <- deep model over the computed facts, or rules
  4. Who in your network can help               <- the Network layer, woven in
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any

from . import clock, companies, db, portfolio
from .context import current_user
from .intel import classify as C
from .intel import financials as F
from .intel import guidance, llm
from .network import graph
from .network import recommend as reco
from .sources import documents, prices

log = logging.getLogger(__name__)
TTL_DOSSIER = 12 * 3600


def _themes_for(c: dict, net: graph.Network) -> list[str]:
    segs = [x.lower() for x in c.get("sector_path", [])]
    # Whole-theme matches only: "consumer" alone would drag bankers into an electronics dossier.
    hits = [t for t in net.themes() if t in segs or (len(t.split()) > 1 and any(t in s for s in segs))]
    h = portfolio.holding(c["ticker"])
    if h and h.get("theme") and h["theme"] not in hits:
        hits.insert(0, h["theme"])
    return hits[:3]


BUSINESS_SCHEMA = {
    "type": "object",
    "properties": {
        "what_it_does": {"type": "string", "description": "2 sentences, plain English"},
        "segments": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "share_pct": {"type": ["number", "null"]}, "note": {"type": "string"}},
            "required": ["name", "share_pct", "note"], "additionalProperties": False}},
        "page": {"type": "integer"},
    },
    "required": ["what_it_does", "segments", "page"],
    "additionalProperties": False,
}


def _business(c: dict) -> dict[str, Any]:
    out: dict[str, Any] = {"sector_path": c.get("sector_path", []), "source": "exchange sector classification"}
    calls = guidance.transcripts(c, 1)
    if not (llm.enabled() and calls):
        return out
    pages = documents.pdf_pages(calls[0]["transcript"]) or []
    opening = "\n\n".join(f"[p.{i + 1}]\n{p}" for i, p in enumerate(pages[:8]) if p.strip())
    res = llm.complete(
        "business_snapshot",
        f"From management's opening remarks in this {c['name']} earnings call ({calls[0].get('period')}), describe what the "
        "company does and its business segments with revenue share if stated. Use only the transcript.",
        tier="fast", schema=BUSINESS_SCHEMA, max_tokens=1500, cache_prefix=opening)
    if res:
        out.update(res)
        out["source"] = f"{calls[0].get('period')} earnings call, p.{res.get('page')}"
    return out


BULLBEAR_SCHEMA = {
    "type": "object",
    "properties": {
        "bull": {"type": "array", "items": {"type": "object", "properties": {"point": {"type": "string"}, "evidence": {"type": "string"}},
                                               "required": ["point", "evidence"], "additionalProperties": False}},
        "bear": {"type": "array", "items": {"type": "object", "properties": {"point": {"type": "string"}, "evidence": {"type": "string"}},
                                               "required": ["point", "evidence"], "additionalProperties": False}},
    },
    "required": ["bull", "bear"],
    "additionalProperties": False,
}


def _bull_bear(c: dict, snap: dict, traj: dict, bs: dict, cash: dict, ledger: dict) -> dict:
    fallback = F.bull_bear_fallback(snap, traj, bs, cash, ledger, F.results_card(c))
    if not llm.enabled():
        return fallback
    facts = {
        "snapshot": snap, "cagr": traj["cagr"], "margins_by_year": dict(zip(traj["years"], traj["margin"])),
        "flags": [f["text"] for f in traj["flags"] + bs["flags"] + cash["flags"]],
        "de_ratio": dict(zip(bs["periods"], bs["de"])), "cfo_pat_5y": cash["conversion_5y"],
        "guidance_score": ledger.get("score"),
        "guidance": [{k: i[k] for k in ("metric", "promised", "horizon", "verdict", "actual", "promised_in")} for i in ledger.get("items", [])[:10]],
    }
    import json

    res = llm.complete(
        "bull_bear",
        f"{c['name']} ({c['ticker']}). Facts computed from reported financials and earnings-call guidance:\n"
        f"{json.dumps(facts, default=str)}\n\nWrite a tight bull case and bear case, 3 points each, for a portfolio manager. "
        "Each point must be supported by a specific number from the facts above, named in 'evidence'. "
        "Use only these facts: no news, no outside knowledge of the company.",
        tier="deep", schema=BULLBEAR_SCHEMA, max_tokens=3000)
    if not res:
        return fallback
    return {**res, "source": "model"}


_build_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


def dossier(ticker: str, force: bool = False) -> dict[str, Any]:
    """Cached per user. Single-flight: if five people open the same cold dossier at once, it's built
    once and the other four wait for that result instead of each re-downloading the transcripts."""
    t = ticker.upper().strip()
    key = f"{current_user()}:{t}"
    if not force:
        hit = db.kv_get("dossier", key, max_age=TTL_DOSSIER)  # per user: weights, people
        if hit:
            return hit
    with _build_locks[key]:
        if not force:
            hit = db.kv_get("dossier", key, max_age=TTL_DOSSIER)  # built while we waited
            if hit:
                return hit
        return _build(t, key)


def _build(t: str, key: str) -> dict[str, Any]:
    c = companies.get(t)
    net = graph.get()
    eod = prices.fetch_eod([t]).get(t) or {}
    snap = F.snapshot(c)
    traj = F.trajectory(c)
    bs = F.balance_sheet(c)
    cash = F.cash_quality(c)
    try:
        ledger = guidance.build_ledger(c)
    except Exception as e:  # a broken transcript must not sink the dossier
        log.warning("ledger %s failed: %s", t, e)
        ledger = {"items": [], "score": {}, "calls": [], "method": "error", "observations": []}
    org = net.org_for_ticker(t)
    themes = _themes_for(c, net)
    if org:
        people = reco.recommend(org, k=5, min_score=0.2)
    else:  # a company outside the network's map: recommend by domain instead
        people = []
        for th in themes:
            seen = {p["person"]["id"] for p in people}
            people += [r for r in reco.recommend(theme=th, text=c["name"], k=3, min_score=0.3) if r["person"]["id"] not in seen]
    filings = []
    for a in c.get("announcements", [])[:8]:
        cat, sub, base = C.classify(a["title"], a.get("summary", ""))
        filings.append({**a, "title": C.clean_title(a["title"]), "category": cat,
                        "category_label": C.LABELS.get(cat), "tier": C.tier_of(base)})
    out = {
        "ticker": t, "name": c["name"], "basis": c.get("basis"), "sector_path": c.get("sector_path"),
        "in_portfolio": t in portfolio.tickers(), "weight": portfolio.weight(t),
        "links": {**c.get("links", {}), "screener": c.get("source_url")},
        "price": eod.get("metrics"),
        "business": _business(c),
        "snapshot": snap, "trajectory": traj, "balance_sheet": bs, "cash": cash,
        "ledger": ledger,
        "bull_bear": _bull_bear(c, snap, traj, bs, cash, ledger),
        "latest_results": F.results_card(c),
        "people": people[:5], "themes": themes,
        "filings": filings,
        "documents": {"annual_reports": c.get("documents", {}).get("annual_reports", [])[:3],
                      "concalls": c.get("documents", {}).get("concalls", [])[:4]},
        "generated_at": clock.now().isoformat(),
        "model": {"enabled": llm.enabled(), "fast": llm.MODEL_FAST, "deep": llm.MODEL_DEEP},
    }
    db.kv_put("dossier", key, out)
    return out
