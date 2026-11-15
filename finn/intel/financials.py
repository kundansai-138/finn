"""Deterministic financial analysis. No model here: arithmetic should never be generated.

Works for both operating companies (Sales / Operating Profit) and lenders (Revenue / Financing
Profit), because screener labels the rows differently and the engine looks rows up by meaning.
"""
from __future__ import annotations

from typing import Any

from ..sources.screener import row


def _f(x: float | None, d: int = 1) -> float | None:
    return None if x is None else round(x, d)


def pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return round((a / b - 1) * 100 * (1 if b > 0 else -1), 1)


def cagr(series: list[float | None], years: int) -> float | None:
    vals = [v for v in series if v is not None]
    if len(vals) <= years:
        return None
    a, b = vals[-1 - years], vals[-1]
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return round(((b / a) ** (1 / years) - 1) * 100, 1)


def is_lender(c: dict) -> bool:
    q = (c.get("quarters") or {}).get("rows", {})
    return "Financing Profit" in q or ("Revenue" in q and "Operating Profit" not in q)


def headline_rows(table: dict | None) -> dict[str, list[float | None] | None]:
    return {
        "revenue": row(table, "Sales", "Revenue"),
        "ebitda": row(table, "Operating Profit", "Financing Profit"),
        "pbt": row(table, "Profit before tax"),
        "pat": row(table, "Net Profit"),
    }


LABELS = {"revenue": "Revenue", "ebitda": "EBITDA", "pbt": "PBT", "pat": "PAT"}


def results_card(c: dict, idx: int = -1) -> dict[str, Any] | None:
    """Headline numbers for a quarter vs the previous quarter (QoQ) and the same quarter last year (YoY)."""
    q = c.get("quarters")
    if not q or len(q["periods"]) < 5:
        return None
    n = len(q["periods"])
    i = idx % n
    rows = headline_rows(q)
    lender = is_lender(c)
    metrics = []
    for k in ("revenue", "ebitda", "pbt", "pat"):
        r = rows[k]
        if not r:
            continue
        v, prev, yago = r[i], r[i - 1] if i >= 1 else None, r[i - 4] if i >= 4 else None
        metrics.append({
            "key": k,
            "label": ("Op. profit" if lender and k == "ebitda" else LABELS[k]),
            "value": v, "prev": prev, "year_ago": yago,
            "qoq": pct(v, prev), "yoy": pct(v, yago),
        })
    rev, eb = rows["revenue"], rows["ebitda"]
    margin = None
    if rev and eb and rev[i] and eb[i] is not None:
        m = eb[i] / rev[i] * 100
        my = (eb[i - 4] / rev[i - 4] * 100) if i >= 4 and rev[i - 4] else None
        mq = (eb[i - 1] / rev[i - 1] * 100) if i >= 1 and rev[i - 1] else None
        margin = {"label": "Op. margin" if lender else "EBITDA margin", "value": _f(m),
                  "yoy_bps": None if my is None else int(round((m - my) * 100)),
                  "qoq_bps": None if mq is None else int(round((m - mq) * 100))}
    return {
        "period": q["periods"][i],
        "prev_period": q["periods"][i - 1] if i >= 1 else None,
        "year_ago_period": q["periods"][i - 4] if i >= 4 else None,
        "metrics": metrics,
        "margin": margin,
        "take": results_take(metrics, margin),
        "unit": "₹ crore",
        "basis": c.get("basis"),
        "filing_pdf": (q.get("pdf") or [None] * n)[i],
    }


def results_take(metrics: list[dict], margin: dict | None) -> str:
    m = {x["key"]: x for x in metrics}
    rev, pat = (m.get("revenue") or {}).get("yoy"), (m.get("pat") or {}).get("yoy")
    bits = []
    if rev is not None:
        bits.append(f"Revenue {rev:+.0f}% YoY")
    if margin and margin.get("yoy_bps") is not None and abs(margin["yoy_bps"]) >= 50:
        d = margin["yoy_bps"]
        bits.append(f"{margin['label']} {'up' if d > 0 else 'down'} {abs(d)} bps to {margin['value']:.1f}%")
    if pat is not None:
        bits.append(f"PAT {pat:+.0f}% YoY")
    take = "; ".join(bits)
    if rev is not None and pat is not None:
        if rev < 0 and pat < 0:
            take += ": a down quarter on both lines."
        elif pat - rev > 10:
            take += ": profit growing faster than sales (operating leverage)."
        elif rev - pat > 10:
            take += ": profit lagging sales, so the margin story needs a look."
        else:
            take += "."
    return take


# ---------------------------------------------------------------------------
# Dossier sections
# ---------------------------------------------------------------------------

def _ratio_num(c: dict, name: str) -> float | None:
    v = c.get("ratios", {}).get(name)
    if not v:
        return None
    v = v.replace("₹", "").replace("Cr.", "").replace("%", "").replace(",", "").strip()
    try:
        return float(v.split("/")[0])
    except ValueError:
        return None


def snapshot(c: dict) -> dict[str, Any]:
    a = c.get("annual") or {}
    rows = headline_rows(a)
    ttm_idx = len(a.get("periods", [])) - 1
    rev = rows["revenue"][ttm_idx] if rows["revenue"] else None
    pat = rows["pat"][ttm_idx] if rows["pat"] else None
    eb = rows["ebitda"][ttm_idx] if rows["ebitda"] else None
    return {
        "market_cap_cr": _ratio_num(c, "Market Cap"),
        "price": _ratio_num(c, "Current Price"),
        "pe": _ratio_num(c, "Stock P/E"),
        "roce": _ratio_num(c, "ROCE"),
        "roe": _ratio_num(c, "ROE"),
        "div_yield": _ratio_num(c, "Dividend Yield"),
        "revenue_ttm": rev, "pat_ttm": pat,
        "margin_ttm": _f(eb / rev * 100) if rev and eb is not None else None,
        "pat_margin_ttm": _f(pat / rev * 100) if rev and pat is not None else None,
        "period_label": a.get("periods", [None])[-1],
        "lender": is_lender(c),
    }


def trajectory(c: dict) -> dict[str, Any]:
    a = c.get("annual") or {}
    periods = a.get("periods", [])
    # Drop the TTM column for CAGR maths; keep it for display.
    fy = [p for p in periods if p != "TTM"]
    rows = headline_rows(a)
    n = len(fy)
    rev = (rows["revenue"] or [])[:n]
    pat = (rows["pat"] or [])[:n]
    eb = (rows["ebitda"] or [])[:n]
    margins = [(_f(e / r * 100) if (r and e is not None) else None) for r, e in zip(rev, eb)]
    flags = []
    r3, r5, p3, p5 = cagr(rev, 3), cagr(rev, 5), cagr(pat, 3), cagr(pat, 5)
    m_now = margins[-1] if margins else None
    m_3 = margins[-4] if len(margins) >= 4 else None
    if m_now is not None and m_3 is not None:
        d = int(round((m_now - m_3) * 100))
        if abs(d) >= 150:
            flags.append({"tone": "pos" if d > 0 else "neg",
                          "text": f"Margin {'expanded' if d > 0 else 'compressed'} {abs(d)} bps over 3 years ({m_3:.1f}% → {m_now:.1f}%)"})
    if r3 is not None and r5 is not None and abs(r3 - r5) >= 5:
        flags.append({"tone": "pos" if r3 > r5 else "neg",
                      "text": f"Revenue growth {'accelerating' if r3 > r5 else 'decelerating'}: 3-yr CAGR {r3:.0f}% vs 5-yr {r5:.0f}%"})
    if r3 is not None and p3 is not None and abs(p3 - r3) >= 6:
        flags.append({"tone": "pos" if p3 > r3 else "neg",
                      "text": f"Profit {'outgrowing' if p3 > r3 else 'lagging'} revenue: PAT 3-yr CAGR {p3:.0f}% vs revenue {r3:.0f}%"})
    q = c.get("quarters") or {}
    qr = headline_rows(q)
    return {
        "years": fy[-10:],
        "revenue": rev[-10:], "pat": pat[-10:], "margin": margins[-10:],
        "cagr": {"revenue_3y": r3, "revenue_5y": r5, "revenue_10y": cagr(rev, 10) if n > 10 else None,
                 "pat_3y": p3, "pat_5y": p5},
        "quarters": {"periods": q.get("periods", [])[-12:], "revenue": (qr["revenue"] or [])[-12:],
                     "pat": (qr["pat"] or [])[-12:],
                     "margin": [(_f(e / r * 100) if (r and e is not None) else None)
                                for r, e in zip((qr["revenue"] or [])[-12:], (qr["ebitda"] or [])[-12:])]},
        "flags": flags,
    }


def balance_sheet(c: dict) -> dict[str, Any]:
    b = c.get("balance_sheet") or {}
    periods = b.get("periods", [])
    eq_cap, res = row(b, "Equity Capital", "Equity Share Capital") or [], row(b, "Reserves") or []
    borrow = row(b, "Borrowings", "Borrowing") or []
    cwip = row(b, "CWIP") or []
    equity = [(e or 0) + (r or 0) if (e is not None or r is not None) else None for e, r in zip(eq_cap, res)]
    de = [(_f(bo / eq, 2) if (bo is not None and eq) else None) for bo, eq in zip(borrow, equity)]
    eff = c.get("efficiency") or {}
    wc = {k: (row(eff, k) or [])[-6:] for k in ("Debtor Days", "Inventory Days", "Days Payable", "Cash Conversion Cycle", "Working Capital Days")}
    roce = (row(eff, "ROCE %", "ROE %") or [])[-6:]
    flags = []
    lender = is_lender(c)
    if de and de[-1] is not None and not lender:
        if de[-1] < 0.1:
            flags.append({"tone": "pos", "text": f"Effectively debt-free (D/E {de[-1]:.2f})"})
        elif len(de) >= 4 and de[-4] is not None and de[-1] - de[-4] >= 0.25:
            flags.append({"tone": "neg", "text": f"Leverage rising: D/E {de[-4]:.2f} → {de[-1]:.2f} over 3 years"})
        elif de[-1] > 1:
            flags.append({"tone": "neg", "text": f"Leveraged: D/E {de[-1]:.2f}"})
    ccc = [x for x in wc.get("Cash Conversion Cycle", []) if x is not None]
    if len(ccc) >= 4 and ccc[-1] - ccc[-4] >= 15:
        flags.append({"tone": "neg", "text": f"Working-capital cycle lengthening: {ccc[-4]:.0f} → {ccc[-1]:.0f} days in 3 years"})
    elif len(ccc) >= 4 and ccc[-4] - ccc[-1] >= 15:
        flags.append({"tone": "pos", "text": f"Working-capital cycle tightening: {ccc[-4]:.0f} → {ccc[-1]:.0f} days in 3 years"})
    if cwip and borrow and cwip[-1] and len(cwip) >= 2 and cwip[-2] and cwip[-1] > 1.5 * cwip[-2]:
        flags.append({"tone": "neu", "text": f"Capital work-in-progress up {pct(cwip[-1], cwip[-2]):.0f}% in the latest period: capex cycle under way"})
    return {"periods": periods[-6:], "borrowings": borrow[-6:], "equity": equity[-6:], "de": de[-6:],
            "cwip": cwip[-6:], "wc_days": wc, "roce": roce, "eff_periods": (eff.get("periods") or [])[-6:],
            "flags": flags, "lender": lender}


def cash_quality(c: dict) -> dict[str, Any]:
    cf = c.get("cash_flow") or {}
    a = c.get("annual") or {}
    periods = cf.get("periods", [])
    cfo = row(cf, "Cash from Operating Activity") or []
    fcf = row(cf, "Free Cash Flow") or []
    cfi = row(cf, "Cash from Investing Activity") or []
    pat_all = headline_rows(a)["pat"] or []
    pat_by = dict(zip(a.get("periods", []), pat_all))
    pat = [pat_by.get(p) for p in periods]
    conv = [(_f(o / p * 100, 0) if (o is not None and p) else None) for o, p in zip(cfo, pat)]
    k = 5
    s_cfo = sum(x for x in cfo[-k:] if x is not None)
    s_pat = sum(x for x in pat[-k:] if x is not None)
    s_fcf = sum(x for x in fcf[-k:] if x is not None) if fcf else None
    conv5 = _f(s_cfo / s_pat * 100, 0) if s_pat else None
    flags = []
    lender = is_lender(c)
    if conv5 is not None and not lender:
        if conv5 >= 90:
            flags.append({"tone": "pos", "text": f"Profits convert to cash: 5-yr CFO/PAT {conv5:.0f}%"})
        elif conv5 < 70:
            flags.append({"tone": "neg", "text": f"Weak cash conversion: 5-yr CFO/PAT only {conv5:.0f}%"})
    if s_fcf is not None and not lender:
        flags.append({"tone": "pos" if s_fcf > 0 else "neg",
                      "text": f"5-yr cumulative free cash flow ₹{s_fcf:,.0f} cr ({'self-funding' if s_fcf > 0 else 'growth funded externally'})"})
    if lender:
        flags.append({"tone": "neu", "text": "For lenders, operating cash flow mostly reflects loan-book growth, so CFO/PAT isn't a quality signal"})
    return {"periods": periods[-8:], "cfo": cfo[-8:], "pat": pat[-8:], "fcf": fcf[-8:], "cfi": cfi[-8:],
            "conversion": conv[-8:], "conversion_5y": conv5, "fcf_5y": s_fcf, "flags": flags, "lender": lender}


def bull_bear_fallback(snap: dict, traj: dict, bs: dict, cash: dict, ledger: dict | None,
                       latest: dict | None = None) -> dict[str, list[dict]]:
    """Rule-based case from the computed flags; the deep model replaces this when available."""
    bull, bear = [], []
    if latest:
        m = {x["key"]: x for x in latest["metrics"]}
        rev, pat = (m.get("revenue") or {}).get("yoy"), (m.get("pat") or {}).get("yoy")
        if pat is not None and pat <= -10:
            bear.append({"point": f"Latest quarter ({latest['period']}) PAT {pat:+.0f}% YoY" + (f" on revenue {rev:+.0f}%" if rev is not None else ""),
                         "evidence": "quarterly results"})
        elif pat is not None and pat >= 20:
            bull.append({"point": f"Latest quarter ({latest['period']}) PAT {pat:+.0f}% YoY" + (f" on revenue {rev:+.0f}%" if rev is not None else ""),
                         "evidence": "quarterly results"})
    if snap.get("margin_ttm") is not None and snap["margin_ttm"] < 6 and not snap.get("lender"):
        bear.append({"point": f"Thin margins: EBITDA margin {snap['margin_ttm']:.1f}% (TTM), so small cost or pricing shocks swing profit",
                     "evidence": "TTM P&L"})
    for sec in (traj, bs, cash):
        for f in sec.get("flags", []):
            (bull if f["tone"] == "pos" else bear if f["tone"] == "neg" else []).append({"point": f["text"], "evidence": "computed from reported financials"})
    r3 = traj["cagr"].get("revenue_3y")
    if r3 is not None and r3 >= 15:
        bull.append({"point": f"Compounding: revenue 3-yr CAGR {r3:.0f}%", "evidence": "annual P&L"})
    if snap.get("roce") and snap["roce"] >= 20:
        bull.append({"point": f"High returns: ROCE {snap['roce']:.0f}%", "evidence": "latest ratios"})
    p3 = traj["cagr"].get("pat_3y")
    if snap.get("pe") and p3 and p3 > 0 and snap["pe"] / p3 > 2.5:
        bear.append({"point": f"Valuation leaves little room: P/E {snap['pe']:.0f} on {p3:.0f}% PAT CAGR", "evidence": "price vs 3-yr profit growth"})
    if ledger:
        s = ledger.get("score") or {}
        if s.get("missed", 0) > s.get("delivered", 0):
            bear.append({"point": f"Credibility gap: missed {s['missed']} of {s.get('judged', 0)} checkable promises", "evidence": "promises ledger"})
        elif s.get("delivered", 0) >= 2 and s.get("delivered", 0) > s.get("missed", 0):
            bull.append({"point": f"Management delivers: {s['delivered']} of {s.get('judged', 0)} checkable promises met", "evidence": "promises ledger"})
    return {"bull": bull[:4], "bear": bear[:4], "source": "rules"}
