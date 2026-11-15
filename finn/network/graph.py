"""The Network: who the investor knows, who *they* know, and why it matters right now.

Everything here is deterministic and explainable: no model is needed to compute that two people
overlapped seven years in adjacent finance roles at Aarti. Each score comes with the sentence
that justifies it, because "Ananya (0.72)" is useless and "Ananya ran IR under him for 7 years"
is the whole product.

Scoring
  warmth(you, c)     tie type × recency decay (half-life ~17 months) + interaction bonus
  link(a, b)         1 - Π(1 - s_i) over shared contexts:
                       co-employment: min(1, years/4) × function affinity × org-size factor
                       co-education:  0.5 same programme & overlapping years, else 0.25
                       explicit:      investor-annotated strength
  path(you→c→t)      warmth(c) × link(c, t)
"""
from __future__ import annotations

import difflib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from typing import Any

from ..config import DATA

TIE_BASE = {"friend": 0.85, "mentor": 0.85, "ex-colleague": 0.75, "classmate": 0.65,
            "founder": 0.55, "met": 0.45, "expert": 0.35}
LARGE_ORGS = {"reliance", "tcs", "hdfcbank", "infosys", "icicibank", "mckinsey", "deloitte",
              "goldman", "bain", "kotak-ie", "hal", "sunpharma"}
FN_AFFINITY = {
    frozenset({"finance", "ir"}): 0.9, frozenset({"finance", "audit"}): 0.7,
    frozenset({"finance", "strategy"}): 0.7, frozenset({"strategy", "ir"}): 0.6,
    frozenset({"manufacturing", "operations"}): 0.9, frozenset({"manufacturing", "supply-chain"}): 0.7,
    frozenset({"procurement", "supply-chain"}): 0.9, frozenset({"procurement", "finance"}): 0.6,
    frozenset({"research", "ir"}): 0.6, frozenset({"sales", "marketing"}): 0.7,
    frozenset({"risk", "product"}): 0.6, frozenset({"finance", "supply-chain"}): 0.5,
}
# Which functions are best placed to explain which kind of event.
EVENT_FN_BOOST = {
    "capex": {"manufacturing": 1.3, "operations": 1.3, "engineering": 1.15, "finance": 1.05},
    "M&A": {"strategy": 1.3, "banking": 1.25, "finance": 1.15},
    "management_change": {"finance": 1.25, "ir": 1.25, "strategy": 1.1},
    "results": {"ir": 1.2, "finance": 1.2, "research": 1.15, "sales": 1.1},
    "litigation": {"regulatory": 1.3, "finance": 1.1},
    "credit_rating": {"finance": 1.2, "risk": 1.2},
}

KNOWS = {
    "finance": "capital allocation, how conservative the guidance is, working-capital discipline",
    "ir": "how management guides and how the Street reads them",
    "strategy": "the M&A pipeline and where the company wants to play next",
    "manufacturing": "plant ramp-ups, capex execution, utilisation and cost curves",
    "operations": "execution on the ground: ramp-ups, unit economics, bottlenecks",
    "sales": "demand, pricing power and customer concentration",
    "marketing": "brand strength, category trends, competitive intensity",
    "merchandising": "category trends, sell-through and competitive intensity",
    "procurement": "input costs, supplier terms and raw-material dynamics",
    "supply-chain": "input costs, inventory and supplier dynamics",
    "risk": "credit quality, underwriting standards and collections",
    "product": "digital funnels and product economics",
    "regulatory": "regulatory history and compliance culture",
    "research": "the Street's model and where consensus might be wrong",
    "engineering": "the technology roadmap and execution risk",
    "founder": "ground-level market dynamics and what competitors are doing",
    "audit": "accounting quality",
    "advisory": "industry structure and the order pipeline",
}


def _today() -> date:
    from ..clock import now
    return now().date()


def _ym(s: str | None, end: bool = False) -> float:
    """'2019-04' / '2019' / None(present) -> fractional year."""
    if not s:
        t = _today()
        return t.year + (t.month - 1) / 12
    parts = str(s).split("-")
    y = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else (12 if end else 1)
    return y + (m - 1) / 12 + (1 / 12 if end else 0)


def _yr(s: str | None) -> str:
    return "now" if not s else str(s)[:4]


def norm_name(n: str) -> str:
    n = re.sub(r"\b(mr|mrs|ms|dr|shri|smt|prof|ca|cs)\.?\s+", "", n.lower())
    return re.sub(r"[^a-z ]", "", n).strip()


@dataclass
class Link:
    strength: float
    reasons: list[str] = field(default_factory=list)
    overlap_years: float = 0.0
    orgs: list[str] = field(default_factory=list)


class Network:
    def __init__(self, raw: dict[str, Any]):
        self.orgs: dict[str, dict] = raw["orgs"]
        self.you: dict = raw["you"]
        self.contacts: dict[str, dict] = {p["id"]: p for p in raw["people"]}
        self.public: dict[str, dict] = {p["id"]: p for p in raw.get("public_profiles", [])}
        self.people: dict[str, dict] = {"you": self.you, **self.contacts, **self.public}
        self.explicit = raw.get("explicit_links", [])
        self.by_ticker = {o["ticker"]: oid for oid, o in self.orgs.items() if o.get("ticker")}

    # ---- basics -------------------------------------------------------------
    def org_name(self, oid: str) -> str:
        return self.orgs.get(oid, {}).get("name", oid)

    def org_for_ticker(self, ticker: str) -> str | None:
        return self.by_ticker.get(ticker.upper())

    def current_role(self, p: dict) -> dict | None:
        cur = [r for r in p.get("roles", []) if not r.get("to")]
        return cur[-1] if cur else (p["roles"][-1] if p.get("roles") else None)

    # ---- warmth (you -> contact) ---------------------------------------------
    def warmth(self, pid: str) -> dict[str, Any]:
        p = self.contacts.get(pid)
        if not p:
            return {"score": 0.0, "label": "unknown"}
        tie = p.get("tie", {})
        base = TIE_BASE.get(tie.get("type", "met"), 0.4)
        last = datetime.strptime(tie.get("last_contact", "2020-01-01"), "%Y-%m-%d").date()
        months = max(0.0, (_today() - last).days / 30.4)
        recency = math.exp(-months / 24)  # ~17-month half-life
        score = base * (0.55 + 0.45 * recency) + min(0.12, 0.02 * tie.get("interactions_12m", 0))
        score = min(1.0, score)
        label = "warm" if months <= 4 else "cooling" if months <= 10 else "dormant"
        return {"score": round(score, 2), "label": label, "months_since": round(months, 1),
                "last_contact": tie.get("last_contact"), "type": tie.get("type"), "context": tie.get("context")}

    # ---- link (a <-> b) -------------------------------------------------------
    def link(self, aid: str, bid: str) -> Link:
        a, b = self.people[aid], self.people[bid]
        parts: list[tuple[float, str, float]] = []
        orgs: list[str] = []
        for ra in a.get("roles", []):
            for rb in b.get("roles", []):
                if ra["org"] != rb["org"]:
                    continue
                lo = max(_ym(ra["from"]), _ym(rb["from"]))
                hi = min(_ym(ra.get("to"), end=True), _ym(rb.get("to"), end=True))
                yrs = hi - lo
                if yrs <= 0.15:
                    continue
                fa, fb = ra.get("function"), rb.get("function")
                aff = 1.0 if fa == fb else FN_AFFINITY.get(frozenset({fa, fb}), 0.45)
                size = 0.55 if ra["org"] in LARGE_ORGS else 1.0
                s = min(1.0, yrs / 4) * aff * size
                rel = "same function" if fa == fb else ("adjacent functions" if aff >= 0.6 else "different functions")
                hi_lbl = "now" if not (ra.get("to") or rb.get("to")) else str(int(hi - 0.01))
                orgs.append(ra["org"])
                parts.append((s, f"{self.org_name(ra['org'])}, {int(lo)}–{hi_lbl}: {yrs:.0f} yr{'s' if yrs >= 1.5 else ''} "
                                 f"overlap, {rel} ({ra['title']} / {rb['title']})", yrs))
        for ea in a.get("education", []):
            for eb in b.get("education", []):
                if ea["org"] != eb["org"]:
                    continue
                lo, hi = max(int(ea["from"]), int(eb["from"])), min(int(ea["to"]), int(eb["to"]))
                if hi - lo < 1:
                    continue
                same = ea.get("degree") == eb.get("degree")
                s = 0.5 if same else 0.25
                parts.append((s, f"{self.org_name(ea['org'])}, {'same programme' if same else 'overlapping years'} ({lo}–{hi})", 0))
        for e in self.explicit:
            if {e["a"], e["b"]} == {aid, bid}:
                parts.append((e.get("strength", 0.7), e["context"], 0))
        if not parts:
            return Link(0.0)
        strength = 1 - math.prod(1 - s for s, _, _ in parts)
        parts.sort(key=lambda x: -x[0])
        return Link(round(strength, 2), [r for _, r, _ in parts], max(y for _, _, y in parts), sorted(set(orgs)))

    # ---- paths (you -> ? -> target) -----------------------------------------
    def paths_to(self, target: str, limit: int = 3) -> list[dict[str, Any]]:
        """Best warm routes to a person. A direct overlap is always included even when weak: it's
        exactly the thing the investor has forgotten ("you were both at Asian Paints in 2009")."""
        out = []
        direct = self.link("you", target)
        direct_path = None
        if direct.strength > 0:
            direct_path = {"via": None, "score": round(direct.strength * 0.9, 2), "hops": 1, "orgs": direct.orgs,
                           "why": direct.reasons, "headline": "You may have crossed paths directly"}
            out.append(direct_path)
        for cid in self.contacts:
            if cid == target:
                continue
            l = self.link(cid, target)
            if l.strength <= 0.05:
                continue
            w = self.warmth(cid)
            out.append({"via": self.person_card(cid), "score": round(w["score"] * l.strength, 2), "hops": 2,
                        "link": l.strength, "warmth": w, "why": l.reasons, "orgs": l.orgs,
                        "headline": f"{self.contacts[cid]['name']} → {self.people[target]['name']}"})
        out.sort(key=lambda x: -x["score"])
        top = out[:limit]
        if direct_path and direct_path not in top:
            top.append(direct_path)
        return top

    # ---- who to call about a company ----------------------------------------
    def people_for_org(self, oid: str, limit: int = 4, event: str | None = None) -> list[dict[str, Any]]:
        """Contacts ranked by how well placed they are to explain this company (or this event at it)."""
        org = self.orgs.get(oid, {})
        themes = set(org.get("themes", []))
        rel_sets = {k: set(org.get(k, [])) for k in ("peers", "customers", "suppliers")}
        # Reverse relationships: if X lists this org as a peer, X is a peer too.
        for other, o in self.orgs.items():
            if oid in o.get("peers", []):
                rel_sets["peers"].add(other)
        res = []
        for cid, p in self.contacts.items():
            best, why, fn = 0.0, [], None
            for r in p.get("roles", []):
                end = _ym(r.get("to"), end=True)
                yrs_since = max(0.0, _ym(None) - end)
                span = f"{_yr(r['from'])}–{_yr(r.get('to'))}"
                title = r["title"].split("→")[-1].strip()
                boost = EVENT_FN_BOOST.get(event or "", {}).get(r.get("function"), 1.0)
                if r["org"] == oid:
                    s = 0.95 if not r.get("to") else max(0.6, 0.9 - 0.04 * yrs_since)
                    w = f"{'Is' if not r.get('to') else 'Was'} {title} at {self.org_name(oid)} ({span})"
                elif r["org"] in rel_sets["peers"]:
                    s = 0.62 if not r.get("to") else max(0.4, 0.6 - 0.03 * yrs_since)
                    w = f"{title} at {self.org_name(r['org'])}, a direct competitor ({span})"
                elif r["org"] in rel_sets["customers"] | rel_sets["suppliers"]:
                    kind = "customer" if r["org"] in rel_sets["customers"] else "supplier"
                    s = 0.55
                    w = f"{title} at {self.org_name(r['org'])}, a {kind} ({span})"
                else:
                    continue
                s = min(1.0, s * boost)
                if s > best:
                    best, fn = s, r.get("function")
                why.append((s, w))
            exp = themes & (set(p.get("tags", [])) | {t for r in p.get("roles", []) for t in self.orgs.get(r["org"], {}).get("themes", [])})
            if not why and exp:
                best = 0.35
                why.append((0.35, f"Has worked in {', '.join(sorted(exp))}"))
                fn = (self.current_role(p) or {}).get("function")
            if best <= 0:
                continue
            w = self.warmth(cid)
            why.sort(key=lambda x: -x[0])
            res.append({
                "person": self.person_card(cid),
                "score": round(best * (0.45 + 0.55 * w["score"]), 2),
                "relevance": round(best, 2),
                "warmth": w,
                "why": [x[1] for x in why[:2]],
                "would_know": KNOWS.get(fn or "", "the industry's structure"),
            })
        res.sort(key=lambda x: -x["score"])
        return res[:limit]

    # ---- expertise map ---------------------------------------------------------
    def expertise(self, theme: str, limit: int = 6) -> list[dict[str, Any]]:
        theme = theme.lower().strip()
        out = []
        for cid, p in self.contacts.items():
            yrs, ev = 0.0, []
            for r in p.get("roles", []):
                ot = [t.lower() for t in self.orgs.get(r["org"], {}).get("themes", [])]
                if theme in ot:
                    y = _ym(r.get("to"), end=True) - _ym(r["from"])
                    recency = 1.0 if not r.get("to") else max(0.5, 1 - 0.05 * (_ym(None) - _ym(r.get("to"), end=True)))
                    yrs += y * recency
                    ev.append(f"{r['title']}, {self.org_name(r['org'])} ({_yr(r['from'])}–{_yr(r.get('to'))})")
            if theme in [t.lower() for t in p.get("tags", [])] and not ev:
                yrs += 1.5
                ev.append("self-described focus area")
            if yrs <= 0:
                continue
            w = self.warmth(cid)
            out.append({"person": self.person_card(cid), "years": round(yrs, 1),
                        "score": round(min(1, yrs / 12) * (0.5 + 0.5 * w["score"]), 2),
                        "evidence": ev, "warmth": w})
        out.sort(key=lambda x: -x["score"])
        return out[:limit]

    def themes(self) -> list[str]:
        ts = {t for o in self.orgs.values() for t in o.get("themes", [])}
        return sorted(ts - {"consulting", "audit", "sell-side research", "investing", "investment banking"})

    # ---- reconnect nudges ---------------------------------------------------
    def reconnect(self, events_by_org: dict[str, list[dict]], min_months: float = 6, limit: int = 3) -> list[dict]:
        out = []
        for cid, p in self.contacts.items():
            w = self.warmth(cid)
            if w["months_since"] < min_months:
                continue
            for r in sorted(p.get("roles", []), key=lambda r: _ym(r.get("to"), True), reverse=True):
                evs = events_by_org.get(r["org"])
                if not evs:
                    continue
                ev = max(evs, key=lambda e: e.get("score", 0))
                rel = "current employer" if not r.get("to") else "former employer"
                out.append({"person": self.person_card(cid), "warmth": w, "org": self.org_name(r["org"]),
                            "relation": rel, "event": {k: ev.get(k) for k in ("id", "title", "category", "ticker")},
                            "reason": f"{w['months_since']:.0f} months since you last spoke. {self.org_name(r['org'])} "
                                      f"({rel}) just filed: {ev.get('title')}"})
                break
        out.sort(key=lambda x: -x["warmth"]["months_since"])
        return out[:limit]

    # ---- entity resolution --------------------------------------------------
    def resolve(self, name: str, org_hint: str | None = None) -> dict[str, Any] | None:
        """Match a name seen in a filing to a known profile. Org context breaks ties between namesakes."""
        n = norm_name(name)
        best, best_s = None, 0.0
        for pid, p in self.people.items():
            if pid == "you":
                continue
            s = difflib.SequenceMatcher(None, n, norm_name(p["name"])).ratio()
            if org_hint and any(r["org"] == org_hint for r in p.get("roles", [])):
                s += 0.1
            if s > best_s:
                best, best_s = pid, s
        if best and best_s >= 0.82:
            return {"id": best, "confidence": round(min(1.0, best_s), 2)}
        return None

    # ---- serialisation ------------------------------------------------------
    def person_card(self, pid: str) -> dict[str, Any]:
        p = self.people[pid]
        cur = self.current_role(p)
        return {"id": pid, "name": p["name"], "headline": p.get("headline"),
                "org": self.org_name(cur["org"]) if cur else None,
                "initials": "".join(w[0] for w in p["name"].split()[:2]).upper(),
                "is_contact": pid in self.contacts, "simulated": p.get("simulated", False)}

    def person_full(self, pid: str) -> dict[str, Any]:
        p = self.people[pid]
        card = self.person_card(pid)
        roles = [{**r, "org_name": self.org_name(r["org"]), "ticker": self.orgs.get(r["org"], {}).get("ticker")}
                 for r in p.get("roles", [])]
        edu = [{**e, "org_name": self.org_name(e["org"])} for e in p.get("education", [])]
        card.update({"roles": roles, "education": edu, "location": p.get("location"),
                     "credentials": p.get("credentials", []), "source": p.get("source"),
                     "tags": p.get("tags", [])})
        if pid in self.contacts:
            card["warmth"] = self.warmth(pid)
            card["overlap_with_you"] = self.link("you", pid).reasons
        else:
            card["paths"] = self.paths_to(pid)
        return card


_networks: dict[str, Network] = {}


def get() -> Network:
    """The current user's network: shared public profiles + their own private contacts.
    Built once per user and kept in memory (it's small); invalidate() after edits."""
    from ..context import current_user
    from .. import store

    uid = current_user()
    if uid not in _networks:
        _networks[uid] = Network(store.network_raw(uid))
    return _networks[uid]


def invalidate(user_id: str | None = None) -> None:
    from . import recommend

    if user_id:
        _networks.pop(user_id, None)
        recommend._indexes.pop(user_id, None)
    else:
        _networks.clear()
        recommend._indexes.clear()
    recommend._memo.clear()
