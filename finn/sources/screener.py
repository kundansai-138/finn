"""screener.in company page -> structured fundamentals + document links.

We use screener for three things only:
  1. Standardised financial tables (quarterly, annual P&L, balance sheet, cash flow, ratios),
     which are a mechanical parse of the company's own filings.
  2. Links to primary documents (annual reports, concall transcripts, results PDFs).
  3. The company's recent exchange announcements (title + date + filing text excerpt).

We deliberately ignore screener's own commentary (pros/cons, "key points") — the brief asks
Pull to stay grounded in primary sources.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from ..config import TTL_SCREENER
from ..http import FetchError, fetch

BASE = "https://www.screener.in"


def _num(text: str) -> float | None:
    t = text.replace(",", "").replace("%", "").replace("₹", "").strip()
    t = t.replace("Cr.", "").strip()
    if t in ("", "-", "--"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _label(td) -> str:
    txt = td.get_text(" ", strip=True)
    return re.sub(r"\s*\+\s*$", "", txt).strip()


def _table(soup, section_id: str) -> dict[str, Any] | None:
    t = soup.select_one(f"section#{section_id} table.data-table")
    if not t:
        return None
    periods = [th.get_text(" ", strip=True) for th in t.select("thead th")][1:]
    rows: dict[str, list[float | None]] = {}
    links: list[str | None] = []
    for tr in t.select("tbody tr"):
        tds = tr.select("td")
        if not tds:
            continue
        label = _label(tds[0])
        if label == "Raw PDF":
            links = [(BASE + a["href"]) if (a := td.select_one("a")) else None for td in tds[1:]]
            continue
        if label in rows:  # avoid clobbering with hidden growth rows of the same name
            continue
        rows[label] = [_num(td.get_text(" ", strip=True)) for td in tds[1:]]
    return {"periods": periods, "rows": rows, "pdf": links or None}


def _ratios(soup) -> dict[str, str]:
    out = {}
    for li in soup.select("#top-ratios li"):
        n, v = li.select_one(".name"), li.select_one(".value")
        if n and v:
            out[n.get_text(" ", strip=True)] = re.sub(r"\s+", " ", v.get_text(" ", strip=True))
    return out


def _announcements(soup) -> list[dict[str, Any]]:
    out = []
    box = soup.select_one("#company-announcements-tab")
    if not box:
        return out
    for li in box.select("ul.list-links li"):
        a = li.select_one("a")
        if not a:
            continue
        meta = a.select_one("div")
        tm = a.select_one("time")
        summary = ""
        if meta:
            summary = meta.get_text(" ", strip=True)
            summary = re.sub(r"^\S+\s+\S+\s+-\s+", "", summary)  # drop leading "13 Nov - "
            meta.extract()
        out.append({
            "title": a.get_text(" ", strip=True),
            "url": a.get("href"),
            "ts": tm.get("datetime") if tm else None,
            "summary": summary,
        })
    return out


def _documents(soup) -> dict[str, list[dict[str, Any]]]:
    ars = []
    sec = soup.select_one(".documents.annual-reports")
    if sec:
        for a in sec.select("ul.list-links li > a"):
            m = re.search(r"(\d{4})", a.get_text())
            ars.append({"year": int(m.group(1)) if m else None, "url": a.get("href")})
    concalls = []
    cc = soup.select_one(".documents.concalls")
    if cc:
        for li in cc.select("ul.list-links li"):
            when = li.select_one("div")
            item = {"period": when.get_text(" ", strip=True) if when else None}
            for a in li.select("a"):
                kind = a.get_text(" ", strip=True).lower()
                if kind in ("transcript", "ppt", "rec"):
                    item[kind] = a.get("href")
            concalls.append(item)
    return {"annual_reports": ars, "concalls": concalls}


def fetch_company(ticker: str, consolidated: bool = True) -> dict[str, Any]:
    """Fetch and parse a company. Falls back to standalone when consolidated isn't published."""
    ticker = ticker.upper().strip()
    url = f"{BASE}/company/{ticker}/{'consolidated/' if consolidated else ''}"
    try:
        html = fetch(url, TTL_SCREENER)
    except FetchError:
        if consolidated:
            return fetch_company(ticker, consolidated=False)
        raise
    try:
        c = parse_company(html, ticker, url, "consolidated" if consolidated else "standalone")
    except ValueError:
        raise FetchError(f"Unknown ticker {ticker}")
    q = c["quarters"]
    # Screener shows an empty consolidated table for standalone-only companies.
    if consolidated and (not q or not any(v for r in q["rows"].values() for v in r if v)):
        return fetch_company(ticker, consolidated=False)
    return c


def parse_company(html: str, ticker: str, url: str = "", basis: str = "consolidated") -> dict[str, Any]:
    """Pure parse (no network), so it can be tested against fixtures."""
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if not h1 or "page not found" in html[:5000].lower():
        raise ValueError(f"not a company page: {ticker}")
    quarters = _table(soup, "quarters")

    sector = soup.select_one("section#peers p.sub")
    sector_path = [x.get_text(strip=True) for x in sector.select("a")] if sector else []
    links = {}
    for a in soup.select(".company-links a"):
        txt = a.get_text(" ", strip=True).lower()
        if "bse" in txt:
            links["bse"] = a.get("href")
        elif "nse" in txt:
            links["nse"] = a.get("href")
        elif a.get("href", "").startswith("http"):
            links.setdefault("website", a.get("href"))

    return {
        "ticker": ticker,
        "name": h1.get_text(" ", strip=True),
        "basis": basis,
        "source_url": url,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "sector_path": sector_path,
        "ratios": _ratios(soup),
        "quarters": quarters,
        "annual": _table(soup, "profit-loss"),
        "balance_sheet": _table(soup, "balance-sheet"),
        "cash_flow": _table(soup, "cash-flow"),
        "efficiency": _table(soup, "ratios"),
        "announcements": _announcements(soup),
        "documents": _documents(soup),
        "links": links,
    }


def row(table: dict | None, *names: str) -> list[float | None] | None:
    """First matching row by (case-insensitive) name prefix. Handles bank vs non-bank labels."""
    if not table:
        return None
    rows = table["rows"]
    for n in names:
        for k, v in rows.items():
            if k.lower() == n.lower():
                return v
    for n in names:
        for k, v in rows.items():
            if k.lower().startswith(n.lower()):
                return v
    return None
