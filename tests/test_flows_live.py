"""Live versions of the flow scenarios: real web search, real pages, real model. They spend money
(about $0.15 for the whole file) and depend on what the web says today, so they're opt-in:

    FINN_TEST_LLM=on python -m pytest -m live tests/test_flows_live.py -s

They assert properties that must hold whatever the search returns (everything shown is verified,
scored above the bar, labelled, and connected to the network where a route exists), not specific names.
"""
import pytest

from finn import clock, store
from finn.config import LLM_ENABLED
from finn.network import discover as D

pytestmark = [pytest.mark.live, pytest.mark.skipif(not LLM_ENABLED, reason="set FINN_TEST_LLM=on and ANTHROPIC_API_KEY")]


def _cost(purpose: str) -> float:
    return sum(r["cost"] or 0 for r in store.ledger_summary() if r["purpose"] == purpose)


def test_live_discovery_for_a_company_outside_the_network():
    out = D.discover(event="capex", text="Titan Company jewellery manufacturing capacity expansion", company="Titan Company", force=True)
    print("\nTitan:", out["stats"], [(c["name"], c["title"], c["fit"]) for c in out["candidates"]])
    assert out["status"] == "ok" and out["candidates"], out["stats"]
    for c in out["candidates"]:
        assert c["verified"]["name"] and c["fit"] >= D.MIN_FIT and c["source"]["url"]
        assert c["label"] == "From the public web · not in your network"
    assert out["stats"]["searches"] >= 1 and _cost("people_discovery") > 0


def test_live_discovery_connects_leads_to_the_network():
    text = "binding term sheet with Vishay for an OSAT semiconductor assembly joint venture"
    out = D.discover(org="kaynes", event="M&A", text=text, force=True)
    print("\nKaynes:", out["stats"], [(c["name"], c["org_name"], [i["name"] for i in c["intro"]]) for c in out["candidates"]])
    at_kaynes = [c for c in out["candidates"] if c["org_id"] == "kaynes"]
    assert at_kaynes and at_kaynes[0]["intro"][0]["id"] == "kiran"


def test_live_push_enrichment_of_a_management_change():
    from finn import enrich as E

    item = {"id": "live-tatapower-ceo", "ticker": "TATAPOWER", "company": "Tata Power", "title": "Change in Management",
            "summary": "Appointment of Sanjay Banga as CEO-designate; Praveer Sinha to retire as MD and CEO.",
            "ts": clock.now().isoformat(), "score": 90, "source": "nse_rss", "category": "management_change"}
    web = E.enrich(item, force=True)
    print("\nTata Power:", web["stats"], [f["text"][:80] for f in web["facts"]], [(p["name"], [r["name"] for r in p["routes"]]) for p in web["people"]])
    assert web["facts"] or web["people"], web["stats"]
    for f in web["facts"]:
        assert f["verified"]["ok"] and f["source"]["tier"] in ("primary", "press", "other")
    assert any(r["id"] == "ishaan" for p in web["people"] for r in p["routes"]) or not web["people"]


def test_live_nudge_draft_keeps_the_name_local():
    from finn.network import nudges

    d = nudges.draft("gaurav")
    print("\nDraft:", d)
    assert d["source"] == "model" and "Gaurav" in d["text"] and "[NAME]" not in d["text"]
