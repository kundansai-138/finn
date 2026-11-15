"""The ask bar: one input for Pull and for direct Network questions.

The model (when available) only *parses* the question into an intent; deterministic tools answer
it. That keeps answers grounded and cheap, and the same router works with no model at all.
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from . import portfolio, store
from .intel import llm
from .network import graph
from .network import recommend as reco

NETWORK_CUE = re.compile(r"\b(who|whom|intro|introduce|connect|expert|go-?to|call|reach|knows?|network|people|contact)\b", re.I)
NEW_ROLE = re.compile(r"\bnew\s+(cfo|ceo|md|chief|managing director|head)\b", re.I)


def _companies(net: graph.Network) -> dict[str, str]:
    """lowercased name/alias/ticker -> ticker"""
    out: dict[str, str] = {}
    for h in portfolio.holdings() + portfolio.research():
        out[h["ticker"].lower()] = h["ticker"]
        out[h["name"].lower()] = h["ticker"]
    for o in net.orgs.values():
        if o.get("ticker"):
            out[o["name"].lower()] = o["ticker"]
            out[o["ticker"].lower()] = o["ticker"]
            for a in o.get("aliases", []):
                out[a.lower()] = o["ticker"]
    return out


def _find_company(q: str, net: graph.Network) -> str | None:
    names = _companies(net)
    ql = q.lower()
    # Longest phrase match first ("asian paints" before "paints")
    for name in sorted(names, key=len, reverse=True):
        if len(name) >= 3 and re.search(rf"\b{re.escape(name)}\b", ql):
            return names[name]
    for tok in re.findall(r"\b[A-Z][A-Z0-9&]{2,14}\b", q):
        if tok.lower() in names:
            return names[tok.lower()]
    return None


def _find_theme(q: str, net: graph.Network) -> str | None:
    ql = q.lower()
    themes = net.themes()
    for t in sorted(themes, key=len, reverse=True):
        if t in ql:
            return t
    words = re.findall(r"[a-z]{4,}", ql)
    best = None, 0.0
    for t in themes:
        for w in words:
            s = difflib.SequenceMatcher(None, w, t.split()[0]).ratio()
            if s > best[1]:
                best = t, s
    return best[0] if best[1] >= 0.85 else None


def _find_person(q: str, net: graph.Network) -> str | None:
    for pid, p in net.people.items():
        if pid == "you":
            continue
        first, *rest = p["name"].lower().split()
        if p["name"].lower() in q.lower() or (rest and f"{first} {rest[-1]}" in q.lower()):
            return pid
    return None


INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["company_research", "who_knows_company", "path_to_person", "expertise", "unknown"]},
        "company": {"type": ["string", "null"]},
        "theme": {"type": ["string", "null"]},
        "person": {"type": ["string", "null"]},
        "role": {"type": ["string", "null"], "description": "e.g. CFO, CEO, MD when asking about a newly appointed person"},
    },
    "required": ["intent", "company", "theme", "person", "role"],
    "additionalProperties": False,
}


def ask(q: str) -> dict[str, Any]:
    q = (q or "").strip()
    net = graph.get()
    if not q:
        return {"type": "empty"}

    ticker = _find_company(q, net)
    person = _find_person(q, net)
    theme = _find_theme(q, net)
    networky = bool(NETWORK_CUE.search(q))

    if llm.enabled() and len(q.split()) > 3 and not (ticker or person):
        parsed = llm.complete(
            "ask_intent",
            f"Investor's question: {q!r}\nKnown themes: {', '.join(net.themes())}.\nClassify the intent and extract entities.",
            tier="fast", schema=INTENT_SCHEMA, max_tokens=300)
        if parsed:
            ticker = ticker or (_find_company(parsed.get("company") or "", net) if parsed.get("company") else None)
            theme = theme or (parsed.get("theme") if parsed.get("theme") in net.themes() else None)
            networky = networky or parsed["intent"] in ("who_knows_company", "path_to_person", "expertise")

    # "Who can introduce me to the new CFO of Deepak Nitrite?"
    if ticker and NEW_ROLE.search(q):
        rows = store.ann_for_ticker(ticker, 5, category="management_change")
        import json

        for r in rows:
            pid = json.loads(r["extra"] or "{}").get("person")
            if pid and pid in net.people:
                person = pid
                break
    if person:
        paths = reco.with_graphs(net, net.paths_to(person), person)
        p = net.people[person]
        if person in net.contacts:
            return {"type": "person", "id": person, "answer": f"{p['name']} is already in your network."}
        best = paths[0] if paths else None
        ans = (f"Best route to {p['name']}: via {best['via']['name']}." if best and best.get("via")
               else f"No warm path to {p['name']} yet." if not best else "You may have crossed paths directly.")
        return {"type": "paths", "person": net.person_full(person), "paths": paths, "answer": ans}

    if ticker and networky:
        org = net.org_for_ticker(ticker)
        people = reco.recommend(org, text=q, k=5, min_score=0.2) if org else []
        name = net.org_name(org) if org else ticker
        ans = f"{len(people)} people in your network can help on {name}." if people else f"No one in your network is close to {name} yet."
        return {"type": "who_for_company", "ticker": ticker, "company": name, "people": people, "answer": ans}

    if theme and (networky or not ticker):
        ex = reco.recommend(theme=theme, text=q, k=6, min_score=0.3)
        return {"type": "expertise", "theme": theme, "people": ex,
                "answer": f"Your go-to people for {theme}: " + ", ".join(e["person"]["name"] for e in ex[:3]) + "." if ex
                else f"No one in your network has worked in {theme}."}

    if ticker:
        return {"type": "company", "ticker": ticker}

    # Unknown token that looks like a ticker: Pull works for any NSE company.
    m = re.fullmatch(r"\s*([A-Za-z][A-Za-z0-9&]{1,14})\s*", q)
    if m:
        return {"type": "company", "ticker": m.group(1).upper(), "unverified": True}

    return {"type": "help", "answer": "Try a ticker (DIXON), a company, a theme (“who knows specialty chemicals?”), or a person."}
