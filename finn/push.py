"""Push: the brief.

refresh()  pulls EOD prices + announcements (screener per company, NSE RSS exchange-wide,
           simulated scenarios) into SQLite and classifies them. Run it on a schedule.
brief()    builds the finite, ranked brief for a time window and signal level, enriching each
           material item with what it means and who in the network can explain it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from . import clock, companies, db, portfolio, store
from .config import DATA, INCLUDE_SCENARIOS
from .intel import classify as C
from .intel import financials, llm
from .network import graph
from .network import recommend as reco
from .sources import nse_rss, prices

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Refresh (the scheduled job)
# ---------------------------------------------------------------------------

def _ann_id(ticker: str, url: str | None, title: str, ts: str) -> str:
    return hashlib.sha1(f"{ticker}|{url or title}|{ts[:16]}".encode()).hexdigest()[:16]


def _store(items: list[dict]) -> int:
    rows = {}
    for it in items:
        cat, sub, base = C.classify(it["title"], it.get("summary", ""))
        aid = it.get("id") or _ann_id(it["ticker"], it.get("url"), it["title"], it["ts"])
        rows[aid] = {"id": aid, "ticker": it["ticker"], "ts": it["ts"], "title": C.clean_title(it["title"]),
                     "summary": it.get("summary"), "url": it.get("url"), "source": it["source"], "category": cat,
                     "subtype": sub, "score": base, "extra": json.dumps(it.get("extra") or {})}
    return store.ann_upsert(list(rows.values()))


def load_scenarios() -> list[dict]:
    with open(DATA / "scenarios.json", encoding="utf-8") as f:
        evs = json.load(f)["events"]
    now = clock.now()
    out = []
    for e in evs:
        ts = (now - timedelta(hours=e["hours_ago"])).replace(microsecond=0)
        out.append({"id": e["id"], "ticker": e["ticker"], "ts": ts.isoformat(), "title": e["title"],
                    "summary": e["summary"], "url": None, "source": "scenario",
                    "extra": {k: e[k] for k in ("person", "amount_cr") if k in e}})
    return out


def refresh(include_prices: bool = True, verbose: bool = False) -> dict[str, Any]:
    tickers = portfolio.tickers()
    stats: dict[str, Any] = {"companies": 0, "announcements": 0, "rss": 0, "errors": []}
    if include_prices:
        eod = prices.fetch_eod(tickers + [r["ticker"] for r in portfolio.research()])
        stats["prices"] = len(eod)
    for t in tickers + [r["ticker"] for r in portfolio.research()]:
        try:
            c = companies.get(t)
            stats["companies"] += 1
            if t in tickers:
                items = [{"ticker": t, "ts": a["ts"], "title": a["title"], "summary": a["summary"],
                          "url": a["url"], "source": "screener"} for a in c.get("announcements", []) if a.get("ts")]
                stats["announcements"] += _store(items)
            if verbose:
                print(f"  {t}: ok")
        except Exception as e:  # one bad ticker must not sink the refresh
            stats["errors"].append(f"{t}: {e}")
            log.warning("refresh %s failed: %s", t, e)
    rss = [dict(x, source="nse_rss") for x in nse_rss.fetch_items(set(tickers))]
    stats["rss"] = _store(rss)
    # Scenarios are re-anchored to "now" on every refresh so the demo always has them in-window.
    store.ann_delete_source("scenario")
    if INCLUDE_SCENARIOS:
        _store(load_scenarios())
    db.kv_put("meta", "last_refresh", {"at": clock.now().isoformat(), **stats})
    return stats


def poll_rss() -> int:
    """Cheap, frequent: NSE's exchange-wide feed, filtered to tracked tickers."""
    tracked = set(portfolio.tickers())
    return _store([dict(x, source="nse_rss") for x in nse_rss.fetch_items(tracked)])


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------

WINDOWS = {"24h": 24, "3d": 72, "7d": 168, "30d": 720}


def _window_hours(window: str) -> float:
    if window in WINDOWS:
        return WINDOWS[window]
    m = re.match(r"(\d+(?:\.\d+)?)\s*([hd])$", window or "")
    if m:
        return float(m.group(1)) * (24 if m.group(2) == "d" else 1)
    return 24


def _dedupe(rows: list[dict]) -> list[dict]:
    """The same filing arrives from BSE (via screener) and NSE RSS; keep one per ticker/category/45 min."""
    rows = sorted(rows, key=lambda r: (r["ticker"], r["category"], r["ts"]))
    out: list[dict] = []
    for r in rows:
        if out and out[-1]["ticker"] == r["ticker"] and out[-1]["category"] == r["category"] \
                and out[-1]["subtype"] == r["subtype"] and abs(_ts(r["ts"]) - _ts(out[-1]["ts"])) < 45 * 60:
            if r["source"] == "screener":  # BSE text tends to be richer
                out[-1] = {**r, "also_on": out[-1]["source"]}
            continue
        out.append(r)
    return out


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


_APPOINTEE = re.compile(r"appoint(?:ment|ed)? of (?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Shri|Smt\.?)?\s*([A-Z][a-zA-Z\.]+(?:\s[A-Z][a-zA-Z\.]+){1,3})\s+as", re.I)


def _people_line(org: str | None, category: str, text: str = "") -> list[dict]:
    """Recommender call: who in the network is best placed to explain this filing."""
    if not org:
        return []
    return reco.recommend(org, event=category, text=text, k=2, min_score=0.5)


def _one_liner(r: dict) -> str | None:
    s = (r.get("summary") or "").strip()
    s = re.sub(r"^(Announcement under Regulation 30 of SEBI.*?(Regulations?,? 2015)?[,.]?\s*)", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s)
    if llm.enabled() and r["tier"] != "routine" and len(s) > 40 and r["source"] != "scenario":
        out = llm.complete(
            "one_liner",
            f"Company: {r['company']}. Exchange filing subject: {r['title']}. Filing text: {s[:1500]}\n\n"
            "In at most 22 words, say what happened, with any number that matters. No preamble, no adjectives.",
            tier="fast", max_tokens=120)
        if out:
            return out.strip()
    return (s[:220] + "…") if len(s) > 220 else (s or None)


def brief(window: str = "24h", level: str = "material") -> dict[str, Any]:
    rules = portfolio.rules()
    hours = _window_hours(window)
    now = clock.now()
    since = (now - timedelta(hours=hours)).isoformat()
    held = {h["ticker"]: h for h in portfolio.holdings()}
    rows = store.ann_since(since, held)
    rows = [r for r in rows if r["ticker"] in held and (rules["include_simulated"] or r["source"] != "scenario")]
    rows = _dedupe(rows)

    eod = prices.fetch_eod(list(held))
    net = graph.get()
    watched, muted = set(rules["watch"]), set(rules["mute"])

    items: list[dict] = []
    for r in rows:
        t = r["ticker"]
        m = (eod.get(t) or {}).get("metrics") or {}
        same_day = m.get("date") == r["ts"][:10]
        org = net.org_for_ticker(t)
        hits = len(_people_line(org, r["category"], f"{r['title']} {r.get('summary') or ''}")) if org else 0
        v = C.score(r["category"], r["subtype"], int(r["score"]), weight=held[t]["weight"], watched=watched,
                    muted=muted, price_move_pct=m.get("change_pct") if same_day else None, network_hits=hits)
        extra = json.loads(r.get("extra") or "{}")
        items.append({
            "id": r["id"], "ticker": t, "company": held[t]["name"], "ts": r["ts"], "title": r["title"],
            "summary": r.get("summary"), "url": r.get("url"), "source": r["source"],
            "simulated": r["source"] == "scenario", "category": r["category"],
            "category_label": C.LABELS.get(r["category"], r["category"]), "subtype": r["subtype"],
            "score": v.score, "tier": v.tier, "why_ranked": v.reasons,
            "alert": r["category"] in watched and v.tier != "routine", "extra": extra, "org": org,
        })

    # Price / volume alerts from end-of-day data
    alerts = []
    for t, s in eod.items():
        if t not in held:
            continue
        m = s.get("metrics") or {}
        if not m:
            continue
        reasons = []
        if abs(m.get("change_pct") or 0) >= rules["price_move_pct"]:
            reasons.append(f"{m['change_pct']:+.1f}% on the day" + (f" ({abs(m['sigma']):.1f}σ)" if m.get("sigma") else ""))
        if (m.get("volume_x") or 0) >= rules["volume_x"]:
            reasons.append(f"volume {m['volume_x']:.1f}× its 20-day average")
        if not reasons:
            continue
        sc = min(95, 62 + abs(m.get("change_pct") or 0) * 3 + max(0, (m.get("volume_x") or 0) - rules["volume_x"]) * 6)
        related = [i for i in items if i["ticker"] == t and i["tier"] != "routine"]
        alerts.append({
            "id": f"px-{t}-{m['date']}", "ticker": t, "company": held[t]["name"], "kind": "price_volume",
            "ts": m["date"], "title": " and ".join(reasons).capitalize(), "metrics": m, "score": round(sc, 1),
            "tier": C.tier_of(sc), "explained_by": related[0]["title"] if related else None,
            "unexplained": not related,
        })
    alerts.sort(key=lambda a: -a["score"])

    # Enrich what will actually be shown
    lvl_min = {"critical": C.CRITICAL, "material": C.MATERIAL, "all": 0}.get(level, C.MATERIAL)
    shown = _group([i for i in items if i["score"] >= max(lvl_min, C.MATERIAL)])
    # Swiped away ("handled") on any device: out of the brief, but counted, so it still ends.
    done_ids = store.dismissed_ids()
    done = [i for i in shown if i["id"] in done_ids]
    shown = [i for i in shown if i["id"] not in done_ids]
    alerts_done = [a for a in alerts if a["id"] in done_ids]
    alerts = [a for a in alerts if a["id"] not in done_ids]
    for i in shown:
        _enrich(i, net)
    shown.sort(key=lambda i: -i["score"])
    routine = [i for i in items if i["score"] < C.MATERIAL]
    hidden_material = [i for i in items if C.MATERIAL <= i["score"] < lvl_min]

    for i in routine if level == "all" else []:
        i["one_liner"] = _one_liner(i)

    # Network: natural reasons to reconnect
    events_by_org: dict[str, list[dict]] = defaultdict(list)
    for i in items:
        if i["tier"] != "routine" and i["org"]:
            events_by_org[i["org"]].append(i)
    reconnect = net.reconnect(events_by_org)

    n_cos = len({i["ticker"] for i in items})
    crit = [i for i in shown if i["tier"] == "critical"]
    headline = _headline(len(items), n_cos, len(crit), len(shown), len(routine), len(alerts), len(done) + len(alerts_done))
    routine_groups = Counter(i["category_label"] for i in routine)
    return {
        "as_of": now.isoformat(), "window": window, "hours": hours, "level": level,
        "headline": headline,
        "counts": {"filings": len(items), "companies": n_cos, "critical": len(crit), "shown": len(shown),
                   "routine": len(routine), "alerts": len(alerts), "hidden_material": len(hidden_material),
                   "done": len(done) + len(alerts_done)},
        "done": [{k: i[k] for k in ("id", "ticker", "company", "title")} for i in done]
                + [{"id": a["id"], "ticker": a["ticker"], "company": a["company"], "title": a["title"]} for a in alerts_done],
        "alerts": alerts, "items": shown,
        "routine": {"groups": routine_groups.most_common(), "items": routine if level == "all" else []},
        "reconnect": reconnect,
        "eod_date": next((s.get("metrics", {}).get("date") for s in eod.values() if s.get("metrics")), None),
        "last_refresh": db.kv_get("meta", "last_refresh"),
    }


def _group(items: list[dict], hours: float = 48) -> list[dict]:
    """One story, one card: filings on the same company and category within 48h fold into the
    highest-scoring one (e.g. a QIP's board approval, opening and allotment)."""
    items = sorted(items, key=lambda i: -i["score"])
    out: list[dict] = []
    for i in items:
        lead = next((o for o in out if o["ticker"] == i["ticker"] and o["category"] == i["category"]
                     and abs(_ts(o["ts"]) - _ts(i["ts"])) <= hours * 3600), None)
        if lead:
            lead.setdefault("related", []).append({k: i[k] for k in ("id", "title", "ts", "url", "summary")})
        else:
            out.append(i)
    return out


def _headline(n: int, cos: int, crit: int, shown: int, routine: int, alerts: int, done: int = 0) -> str:
    if n == 0 and not alerts and not done:
        return "Nothing filed across your portfolio in this window."
    things = shown + alerts
    lead = {0: "Nothing needs you", 1: "One thing matters"}.get(things, f"{things} things matter")
    if done:
        lead = {0: "All handled", 1: "One thing left"}.get(things, f"{things} things left")
    return f"{lead}. {n} filing{'s' if n != 1 else ''} across {cos} compan{'ies' if cos != 1 else 'y'}; {routine} routine, folded away."


def _enrich(i: dict, net: graph.Network) -> None:
    cat, t = i["category"], i["ticker"]
    i["one_liner"] = _one_liner(i)
    i["people"] = _people_line(i["org"], cat, f"{i['title']} {i.get('summary') or ''}")
    if cat == "results" and i["subtype"] in ("filing", "board_outcome"):
        try:
            c = companies.get(t)
            i["results"] = financials.results_card(c)
        except Exception as e:
            log.warning("results card %s: %s", t, e)
    if cat == "management_change":
        pid = (i.get("extra") or {}).get("person")
        if not pid:
            m = _APPOINTEE.search(f"{i['title']} {i.get('summary') or ''}")
            if m:
                hit = net.resolve(m.group(1), i["org"])
                pid = hit["id"] if hit else None
                i["appointee_name"] = m.group(1)
        if pid and pid in net.people:
            prof = net.person_full(pid)
            i["person"] = prof
            i["paths"] = reco.with_graphs(net, net.paths_to(pid), pid)
    if cat in ("M&A", "capex", "litigation", "fund_raise", "credit_rating", "pledging") and i.get("people"):
        best = i["people"][0]
        i["best_call"] = {**best, "because": f"{best['why'][0]}. Likely knows {best['would_know']}."}
