"""Fast checks for the parts where a silent mistake would mislead an investor.

    python -m pytest -q

Network data is synthetic and local; the agent test stubs the model, so nothing here costs money.
Runs against the test database set up in conftest.py.
"""
import json
from types import SimpleNamespace as NS

import pytest

from finn.agent import verify
from finn.intel import classify as C
from finn.network import recommend as R


# ---- filing triage ----------------------------------------------------------
@pytest.mark.parametrize("title,summary,cat", [
    ("Change in Management", "Sanjay Banga appointed CEO Designate; CEO and MD from May 1, 2027.", "management_change"),
    ("General Updates", "HAL to acquire CAE Canada's 50% stake in HATSOFF.", "M&A"),
    ("Monitoring Agency Report", "No deviation in QIP proceeds use.", "unclassified"),   # paperwork stays paperwork
    ("Investor Presentation", "Presentation on Unaudited Financial Results.", "earnings_call"),  # not a results filing
    ("Regulation 30 Capacity Addition", "Board approves ₹11,871 crore investment in 1,800 MW project.", "capex"),
    ("Analyst / Investor Meet - Outcome", "Transcript filed; board approved up to ₹5,000 crore capital raise.", "fund_raise"),
])
def test_classifier_body_escalation(title, summary, cat):
    assert C.classify(title, summary)[0] == cat


def test_division_ceo_is_not_company_ceo():
    assert C.classify("Change in Management", "Shri X appointed CEO of Helicopter Complex")[1] == "division_head"


# ---- grounding check --------------------------------------------------------
EV = [{"id": "E1", "title": "x", "text": "Revenue ₹21,794 cr, PAT 411 cr, margin 2.74%"}]


def test_verifier_accepts_rounding_and_rejects_invention():
    assert verify.check("Revenue was ₹21,794 cr and margin 2.7% [E1].", EV)["ok"]
    assert verify.check("Revenue was ₹25,000 cr [E1].", EV)["unsupported"] == ["₹25,000 cr"]


def test_verifier_flags_uncited_figures_and_unknown_citations():
    assert verify.check("Margin was 2.7%.", EV)["uncited"]
    assert verify.check("Margin 2.7% [E9]", EV)["unknown_citations"] == ["E9"]


def test_verifier_ignores_years_and_list_numbers():
    assert verify.check("In FY27 and 2026 there were 3 calls [E1].", EV)["figures"] == 0


@pytest.mark.parametrize("text", ["This week (Nov 10–15, 2026), six names filed updates.", "I'd need the price reaction on 12 Nov and index moves for 13 Nov.",
                                  "The board met on November 12, 2026 [E1]."])
def test_verifier_treats_dates_as_dates(text):
    assert verify.check(text, EV)["ok"]


@pytest.mark.parametrize("text,bad", [("Revenue Sep 12.5% [E1].", "12.5%"), ("Margin of 2.7 Sep quarter vs 9.9 Mar [E1].", "9.9"),
                                      ("Margin 15 points lower [E1].", "15")])
def test_verifier_doesnt_mistake_figures_for_dates(text, bad):
    assert bad in verify.check(text, EV)["unsupported"]


def test_verifier_checks_arithmetic_on_figures_shown_beside_it():
    ev = [{"id": "E1", "title": "x", "text": "PBT: Sep 2025 924, Sep 2026 532. Other income: Sep 2025 497, Sep 2026 72."}]
    shown = verify.check("Ex other income, PBT was ₹460 cr (₹532 cr − ₹72 cr) vs ₹427 cr (₹924 cr − ₹497 cr) [E1].", ev)
    assert shown["ok"] and shown["derived"] == 2
    assert verify.check("PBT fell 42% from ₹924 cr to ₹532 cr [E1].", ev)["ok"]           # % change of the two
    # the same derived figures without their inputs can't be traced: rejected
    assert verify.check("Core PBT rose to ₹460 cr from ₹427 cr [E1].", ev)["unsupported"] == ["₹460 cr", "₹427 cr"]
    assert not verify.check("PBT fell 30% from ₹924 cr to ₹532 cr [E1].", ev)["ok"]       # wrong arithmetic


# ---- recommender ------------------------------------------------------------
def test_insider_and_function_fit_rank_first():
    recs = R.recommend("lauruslabs", event="capex", k=3)
    assert recs[0]["person"]["id"] == "priya"           # ran Laurus manufacturing: right person for capex
    assert all(r["why"] for r in recs)                  # nobody recommended without a reason
    assert recs[0]["graph"]["edges"]                    # and an evidence graph


def test_domain_similarity_generalises_across_related_themes():
    names = [r["person"]["id"] for r in R.recommend(theme="semiconductors", k=3)]
    assert "sneha" in names


def test_feedback_moves_weights_toward_preference(as_user):
    from finn import store

    with as_user("fb-test"):
        store.seed_user("fb-test")
        before = {r["person"]["id"]: r["score"] for r in R.recommend("piind", event="M&A", k=4)}["zara"]
        for _ in range(3):
            R.feedback("zara", {"org": "piind", "event": "M&A"}, 1)
        after = {r["person"]["id"]: r["score"] for r in R.recommend("piind", event="M&A", k=4)}["zara"]
        assert after > before


# ---- agent graph with a stubbed model -----------------------------------------
def test_agent_repairs_an_ungrounded_answer(monkeypatch):
    from finn.agent import core, memory, tools
    from finn.intel import llm

    monkeypatch.setattr(core, "LLM_ENABLED", True)
    monkeypatch.setattr(tools, "LLM_ENABLED", True)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: None)
    monkeypatch.setattr(memory, "save", lambda t: None)
    monkeypatch.setattr(memory, "learn_from", lambda m: [])
    monkeypatch.setattr(tools, "run", lambda name, args: [{"title": "Dixon snapshot", "source": "computed", "cite": "DIXON",
                                                           "url": None, "text": "TTM margin 3.2%."}])
    n = {"answer": 0}

    def fake_call(purpose, **kw):
        if purpose == "agent_plan":
            return {"steps": [{"tool": "company_overview", "args": dict.fromkeys(core.ARG_KEYS) | {"ticker": "DIXON"}, "why": ""},
                              {"tool": "made_up_tool", "args": dict.fromkeys(core.ARG_KEYS), "why": ""}], "answer_style": "brief"}, None
        if purpose == "agent_answer":
            n["answer"] += 1
            text = "Margin is 3.2% and will hit 9% [E1]." if n["answer"] == 1 else "Margin is 3.2% [E1]."
            return {"answer": text, "confidence": "medium", "follow_ups": []}, None
        raise AssertionError(purpose)

    monkeypatch.setattr(llm, "call", fake_call)
    events = []
    # A comparison goes to the model planner (rules plan single-entity questions on their own).
    st = core.run("How do Dixon's margins compare with peers?", None, None, events.append)
    plan = next(e for e in events if e["type"] == "plan")
    assert [s["tool"] for s in plan["steps"]] == ["company_overview"]   # invented tool dropped
    assert n["answer"] == 2                                              # one repair pass
    assert st["verification"]["ok"]


def test_contact_names_never_reach_the_model(monkeypatch):
    """Privacy: the investor's contacts are pseudonymised in everything sent to the model, then restored."""
    from finn.agent import core, memory, tools
    from finn.intel import llm

    monkeypatch.setattr(core, "LLM_ENABLED", True)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: None)
    monkeypatch.setattr(memory, "save", lambda t: None)
    monkeypatch.setattr(memory, "learn_from", lambda m: [])
    person = {"id": "meera", "name": "Meera Krishnan", "is_contact": True}
    monkeypatch.setattr(tools, "run", lambda name, args: [{"title": "Network: Meera Krishnan", "source": "network", "cite": "your network",
                                                           "url": None, "person": person, "text": "Meera ran PI's plants for 5 years."}])
    sent = []

    def fake_call(purpose, **kw):
        if purpose == "agent_plan":
            return {"steps": [{"tool": "who_can_help", "args": dict.fromkeys(core.ARG_KEYS) | {"ticker": "PIIND"}, "why": ""}],
                    "answer_style": "brief"}, None
        sent.append(kw["system"] + json.dumps(kw["messages"]))
        return {"answer": "Call Contact-1: ran PI's plants for 5 years [E1].", "confidence": "high", "follow_ups": []}, None

    monkeypatch.setattr(llm, "call", fake_call)
    st = core.run("Who should I call about PI Industries?", None, None, lambda e: None)
    assert sent and all("Meera" not in s for s in sent)          # the model never saw the name
    assert "Meera Krishnan" in st["answer"]["answer"]            # the investor does


# ---- the unanswerable route and rules-first planning -------------------------
@pytest.mark.parametrize("q", ["What will the Sensex do tomorrow?", "Will Kaynes stock recover?", "Where will Nifty be next year?",
                               "Is the Sensex going to crash?", "What will Dixon's share price be by year-end?"])
def test_price_forecasts_are_unanswerable(q):
    from finn.agent import core

    assert core.unanswerable(q, entity=True, matched_intents=True) == "forecast"


@pytest.mark.parametrize("q", ["Why did HAL's stock move today?", "What will Dixon's revenue be next year?", "What price did Dixon close at?",
                               "What is the market share of Dixon?", "Is the market pricing in the downgrade?", "Why are Dixon shares down today?",
                               "What changed across my portfolio this week?", "What did PI Industries guide for FY27?"])
def test_grounded_questions_are_not_declined(q):
    from finn.agent import core

    assert core.unanswerable(q, entity=True, matched_intents=True) is None


def _no_model_no_tools(monkeypatch):
    from finn.agent import core, memory, tools
    from finn.intel import llm

    monkeypatch.setattr(core, "LLM_ENABLED", True)
    monkeypatch.setattr(memory, "save", lambda t: None)
    monkeypatch.setattr(memory, "learn_from", lambda m: [])
    monkeypatch.setattr(llm, "complete", lambda *a, **k: None)

    def boom(*a, **k):
        raise AssertionError(f"unexpected call: {a[:1]}")

    monkeypatch.setattr(tools, "run", boom)
    monkeypatch.setattr(llm, "call", boom)
    return core


@pytest.mark.parametrize("q,kind", [("What will the Sensex do tomorrow?", "forecast"), ("Tell me something interesting", "open_ended")])
def test_unanswerable_questions_are_declined_without_tools_or_model(monkeypatch, q, kind):
    """Nothing gathered means nothing to invent from: no tool runs, no model call, a fixed decline."""
    core = _no_model_no_tools(monkeypatch)
    st = core.run(q, None, None, lambda e: None)
    assert st["decline"] == kind and st["plan"] == [] and not st["evidence"]
    assert "couldn't find" in st["answer"]["answer"].lower()
    assert st["answer"]["follow_ups"]                       # narrowed to questions FINN can ground
    assert st["verification"]["ok"] and st["answer_mode"] == "declined"


def test_confident_rule_plan_skips_the_model_planner(monkeypatch):
    from finn.agent import core, memory, tools
    from finn.intel import llm

    monkeypatch.setattr(core, "LLM_ENABLED", True)
    monkeypatch.setattr(memory, "save", lambda t: None)
    monkeypatch.setattr(memory, "learn_from", lambda m: [])
    monkeypatch.setattr(llm, "complete", lambda *a, **k: None)
    monkeypatch.setattr(tools, "run", lambda name, args: [{"title": "Laurus leverage", "source": "computed", "cite": "LAURUSLABS",
                                                           "url": None, "text": "Debt-to-equity Sep 2026 0.43x."}])

    def fake_call(purpose, **kw):
        assert purpose == "agent_answer", purpose               # no planning call
        return {"answer": "Debt-to-equity is 0.43x [E1].", "confidence": "high", "follow_ups": []}, None

    monkeypatch.setattr(llm, "call", fake_call)
    st = core.run("What is Laurus Labs' debt level?", None, None, lambda e: None)
    assert st["plan_source"] == "rules"
    assert any(s["tool"] == "financials" and s["args"].get("statement") == "balance_sheet" for s in st["plan"])
    assert st["verification"]["ok"]


def test_an_empty_rule_plan_gets_one_model_replan(monkeypatch):
    from finn.agent import core, memory, tools
    from finn.intel import llm

    monkeypatch.setattr(core, "LLM_ENABLED", True)
    monkeypatch.setattr(memory, "save", lambda t: None)
    monkeypatch.setattr(memory, "learn_from", lambda m: [])
    monkeypatch.setattr(llm, "complete", lambda *a, **k: None)
    hit = [{"title": "Laurus call", "source": "primary", "cite": "LAURUSLABS call", "url": None, "text": "Net debt is coming down."}]
    monkeypatch.setattr(tools, "run", lambda name, args: hit if name == "search_transcripts" else [])
    planned = []

    def fake_call(purpose, **kw):
        if purpose == "agent_plan":
            planned.append(kw["messages"][0]["content"])
            return {"steps": [{"tool": "search_transcripts", "args": dict.fromkeys(core.ARG_KEYS) | {"ticker": "LAURUSLABS", "query": "debt"},
                               "why": ""}], "answer_style": "brief"}, None
        return {"answer": "Management says net debt is coming down [E1].", "confidence": "medium", "follow_ups": []}, None

    monkeypatch.setattr(llm, "call", fake_call)
    st = core.run("What is Laurus Labs' debt level?", None, None, lambda e: None)
    assert len(planned) == 1 and "returned no evidence" in planned[0]   # rules found nothing; the model replanned once
    assert st["plan_source"] == "model" and st["evidence"]


def test_leverage_is_computed_with_its_inputs():
    from finn.agent import tools

    c = {"name": "X Ltd", "balance_sheet": {"periods": ["Mar 2025", "Mar 2026"], "rows": {
        "Equity Capital": [100, 100], "Reserves": [900, 1400], "Borrowings": [600, 450]}},
        "annual": {"periods": ["Mar 2026", "TTM"], "rows": {"Operating Profit": [800, 900], "Interest": [40, 45]}}}
    ev = tools._leverage("X", c, 6)
    assert ev["source"] == "computed"
    assert "Mar 2026 0.30x" in ev["text"] and "Mar 2025 0.60x" in ev["text"]       # 450 / 1,500 and 600 / 1,000
    assert "0.50x" in ev["text"] and "20.0x" in ev["text"]                           # 450 / 900 and 900 / 45
    assert "−₹150 cr, −25.0%" in ev["text"]
    # an answer quoting the computed ratio now passes the grounding check
    assert verify.check("Debt-to-equity fell to 0.3x [E1].", [{**ev, "id": "E1"}])["ok"]
    assert tools._leverage("BANK", {"name": "Bank", "balance_sheet": {"periods": ["Mar 2026"], "rows": {"Borrowing": [5]}}}, 6) is None


# ---- no model answer: say why, plainly; never dump raw passages as an answer ----------------------
def _stub_agent(monkeypatch):
    from finn.agent import core, memory, tools

    monkeypatch.setattr(memory, "save", lambda t: None)
    monkeypatch.setattr(memory, "learn_from", lambda m: [])
    monkeypatch.setattr(tools, "run", lambda name, args: [{"title": "Bajaj Finance call", "source": "primary", "cite": "BAJFINANCE call p.2",
                                                           "url": None, "text": "RAJEEV JAIN - VICE CHAIRMAN. Bajaj Finance Limited Page 2 of 19."}])
    return core


def test_no_model_connected_says_so(monkeypatch):
    from finn.intel import llm

    core = _stub_agent(monkeypatch)
    monkeypatch.setattr(core, "LLM_ENABLED", False)
    monkeypatch.setattr(llm, "LLM_ENABLED", False)
    events = []
    st = core.run("How is BAJFINANCE's margin holding up?", None, None, events.append)
    ans = next(e for e in events if e["type"] == "answer")
    assert ans["mode"] == "no_model" and ans["reason"] == "no_key"
    assert "You haven't added an API key" in ans["text"] and "RAJEEV" not in ans["text"]
    assert st["answer_mode"] == "no_model"


def test_visitor_key_failure_names_the_key_and_the_error(monkeypatch):
    from finn.intel import llm

    core = _stub_agent(monkeypatch)
    monkeypatch.setenv("FINN_LLM", "on")
    monkeypatch.setattr(llm, "_down", {})

    def failing_call(purpose, **kw):
        llm.log.warning("LLM API error 404 (%s): model gemini-9 is not found", purpose)
        return None, None

    monkeypatch.setattr(llm, "call", failing_call)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: None)
    t = llm.use_key("AIzaTestKey123", "gemini")
    try:
        events = []
        core.run("How is BAJFINANCE's margin holding up?", None, None, events.append)
    finally:
        llm.reset_key(t)
    ans = next(e for e in events if e["type"] == "answer")
    assert ans["mode"] == "no_model" and ans["reason"] == "error"
    assert ans["text"].startswith("The API key you provided (Google Gemini) couldn't produce this answer")
    assert "model gemini-9 is not found" in ans["text"]
