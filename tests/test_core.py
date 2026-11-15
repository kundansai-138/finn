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
    st = core.run("How are Dixon's margins?", None, None, events.append)
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
