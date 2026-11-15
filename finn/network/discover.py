"""People outside the investor's network who could help with a cause, found on the public web.

Used when the network has no one strong for the cause ("no one we know understands an OSAT joint
venture at this company"). Every person returned is:

  found     by a server-side web search (basic tool, so each citation carries the source's own
            words in `cited_text`, which is what makes verification possible);
  verified  in code, not by the model: the person's surname must appear in the cited source text of
            the line that names them, and the organisation should too. Lines without a citation,
            or whose citation doesn't contain the name, are dropped. This removes invented or
            conflated names, the main way web people-search goes wrong;
  scored    deterministically (no model in the ranking), with its own weights, because there is no
            relationship to score: only how well the person's role fits the cause;
  labelled  "From the public web, not in your network", with the source link, and an intro route
            if someone the investor knows works or worked at the same organisation.

Privacy: the search query carries only public facts (company, event, filing subject). Contact names
never leave the app. Cost: one Sonnet call with at most 3 searches (~$0.05-0.10), cached 7 days per
cause and shared across users, since the facts are public; intro routes are computed per user after
the cache.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import math
import re
import threading
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

from .. import db
from ..config import LLM_ENABLED, MODEL_WEB
from ..intel import llm
from .graph import EVENT_FN_BOOST, Network, _ym, get as get_network, norm_name
from .recommend import Query, _seniority, cause_topics, role_topic_match

log = logging.getLogger(__name__)

TTL = 7 * 86400
MAX_SEARCHES = 3

# ---------------------------------------------------------------------------
# When is the network not enough?
# ---------------------------------------------------------------------------
def coverage(recs: list[dict]) -> dict[str, Any]:
    """'strong': someone with a cause-specific link (worked there, competitor, or the exact topic);
    'partial': only general domain overlap; 'none': nobody. The web is searched unless strong."""
    strong = [r for r in recs if r["score"] >= 0.8 and (r["features"].get("insider", 0) >= 0.5
              or r["features"].get("adjacent", 0) >= 0.4 or r["features"].get("topic", 0) >= 0.5)]
    if strong:
        return {"level": "strong", "best": strong[0]["person"]["id"], "search_web": False,
                "reason": f"{len(strong)} contact{'s' if len(strong) > 1 else ''} with direct experience"}
    partial = [r for r in recs if r["score"] >= 0.5]
    if partial:
        return {"level": "partial", "best": partial[0]["person"]["id"], "search_web": True,
                "reason": "only general domain overlap in your network"}
    return {"level": "none", "best": None, "search_web": True, "reason": "no one in your network covers this"}


# ---------------------------------------------------------------------------
# Role -> function, organisation name -> org id
# ---------------------------------------------------------------------------
FN_PATTERNS = [  # first match wins
    ("ir", r"investor relations|\bIR\b"),
    ("finance", r"\bcfo\b|chief financial|finance|treasur|controller|\baccounts\b"),
    ("banking", r"investment bank|\bbanker\b"),
    ("legal", r"\blegal\b|general counsel|company secretary"),
    ("regulatory", r"regulatory|quality assurance|\bcompliance\b"),
    ("risk", r"\bcro\b|chief risk|\brisk\b|\bcredit\b"),
    ("strategy", r"strategy|corporate development|\bm&a\b|mergers|business development"),
    ("operations", r"\bcoo\b|chief operating|operations|\bplant\b|manufactur|production|assembly|supply chain|\bosat\b|\bfab\b"),
    ("engineering", r"\bcto\b|chief technology|engineer|technology|\br&d\b"),
    ("sales", r"\bsales\b|commercial|key account"),
    ("marketing", r"marketing|\bbrand\b"),
    ("research", r"analyst|research"),
    ("investing", r"fund manager|portfolio manager|venture|private equity"),
    ("leadership", r"\bceo\b|chief executive|managing director|\bmd\b|chairman|chairperson|founder|president|whole-?time director|executive director"),
]


def infer_function(title: str) -> str | None:
    return next((fn for fn, rx in FN_PATTERNS if re.search(rx, title or "", re.I)), None)


_ORG_NOISE = r"\b(limited|ltd|pvt|private|inc|plc|llp|corporation|corp|co|company|group|the|india|of)\b"


def _org_norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(_ORG_NOISE, " ", re.sub(r"[^a-z0-9& ]", " ", (s or "").lower()))).strip()


def resolve_org(net: Network, name: str) -> str | None:
    n = _org_norm(name)
    if not n:
        return None
    best, best_s = None, 0.0
    for oid, o in net.orgs.items():
        for cand in [o.get("name", ""), *(o.get("aliases") or []), o.get("ticker") or ""]:
            c = _org_norm(cand)
            if not c:
                continue
            if c == n:
                return oid
            s = difflib.SequenceMatcher(None, n, c).ratio()
            if len(c) >= 4 and len(n) >= 4 and (n.startswith(c + " ") or c.startswith(n + " ")):
                s = max(s, 0.9)
            if s > best_s:
                best, best_s = oid, s
    return best if best_s >= 0.86 else None


# ---------------------------------------------------------------------------
# Search + verification
# ---------------------------------------------------------------------------
SYSTEM = (
    "You find people for an investment analyst. Use web search. Report only people who are named in "
    "the search results, one per line, in exactly this format and nothing else:\n"
    "PERSON: <full name> | <exact title> | <organisation> | <current or former, with years if stated> | "
    "<why they would understand this, one clause> | <URL of the search result that names them>\n"
    "At most 6 people. Never guess a name, title or employer: if the results name no one suitable, write NONE. "
    "No introduction and no closing remarks."
)


def _cause_text(company: str | None, event: str | None, topics: set[str], text: str) -> str:
    bits = [company or "", (event or "").replace("_", " "), ", ".join(sorted(topics)), text[:300]]
    return " · ".join(b for b in bits if b)


def _prompt(company: str | None, event: str | None, topics: set[str], text: str) -> str:
    fns = sorted(EVENT_FN_BOOST.get(event or "", {}), key=lambda f: -EVENT_FN_BOOST[event][f]) if event in EVENT_FN_BOOST else []
    asks = []
    if company:
        asks.append(f"executives at {company}" + (f" in {', '.join(fns[:3])}" if fns else "") + " who would know about this")
    if topics:
        asks.append(f"people who run or recently ran {', '.join(sorted(topics))} businesses at other Indian or global companies")
    asks.append("named industry experts quoted on this subject")
    from ..clock import now

    return (f"Today is {now():%B %Y}. The event: {_cause_text(company, event, topics, text)}.\n"
            f"Find people who could explain it to an investor: {'; '.join(asks)}. "
            "Search separately for the company's current leadership and for the subject itself. Prefer people in "
            "their role now and sources from the last two years: company websites, annual reports, exchange filings, "
            "established business press.")


def _lines_with_citations(resp: Any) -> list[tuple[str, list[dict]]]:
    """Reassemble the answer into lines, keeping any citation attached to the text it supports."""
    lines: list[tuple[str, list[dict]]] = []
    buf, cits = "", []
    for b in resp.content:
        if getattr(b, "type", "") != "text":
            continue
        bc = [{"url": getattr(c, "url", None), "title": getattr(c, "title", None) or "",
               "cited_text": getattr(c, "cited_text", None) or ""} for c in (getattr(b, "citations", None) or [])]
        parts = b.text.split("\n")
        for i, part in enumerate(parts):
            buf += part
            cits += bc
            if i < len(parts) - 1:
                lines.append((buf, cits))
                buf, cits = "", []
    if buf.strip():
        lines.append((buf, cits))
    return lines


def _url_key(u: str) -> str:
    p = urlparse((u or "").strip())
    host = p.netloc.lower()
    for pre in ("www.", "ww."):
        if host.startswith(pre):
            host = host[len(pre):]
    return (host + p.path.rstrip("/")).lower()


def _age_years(page_age: str | None) -> float | None:
    m = re.match(r"\s*(\d+)\s+days?\s+ago", page_age or "")
    return round(int(m.group(1)) / 365, 1) if m else None


def _result_urls(resp: Any) -> dict[str, dict]:
    """What the search actually returned: a URL the model names must be one of these."""
    out = {}
    for b in resp.content:
        if getattr(b, "type", "") == "web_search_tool_result" and isinstance(getattr(b, "content", None), list):
            for r in b.content:
                out[_url_key(r.url)] = {"url": r.url, "title": getattr(r, "title", "") or "",
                                        "age_years": _age_years(getattr(r, "page_age", None))}
    return out


_GENERIC_ORG_WORDS = {"limited", "india", "technologies", "technology", "industries", "solutions", "services",
                      "systems", "international", "global", "group", "company", "corporation", "electronics"}


def _surname_tokens(name: str) -> list[str]:
    return [t for t in norm_name(name).split() if len(t) >= 3]


def verify(name: str, org: str, text: str, hosts: str = "") -> dict[str, Any]:
    """Check the claim against the source's own words (cited text or the fetched page), never
    against the model's summary."""
    src = re.sub(r"\s+", " ", text or "").lower()
    toks = _surname_tokens(name)
    surname_ok = bool(toks) and bool(re.search(rf"\b{re.escape(toks[-1])}\b", src))
    first_ok = len(toks) < 2 or bool(re.search(rf"\b{re.escape(toks[0])}\b", src))
    org_toks = [t for t in re.findall(r"[a-z0-9]+", org.lower()) if len(t) >= 4 and t not in _GENERIC_ORG_WORDS]
    org_ok = bool(org_toks) and any(t in src or t in hosts for t in org_toks)
    return {"name": surname_ok, "full_name": surname_ok and first_ok, "org": org_ok}


def _quote(text: str, name: str) -> str:
    """The words around the name in the source: a short pointer, not a reproduction."""
    toks = _surname_tokens(name)
    flat = re.sub(r"\s+", " ", text or "")
    m = re.search(rf"\b{re.escape(toks[-1])}\b", flat, re.I) if toks else None
    if not m:
        return ""
    lo = max(0, m.start() - 90)
    q = flat[lo:m.end() + 70].strip()
    return ("…" if lo else "") + q[:170] + "…"


def page_text(url: str) -> str | None:
    """Fetch the source ourselves (honest user agent, cached). PDFs: first 40 pages."""
    from ..http import fetch
    from ..sources import documents

    try:
        if url.lower().split("?")[0].endswith(".pdf"):
            pages = documents.pdf_pages(url, max_pages=40)
            return "\n".join(pages) if pages else None
        from bs4 import BeautifulSoup

        html = fetch(url, TTL, timeout=20)
        if isinstance(html, bytes):
            html = html.decode("utf-8", "ignore")
        soup = BeautifulSoup(html, "lxml")
        for t in soup(["script", "style", "nav", "footer"]):
            t.decompose()
        return soup.get_text(" ", strip=True)
    except Exception as e:  # blocked, timeout, gone: verification falls back to the result title
        log.info("discover: could not fetch %s: %s", url, e)
        return None


def _split(line: str) -> dict[str, str] | None:
    m = re.search(r"PERSON:\s*(.+)$", line.replace("**", ""))
    if not m:
        return None
    parts = [x.strip() for x in m.group(1).split("|")]
    if len(parts) < 3:
        return None
    url = next((x for x in reversed(parts) if x.startswith("http")), "")
    rest = [x for x in parts if x != url]
    return {"name": rest[0], "title": rest[1], "org": rest[2], "when": rest[3] if len(rest) > 3 else "",
            "why": " | ".join(rest[4:]) if len(rest) > 4 else "", "url": url}


_FORMER = re.compile(r"\bformer|\bex-|\bwas\b|until|retired|stepped down|(19|20)\d\d\s*[-–]\s*(19|20)\d\d", re.I)


def _is_current(when: str, title: str) -> bool:
    """The format asks the model to start with 'current' or 'former'; trust that first word, and
    fall back to cues only when it's missing ('previously CEO' inside a current title is not former)."""
    w = (when or "").strip().lower()
    if w.startswith("current") or w.startswith("since") or w.startswith("from"):
        return True
    if w.startswith("former") or w.startswith("past") or w.startswith("ex"):
        return False
    return not _FORMER.search(when or "")


def _parse(resp: Any) -> tuple[list[dict], dict[str, int]]:
    from concurrent.futures import ThreadPoolExecutor

    stats = {"lines": 0, "url_not_in_results": 0, "unverified": 0, "kept": 0, "fetched": 0, "fetch_failed": 0}
    results = _result_urls(resp)
    rows = []
    for line, cits in _lines_with_citations(resp):
        f = _split(line)
        if not f:
            continue
        stats["lines"] += 1
        res = results.get(_url_key(f["url"])) if f["url"] else None
        if cits:   # the model cited: the cited text is the evidence
            rows.append((f, " ".join(c["cited_text"] + " " + c["title"] for c in cits), cits[0]["url"], res, "citation"))
        elif res:  # no citation: the named URL must be a real search result; read the page ourselves
            rows.append((f, None, res["url"], res, "page"))
        else:
            stats["url_not_in_results"] += 1
    need = list({u for f, txt, u, res, how in rows if txt is None})
    with ThreadPoolExecutor(max_workers=4) as ex:
        pages = dict(zip(need, ex.map(page_text, need)))
    out, seen = [], set()
    for f, txt, url, res, how in rows:
        if txt is None:
            txt = pages.get(url)
            if txt:
                stats["fetched"] += 1
            else:
                stats["fetch_failed"] += 1
                txt, how = (res or {}).get("title", ""), "result title"
        v = verify(f["name"], f["org"], txt, urlparse(url or "").netloc.lower())
        if not v["name"]:
            stats["unverified"] += 1
            continue
        key = norm_name(f["name"])
        if key in seen:
            continue
        seen.add(key)
        age = (res or {}).get("age_years")
        out.append({"name": f["name"], "title": f["title"], "org_name": f["org"], "when": f["when"], "why": f["why"],
                    "current": _is_current(f["when"], f["title"]),
                    "verified": {**v, "how": how},
                    "source": {"url": url, "title": ((res or {}).get("title") or "")[:120], "age_years": age,
                               "quote": _quote(txt, f["name"]) if how != "result title" else ""}})
        stats["kept"] += 1
    return out, stats


def _search(company: str | None, event: str | None, topics: set[str], text: str) -> tuple[list[dict], dict]:
    """One web-search call. Replaced by a stub in tests."""
    _, resp = llm.call(
        "people_discovery", model=MODEL_WEB, effort="low", max_tokens=1800, system=SYSTEM,
        messages=[{"role": "user", "content": _prompt(company, event, topics, text)}],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES}],
    )
    if resp is None:
        return [], {"error": "model unavailable"}
    found, stats = _parse(resp)
    stats["searches"] = getattr(getattr(resp.usage, "server_tool_use", None), "web_search_requests", 0) or 0
    return found, stats


# ---------------------------------------------------------------------------
# Scoring (deterministic; different from the in-network recommender on purpose)
# ---------------------------------------------------------------------------
WEB_WEIGHTS = {"company": 0.30, "topic": 0.25, "function_fit": 0.20, "seniority": 0.10, "evidence": 0.10, "current": 0.05}
WEB_LABEL = {"company": "Works at / next to the company", "topic": "Works on this exact subject",
             "function_fit": "Right function for the event", "seniority": "Seniority",
             "evidence": "Verified in the source", "current": "In the role now"}
MIN_FIT = 0.35


def same_company(a: str | None, b: str | None) -> bool:
    """'Titan Industries Limited (now Titan Company)' is Titan Company: every distinctive word of
    the company's name appears in the candidate's organisation."""
    core = [t for t in _org_norm(a or "").split() if len(t) >= 3 and t not in _GENERIC_ORG_WORDS]
    other = set(_org_norm(b or "").split())
    return bool(core) and all(t in other for t in core)


def score_candidate(net: Network, q: Query, c: dict, company: str | None = None) -> dict[str, Any]:
    oid = resolve_org(net, c["org_name"])
    org = net.orgs.get(q.org or "", {})
    peers = set(org.get("peers", [])) | {o for o, d in net.orgs.items() if q.org and q.org in d.get("peers", [])}
    cust = set(org.get("customers", [])) | set(org.get("suppliers", []))
    f = dict.fromkeys(WEB_WEIGHTS, 0.0)
    if (oid and oid == q.org) or (not q.org and same_company(company, c["org_name"])):
        f["company"] = 1.0 if c["current"] else 0.7
    elif oid in peers:
        f["company"] = 0.6
    elif oid in cust:
        f["company"] = 0.5
    f["topic"] = role_topic_match(net, {"org": oid or "", "title": c["title"]}, cause_topics(q))
    fn = infer_function(c["title"])
    if fn == "leadership":
        f["function_fit"] = 0.7          # the CEO/MD knows any event at their own company
    elif q.event in EVENT_FN_BOOST:
        f["function_fit"] = max(0.0, (EVENT_FN_BOOST[q.event].get(fn or "", 1.0) - 1.0) / 0.3)
    elif fn:
        f["function_fit"] = 0.4
    if f["topic"] >= 1.0:                # runs the subject itself: fits whatever the event type
        f["function_fit"] = max(f["function_fit"], 1.0)
    f["seniority"] = _seniority(c["title"])
    v = c["verified"]
    f["evidence"] = (1.0 if v["org"] and v["full_name"] else 0.7) * (0.6 if v.get("how") == "result title" else 1.0)
    age = (c.get("source") or {}).get("age_years")
    fresh = 1.0 if age is None or age < 1.5 else 0.6 if age < 4 else 0.3   # an old article may describe an old job
    f["current"] = (1.0 if c["current"] else 0.4) * fresh
    contrib = {k: WEB_WEIGHTS[k] * v for k, v in f.items()}
    fit = sum(contrib.values())
    return {"org_id": oid, "function": fn, "features": {k: round(v, 3) for k, v in f.items()}, "fit": round(fit, 3),
            "contributions": [{"feature": k, "label": WEB_LABEL[k], "value": round(v, 2)}
                              for k, v in sorted(contrib.items(), key=lambda kv: -kv[1]) if v > 0.02]}


def intro_routes(net: Network, oid: str | None, limit: int = 2) -> list[dict]:
    """Contacts who work or worked at the same organisation: the warm way in."""
    if not oid:
        return []
    out = []
    for pid, p in net.contacts.items():
        for r in p.get("roles", []):
            if r["org"] != oid:
                continue
            w = net.warmth(pid)
            since = 0.0 if not r.get("to") else max(0.0, _ym(None) - _ym(r["to"], end=True))
            s = w["score"] * (1.0 if not r.get("to") else 0.6 * math.exp(-since / 5))
            out.append({"id": pid, "name": p["name"], "warmth": w, "score": round(s, 2),
                        "relation": "works there now" if not r.get("to") else f"worked there until {str(r['to'])[:4]}"})
            break
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


def cache_key(company: str | None, event: str | None, topics: set[str], text: str) -> str:
    return hashlib.sha1(json.dumps([company, event, sorted(topics), (text or "")[:160]]).encode()).hexdigest()[:16]


def discover(org: str | None = None, event: str | None = None, text: str = "", theme: str | None = None,
             company: str | None = None, k: int = 4, force: bool = False) -> dict[str, Any]:
    """Public-web people for a cause, verified, scored and checked against the user's network."""
    net = get_network()
    q = Query(org=org, theme=theme.lower() if theme else None, event=event, text=text or "")
    company = company or (net.org_name(org) if org else None)
    topics = cause_topics(q)
    if not LLM_ENABLED:
        return {"status": "unavailable", "reason": "Web search needs a model key.", "candidates": [], "cached": False}
    key = cache_key(company, event, topics, text)
    hit = None if force else db.kv_get("discover", key, max_age=TTL)
    cached = hit is not None
    if hit is None:
        with _locks[key]:
            hit = None if force else db.kv_get("discover", key, max_age=TTL)
            cached = hit is not None
            if hit is None:
                found, stats = _search(company, event, topics, text)
                hit = {"found": found, "stats": stats}
                if "error" not in stats:
                    db.kv_put("discover", key, hit)
    out, already = [], []
    for c in hit["found"]:
        s = score_candidate(net, q, c, company)
        match = net.resolve(c["name"], org_hint=s["org_id"])
        if match and match["id"] in net.contacts:
            already.append({"name": c["name"], "id": match["id"]})
            continue
        if s["fit"] < MIN_FIT:
            continue
        intros = intro_routes(net, s["org_id"])
        out.append({**c, **s, "intro": intros, "known_profile": match["id"] if match else None,
                    "rank_score": round(s["fit"] + 0.08 * (intros[0]["score"] if intros else 0), 3),
                    "label": "From the public web · not in your network"})
    out.sort(key=lambda x: -x["rank_score"])
    return {"status": "ok" if "error" not in hit["stats"] else "error", "query": {"org": org, "event": event, "text": text[:200],
            "company": company, "topics": sorted(topics)}, "candidates": out[:k], "already_in_network": already,
            "stats": hit["stats"], "cached": cached}


def people_for_cause(org: str | None = None, event: str | None = None, text: str = "", theme: str | None = None,
                     k: int = 4, mode: str = "pull", web: bool = True) -> dict[str, Any]:
    """The whole flow: rank the network for the cause; if no one is strong, go to the web."""
    from . import recommend as reco

    recs = reco.recommend(org, theme=theme, event=event, text=text, k=k, mode=mode)
    cov = coverage(recs)
    out = {"network": recs, "coverage": cov, "web": None}
    if web and cov["search_web"]:
        out["web"] = discover(org=org, event=event, text=text, theme=theme)
    return out
