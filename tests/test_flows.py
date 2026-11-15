"""Scenario tests for the three network workflows. Deterministic: the web search is replaced by a fake
response in the SDK's shape, and page fetches by fixture text, so the real parsing, verification,
scoring and caching code runs with no network and no spend. Live versions: tests/test_flows_live.py.

  Flow 1  Pull after an update: cause-based ranking of the network; when no one covers the cause,
          verified people from the public web, offered as "add to network".
  Flow 2  Push: web context for an important filing (facts and people, each verified against its
          source), and a push-mode ranking that favours whoever you can actually call today.
  Flow 3  Network growth and keep-in-touch: adding contacts and leads, logging conversations, and
          nudges ranked by how overdue, how valuable, and whether there's a reason to call now.
"""
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from finn import clock, db, store
from finn.api import app
from finn.network import discover as D
from finn.network import graph, nudges
from finn.network import recommend as R

OSAT = "binding term sheet with Vishay for an OSAT semiconductor assembly joint venture"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def seed(uid: str) -> None:
    """Seed a separate user (inside their own RLS scope), so mutations don't touch the demo user."""
    from finn.context import reset_user, set_user

    tok = set_user(uid)
    try:
        store.seed_user(uid)
        graph.invalidate(uid)
    finally:
        reset_user(tok)


def ids(recs):
    return [r["person"]["id"] for r in recs]


def fake_response(text: str, results: list[tuple[str, str, str | None]]):
    """A Messages API response in the SDK's shape: a search, its results, then the model's lines."""
    return NS(content=[NS(type="server_tool_use"),
                       NS(type="web_search_tool_result", content=[NS(url=u, title=t, page_age=a) for u, t, a in results]),
                       NS(type="text", text=text, citations=None)],
              usage=NS(server_tool_use=NS(web_search_requests=1)))


@pytest.fixture
def pages(monkeypatch):
    """Page fetches return fixture text (None = the site blocked us)."""
    book: dict[str, str | None] = {}
    monkeypatch.setattr(D, "page_text", lambda url: book.get(url))
    return book


# ===========================================================================
# Flow 1: pull, cause-based ranking, web fallback
# ===========================================================================
def test_1_1_named_subject_outranks_the_generic_insider():
    """Kaynes signs an OSAT joint venture. The two semiconductor-assembly people should come before the
    Kaynes sales VP: he knows the company, they know the subject."""
    top = R.recommend("kaynes", event="M&A", text=OSAT, k=3)
    assert set(ids(top)[:2]) == {"sneha", "arjun"} and ids(top)[2] == "kiran"
    sneha = next(r for r in top if r["person"]["id"] == "sneha")
    assert sneha["features"]["topic"] == 1.0 and sneha["features"]["function_fit"] == 1.0
    assert any("OSAT" in w for w in sneha["why"])


def test_1_2_without_a_named_subject_the_insider_still_leads():
    """The insider discount applies only when the filing names a subject the company isn't mainly in."""
    assert ids(R.recommend("kaynes", k=3))[0] == "kiran"
    kiran = next(r for r in R.recommend("kaynes", event="M&A", text="board approves results", k=5) if r["person"]["id"] == "kiran")
    assert kiran["features"]["insider"] == 1.0


def test_1_3_a_sector_theme_does_not_override_the_event():
    """'Specialty chemicals M&A' should surface the M&A person, not the plant head whose title happens to
    mention phenolics: a broad sector theme says nothing about which function is needed."""
    assert ids(R.recommend(theme="specialty chemicals", event="M&A", k=3))[0] == "zara"


def test_1_4_coverage_decides_when_to_search_the_web():
    strong = D.coverage(R.recommend("kaynes", event="M&A", text=OSAT, k=4))
    assert strong["level"] == "strong" and not strong["search_web"]
    none = D.coverage(R.recommend(event="capex", text="Titan Company jewellery plant expansion", k=4))
    assert none["level"] == "none" and none["search_web"]


def test_1_5_verification_keeps_only_people_the_source_names(pages):
    """Three claimed people: one is on the fetched page, one cites a URL the search never returned
    (invented source), one isn't on the page it cites (conflated name). Only the first survives."""
    good, other = "https://www.business-standard.com/a", "https://example.com/b"
    pages[good] = "Kaynes Semicon appointed Raghu Panicker as chief executive of the OSAT unit in Sanand."
    pages[other] = "An unrelated page about Kaynes Semicon's facility."
    text = ("PERSON: Raghu Panicker | CEO | Kaynes Semicon | current | runs the OSAT unit | " + good + "\n"
            "PERSON: Asha Invented | CTO | Kaynes Semicon | current | x | https://not-a-result.com/z\n"
            "PERSON: Vikas Conflated | COO | Kaynes Semicon | current | x | " + other)
    found, stats = D._parse(fake_response(text, [(good, "Kaynes Semicon CEO", "30 days ago"), (other, "Facility", None)]))
    assert [f["name"] for f in found] == ["Raghu Panicker"]
    assert stats["url_not_in_results"] == 1 and stats["unverified"] == 1
    assert found[0]["verified"]["how"] == "page" and found[0]["verified"]["org"]
    assert "Raghu Panicker" in found[0]["source"]["quote"] and found[0]["source"]["age_years"] == 0.1


def test_1_6_web_scoring_prefers_current_relevant_well_sourced_people():
    net = graph.get()
    q = R.Query(event="capex", text="Titan Company jewellery plant expansion")
    mk = lambda **kw: {"verified": {"name": True, "full_name": True, "org": True, "how": "page"},  # noqa: E731
                       "source": {"age_years": 0.5}, "current": True, **kw}
    ceo = D.score_candidate(net, q, mk(name="A", title="CEO, Jewellery Division", org_name="Titan Company Limited"), "Titan Company")
    old = D.score_candidate(net, q, mk(name="B", title="Managing Director", org_name="Titan Industries Ltd (now Titan Company)",
                                       current=False, source={"age_years": 13.5}), "Titan Company")
    expert = D.score_candidate(net, q, mk(name="C", title="Retail analyst", org_name="Some Brokerage"), "Titan Company")
    assert ceo["fit"] > old["fit"] > expert["fit"]
    assert ceo["features"]["company"] == 1.0 and old["features"]["company"] == 0.7   # matched by name: Titan isn't in the map
    assert expert["fit"] < D.MIN_FIT                                               # filtered out


def test_1_7_discover_dedupes_finds_intros_and_caches(monkeypatch, pages):
    """End to end with a fake search: a lead at Kaynes comes with an intro via Kiran (who works there);
    someone already in the network is reported, not re-suggested; the second call is served from cache."""
    url = "https://www.business-standard.com/kaynes"
    pages[url] = "Kaynes Semicon chief executive Raghu Panicker said ... Kiran Reddy, VP Sales at Kaynes Technology, added ..."
    calls = []

    def fake_search(company, event, topics, text):
        calls.append(1)
        return D._parse(fake_response(
            f"PERSON: Raghu Panicker | Managing Director & CEO | Kaynes Semicon | current | runs OSAT | {url}\n"
            f"PERSON: Kiran Reddy | VP Sales | Kaynes Technology | current | sells to customers | {url}",
            [(url, "Kaynes Semicon", "10 days ago")]))

    monkeypatch.setattr(D, "LLM_ENABLED", True)
    monkeypatch.setattr(D, "_search", fake_search)
    out = D.discover(org="kaynes", event="M&A", text=OSAT + " test-1-7")
    assert [c["name"] for c in out["candidates"]] == ["Raghu Panicker"]
    assert out["already_in_network"][0]["id"] == "kiran"
    lead = out["candidates"][0]
    assert lead["intro"][0]["id"] == "kiran" and lead["label"].startswith("From the public web")
    again = D.discover(org="kaynes", event="M&A", text=OSAT + " test-1-7")
    assert again["cached"] and len(calls) == 1


def test_1_8_without_a_key_discovery_says_so():
    out = D.discover(event="capex", text="anything")
    assert out["status"] == "unavailable" and out["candidates"] == []


def test_1_9_dossier_cause_comes_from_the_latest_material_update():
    from finn import pull

    now = clock.now()
    c = {"name": "Kaynes Technology", "announcements": [
        {"title": "Analyst / Investor Meet - Intimation", "summary": "Earnings call", "ts": now.isoformat()},
        {"title": "Execution Of Binding Term Sheet", "summary": "Vishay to hold 49% stake in India OSAT business.",
         "ts": (now - timedelta(days=2)).isoformat()},
    ]}
    cause = pull._cause(c, "kaynes", ["electronics manufacturing"])
    assert cause["event"] == "M&A" and cause["coverage"]["level"] == "strong"   # skipped the routine meeting notice
    assert ids(cause["people"])[0] in ("sneha", "arjun")
    stale = {"name": "X", "announcements": [{"title": "Execution Of Binding Term Sheet", "summary": "",
                                              "ts": (now - timedelta(days=90)).isoformat()}]}
    assert pull._cause(stale, "kaynes", []) is None                              # older than the window


def test_1_10_adding_a_web_lead_to_the_network(client):
    h = {"X-FINN-User": "flow1"}
    seed("flow1")
    body = {"name": "Raghu Panicker", "title": "Managing Director & CEO", "org_name": "Kaynes Technology",
            "lead": True, "source_url": "https://www.business-standard.com/kaynes"}
    r = client.post("/api/network/contacts", json=body, headers=h).json()
    assert not r["existed"] and r["person"]["is_contact"]
    p = client.get(f"/api/people/{r['id']}", headers=h).json()
    assert p["warmth"]["label"] == "not yet contacted" and p["roles"][0]["org"] == "kaynes"
    assert client.post("/api/network/contacts", json=body, headers=h).json()["existed"]          # dedupe
    assert client.get(f"/api/people/{r['id']}", headers={"X-FINN-User": "someone-else"}).status_code == 404  # private


# ===========================================================================
# Flow 2: push, web context first, then push-mode people
# ===========================================================================
from finn import enrich as E  # noqa: E402

TP_URL = "https://www.business-standard.com/tatapower"
TP_PAGE = ("Tata Power names Sanjay Banga as CEO-designate. Banga, who heads Tata Power Renewable Energy, will take over "
           "from Praveer Sinha on 1 May 2027 for five years. The board also approved Rs 11,871 crore for a 1,800 MW "
           "pumped storage project at Shirwata.")


def test_2_1_fact_check_needs_every_number_on_the_page():
    assert E.verify_fact("The board approved ₹11,871 crore for a 1,800 MW pumped storage project.", TP_PAGE)["ok"]
    bad = E.verify_fact("The board approved ₹11,871 crore for a 1,900 MW pumped storage project.", TP_PAGE)
    assert not bad["ok"] and bad["missing"] == ["1900"]
    assert E.verify_fact("Banga heads Tata Power Renewable Energy and takes over from Praveer Sinha.", TP_PAGE)["ok"]
    assert not E.verify_fact("Analysts expect margins to collapse after aggressive bidding.", TP_PAGE)["ok"]


def test_2_2_enrichment_parses_verifies_and_connects(monkeypatch, pages):
    pages[TP_URL] = TP_PAGE
    text = (f"FACT: Banga takes over on 1 May 2027 for five years. | {TP_URL}\n"
            f"FACT: The project will cost ₹12,500 crore. | {TP_URL}\n"
            "FACT: Shares rose 4% on the news. | https://invented.example/x\n"
            f"PERSON: Sanjay Banga | CEO-designate | Tata Power | incoming MD & CEO | "
            f"CEO at Tata Power Renewable Energy (2022-present); President at Tata Power (2018-2022) | {TP_URL}")
    web, stats = E._parse(fake_response(text, [(TP_URL, "Tata Power names CEO-designate", "2 days ago")]))
    assert [f["text"][:12] for f in web["facts"]] == ["Banga takes "]          # wrong number and invented source dropped
    assert stats["unverified"] == 1 and stats["url_not_in_results"] == 1
    assert web["facts"][0]["source"]["tier"] == "press"
    banga = web["people"][0]
    assert banga["prior"][1] == {"title": "President", "org_name": "Tata Power", "from": "2018", "to": "2022"}
    routes = E.connect(web)["people"][0]["routes"]
    assert routes and routes[0]["id"] == "ishaan"                              # Ishaan worked at Tata Power


def test_2_3_budget_eligibility_and_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(E, "LLM_ENABLED", True)
    monkeypatch.setattr(E, "_search", lambda item: (calls.append(item["id"]) or ({"facts": [], "people": []}, {"searches": 1})))
    real = {"id": "t-2-3-real", "ticker": "TATAPOWER", "company": "Tata Power", "title": "Change in Management",
            "ts": clock.now().isoformat(), "score": 90, "source": "nse_rss", "category": "management_change"}
    sim = {**real, "id": "t-2-3-sim", "source": "scenario"}
    routine = {**real, "id": "t-2-3-routine", "score": 10}
    assert not E.eligible(sim) and not E.eligible(routine) and E.eligible(real)
    monkeypatch.setattr(E, "spent_today", lambda: 999.0)                        # over budget: nothing runs
    assert E.enrich_pending([real, sim, routine])["enriched"] == 0 and calls == []
    monkeypatch.setattr(E, "spent_today", lambda: 0.0)
    assert E.enrich_pending([real, sim, routine])["enriched"] == 1
    assert E.enrich_pending([real])["enriched"] == 0 and calls == ["t-2-3-real"]   # cached forever per filing
    items = [dict(real)]
    E.attach(items)
    assert items[0]["web"]["label"].startswith("From the web")


def test_2_3b_trusted_sources_first_open_web_only_when_thin(monkeypatch):
    """Exchanges, regulators, rating agencies and reachable business press first; the open web is a
    second, paid call only when the trusted pass finds fewer than two verified items."""
    fact = lambda t, host: {"text": t, "source": {"url": f"https://{host}/x", "tier": E.source_tier(f"https://{host}/x")}}  # noqa: E731
    calls = []

    def one(item, domains):
        calls.append("trusted" if domains else "open")
        if domains:
            return {"facts": [fact("A 1", "www.business-standard.com")] * (2 if item["id"] == "rich" else 0), "people": []}, {"searches": 1}
        return {"facts": [fact("B 2", "someblog.in")], "people": []}, {"searches": 1}

    monkeypatch.setattr(E, "_one", one)
    out, st = E._search({"id": "rich"})
    assert calls == ["trusted"] and st["pass"] == "trusted"
    calls.clear()
    out, st = E._search({"id": "thin"})
    assert calls == ["trusted", "open"] and st["pass"] == "trusted+open" and out["facts"][0]["source"]["tier"] == "other"
    assert "economictimes.indiatimes.com" not in E.TRUSTED_DOMAINS        # blocks the crawler: can never be cited


def test_2_4_push_scores_the_value_of_one_call_today():
    """Push asks 'who do I call now?': EV = P(help) x P(pick up) x freshness, multiplied in odds space.
    Relevance still leads (Sneha, the OSAT expert, stays first though the tie is dormant), the warmer
    of two similar people is preferred, and a weak match (Kiran, sales) drops below the push bar, so
    an alert interrupts you with fewer, surer names (evals: precision 0.97 vs 0.89 on what's shown)."""
    pull = R.recommend(theme="semiconductors", k=3, mode="pull")
    push = R.recommend(theme="semiconductors", k=3, mode="push")
    assert ids(pull)[0] == ids(push)[0] == "sneha"
    cv = {r["person"]["id"]: r["call_value"] for r in push}
    assert cv["sneha"]["reachable"] < cv["arjun"]["reachable"] and cv["sneha"]["relevance"] > cv["arjun"]["relevance"]
    kiran_pull = next(r["score"] for r in pull if r["person"]["id"] == "kiran")
    kiran_push = next(r["score"] for r in push if r["person"]["id"] == "kiran")
    assert kiran_pull >= 0.5 > kiran_push                                       # shown by pull, gated out of an alert
    p, parts = R.push_value(0.9, {"warmth": 1.0, "recency": 1.0})
    assert abs(p - 0.9) < 1e-9 and parts == {"relevance": 0.9, "reachable": 1.0, "fresh": 1.0}
    cold, _ = R.push_value(0.9, {"warmth": 0.0, "recency": 0.0})
    assert 0.6 < cold < 0.9                                                     # cold costs at most ~1.5 log-odds


def test_2_5_brief_uses_push_mode_and_carries_nudges(client):
    b = client.get("/api/brief?window=24h&level=material").json()
    with_people = [i for i in b["items"] if i.get("people")]
    assert with_people and all(p["mode"] == "push" for i in with_people for p in i["people"])
    assert "nudges" in b and len(b["nudges"]) <= 3


def test_2_6_feedback_learns_per_mode():
    seed("flow2")
    from finn.context import reset_user, set_user

    tok = set_user("flow2")
    try:
        graph.invalidate("flow2")
        rec = R.recommend("kaynes", event="M&A", text=OSAT, k=1, mode="push")[0]
        R.feedback(rec["person"]["id"], rec["query"], 1)
        assert R.weights("push") != R.PRIORS["push"] and R.weights("pull") == R.PRIORS["pull"]
        assert R.model_card("push")["mode"] == "push"
    finally:
        reset_user(tok)


# ===========================================================================
# Flow 3: adding people, keep-in-touch nudges
# ===========================================================================
def test_3_1_cadence_follows_the_rhythm_you_actually_keep():
    assert nudges.cadence_days({"type": "friend", "interactions_12m": 11}) == 30      # monthly
    assert nudges.cadence_days({"type": "friend", "interactions_12m": 0}) == 60       # capped by the tie
    assert nudges.cadence_days({"type": "met", "interactions_12m": 50}) == 21         # floor
    assert [nudges.urgency(x) for x in (0.8, 1.0, 2.0, 3.0, 5.0)] == [0.0, 0.4, 0.7, 1.0, 1.0]
    assert nudges.hook_strength(40) == 0.0 and nudges.hook_strength(45) == 0.4 and nudges.hook_strength(95) == 1.0


def test_3_2_overdue_portfolio_contacts_with_a_reason_to_call_rank_first():
    out = nudges.compute(include_all=True)
    by = {n["person"]["id"]: n for n in out}
    g = by["gaurav"]   # at Deepak Nitrite (held), which just filed a CFO change
    assert g["hook"] and g["hook"]["org"] == "Deepak Nitrite" and g["scores"]["value"] >= 0.8
    no_link = [n for n in out if n["scores"]["value"] == 0 and n["overdue"] >= g["overdue"]]
    assert all(g["scores"]["priority"] > n["scores"]["priority"] for n in no_link)
    assert "you usually talk" in g["reason"] and "Deepak Nitrite" in g["reason"]
    assert out == sorted(out, key=lambda n: -n["scores"]["priority"])


def test_3_3_logging_a_conversation_clears_the_nudge(client):
    h = {"X-FINN-User": "flow3"}
    seed("flow3")
    before = {n["person"]["id"] for n in client.get("/api/network/nudges?all=true", headers=h).json()["items"] if n["due"]}
    assert "gaurav" in before
    r = client.post("/api/network/contacts/gaurav/touch", json={"note": "Caught up on the new CFO"}, headers=h).json()
    assert r["warmth"]["label"] == "warm" and r["interactions_12m"] == 2
    after = {n["person"]["id"]: n for n in client.get("/api/network/nudges?all=true", headers=h).json()["items"]}
    assert not after["gaurav"]["due"] and after["gaurav"]["days_since"] == 0
    assert client.post("/api/network/contacts/nobody/touch", json={}, headers=h).status_code == 404


def test_3_4_a_new_lead_gets_a_first_contact_nudge_with_an_intro(client):
    h = {"X-FINN-User": "flow3b"}
    seed("flow3b")
    r = client.post("/api/network/contacts", headers=h, json={
        "name": "Raghu Panicker", "title": "CEO", "org_name": "Kaynes Technology", "lead": True}).json()
    from finn.context import reset_user, set_user

    tok = set_user("flow3b")
    try:   # pretend it was added 8 days ago
        with store.tx() as c:
            c.execute(store.contacts.update().where(store.contacts.c.person_id == f"flow3b:{r['id']}")
                      .values(since=(clock.now().date() - timedelta(days=8)).isoformat()))
        graph.invalidate("flow3b")
        n = next(x for x in nudges.compute(include_all=True) if x["person"]["id"] == r["id"])
    finally:
        reset_user(tok)
    assert n["lead"] and n["due"] and n["intro"]["id"] == "kiran"
    assert "haven't reached out" in n["reason"] and "Kiran" in n["reason"]


def test_3_5_weekly_push_never_repeats_a_person(monkeypatch):
    from finn import notify

    sent = []
    monkeypatch.setattr(store, "push_subs_for", lambda user_id=None: [{"endpoint": "x"}])
    monkeypatch.setattr(notify, "send", lambda title, body, **kw: sent.append((title, kw["tag"])) or {"sent": 1})
    first = nudges.dispatch()
    second = nudges.dispatch()
    assert 1 <= first["items"] <= 2 and second["items"] == 0 and "this week" in second["reason"]
    assert len({tag for _, tag in sent}) == len(sent) == first["items"]           # one push per person
    assert all(t.startswith(("Reconnect with", "Introduce yourself to")) for t, _ in sent)


def test_3_6_draft_never_sends_the_contacts_name_to_the_model(monkeypatch):
    from finn.intel import llm

    seen = []
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "complete", lambda purpose, prompt, **kw: seen.append(prompt + kw.get("system", "")) or
                        "Hi [NAME], saw the CFO news at Deepak Nitrite. Coffee next week?")
    d = nudges.draft("gaurav")
    assert d["source"] == "model" and d["text"].startswith("Hi Gaurav")
    assert seen and "Gaurav" not in seen[0] and "[NAME]" in seen[0]


def test_3_7_draft_falls_back_to_a_template_without_a_key():
    d = nudges.draft("gaurav")
    assert d["source"] == "template" and "Gaurav" in d["text"] and "[NAME]" not in d["text"]
