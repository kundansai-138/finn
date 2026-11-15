"""Deterministic logic: parsers, financial maths, fiscal calendar, guidance verification, chunking.
These are the places where a silent bug would quietly mislead an investor, so they're pinned."""
from pathlib import Path

import pytest

from finn.agent.retrieval import chunk_transcript
from finn.intel import financials as F
from finn.intel import guidance as G
from finn.sources import nse_rss, screener

FX = Path(__file__).parent / "fixtures"


# ---- parsers ------------------------------------------------------------------
@pytest.fixture(scope="module")
def acme():
    return screener.parse_company((FX / "screener_company.html").read_text(encoding="utf-8"), "ACME", "u")


def test_screener_parser_reads_every_section(acme):
    assert acme["name"] == "Acme Widgets Ltd"
    assert acme["sector_path"] == ["Industrials", "Capital Goods", "Electrical Equipment"]
    q = acme["quarters"]
    assert q["periods"][-1] == "Sep 2026" and q["rows"]["Sales"][-1] == 1200.0     # "Sales +" label cleaned
    assert q["rows"]["OPM %"][-1] == 17.0 and q["pdf"][-1].endswith("/9/2026/")
    assert acme["announcements"][0]["ts"].startswith("2026-11-13") and acme["announcements"][0]["summary"] == "Mr. X appointed CFO."
    assert [c["period"] for c in acme["documents"]["concalls"]] == ["Nov 2026", "Aug 2026"]
    assert acme["documents"]["annual_reports"][0]["year"] == 2026


def test_screener_rejects_non_company_pages():
    with pytest.raises(ValueError):
        screener.parse_company("<html><body>Page not found</body></html>", "NOPE")


def test_nse_rss_maps_symbol_from_filename():
    xml = b"""<rss><channel><item><title>Acme Widgets Limited</title>
      <link>https://nsearchives.nseindia.com/corporate/ACME_14112026015016_Outcome.pdf</link>
      <description>Acme Widgets Limited has informed the Exchange regarding Outcome of Board Meeting |SUBJECT: Outcome of Board Meeting</description>
      <pubDate>14-Nov-2026 01:51:14</pubDate></item></channel></rss>"""
    [it] = nse_rss.parse_items(xml, {"ACME"})
    assert it["ticker"] == "ACME" and it["title"] == "Outcome of Board Meeting" and it["ts"].endswith("+05:30")
    assert nse_rss.parse_items(xml, {"OTHER"}) == []


# ---- financial maths ----------------------------------------------------------
def test_results_card_qoq_yoy_and_margin(acme):
    r = F.results_card(acme)
    m = {x["key"]: x for x in r["metrics"]}
    assert r["period"] == "Sep 2026" and r["prev_period"] == "Jun 2026" and r["year_ago_period"] == "Sep 2025"
    assert m["revenue"]["qoq"] == pytest.approx(4.3, abs=0.05)   # 1200 / 1150
    assert m["revenue"]["yoy"] == 20.0                            # 1200 / 1000
    assert m["pat"]["yoy"] == pytest.approx(33.3, abs=0.05)
    assert r["margin"]["value"] == pytest.approx(16.7, abs=0.05) and r["margin"]["yoy_bps"] == 167


def test_lenders_get_lender_labels():
    c = {"quarters": {"periods": ["a"] * 5, "rows": {"Revenue": [1] * 5, "Financing Profit": [1] * 5, "Net Profit": [1] * 5}}}
    assert F.is_lender(c)
    assert F.results_card(c)["metrics"][1]["label"] == "Op. profit"


def test_cagr():
    assert F.cagr([100, 110, 121], 2) == 10.0
    assert F.cagr([0, 50, 100], 2) is None          # undefined from zero
    assert F.cagr([100], 3) is None


# ---- fiscal calendar + guidance verification ------------------------------------------
@pytest.mark.parametrize("call,quarter", [("Nov 2026", "Sep 2026"), ("Feb 2026", "Dec 2025"), ("May 2026", "Mar 2026"), ("Aug 2026", "Jun 2026")])
def test_call_month_maps_to_the_quarter_discussed(call, quarter):
    assert G.ref_quarter(call) == quarter


def test_indian_fiscal_year():
    assert G.fy_of(2026, 11) == 2027 and G.fy_of(2026, 2) == 2026
    assert G.fy_quarters(2027) == ["Jun 2026", "Sep 2026", "Dec 2026", "Mar 2027"]


def _series(rev):
    return {"q": {"revenue": rev, "ebitda": {k: v * 0.15 for k, v in rev.items()}, "pat": {}}, "a": {}}


def test_guidance_verdicts():
    rev = {f"{m} {y}": v for (m, y, v) in [("Jun", 2024, 100), ("Sep", 2024, 100), ("Dec", 2024, 100), ("Mar", 2025, 100),
                                           ("Jun", 2025, 120), ("Sep", 2025, 120), ("Dec", 2025, 125), ("Mar", 2026, 125)]}
    s = _series(rev)
    base = {"metric": "revenue", "kind": "growth_pct", "horizon": "FY26", "quote": "", "high": None}
    assert G.verify({**base, "low": 20}, {}, s)["verdict"] == "delivered"     # actual 22.5%
    assert G.verify({**base, "low": 30}, {}, s)["verdict"] == "missed"
    ytd = {k: v for k, v in rev.items() if k in ("Jun 2024", "Sep 2024", "Jun 2025", "Sep 2025")}
    v = G.verify({**base, "low": 25, "horizon": "FY26"}, {}, _series(ytd))
    assert v["verdict"] == "behind" and "YTD" in v["window"]                   # incomplete year: tracking, not final
    assert G.verify({**base, "metric": "segment", "low": 10}, {}, s)["verdict"] == "unverifiable"


# ---- transcript chunking ----------------------------------------------------------
def test_chunker_pairs_questions_with_answers_and_skips_letterhead():
    pages = [
        "Tel.: 022 1234\nEncl.: As above\nSub: Transcript of the call",
        "Moderator: The first question is from the line of Riya Shah from Alpha Capital.\n"
        "Riya Shah: What is the margin outlook for next year?\n"
        "Ravi Kumar: We expect EBITDA margin of 14% to 15% next year as the new plant ramps up and utilisation improves steadily.",
    ]
    chunks = chunk_transcript(pages, "Nov 2026", "u", "ACME")
    assert chunks and all(c["speaker"] not in ("Tel.", "Encl.") for c in chunks)
    qa = [c for c in chunks if c["qa"]]
    assert qa and qa[0]["text"].startswith("Q (Riya Shah)") and "14% to 15%" in qa[0]["text"] and qa[0]["page"] == 2
