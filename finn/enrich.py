"""Push enrichment: what the public web adds to a filing, verified, for the items worth a push.

A filing says "Appointment of Chief Financial Officer"; the investor's next questions are who is
this, where were they before, and do I know anyone who worked with them. This module answers those
with one web-search call per filing, then checks every line in code before showing it:

  FACT lines    the named URL must be one the search returned; the page is fetched and every number
                in the fact must appear on it (no numbers: most of its content words must). A fact
                that fails is dropped, not softened.
  PERSON lines  the surname must appear on the source page (as in discover.py); prior employers are
                matched to the investor's network to find who overlapped with them.

Budget: material and critical portfolio items only, scenario (simulated) items never, at most
`MAX_PER_RUN` per refresh and a daily dollar cap (FINN_WEB_BUDGET_USD, default $0.50). Results are
cached forever per filing (a filing doesn't change), so the brief reads them for free. Everything
from here is labelled secondary: the filing remains the primary source.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

from . import db, store
from .config import LLM_ENABLED, MODEL_WEB
from .intel import classify as C
from .intel import llm
from .network import discover as D
from .network.graph import get as get_network

log = logging.getLogger(__name__)

MAX_PER_RUN = 6
FORMAT = 3   # bump when the cached shape changes: older entries are refetched, never shown half-formed
DAILY_BUDGET = float(os.getenv("FINN_WEB_BUDGET_USD", "0.50"))
PURPOSE = "push_enrich"

ASK = {
    "management_change": "who the incoming and outgoing people are, their previous roles with years, and their track record",
    "M&A": "who the counterparty is, its size, the reported deal value and the stated rationale",
    "capex": "the project's size, capacity, timeline and how it is funded",
    "fund_raise": "the size, pricing, discount and named investors",
    "credit_rating": "the agency, the rating action and its stated rationale",
    "litigation": "what the case or regulatory observations are about and their history",
    "results": "how the numbers compared with expectations and how the stock reacted",
    "pledging": "who pledged, how much of their holding, and why",
}

SYSTEM = (
    "You add context to a stock-exchange filing for an investor. Use web search. Report only what the search "
    "results state, in exactly these line formats and nothing else:\n"
    "FACT: <one sentence the filing itself does not say, with numbers and dates where given> | <URL of the search result>\n"
    "PERSON: <full name> | <title in this event> | <organisation> | <their role in this event> | "
    "<previous roles as 'title at organisation (from-to)', separated by ;> | <URL of the search result>\n"
    "At most 4 FACT lines and 2 PERSON lines. Never infer or guess. If the results add nothing, write NONE. "
    "No introduction and no closing remarks."
)


def _prompt(item: dict) -> str:
    from .clock import now

    ask = ASK.get(item.get("category") or "", "what it means and any numbers reported")
    return (f"Today is {now():%d %B %Y}. Filing by {item['company']} ({item['ticker']}) on {str(item['ts'])[:10]}: "
            f"{item['title']}.\nFiling text: {(item.get('summary') or '')[:800]}\n"
            f"Find what an investor needs beyond the filing: {ask}. Prefer the company, exchanges, rating agencies and "
            "established business press; prefer the last 12 months.")


_STOP = {"which", "their", "there", "these", "those", "about", "after", "before", "while", "would", "could", "company",
         "limited", "announced", "reported", "according", "filing", "india", "indian"}


def _numbers(s: str) -> list[str]:
    """Numbers as they'd be written on the page: '1,200' -> '1200', '4.5%' -> '4.5'."""
    return [n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", s) if len(n.replace(",", "")) >= 2]


def verify_fact(fact: str, page: str) -> dict[str, Any]:
    flat = re.sub(r"\s+", " ", page or "").lower().replace(",", "")
    nums = _numbers(fact)
    if nums:
        missing = [n for n in nums if n not in flat]
        return {"ok": not missing, "how": "numbers", "missing": missing}
    words = [w for w in re.findall(r"[a-z]{5,}", fact.lower()) if w not in _STOP]
    hit = sum(w in flat for w in words)
    return {"ok": bool(words) and hit / len(words) >= 0.6, "how": "words", "coverage": round(hit / max(1, len(words)), 2)}


ROLE_RX = re.compile(r"^(?P<title>.+?)\s+at\s+(?P<org>.+?)(?:\s*\((?P<from>(?:19|20)\d\d)?\s*[-–to ]*\s*(?P<to>(?:19|20)\d\d|present|now)?\))?\s*$", re.I)


def _prior_roles(s: str) -> list[dict]:
    out = []
    for part in [x.strip() for x in (s or "").split(";") if x.strip()]:
        m = ROLE_RX.match(part)
        if m:
            to = m["to"]
            org, frm = m["org"], m["from"]
            since = re.search(r"\s*\((?:from|since)\s+((?:19|20)\d\d)(?:\s+onwards)?\)\s*$", org, re.I)
            if since:                                   # "Tata Power (from 2018)"
                org, frm = org[:since.start()], frm or since.group(1)
            org = re.sub(r"\s*\((?![^)]*\d{4})[^)]*\)\s*$", "", org).strip()   # drop "(to date)", "(years not stated)"
            out.append({"title": m["title"].strip(), "org_name": org, "from": frm,
                        "to": None if (to or "").lower() in ("present", "now") else to})
    return out[:5]


PRIMARY_HOSTS = ("nseindia.com", "bseindia.com", "sebi.gov.in", "rbi.org.in", "crisil.com", "icra.in", "careratings.com",
                 "indiaratings.co.in", "careedge.in")
PRESS_HOSTS = ("economictimes.indiatimes.com", "business-standard.com", "livemint.com", "moneycontrol.com", "reuters.com",
               "bloomberg.com", "thehindubusinessline.com", "financialexpress.com", "cnbctv18.com", "ndtvprofit.com",
               "thehindu.com", "ft.com", "wsj.com", "businesstoday.in", "fortuneindia.com")
TIER_RANK = {"primary": 0, "company": 0, "press": 1, "other": 2}


def source_tier(url: str, company_hint: str = "") -> str:
    """How much to trust a source: exchange/regulator/rating agency, established business press, or other."""
    host = urlparse(url or "").netloc.lower()
    if any(host == h or host.endswith("." + h) for h in PRIMARY_HOSTS):
        return "primary"
    if any(host == h or host.endswith("." + h) for h in PRESS_HOSTS):
        return "press"
    return "other"


def _split(line: str) -> tuple[str, list[str]] | None:
    m = re.search(r"\b(FACT|PERSON):\s*(.+)$", line.replace("**", ""))
    return (m.group(1), [x.strip() for x in m.group(2).split("|")]) if m else None


def _parse(resp: Any) -> tuple[dict, dict]:
    from concurrent.futures import ThreadPoolExecutor

    results = D._result_urls(resp)
    stats = {"facts": 0, "facts_kept": 0, "people": 0, "people_kept": 0, "url_not_in_results": 0, "unverified": 0}
    rows = []
    for line, _cits in D._lines_with_citations(resp):
        sp = _split(line)
        if not sp:
            continue
        kind, parts = sp
        url = next((x for x in reversed(parts) if x.startswith("http")), "")
        fields = [x for x in parts if x != url]
        stats["facts" if kind == "FACT" else "people"] += 1
        res = results.get(D._url_key(url)) if url else None
        if not res:
            stats["url_not_in_results"] += 1
            continue
        rows.append((kind, fields, res))
    urls = list({r[2]["url"] for r in rows})
    with ThreadPoolExecutor(max_workers=4) as ex:
        pages = dict(zip(urls, ex.map(D.page_text, urls)))
    facts, people = [], []
    for kind, fields, res in rows:
        page = pages.get(res["url"])
        how = "page" if page else "result title"
        text = page or res["title"]
        src = {"url": res["url"], "title": res["title"][:120], "age_years": res["age_years"], "tier": source_tier(res["url"])}
        if kind == "FACT":
            v = verify_fact(fields[0], text)
            if not v["ok"]:
                stats["unverified"] += 1
                continue
            facts.append({"text": fields[0], "source": src, "verified": {**v, "against": how}})
            stats["facts_kept"] += 1
        else:
            if len(fields) < 3:
                continue
            name, title, org = fields[0], fields[1], fields[2]
            v = D.verify(name, org, text, urlparse(res["url"]).netloc.lower())
            if not v["name"]:
                stats["unverified"] += 1
                continue
            people.append({"name": name, "title": title, "org_name": org,
                           "event_role": fields[3] if len(fields) > 3 else "",
                           "prior": _prior_roles(fields[4] if len(fields) > 4 else ""),
                           "verified": {**v, "how": how}, "source": {**src, "quote": D._quote(text, name) if page else ""}})
            stats["people_kept"] += 1
    facts.sort(key=lambda f: TIER_RANK[f["source"]["tier"]])
    return {"facts": facts, "people": people}, stats


# Trusted first. Most Indian business dailies (Economic Times, Mint, Moneycontrol, BusinessLine, Financial
# Express, CNBC-TV18, NDTV Profit) and Reuters, FT and WSJ block Anthropic's crawler, so the search tool
# refuses them (400) and can never cite them. What's left that an investor would trust: exchanges,
# regulators, rating agencies, and the business press that allows it. Measured on a Tata Power CEO change:
# trusted-only kept 3/3 facts, all primary or press, in 12 s; the open web kept 4 facts, all from blogs, in 29 s.
TRUSTED_DOMAINS = list(PRIMARY_HOSTS) + ["business-standard.com", "bloomberg.com", "businesstoday.in"]


def _one(item: dict, domains: list[str] | None) -> tuple[dict, dict]:
    tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}
    if domains:
        tool["allowed_domains"] = domains
    _, resp = llm.call(PURPOSE, model=MODEL_WEB, effort="low", max_tokens=1600, system=SYSTEM,
                       messages=[{"role": "user", "content": _prompt(item)}], tools=[tool])
    if resp is None:
        return {"facts": [], "people": []}, {"error": "model unavailable"}
    out, stats = _parse(resp)
    stats["searches"] = getattr(getattr(resp.usage, "server_tool_use", None), "web_search_requests", 0) or 0
    return out, stats


def _search(item: dict) -> tuple[dict, dict]:
    """Trusted sources first; the open web only if they add little. Replaced by a stub in tests."""
    out, stats = _one(item, TRUSTED_DOMAINS)
    stats["pass"] = "trusted"
    if "error" not in stats and len(out["facts"]) + len(out["people"]) >= 2:
        return out, stats
    more, st2 = _one(item, None)
    if "error" in st2:
        return (out, stats) if "error" not in stats else (more, st2)
    seen_f = {f["text"].lower()[:60] for f in out["facts"]}
    seen_p = {D.norm_name(p["name"]) for p in out["people"]}
    out["facts"] += [f for f in more["facts"] if f["text"].lower()[:60] not in seen_f]
    out["people"] += [p for p in more["people"] if D.norm_name(p["name"]) not in seen_p]
    out["facts"].sort(key=lambda f: TIER_RANK[f["source"]["tier"]])
    merged = {k: (stats.get(k, 0) + st2.get(k, 0)) for k in set(stats) | set(st2) if k not in ("pass", "error")}
    return out, {**merged, "pass": "trusted+open" if "error" not in stats else "open"}


def connect(web: dict) -> dict:
    """Per user, after the (shared) cache: who in the investor's network can vouch for these people."""
    net = get_network()
    for p in web.get("people", []):
        match = net.resolve(p["name"])
        p["in_network"] = match["id"] if match and match["id"] in net.contacts else None
        routes: dict[str, dict] = {}
        for r in p.get("prior", []) + [{"org_name": p["org_name"], "from": None, "to": None}]:
            oid = D.resolve_org(net, r["org_name"])
            for x in D.intro_routes(net, oid, limit=2):
                if x["id"] not in routes or x["score"] > routes[x["id"]]["score"]:
                    routes[x["id"]] = {**x, "via_org": net.org_name(oid)}
        p["routes"] = sorted(routes.values(), key=lambda x: -x["score"])[:2]
    return web


def eligible(item: dict) -> bool:
    return (item.get("source") != "scenario" and (item.get("score") or 0) >= C.MATERIAL)


def enrich(item: dict, force: bool = False) -> dict[str, Any] | None:
    """Web context for one brief item, cached forever per filing."""
    if not LLM_ENABLED:
        return None
    hit = None if force else db.kv_get("enrich", item["id"])
    if hit is not None and hit.get("v") != FORMAT:
        hit = None
    if hit is None:
        if not eligible(item):
            return None
        found, stats = _search(item)
        hit = {**found, "stats": stats, "at": time.time(), "label": "From the web · secondary to the filing", "v": FORMAT}
        if "error" not in stats:
            db.kv_put("enrich", item["id"], hit)
    return connect(hit)


def spent_today() -> float:
    from .clock import now

    start = now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return store.spend_since(start, [PURPOSE])


def enrich_pending(items: list[dict], limit: int = MAX_PER_RUN) -> dict[str, Any]:
    """Called after a refresh: enrich the most important new items, within budget."""
    todo = [i for i in sorted(items, key=lambda x: -(x.get("score") or 0)) if eligible(i)]
    have = store.kv_get_many("enrich", [i["id"] for i in todo])
    todo = [i for i in todo if (have.get(i["id"]) or {}).get("v") != FORMAT][:limit]
    done, skipped = 0, 0
    for i in todo:
        if spent_today() >= DAILY_BUDGET:
            skipped = len(todo) - done
            break
        try:
            if enrich(i) is not None:
                done += 1
        except Exception as e:  # enrichment must never break the refresh
            log.warning("enrich %s failed: %s", i["id"], e)
    return {"enriched": done, "skipped_budget": skipped, "spent_today_usd": round(spent_today(), 4)}


def attach(items: list[dict]) -> None:
    """Cheap: attach whatever is already cached to brief items (one query)."""
    have = store.kv_get_many("enrich", [i["id"] for i in items])
    if not have:
        return
    for i in items:
        if (have.get(i["id"]) or {}).get("v") == FORMAT:
            i["web"] = connect(have[i["id"]])
