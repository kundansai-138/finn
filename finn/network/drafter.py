"""Message drafter: a first message to someone in the network, grounded in what FINN knows.

The investor picks what they want (get a view, ask for an intro, catch up, introduce themselves) and
where it goes (WhatsApp, email, LinkedIn). The draft uses only facts FINN can show back: how you know
the person, when you last spoke, the filing that gives a reason to call, and how they know the person
you want to meet. Those facts are returned with the draft so the investor can see what it rests on.

Deterministic templates by default. With a model key, the fast model writes it instead, from the
same facts, and never sees a real name: recipient and target are [NAME] and [TARGET].
FINN never sends anything: the app copies the text or opens the investor's own WhatsApp or mail app.
"""
from __future__ import annotations

from typing import Any

from .. import clock, portfolio, store
from ..intel import classify as C
from .graph import get as get_network

INTENTS = {"view": "Get their view", "intro": "Ask for an intro", "catchup": "Catch up", "first": "Introduce yourself"}
CHANNELS = ("whatsapp", "email", "linkedin")
LINKEDIN_MAX = 300   # connection-note limit

EVENT_PHRASE = {
    "results": "{co} has just put out its results",
    "management_change": "{co} has announced a leadership change",
    "M&A": "{co} has signed an acquisition",
    "capex": "{co} has approved a large capex plan",
    "fund_raise": "{co} is raising capital",
    "credit_rating": "{co} has had a credit rating action",
    "litigation": "{co} has disclosed a legal matter",
    "pledging": "there's been promoter pledging news at {co}",
    "insider_trading": "there have been insider trades at {co}",
    "dividend": "{co} has announced a dividend",
}


def _first(name: str) -> str:
    return (name or "").split()[0]


def _months(last: str | None) -> float | None:
    if not last:
        return None
    from datetime import datetime

    try:
        return max(0.0, (clock.now().date() - datetime.strptime(last[:10], "%Y-%m-%d").date()).days / 30.4)
    except ValueError:
        return None


def facts(pid: str, ticker: str | None = None, event_id: str | None = None, target_id: str | None = None) -> dict[str, Any]:
    net = get_network()
    if pid not in net.people or pid == "you":
        raise KeyError(pid)
    p = net.people[pid]
    contact = net.contacts.get(pid)
    tie = (contact or {}).get("tie", {})
    cur = net.current_role(p)
    f: dict[str, Any] = {
        "first": _first(p["name"]), "name": p["name"],
        "role": f"{cur['title'].split('→')[-1].strip()}, {net.org_name(cur['org'])}" if cur else p.get("headline"),
        "contact": bool(contact) and tie.get("type") != "to-meet" and bool(tie.get("last_contact")),
        "tie_context": tie.get("context"), "months": _months(tie.get("last_contact")),
    }
    if contact:
        f["overlap"] = (net.link("you", pid).reasons or [None])[0]
    # The news that gives a reason to write
    ev = store.ann_get(event_id) if event_id else None
    t = (ev or {}).get("ticker") or (ticker.upper() if ticker else None)
    if t:
        h = portfolio.holding(t) or next((r for r in portfolio.research() if r["ticker"] == t), None)
        oid = net.org_for_ticker(t)
        f["company"] = (h or {}).get("name") or (net.org_name(oid) if oid else t)
        f["company_short"] = f["company"].replace(" Ltd", "").replace(" Limited", "")
        f["ticker"] = t
        if oid:
            rel = next((("now" if not r.get("to") else "before") for r in p.get("roles", []) if r["org"] == oid), None)
            f["at_company"] = rel   # "now" = works there, "before" = worked there
    if ev:
        cat, _, _ = C.classify(ev["title"], ev.get("summary") or "")
        f["event"] = {"title": C.clean_title(ev["title"]), "category": cat, "label": C.LABELS.get(cat, cat), "ts": ev["ts"],
                      "simulated": ev.get("source") == "scenario"}
    if target_id and target_id in net.people:
        tp = net.people[target_id]
        link = net.link(pid, target_id)
        f["target"] = {"first": _first(tp["name"]), "name": tp["name"], "headline": tp.get("headline"),
                       "shared": net.org_name(link.orgs[0]) if link.orgs else None, "how": (link.reasons or [None])[0]}
    return f


def grounds(f: dict) -> list[str]:
    """What the draft rests on, as sentences the investor can check."""
    out = []
    if f.get("tie_context"):
        out.append(f"How you know {f['first']}: {f['tie_context']}")
    if f.get("months") is not None:
        m = f["months"]
        out.append(f"Last spoke {'this month' if m < 1 else f'{m:.0f} months ago'}")
    elif not f.get("contact"):
        out.append(f"You haven't spoken to {f['first']} yet")
    if f.get("event"):
        out.append(f"{f['company_short']}: {f['event']['label'].lower()} · {f['event']['title']}")
    if f.get("at_company"):
        out.append(f"{f['first']} {'works at' if f['at_company'] == 'now' else 'used to work at'} {f['company_short']}")
    if f.get("target") and f["target"].get("how"):
        out.append(f"{f['first']} and {f['target']['first']}: {f['target']['how']}")
    return out


def _template(f: dict, intent: str, channel: str, variant: int) -> tuple[str, str | None]:
    first, co = f["first"], f.get("company_short")
    v = variant % 2
    # 1. Opener: honest about how long it has been
    m = f.get("months")
    if intent == "first" or not f.get("contact"):
        opener = "We haven't met yet: I invest in listed Indian companies." if v == 0 else "I'm an investor in listed Indian companies, and we haven't met yet."
    elif m is not None and m >= 6:
        opener = "It's been far too long." if v == 0 else "It's been a while since we last spoke, hope all is well."
    else:
        opener = "Hope you're doing well." if v == 0 else "Hope things are good on your side."
    # 2. The reason to write now
    reason = ""
    if f.get("event") and co:
        phrase = EVENT_PHRASE.get(f["event"]["category"], "{co} has made an announcement").format(co=co)
        if intent == "intro" and f.get("target"):
            phrase = f"{co} has named {f['target']['name']} to its leadership team"
        reason = f"Saw that {phrase}." if v == 0 else f"{phrase[0].upper()}{phrase[1:]}, and you came to mind."
    elif co:
        reason = f"I've been spending time on {co}." if v == 0 else f"I'm doing some work on {co}."
    # 3. Why them (only what FINN can back up)
    why = ""
    if intent != "intro" and f.get("at_company"):
        why = (f"Given your time there, you'd have a sharper read on it than anyone." if f["at_company"] == "before"
               else "You'd see this from the inside better than anyone.")
    # 4. The ask
    if intent == "intro" and f.get("target"):
        tf = f["target"]["first"]
        where = f" from {f['target']['shared']}" if f["target"].get("shared") else ""
        ask = (f"I believe you know {tf}{where}. Would you be comfortable introducing us? I'm happy to send a short note you can forward."
               if v == 0 else f"You worked with {tf}{where}, if I'm right. Would you be open to connecting us? I can send a two-line blurb to make it easy.")
    elif intent == "view":
        ask = ("Would you have 20 minutes this week to share how you see it?" if v == 0
               else "Could I borrow 20 minutes for your take? Happy to work around your week.")
    elif intent == "first":
        ask = (f"I'd value 20 minutes to learn how you see {co or 'your industry'}, whenever suits you." if v == 0
               else f"Would you be open to a short call about {co or 'your space'}? I'd value your perspective.")
    else:
        ask = ("Would love to catch up properly. Coffee or a call in the next couple of weeks?" if v == 0
               else "Shall we find time for a proper catch-up soon?")
    hello = f"Hi {first},"
    body_parts = [p for p in (opener, reason, why, ask) if p]
    # On a chat line the greeting runs into the first sentence: "Hi Ananya, hope you're doing well."
    inline = [body_parts[0][0].lower() + body_parts[0][1:], *body_parts[1:]] if body_parts else []
    if channel == "linkedin":
        text = " ".join([hello, *inline])
        if len(text) > LINKEDIN_MAX:   # drop the softest sentences first
            text = " ".join(p for p in [hello, reason, ask] if p)
        if len(text) > LINKEDIN_MAX:
            text = text[: LINKEDIN_MAX - 1].rsplit(" ", 1)[0] + "…"
        return text, None
    if channel == "email":
        subject = {"view": f"Your read on {co}?" if co else "Your read?",
                   "intro": f"Introduction to {f['target']['first']}?" if f.get("target") else "An introduction?",
                   "first": f"{co}: 20 minutes?" if co else "20 minutes?",
                   "catchup": "Catching up"}[intent]
        return f"{hello}\n\n" + " ".join(body_parts[:2]) + ("\n\n" + " ".join(body_parts[2:]) if body_parts[2:] else "") + "\n\nBest,", subject
    return " ".join([hello, *inline]), None


SYSTEM = ("You write the first message an investor sends to someone in their network. Use only the facts given; never invent "
          "shared history, numbers or names. Warm, specific, brief, no flattery, never ask for confidential or price-sensitive "
          "information. Use [NAME] for the recipient and [TARGET] for anyone they might introduce. Plain text, no sign-off name.")


def draft(pid: str, intent: str = "view", channel: str = "whatsapp", ticker: str | None = None,
          event_id: str | None = None, target_id: str | None = None, variant: int = 0) -> dict[str, Any]:
    from ..intel import llm

    intent = intent if intent in INTENTS else "view"
    channel = channel if channel in CHANNELS else "whatsapp"
    f = facts(pid, ticker, event_id, target_id)
    if intent == "intro" and not f.get("target"):
        intent = "view"
    if not f["contact"] and intent == "catchup":
        intent = "first"
    if f["contact"] and intent == "first":
        intent = "catchup"
    options = (["intro"] if f.get("target") else []) + ["view"] + (["catchup"] if f["contact"] else ["first"])
    g = grounds(f)
    text, subject = _template(f, intent, channel, variant)
    source = "template"
    if llm.enabled():
        anon = [s.replace(f["name"], "[NAME]").replace(f["first"], "[NAME]") for s in g]
        if f.get("target"):
            anon = [s.replace(f["target"]["name"], "[TARGET]").replace(f["target"]["first"], "[TARGET]") for s in anon]
        limit = {"whatsapp": "2-3 short sentences", "email": "a short email body (3-4 sentences)",
                 "linkedin": f"under {LINKEDIN_MAX} characters"}[channel]
        prompt = (f"Goal: {INTENTS[intent].lower()}. Channel: {channel}, {limit}. Variation {variant}.\n"
                  f"Recipient: [NAME], {f.get('role') or 'professional contact'}.\nFacts:\n- " + "\n- ".join(anon or ["none"]) +
                  "\nWrite the message.")
        out = llm.complete("message_draft", prompt, tier="fast", system=SYSTEM, max_tokens=1500)
        if out:
            text = out.strip().replace("[NAME]", f["first"])
            if f.get("target"):
                text = text.replace("[TARGET]", f["target"]["first"])
            source = "model"
    return {"person_id": pid, "intent": intent, "channel": channel, "text": text, "subject": subject,
            "grounds": g, "source": source, "intents": [{"id": k, "label": INTENTS[k]} for k in options],
            "simulated": bool((f.get("event") or {}).get("simulated"))}
