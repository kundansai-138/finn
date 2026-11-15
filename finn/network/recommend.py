"""Network recommender: who in your network is best placed to help with *this*.

Built like a production recommendation system, scaled down to one investor's network:

  1. CANDIDATE GENERATION (recall): union of four independent sources, so one weak signal
     can't hide a good person.
       a. structural: worked at the company, a competitor, a customer or a supplier
       b. graph walk: personalised PageRank on a people-org-theme graph seeded at the
          query (Pinterest's Pixie idea: random walks find multi-hop relevance)
       c. domain similarity: time-decayed career "theme vectors", smoothed by theme
          co-occurrence (item-item similarity: agrochemicals and specialty chemicals share
          companies, so experience in one partly counts for the other)
       d. text similarity: TF-IDF between the event or question and each profile

  2. RANKING (precision): a logistic model over explainable features. Weights start from hand-set
     priors and learn from the investor's "helpful / not relevant" feedback (online SGD with an
     L2 pull back towards the prior, so a handful of clicks nudges it rather than overturning it).

  3. RE-RANKING: MMR diversity, so the top three aren't three people from the same company.

Each result carries per-feature contributions ("why this person") and an evidence graph
(You → person → bridge → target) that the UI draws. No model calls: it's fast, private, and
every number can be explained.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np

from .. import store
from ..context import current_user
from .graph import EVENT_FN_BOOST, KNOWS, Network, _ym, _yr, get as get_network

# ---------------------------------------------------------------------------
# Features and priors
# ---------------------------------------------------------------------------
FEATURES = ["insider", "adjacent", "domain", "topic", "graph", "text", "function_fit", "recency", "seniority", "warmth"]
FEATURE_LABEL = {
    "insider": "Worked there", "adjacent": "Competitor / customer side", "domain": "Domain overlap",
    "topic": "Knows this exact topic", "graph": "Network proximity", "text": "Matches the wording",
    "function_fit": "Right function for the cause", "recency": "Recent experience", "seniority": "Seniority",
    "warmth": "Warm relationship",
}
# Two scoring engines, because the questions differ:
#   pull  "who understands this best?" (research). A logistic model over all the features; warmth is
#         one signal among many, because for research you can always find a way to reach someone.
#   push  "who should I call about this *today*?" (an alert just landed). The expected value of one
#         call, a product, not a sum:
#             EV = P(they can help) × P(they'll pick up) × freshness of what they know
#         P(help) is the relevance model with warmth taken out; P(pick up) = 0.35 + 0.65·warmth
#         (a dormant contact still answers sometimes); freshness = 0.6 + 0.4·recency of their
#         relevant experience. Multiplying means warmth can only separate people of similar
#         relevance: a strong tie never lifts someone who can't help, which an additive warmth
#         weight did (measured in evals/run.py `modes`: the additive version lost on both label sets).
PRIORS = {
    "pull": {"bias": -4.6, "insider": 2.6, "adjacent": 1.8, "domain": 1.5, "topic": 2.2, "graph": 1.0, "text": 0.9,
             "function_fit": 1.3, "recency": 0.6, "seniority": 0.5, "warmth": 0.9},
    # push relevance: the same evidence minus warmth and recency, which enter as multipliers instead
    "push": {"bias": -4.2, "insider": 2.6, "adjacent": 1.8, "domain": 1.5, "topic": 2.2, "graph": 1.0, "text": 0.9,
             "function_fit": 1.3, "recency": 0.0, "seniority": 0.5, "warmth": 0.0},
}
REACH_FLOOR, FRESH_FLOOR = 0.35, 0.6


def push_value(p_rel: float, f: dict[str, float]) -> tuple[float, dict[str, float]]:
    """Multiply in odds space, not on saturated probabilities: relevance probabilities for qualified
    people all sit at 0.95-0.99, so a raw product would let reachability swamp real differences in
    relevance. log EV = logit(P help) + ln(reach) + ln(fresh): reach is worth at most ~1 log-odds unit."""
    reach = REACH_FLOOR + (1 - REACH_FLOOR) * f["warmth"]
    fresh = FRESH_FLOOR + (1 - FRESH_FLOOR) * f["recency"]
    z = math.log(max(p_rel, 1e-9) / max(1 - p_rel, 1e-9)) + math.log(reach) + math.log(fresh)
    return 1 / (1 + math.exp(-z)), {"relevance": round(p_rel, 3), "reachable": round(reach, 3), "fresh": round(fresh, 3)}
PRIOR = PRIORS["pull"]
LR, L2 = 0.6, 0.08

# Topic extraction: the cause's own words often name a sub-domain the company's sector tags miss
# ("OSAT" inside an electronics company, "pumped storage" inside a power utility).
TOPIC_PATTERNS = {
    "semiconductors": r"\bosat\b|semiconductor|\bchips?\b|\bfab\b|wafer|assembly (and|&) test",
    "pharma cdmo": r"\bcdmo\b|contract (development|manufacturing)|\bapi\b manufactur|custom synthesis",
    "agrochemicals": r"agro|crop protection|biological|pesticide|fungicide",
    "specialty chemicals": r"specialty chem|fluoro|phenol|nitration",
    "fluorochemicals": r"fluoro|refrigerant",
    "power & renewables": r"pumped storage|\bsolar\b|\bwind\b|renewable|transmission|\bMW\b|\bGW\b",
    "consumer lending": r"consumer (loan|finance|lending)|personal loan|credit card",
    "rural lending": r"rural|tractor|vehicle finance|microfinance",
    "quick commerce": r"dark store|quick commerce|instant delivery",
    "electronics manufacturing": r"\bems\b|electronics manufactur|\bpcb\b|handset|smartphone assembl",
    "retail": r"\bstores?\b|retail|fashion|apparel",
    "defence": r"defen[cs]e|aircraft|helicopter|aerospace",
    "cement": r"cement|clinker",
    "it services": r"it services|software services|digital transformation",
    "pharma": r"usfda|\bfda\b|warning letter|formulation|generic",
}


def topics_from(text: str) -> set[str]:
    return {t for t, rx in TOPIC_PATTERNS.items() if re.search(rx, text or "", re.I)}


def cause_topics(q: "Query") -> set[str]:
    """The subject of the cause: topics named in its text, plus an explicit theme."""
    return topics_from(q.text) | ({q.theme} if q.theme else set())


def role_topic_match(net: Network, r: dict, topics: set[str]) -> float:
    """How directly one role touched the topic: 1.0 the job itself names it ("Director, OSAT Program"),
    0.7 it's the employer's main business, 0.25 it's a side business of the employer."""
    if not topics:
        return 0.0
    if any(re.search(TOPIC_PATTERNS[t], r.get("title", ""), re.I) for t in topics if t in TOPIC_PATTERNS):
        return 1.0
    themes = net.orgs.get(r["org"], {}).get("themes", [])
    if themes and themes[0] in topics:
        return 0.7
    return 0.25 if topics & set(themes) else 0.0


def _seniority(title: str) -> float:
    t = title.split("→")[-1].lower()
    if re.search(r"\b(chief|ceo|cfo|coo|cio|founder|head|vp|vice president|director|plant head|general manager|gm|partner|adviser)\b", t):
        return 1.0
    if re.search(r"\b(senior|manager|lead|controller|principal)\b", t):
        return 0.65
    return 0.4


def _years(r: dict) -> float:
    return max(0.1, _ym(r.get("to"), end=True) - _ym(r["from"]))


def _since(r: dict) -> float:
    return 0.0 if not r.get("to") else max(0.0, _ym(None) - _ym(r["to"], end=True))


# ---------------------------------------------------------------------------
# Index: built once from the network (people, orgs, themes)
# ---------------------------------------------------------------------------
class Index:
    def __init__(self, net: Network):
        self.net = net
        self.contacts = list(net.contacts)
        self.themes = sorted({t for o in net.orgs.values() for t in o.get("themes", [])})
        self.t_idx = {t: i for i, t in enumerate(self.themes)}
        self._theme_similarity()
        self._theme_vectors()
        self._graph()
        self._tfidf()

    # -- item-item similarity between themes, from co-occurrence in companies
    def _theme_similarity(self) -> None:
        orgs = [o for o in self.net.orgs.values() if o.get("themes")]
        M = np.zeros((len(orgs), len(self.themes)))
        for i, o in enumerate(orgs):
            for t in o["themes"]:
                M[i, self.t_idx[t]] = 1.0
        norms = np.linalg.norm(M, axis=0) + 1e-9
        C = (M.T @ M) / np.outer(norms, norms)
        self.S = np.eye(len(self.themes)) + 0.6 * (C - np.diag(np.diag(C)))

    # -- each person as a time-decayed, seniority-weighted vector over themes
    def _theme_vectors(self) -> None:
        self.tv: dict[str, np.ndarray] = {}
        self.tv_raw: dict[str, np.ndarray] = {}
        for pid in self.contacts:
            p = self.net.contacts[pid]
            v = np.zeros(len(self.themes))
            for r in p.get("roles", []):
                w = _years(r) ** 0.7 * math.exp(-_since(r) / 8) * (0.6 + 0.4 * _seniority(r["title"]))
                for t in self.net.orgs.get(r["org"], {}).get("themes", []):
                    v[self.t_idx[t]] += w
            for t in p.get("tags", []):
                if t in self.t_idx:
                    v[self.t_idx[t]] += 1.0
            self.tv_raw[pid] = v
            s = v @ self.S
            n = np.linalg.norm(s)
            self.tv[pid] = s / n if n else s

    # -- heterogeneous graph for personalised PageRank (people, orgs, themes; "you" excluded:
    #    relevance is about what someone knows, reachability is scored separately)
    def _graph(self) -> None:
        nodes = [f"p:{c}" for c in self.contacts] + [f"o:{o}" for o in self.net.orgs] + [f"t:{t}" for t in self.themes]
        self.nid = {n: i for i, n in enumerate(nodes)}
        A = np.zeros((len(nodes), len(nodes)))

        def link(a: str, b: str, w: float) -> None:
            i, j = self.nid[a], self.nid[b]
            A[i, j] += w
            A[j, i] += w

        for pid in self.contacts:
            p = self.net.contacts[pid]
            for r in p.get("roles", []):
                link(f"p:{pid}", f"o:{r['org']}", math.sqrt(_years(r)) * math.exp(-_since(r) / 6) * (0.6 + 0.4 * _seniority(r["title"])))
            for e in p.get("education", []):
                link(f"p:{pid}", f"o:{e['org']}", 0.25)
        for oid, o in self.net.orgs.items():
            for t in o.get("themes", []):
                link(f"o:{oid}", f"t:{t}", 1.0)
            for peer in o.get("peers", []):
                if f"o:{peer}" in self.nid:
                    link(f"o:{oid}", f"o:{peer}", 0.6)
            for k in ("customers", "suppliers"):
                for x in o.get(k, []):
                    if f"o:{x}" in self.nid:
                        link(f"o:{oid}", f"o:{x}", 0.5)
        deg = A.sum(axis=1, keepdims=True)
        deg[deg == 0] = 1
        self.P = A / deg

    def ppr(self, seeds: dict[str, float], restart: float = 0.3, iters: int = 40) -> np.ndarray:
        s = np.zeros(self.P.shape[0])
        for n, w in seeds.items():
            if n in self.nid:
                s[self.nid[n]] += w
        if not s.sum():
            return s
        s /= s.sum()
        r = s.copy()
        PT = self.P.T
        for _ in range(iters):
            r = (1 - restart) * (PT @ r) + restart * s
        return r

    # -- TF-IDF over profile "documents"
    def _tfidf(self) -> None:
        from sklearn.feature_extraction.text import TfidfVectorizer

        docs = []
        for pid in self.contacts:
            p = self.net.contacts[pid]
            parts = [p.get("headline", "")] + p.get("tags", [])
            for r in p.get("roles", []):
                o = self.net.orgs.get(r["org"], {})
                parts += [r["title"], o.get("name", ""), r.get("function", "")] + o.get("themes", [])
            docs.append(" ".join(parts))
        self.vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True)
        self.X = self.vec.fit_transform(docs)

    def text_sim(self, text: str) -> np.ndarray:
        if not text.strip():
            return np.zeros(len(self.contacts))
        q = self.vec.transform([text])
        return (self.X @ q.T).toarray().ravel()


_indexes: dict[str, Index] = {}


def index() -> Index:
    """Per-user index (each user's network differs); built once, kept in memory."""
    uid = current_user()
    if uid not in _indexes:
        _indexes[uid] = Index(get_network())
    return _indexes[uid]


# ---------------------------------------------------------------------------
# Learned weights (per investor), updated from feedback
# ---------------------------------------------------------------------------
def _stored() -> dict:
    s = store.reco_weights_get() or {}
    if s and "pull" not in s and "push" not in s:   # older single-mode format
        s = {"pull": s}
    return s


def weights(mode: str = "pull") -> dict[str, float]:
    return {**PRIORS[mode], **(_stored().get(mode) or {})}


def _score(f: dict[str, float], w: dict[str, float]) -> tuple[float, dict[str, float]]:
    contrib = {k: w[k] * f[k] for k in FEATURES}
    z = w["bias"] + sum(contrib.values())
    return 1 / (1 + math.exp(-z)), contrib


@dataclass
class Query:
    org: str | None = None
    theme: str | None = None
    event: str | None = None
    text: str = ""
    mode: str = "pull"

    @property
    def key(self) -> str:
        return hashlib.sha1(json.dumps([self.org, self.theme, self.event, self.text[:200], self.mode]).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Features for one (query, person)
# ---------------------------------------------------------------------------
def _features(ix: Index, q: Query, pid: str, ppr_norm: float, text_norm: float, qvec: np.ndarray) -> tuple[dict, dict]:
    net = ix.net
    p = net.contacts[pid]
    org = net.orgs.get(q.org or "", {})
    peers = set(org.get("peers", [])) | {o for o, d in net.orgs.items() if q.org and q.org in d.get("peers", [])}
    cust = set(org.get("customers", [])) | set(org.get("suppliers", []))
    qthemes = set(org.get("themes", [])) | ({q.theme} if q.theme else set())
    f = dict.fromkeys(FEATURES, 0.0)
    best_role, best_rel = None, 0.0
    bridges: list[dict] = []
    for r in p.get("roles", []):
        yrs, since = _years(r), _since(r)
        rel = 0.0
        if q.org and r["org"] == q.org:
            v = (0.85 + 0.15 * min(1, yrs / 3)) if not r.get("to") else math.exp(-since / 5) * (0.6 + 0.3 * min(1, yrs / 3))
            f["insider"] = max(f["insider"], v)
            rel = 1.0
            bridges.append({"kind": "insider", "org": r["org"], "role": r, "w": v})
        elif r["org"] in peers or r["org"] in cust:
            base = 0.75 if r["org"] in peers else 0.65
            v = base * math.exp(-since / 6) * min(1, (yrs / 2) ** 0.5)
            f["adjacent"] = max(f["adjacent"], v)
            rel = 0.8
            bridges.append({"kind": "peer" if r["org"] in peers else "customer", "org": r["org"], "role": r, "w": v})
        elif qthemes & set(net.orgs.get(r["org"], {}).get("themes", [])):
            rel = 0.6
            bridges.append({"kind": "theme", "org": r["org"], "role": r, "w": 0.5 * math.exp(-since / 6),
                            "themes": sorted(qthemes & set(net.orgs.get(r["org"], {}).get("themes", [])))})
        if rel:
            f["recency"] = max(f["recency"], 1.0 if not r.get("to") else math.exp(-since / 4))
            f["seniority"] = max(f["seniority"], _seniority(r["title"]))
            if rel > best_rel or (rel == best_rel and not r.get("to")):
                best_role, best_rel = r, rel
    f["domain"] = float(max(0.0, ix.tv[pid] @ qvec)) if qvec.any() else 0.0
    # Topic: years doing the *specific subject* the cause names, credited at role level so that
    # working at a company that merely has a side business in it doesn't count as expertise.
    topic_years, topic_role = 0.0, None
    tq = cause_topics(q)
    for r in p.get("roles", []):
        m = role_topic_match(net, r, tq)
        if m:
            topic_years += m * _years(r) * math.exp(-_since(r) / 8)
            if m >= 0.7 and (topic_role is None or not r.get("to")):
                topic_role = r
            if not any(b["org"] == r["org"] for b in bridges):
                bridges.append({"kind": "theme", "org": r["org"], "role": r, "w": 0.45 * math.exp(-_since(r) / 6),
                                "themes": sorted(tq & set(net.orgs.get(r["org"], {}).get("themes", []))) or sorted(tq)})
    for _t in tq & set(p.get("tags", [])):
        topic_years += 1.5
    f["topic"] = min(1.0, topic_years / 6)
    f["graph"] = ppr_norm
    f["text"] = min(1.0, text_norm)
    fn = (best_role or {}).get("function") or (net.current_role(p) or {}).get("function")
    if q.event and q.event in EVENT_FN_BOOST:
        f["function_fit"] = max(0.0, (EVENT_FN_BOOST[q.event].get(fn, 1.0) - 1.0) / 0.3)
    elif best_role:
        f["function_fit"] = 0.4
    # The cause has two halves: the event (what kind of action) and the subject the filing names
    # ("an OSAT joint venture"). Someone who ran that specific subject fits the cause whatever the
    # event type. Only subjects named in the text count here: a broad sector theme says nothing about
    # which function is needed, so the event alone decides that.
    named = topics_from(q.text)
    if named:
        direct = max((role_topic_match(net, r, named) for r in p.get("roles", [])), default=0.0)
        if direct >= 0.7:
            f["function_fit"] = max(f["function_fit"], 1.0 if direct >= 1.0 else 0.6)
    # Insider knowledge is about the company, not necessarily the subject. When the cause names
    # a subject the company isn't primarily in (a new venture), discount insiders who don't touch it.
    if f["insider"] and named and not (named & set(org.get("themes", [])[:1])):
        own = max((role_topic_match(net, r, named) for r in p.get("roles", []) if r["org"] == q.org), default=0.0)
        f["insider"] *= 0.5 + 0.5 * max(own, min(1.0, f["function_fit"]))
    f["warmth"] = net.warmth(pid)["score"]
    # theme years for the evidence graph and the expertise map
    theme_years = {}
    for t in qthemes:
        y = sum(_years(r) for r in p.get("roles", []) if t in net.orgs.get(r["org"], {}).get("themes", []))
        if y > 0:
            theme_years[t] = round(y, 1)
    raw = ix.tv_raw[pid]
    top_t = ix.themes[int(np.argmax(raw))] if raw.any() else None
    top_y = round(sum(_years(r) for r in p.get("roles", []) if top_t in net.orgs.get(r["org"], {}).get("themes", [])), 1) if top_t else 0
    kind_rank = {"insider": 0, "peer": 1, "customer": 1, "theme": 2}
    bridges.sort(key=lambda b: (kind_rank[b["kind"]], -b["w"]))
    return f, {"best_role": best_role, "function": fn, "bridges": bridges, "theme_years": theme_years,
               "top_theme": (top_t, top_y), "topic_role": topic_role, "topics": sorted(tq)}


def _why(net: Network, f: dict, ctx: dict, contrib: dict, q: Query) -> list[str]:
    out = []
    for b in ctx["bridges"][:2]:
        r = b["role"]
        title = r["title"].split("→")[-1].strip()
        span = f"{_yr(r['from'])}–{_yr(r.get('to'))}"
        name = net.org_name(b["org"])
        if b["kind"] == "insider":
            out.append(f"{'Is' if not r.get('to') else 'Was'} {title} at {name} ({span})")
        elif b["kind"] in ("peer", "customer"):
            out.append(f"{title} at {name}, a {'direct competitor' if b['kind'] == 'peer' else 'customer/supplier'} ({span})")
        else:
            out.append(f"{title} at {name} ({span}): {', '.join(b['themes'])}")
    if ctx["theme_years"] and not any(b["kind"] == "insider" for b in ctx["bridges"][:1]):
        t, y = max(ctx["theme_years"].items(), key=lambda kv: kv[1])
        out.append(f"{y:.0f} years in {t}")
    if not out and ctx.get("top_theme", (None,))[0]:
        t, y = ctx["top_theme"]
        out.append(f"{y:.0f} years in {t}, an adjacent domain")
    tr = ctx.get("topic_role")
    if tr and f["topic"] >= 0.5 and not any(tr["title"] in o for o in out):
        out.append(f"Worked directly on {', '.join(ctx['topics'])}: {tr['title'].split('→')[-1].strip()}, {net.org_name(tr['org'])}")
    elif contrib.get("function_fit", 0) > 0.5 and ctx["function"] and q.event:
        out.append(f"{ctx['function'].replace('-', ' ').capitalize()} background suits a {q.event.replace('_', ' ')} question")
    return out[:3]


# ---------------------------------------------------------------------------
# Evidence graph: You → person → bridge(s) → target
# ---------------------------------------------------------------------------
def evidence_graph(net: Network, pid: str, q: Query, ctx: dict, f: dict) -> dict[str, Any]:
    p = net.contacts[pid]
    w = net.warmth(pid)
    target_label = net.org_name(q.org) if q.org else (q.theme or "this topic").title()
    nodes = [
        {"id": "you", "label": "You", "kind": "you", "col": 0},
        {"id": pid, "label": p["name"], "sub": (net.current_role(p) or {}).get("title", "").split("→")[-1].strip(), "kind": "person", "col": 1},
        {"id": "target", "label": target_label, "sub": (q.event or "").replace("_", " "), "kind": "target", "col": 3},
    ]
    edges = [{"from": "you", "to": pid, "label": f"{w.get('type', '')} · {w.get('label', '')}", "w": w["score"]}]
    seen = set()
    for b in ctx["bridges"][:3]:
        r = b["role"]
        span = f"{_yr(r['from'])}–{_yr(r.get('to'))}"
        title = r["title"].split("→")[-1].strip()
        if b["kind"] == "insider":
            edges.append({"from": pid, "to": "target", "label": f"{title}, {span}", "w": b["w"]})
            continue
        nid = f"o:{b['org']}"
        if nid in seen:
            continue
        seen.add(nid)
        nodes.append({"id": nid, "label": net.org_name(b["org"]), "kind": "org", "col": 2})
        edges.append({"from": pid, "to": nid, "label": f"{title}, {span}", "w": b["w"]})
        rel = {"peer": "competitor", "customer": "customer / supplier", "theme": " · ".join(b.get("themes", []))}[b["kind"]]
        edges.append({"from": nid, "to": "target", "label": rel, "w": b["w"]})
    if not ctx["bridges"]:
        same = bool(ctx["theme_years"])
        t, y = max(ctx["theme_years"].items(), key=lambda kv: kv[1]) if same else ctx.get("top_theme", (None, 0))
        if t:
            nodes.append({"id": f"t:{t}", "label": t, "kind": "theme", "col": 2})
            edges.append({"from": pid, "to": f"t:{t}", "label": f"{y:.0f} yrs", "w": f["domain"]})
            edges.append({"from": f"t:{t}", "to": "target", "label": "same domain" if same else "related domain", "w": f["domain"]})
    return {"nodes": nodes, "edges": edges}


def with_graphs(net: Network, paths: list[dict], target_id: str) -> list[dict]:
    for p in paths:
        p["graph"] = path_graph(net, p, target_id)
    return paths


def path_graph(net: Network, path: dict, target_id: str) -> dict[str, Any]:
    """Evidence graph for a route to a *person* (e.g. an incoming CFO)."""
    t = net.people[target_id]
    nodes = [{"id": "you", "label": "You", "kind": "you", "col": 0},
             {"id": target_id, "label": t["name"], "sub": t.get("headline", ""), "kind": "target", "col": 3}]
    edges = []
    via = path.get("via")
    src = "you"
    if via:
        nodes.append({"id": via["id"], "label": via["name"], "sub": via.get("headline", ""), "kind": "person", "col": 1})
        edges.append({"from": "you", "to": via["id"], "label": f"{path['warmth'].get('type', '')} · {path['warmth'].get('label', '')}", "w": path["warmth"]["score"]})
        src = via["id"]
    for k, oid in enumerate(path.get("orgs", [])[:2]):
        nodes.append({"id": f"o:{oid}", "label": net.org_name(oid), "kind": "org", "col": 2})
        reason = next((w for w in path.get("why", []) if net.org_name(oid) in w), "")
        edges.append({"from": src, "to": f"o:{oid}", "label": reason.split(":")[0].replace(net.org_name(oid) + ", ", ""), "w": path.get("link", path["score"])})
        edges.append({"from": f"o:{oid}", "to": target_id, "label": "overlapped here", "w": path.get("link", path["score"])})
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_memo: dict[tuple, list] = {}
MEMO_MAX = 2000


def recommend(org: str | None = None, theme: str | None = None, event: str | None = None,
              text: str = "", k: int = 4, min_score: float = 0.0, diversify: bool = True,
              mode: str = "pull") -> list[dict[str, Any]]:
    """Memoised per user: the brief asks about the same filings repeatedly. Cleared on feedback."""
    key = (current_user(), org, theme, event, text, k, min_score, diversify, mode)
    hit = _memo.get(key)
    if hit is not None:
        return hit
    out = _recommend(org, theme, event, text, k, min_score, diversify, mode)
    if len(_memo) > MEMO_MAX:
        _memo.clear()
    _memo[key] = out
    return out


def _recommend(org: str | None, theme: str | None, event: str | None, text: str, k: int, min_score: float,
               diversify: bool, mode: str = "pull") -> list[dict[str, Any]]:
    ix = index()
    net = ix.net
    q = Query(org=org, theme=theme.lower() if theme else None, event=event, text=text or "", mode=mode)
    o = net.orgs.get(org or "", {})
    qthemes = (list(o.get("themes", [])) + ([q.theme] if q.theme and q.theme in ix.t_idx else [])
               + [t for t in topics_from(q.text) if t in ix.t_idx])
    qvec = np.zeros(len(ix.themes))
    for t in qthemes:
        if t in ix.t_idx:
            qvec[ix.t_idx[t]] += 1.0
    qvec = qvec @ ix.S
    if np.linalg.norm(qvec):
        qvec /= np.linalg.norm(qvec)

    seeds = {}
    if org:
        seeds[f"o:{org}"] = 1.0
        for p in o.get("peers", []):
            seeds[f"o:{p}"] = 0.3
    for t in qthemes:
        seeds[f"t:{t}"] = seeds.get(f"t:{t}", 0) + 0.5
    r = ix.ppr(seeds)
    ppr = np.array([r[ix.nid[f"p:{c}"]] for c in ix.contacts])
    ppr_n = ppr / ppr.max() if ppr.max() > 0 else ppr
    qtext = " ".join(filter(None, [text, o.get("name"), " ".join(qthemes), (event or "").replace("_", " ")]))
    ts = ix.text_sim(qtext)
    ts_n = ts / ts.max() if ts.max() > 0 else ts

    # Stage 1: candidate generation, a union of independent sources
    cands: set[str] = set()
    for i, pid in enumerate(ix.contacts):
        roles = net.contacts[pid].get("roles", [])
        if org and any(rr["org"] == org for rr in roles):
            cands.add(pid)
    cands |= {ix.contacts[i] for i in np.argsort(-ppr_n)[:8] if ppr_n[i] > 0.15}
    dom = np.array([float(ix.tv[c] @ qvec) if qvec.any() else 0.0 for c in ix.contacts])
    cands |= {ix.contacts[i] for i in np.argsort(-dom)[:8] if dom[i] > 0.2}
    cands |= {ix.contacts[i] for i in np.argsort(-ts_n)[:4] if ts[i] > 0.08}

    # Stage 2: ranking
    w = weights(mode)
    scored = []
    for pid in cands:
        i = ix.contacts.index(pid)
        f, ctx = _features(ix, q, pid, float(ppr_n[i]), float(ts_n[i]), qvec)
        if not (f["insider"] or f["adjacent"] or f["topic"] or f["domain"] >= 0.2 or f["graph"] >= 0.3):
            continue
        p_, contrib = _score(f, w)
        parts = None
        if mode == "push":
            p_, parts = push_value(p_, f)
        if p_ < min_score:
            continue
        scored.append((p_, pid, f, ctx, contrib, parts))
    scored.sort(key=lambda x: -x[0])

    # Stage 3: MMR diversity (don't stack three people from the same bridge company)
    picked: list[tuple] = []
    pool = scored[:]
    lam = 0.78
    while pool and len(picked) < k:
        def mmr(x):
            if not diversify or not picked:
                return x[0]
            bx = {b["org"] for b in x[3]["bridges"][:1]}  # noqa: B023
            sim = max((1.0 if bx & {b["org"] for b in y[3]["bridges"][:1]} else float(ix.tv[x[1]] @ ix.tv[y[1]]) * 0.5) for y in picked)
            return lam * x[0] - (1 - lam) * sim
        best = max(pool, key=mmr)
        picked.append(best)
        pool.remove(best)

    out = []
    for p_, pid, f, ctx, contrib, parts in picked:
        warm = net.warmth(pid)
        top = sorted(((kk, v) for kk, v in contrib.items() if v > 0.05), key=lambda kv: -kv[1])
        out.append({
            "person": net.person_card(pid),
            "score": round(p_, 3),
            "relevance": round(p_, 3),
            "warmth": warm,
            "why": _why(net, f, ctx, contrib, q),
            "would_know": KNOWS.get(ctx["function"] or "", "the industry's structure"),
            "features": {kk: round(v, 3) for kk, v in f.items()},
            "contributions": [{"feature": kk, "label": FEATURE_LABEL[kk], "value": round(v, 2)} for kk, v in top],
            "years": max(ctx["theme_years"].values()) if ctx["theme_years"] else 0,
            "evidence": [b["role"]["title"].split("→")[-1].strip() + ", " + net.org_name(b["org"]) +
                         f" ({_yr(b['role']['from'])}–{_yr(b['role'].get('to'))})" for b in ctx["bridges"][:3]],
            "graph": evidence_graph(net, pid, q, ctx, f),
            "query": {"org": q.org, "theme": q.theme, "event": q.event, "text": q.text[:200], "key": q.key, "mode": q.mode},
            "mode": q.mode,
            **({"call_value": parts} if parts else {}),
        })
    return out


def feedback(person_id: str, query: dict, label: int) -> dict[str, Any]:
    """Online logistic-regression update from one 'helpful' (1) / 'not relevant' (0) signal."""
    ix = index()
    if person_id not in ix.contacts:
        raise KeyError(person_id)
    mode = query.get("mode") or "pull"
    q = Query(org=query.get("org"), theme=query.get("theme"), event=query.get("event"), text=query.get("text") or "", mode=mode)
    # Recompute features exactly as they were scored
    recs = {r["person"]["id"]: r for r in _recommend(q.org, q.theme, q.event, q.text, 50, 0.0, False, mode)}
    f = recs.get(person_id, {}).get("features") or dict.fromkeys(FEATURES, 0.0)
    w = weights(mode)
    prior = PRIORS[mode]
    p_, _ = _score(f, w)   # push: learn P(help); reach and freshness stay fixed and explainable
    err = float(label) - p_
    for kk in FEATURES:
        if mode == "push" and kk in ("warmth", "recency"):
            continue
        w[kk] += LR * (err * f[kk] - L2 * (w[kk] - prior[kk]))
    w["bias"] += LR * (err - L2 * (w["bias"] - prior["bias"]))
    stored = _stored()
    stored[mode] = w
    store.reco_weights_put(stored)
    for key in [k_ for k_ in _memo if k_[0] == current_user()]:   # rankings change with the weights
        _memo.pop(key, None)
    n = store.reco_feedback_add({"person_id": person_id, "query_key": q.key, "query": query, "label": int(label), "p_before": round(p_, 3)})
    return {"weights": {k: round(v, 3) for k, v in w.items()}, "n_feedback": n, "p_before": round(p_, 3)}


def model_card(mode: str = "pull") -> dict[str, Any]:
    w = weights(mode)
    return {"mode": mode, "features": [{"feature": k, "label": FEATURE_LABEL[k], "prior": PRIORS[mode][k], "learned": round(w[k], 3)}
                                       for k in FEATURES],
            "bias": round(w["bias"], 3), "n_feedback": store.reco_feedback_count()}
