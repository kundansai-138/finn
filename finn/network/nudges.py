"""Keep-in-touch nudges: who you are losing touch with, ranked by what losing them would cost.

A relationship CRM's "you haven't spoken in a while", made specific to an investor. Deterministic and
explainable; every number below is shown back to the investor as a sentence.

  cadence   the rhythm you actually keep with this person: 365 / (interactions in the last 12
            months + 1) days, never longer than the tie needs (friend 60 days ... met 180) and
            never shorter than 21. A monthly contact quiet for 10 weeks is a signal; a once-a-year
            contact quiet for 10 weeks is not.
  overdue   days since you last spoke / cadence. 1.0 = exactly on rhythm.
  urgency   0 until overdue reaches 1, then 0.4 rising to 1.0 at 3× the usual gap.
  value     how much this person matters to the portfolio right now: their strongest link to a
            holding (worked there 1.0, competitor/customer 0.8, same domain 0.5), scaled by the
            holding's weight; research names count at 70%.
  hook      a natural reason to get in touch now: something material was filed in the last 14 days
            by a company they work or worked at, weighted by how material (a CFO change or an
            acquisition is a better reason to call than a rumour clarification): 0.4 at the
            materiality threshold rising to 1.0 for the most important filings; current employer
            counts fully, a former one at 80%.

  priority = 0.50·urgency + 0.30·value + 0.20·hook

A nudge is raised when the contact is past their rhythm and priority ≥ 0.40, or when there is a hook
and they are at least 60% of the way there. Leads added from the public web ("to-meet") get a
first-contact nudge a week after being added, with the warmest intro route.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from .. import clock, portfolio, store
from ..intel import classify as C
from .graph import Network, get as get_network

TYPE_CAP_DAYS = {"friend": 60, "mentor": 60, "ex-colleague": 90, "classmate": 120, "founder": 90,
                 "met": 180, "expert": 120, "to-meet": 14}
MIN_CADENCE = 21
HOOK_DAYS = 14
W_URGENCY, W_VALUE, W_HOOK = 0.50, 0.30, 0.20
MIN_PRIORITY = 0.40
PUSH_PRIORITY = 0.55
PUSH_COOLDOWN_DAYS = 14


def _today() -> date:
    return clock.now().date()


def _parse(d: str | None) -> date | None:
    if not d:
        return None
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
    except ValueError:
        try:
            return date(int(str(d)[:4]), 1, 1)
        except ValueError:
            return None


def cadence_days(tie: dict) -> int:
    cap = TYPE_CAP_DAYS.get(tie.get("type") or "met", 120)
    if tie.get("type") == "to-meet":   # a new lead: the first-contact window, no rhythm yet
        return cap
    observed = 365 / ((tie.get("interactions_12m") or 0) + 1)
    return int(max(MIN_CADENCE, min(cap, observed)))


def urgency(overdue: float) -> float:
    return 0.0 if overdue < 1 else min(1.0, 0.4 + 0.6 * (overdue - 1) / 2)


def _cadence_label(days: int) -> str:
    if days <= 35:
        return "every month or so"
    if days <= 75:
        return "every couple of months"
    if days <= 120:
        return "every quarter"
    return "a couple of times a year"


def hook_strength(score: float) -> float:
    """0.4 at the materiality threshold, 1.0 at the top of the scale."""
    if score < C.MATERIAL:
        return 0.0
    return round(min(1.0, 0.4 + 0.6 * (score - C.MATERIAL) / (95 - C.MATERIAL)), 3)


def portfolio_value(net: Network) -> dict[str, tuple[float, str]]:
    """Each contact's strongest link to a holding: (value 0-1, the sentence that explains it)."""
    hold = [(h, 1.0) for h in portfolio.holdings()] + [(h, 0.7) for h in portfolio.research()]
    wmax = max([h.get("weight") or 0 for h, _ in hold] + [1e-9])
    best: dict[str, tuple[float, str]] = {}
    for h, kind_w in hold:
        oid = net.org_for_ticker(h["ticker"])
        o = net.orgs.get(oid or "", {})
        peers = set(o.get("peers", [])) | {x for x, d in net.orgs.items() if oid and oid in d.get("peers", [])}
        adj = peers | set(o.get("customers", [])) | set(o.get("suppliers", []))
        themes = set(o.get("themes", [])) | ({h["theme"].lower()} if h.get("theme") else set())
        scale = kind_w * (0.7 + 0.3 * (h.get("weight") or 0) / wmax if kind_w == 1.0 else 1.0)
        held = f"{h['weight']:.1f}% of the portfolio" if h.get("weight") else "on your research list"
        for pid, p in net.contacts.items():
            v, why = 0.0, ""
            for r in p.get("roles", []):
                if oid and r["org"] == oid:
                    cand = 1.0 if not r.get("to") else 0.85
                    why_ = f"{'is' if not r.get('to') else 'was'} at {h['name']} ({held})"
                elif r["org"] in adj:
                    cand, why_ = 0.75, f"knows {h['name']} ({held}) from {net.org_name(r['org'])}, a competitor or customer"
                elif themes & set(net.orgs.get(r["org"], {}).get("themes", [])):
                    cand, why_ = 0.5, f"works in the same domain as {h['name']} ({held})"
                else:
                    continue
                if cand > v:
                    v, why = cand, why_
            if v and v * scale > best.get(pid, (0, ""))[0]:
                best[pid] = (round(v * scale, 3), why)
    return best


def hooks(net: Network, days: int = HOOK_DAYS) -> dict[str, list[dict]]:
    """Material filings in the last `days` by organisations in the network, by org."""
    since = (clock.now() - timedelta(days=days)).isoformat()
    by_ticker = {o["ticker"]: oid for oid, o in net.orgs.items() if o.get("ticker")}
    out: dict[str, list[dict]] = defaultdict(list)
    for a in store.ann_since(since, list(by_ticker)):
        if (a.get("score") or 0) >= C.MATERIAL:
            out[by_ticker[a["ticker"]]].append(a)
    return out


def compute(limit: int = 6, include_all: bool = False) -> list[dict[str, Any]]:
    net = get_network()
    today = _today()
    value = portfolio_value(net)
    ev = hooks(net)
    out = []
    for pid, p in net.contacts.items():
        tie = p.get("tie", {})
        cad = cadence_days(tie)
        last = _parse(tie.get("last_contact"))
        lead = tie.get("type") == "to-meet" and last is None
        if lead:
            added = _parse(tie.get("since")) or today
            days = (today - added).days
            overdue = days / cad
        elif last is None:
            continue
        else:
            days = (today - last).days
            overdue = days / cad
        hook, hs = None, 0.0
        for r in p.get("roles", []):
            for a in ev.get(r["org"], []):
                strength = hook_strength(a.get("score") or 0) * (1.0 if not r.get("to") else 0.8)
                if strength > hs:
                    hs = strength
                    hook = {"org": net.org_name(r["org"]), "relation": "current" if not r.get("to") else "former",
                            "title": a["title"], "ticker": a["ticker"], "ts": a["ts"], "category": a.get("category"),
                            "id": a["id"], "strength": round(strength, 2)}
        v, vwhy = value.get(pid, (0.0, ""))
        u = urgency(overdue) if not lead else (0.5 if overdue >= 0.5 else 0.0)
        pri = W_URGENCY * u + W_VALUE * v + W_HOOK * hs
        due = (overdue >= 1 and pri >= MIN_PRIORITY) or (hs >= 0.6 and overdue >= 0.6) or (lead and overdue >= 0.5)
        if not due and not include_all:
            continue
        first = p["name"].split()[0]
        if lead:
            parts = [f"You added {first} {days} day{'s' if days != 1 else ''} ago and haven't reached out yet."]
        else:
            parts = [f"{days} days since you spoke; you usually talk {_cadence_label(cad)}."]
        if vwhy:
            parts.append(f"{first} {vwhy}.")
        if hook:
            parts.append(f"{hook['org']} ({hook['relation']} employer) just filed: {hook['title']}.")
        intro = None
        if lead:
            from .discover import intro_routes

            cur = net.current_role(p)
            routes = intro_routes(net, cur["org"] if cur else None)
            routes = [r for r in routes if r["id"] != pid]
            if routes:
                intro = routes[0]
                parts.append(f"{intro['name'].split()[0]} {intro['relation']} and could introduce you.")
        out.append({
            "person": net.person_card(pid), "warmth": net.warmth(pid), "lead": lead,
            "days_since": days, "cadence_days": cad, "overdue": round(overdue, 2),
            "scores": {"urgency": round(u, 2), "value": round(v, 2), "hook": round(hs, 2), "priority": round(pri, 3)},
            "contributions": [{"feature": "urgency", "label": "Past your usual rhythm", "value": round(W_URGENCY * u, 2)},
                              {"feature": "value", "label": "Matters to your portfolio", "value": round(W_VALUE * v, 2)},
                              {"feature": "hook", "label": "A reason to call now", "value": round(W_HOOK * hs, 2)}],
            "hook": hook, "intro": intro, "reason": " ".join(parts), "due": due,
        })
    out.sort(key=lambda x: -x["scores"]["priority"])
    return out if include_all else out[:limit]


def dispatch(user_id: str | None = None) -> dict[str, Any]:
    """Push the most important nudge or two. At most one push per person every two weeks."""
    from .. import notify

    if not store.push_subs_for(user_id):
        return {"sent": 0, "reason": "no devices subscribed"}
    week = _today().isocalendar()
    sentinel = f"nudges-week:{week[0]}-{week[1]}"
    if store.notified_filter([sentinel], user_id):   # the weekly cap lives here, not only in the scheduler
        return {"sent": 0, "items": 0, "reason": "already nudged this week"}
    items = [n for n in compute(limit=10) if n["scores"]["priority"] >= PUSH_PRIORITY]
    ids = {n["person"]["id"]: f"nudge:{n['person']['id']}:{week[0]}-{(week[1] - 1) // 2}" for n in items}
    seen = set(store.notified_filter(list(ids.values()), user_id))
    fresh = [n for n in items if ids[n["person"]["id"]] not in seen][:2]
    sent = 0
    for n in fresh:
        title = f"{'Introduce yourself to' if n['lead'] else 'Reconnect with'} {n['person']['name']}"
        r = notify.send(title, n["reason"][:220], url=f"/#/p/{n['person']['id']}", tag=ids[n["person"]["id"]], user_id=user_id)
        sent += r["sent"]
    store.notified_mark([ids[n["person"]["id"]] for n in fresh] + [sentinel], user_id)
    return {"sent": sent, "items": len(fresh)}


DRAFT_SYSTEM = ("You write short, natural catch-up messages from an investor to someone they know. Two or three sentences, "
                "warm and specific, no flattery, no asks for confidential information. Use the placeholder [NAME] for the "
                "recipient's name. Plain text only.")


def draft(pid: str) -> dict[str, Any]:
    """A first draft of the message. The contact's name never reaches the model: it sees [NAME]."""
    from ..intel import llm

    net = get_network()
    n = next((x for x in compute(include_all=True) if x["person"]["id"] == pid), None)
    if n is None:
        raise KeyError(pid)
    p = net.contacts[pid]
    first = p["name"].split()[0]
    cur = net.current_role(p)
    role = f"{cur['title'].split('→')[-1].strip()} at {net.org_name(cur['org'])}" if cur else ""
    hook = f" Their {n['hook']['relation']} employer {n['hook']['org']} just announced: {n['hook']['title']}." if n.get("hook") else ""
    how = "You have not met yet." if n["lead"] else f"How you know them: {p.get('tie', {}).get('context') or 'professional contact'}."
    prompt = (f"Recipient: [NAME], {role}. {how} Last spoke {n['days_since']} days ago.{hook}\n"
              "Write the message.")
    # Room for the model's (adaptive) thinking as well as the 2-3 sentences; only what's generated is billed.
    text = llm.complete("nudge_draft", prompt, tier="fast", system=DRAFT_SYSTEM, max_tokens=1500) if llm.enabled() else None
    source = "model"
    if not text:
        source = "template"
        text = (f"Hi [NAME], it's been a while. {'Saw the news from ' + n['hook']['org'] + '. ' if n.get('hook') else ''}"
                "Would love to catch up over a call in the next couple of weeks if you have time.")
    return {"person_id": pid, "text": text.replace("[NAME]", first).strip(), "source": source}
