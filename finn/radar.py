"""The two "what needs me" views: the portfolio radar (home screen) and the people to-do list.

Both are composed from the brief (push.brief), so ranking, filtering, simulated-data rules and
swipe-to-handle stay in one place. They only reshape it for a phone:

  radar   every holding as a tile sized by weight, plus the few holdings that need a look, each
          reduced to one line and one person ("Ananya can introduce you"). Nothing else on screen.
  todo    every person worth contacting now, verb first: calls the news gives you a reason for, then
          keep-in-touch nudges. One row per person; the row says what to do and why.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from . import portfolio, push, store
from .network import nudges

log = logging.getLogger(__name__)


def _first(name: str) -> str:
    return (name or "").split()[0]


TOPIC = {"results": "results", "management_change": "leadership change", "M&A": "acquisition", "capex": "capex plan",
         "fund_raise": "fund raise", "credit_rating": "rating action", "litigation": "legal matter", "pledging": "promoter pledge",
         "insider_trading": "insider trades", "dividend": "dividend", "bonus_split": "bonus or split"}


def _about(company: str, category: str, label: str) -> str:
    """'PI Industries' acquisition', 'Laurus Labs' capex plan', 'HDFC Bank's leadership change'."""
    co = company.replace(" Ltd", "").replace(" Limited", "")
    return f"{co}{chr(39) if co.endswith('s') else chr(39) + 's'} {TOPIC.get(category, label.lower())}"


def hook(item: dict) -> dict | None:
    """The one person attached to a filing, and what to do with them."""
    via = next((p for p in item.get("paths") or [] if p.get("via")), None)
    if item.get("person") and via:
        t = item["person"]
        return {"person": via["via"], "intent": "intro", "target": {k: t.get(k) for k in ("id", "name", "headline")},
                "text": f"{_first(via['via']['name'])} can introduce you to {_first(t['name'])}",
                "because": (via.get("why") or [""])[0], "warmth": via.get("warmth")}
    rec = item.get("best_call") or ((item.get("people") or [None])[0])
    if rec:
        return {"person": rec["person"], "intent": "view", "target": None,
                "text": f"Best call: {rec['person']['name']}",
                "because": rec.get("because") or (rec.get("why") or [""])[0], "warmth": rec.get("warmth")}
    return None


def radar(window: str = "24h") -> dict[str, Any]:
    b = push.brief(window, "material")
    held = portfolio.holdings()
    items, alerts = defaultdict(list), defaultdict(list)
    for i in b["items"]:
        items[i["ticker"]].append(i)
    for a in b["alerts"]:
        alerts[a["ticker"]].append(a)
    from .sources import prices

    eod = prices.fetch_eod([h["ticker"] for h in held])
    tiles, needs = [], []
    for h in held:
        t = h["ticker"]
        m = (eod.get(t) or {}).get("metrics") or {}
        its, als = items.get(t, []), alerts.get(t, [])
        status = "filing" if its else "move" if als else None
        tiles.append({"ticker": t, "name": h["name"], "weight": h.get("weight") or 0, "theme": h.get("theme"),
                      "change_pct": m.get("change_pct"), "volume_x": m.get("volume_x"), "status": status})
        if not status:
            continue
        lead = max(its, key=lambda i: i["score"]) if its else None
        move = als[0] if als else None
        needs.append({
            "ticker": t, "company": h["name"], "kind": status,
            "ids": [i["id"] for i in its] + [a["id"] for a in als],
            "event_id": lead["id"] if lead else None,
            "score": max([i["score"] for i in its] + [a["score"] for a in als]),
            "tier": lead["tier"] if lead else move["tier"],
            "label": lead["category_label"] if lead else "Price and volume",
            "title": lead["title"] if lead else move["title"],
            "line": (lead.get("one_liner") or lead.get("summary")) if lead else
                    ("No filing explains this move." if move.get("unexplained") else f"Likely: {move.get('explained_by')}"),
            "ts": lead["ts"] if lead else move["ts"], "simulated": bool(lead and lead.get("simulated")),
            "url": lead.get("url") if lead else None,
            "more": len(its) - 1 if its else 0,
            "move": {k: move["metrics"].get(k) for k in ("change_pct", "volume_x")} if move else None,
            "hook": hook(lead) if lead else None,
        })
    needs.sort(key=lambda n: -n["score"])
    quiet = [x for x in tiles if not x["status"]]
    biggest = max(quiet, key=lambda x: abs(x["change_pct"] or 0), default=None)
    n = len(needs)
    headline = (f"{n} of {len(held)} holdings need a look" if n > 1 else
                f"1 of {len(held)} holdings needs a look" if n == 1 else
                ("Everything you hold is quiet" if b["counts"]["done"] == 0 else "All handled. Nothing else needs you"))
    if needs:
        top = needs[0]
        sub = f"Start with {top['company']}." + (f" {top['hook']['text']}." if top.get("hook") else "")
    else:
        sub = f"{b['counts']['filings']} filing{'s' if b['counts']['filings'] != 1 else ''} read for you in this window."
    return {
        "as_of": b["as_of"], "window": window, "headline": headline, "sub": sub,
        "tiles": tiles, "needs": needs,
        "quiet": {"count": len(quiet), "routine": b["counts"]["routine"],
                  "biggest": ({"ticker": biggest["ticker"], "name": biggest["name"], "change_pct": biggest["change_pct"]}
                              if biggest and biggest["change_pct"] else None)},
        "done": b["counts"]["done"], "filings": b["counts"]["filings"],
        "eod_date": b.get("eod_date"), "last_refresh": b.get("last_refresh"),
    }


def todo(window: str = "7d") -> dict[str, Any]:
    """People worth contacting now: the news gives a reason first, then the keep-in-touch rhythm."""
    b = push.brief(window, "material")
    done = store.dismissed_ids()
    out: dict[str, dict] = {}
    for i in b["items"]:
        h = hook(i)
        if not h:
            continue
        pid = h["person"]["id"]
        tid = f"todo:{i['id']}:{pid}"
        if tid in done or pid in out:
            continue
        first = _first(h["person"]["name"])
        title = (f"Ask {first} to introduce you to {h['target']['name']}" if h["intent"] == "intro"
                 else f"Ask {first} about {_about(i['company'], i['category'], i['category_label'])}")
        out[pid] = {"id": tid, "kind": "news", "person": h["person"], "warmth": h.get("warmth"), "intent": h["intent"],
                    "title": title, "reason": h["because"], "ticker": i["ticker"], "company": i["company"],
                    "event": {"id": i["id"], "title": i["title"], "label": i["category_label"], "ts": i["ts"]},
                    "target": h["target"], "simulated": i.get("simulated", False), "score": i["score"]}
    try:
        ns = nudges.compute(limit=8)
    except Exception as e:  # a nudge bug must not take the list down
        log.warning("nudges failed: %s", e)
        ns = []
    for n in ns:
        pid = n["person"]["id"]
        if pid in out:
            out[pid]["also"] = n["reason"]
            continue
        hk = n.get("hook") or {}
        out[pid] = {"id": f"nudge:{pid}", "kind": "lead" if n["lead"] else "nudge", "person": n["person"],
                    "warmth": n["warmth"], "intent": "first" if n["lead"] else "catchup",
                    "title": f"{'Introduce yourself to' if n['lead'] else 'Reconnect with'} {n['person']['name']}",
                    "reason": n["reason"], "ticker": hk.get("ticker"), "company": hk.get("org"),
                    "event": {"id": hk["id"], "title": hk["title"], "label": hk.get("category"), "ts": hk.get("ts")} if hk else None,
                    "target": None, "simulated": False, "score": n["scores"]["priority"] * 100}
    rows = sorted(out.values(), key=lambda r: (r["kind"] != "news", -r["score"]))
    return {"items": rows, "window": window}


def todo_done(item_id: str, person_id: str | None) -> dict[str, Any]:
    """Mark a to-do handled. Contacting someone also restarts their keep-in-touch clock."""
    from . import clock

    if item_id.startswith("todo:"):
        store.dismiss(item_id)
    touched = None
    if person_id:
        touched = store.contact_touch(person_id, clock.now().date().isoformat(), None)
    return {"ok": True, "touched": touched is not None}
