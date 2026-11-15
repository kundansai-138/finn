"""Promises ledger: what management guided, what was then delivered.

Pipeline (per company, last N concall transcripts, all primary sources):

  1. EXTRACT   transcript -> forward-looking, numeric statements by *management* (not analysts)
               fast model with a strict JSON schema; heuristic extractor when no key is set
  2. NORMALISE metric (revenue growth, margin, PAT...), range (low/high), horizon (FY27, next quarter)
  3. VERIFY    deterministic: compare against reported quarterly/annual numbers -> verdict.
               A model never does arithmetic a few lines of Python can do.
  4. GROUP     the same promise repeated across calls -> one line, with "revised from X to Y"
  5. JUDGE     (optional, deep model) qualitative promises + narrative-vs-numbers observations

Every ledger line carries the quote, the call it came from and the page, so the investor can check.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any

from ..sources import documents
from ..sources.screener import row
from . import llm
from .financials import headline_rows, is_lender

log = logging.getLogger(__name__)

MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

# ---------------------------------------------------------------------------
# Calendar helpers (Indian fiscal year: FY27 = Apr 2026 - Mar 2027)
# ---------------------------------------------------------------------------

def fy_of(year: int, month: int) -> int:
    return year + 1 if month >= 4 else year


def ref_quarter(call_period: str) -> str | None:
    """'Nov 2026' (call month) -> 'Sep 2026' (quarter being discussed)."""
    try:
        mon, yr = call_period.split()
        m, y = MONTHS[mon[:3]], int(yr)
    except (ValueError, KeyError):
        return None
    qm = {1: 12, 2: 12, 3: 12, 4: 3, 5: 3, 6: 3, 7: 6, 8: 6, 9: 6, 10: 9, 11: 9, 12: 9}[m]
    qy = y - 1 if m <= 3 and qm == 12 else y
    return f"{['Mar', 'Jun', 'Sep', 'Dec'][[3, 6, 9, 12].index(qm)]} {qy}"


def fy_quarters(fy: int) -> list[str]:
    return [f"Jun {fy - 1}", f"Sep {fy - 1}", f"Dec {fy - 1}", f"Mar {fy}"]


# ---------------------------------------------------------------------------
# 1. Extraction
# ---------------------------------------------------------------------------

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "statements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "enum": ["revenue", "ebitda_margin", "ebitda", "pat", "volume", "capex", "debt", "segment", "other"]},
                    "kind": {"type": "string", "enum": ["growth_pct", "margin_pct", "absolute_cr", "qualitative"]},
                    "low": {"type": ["number", "null"]},
                    "high": {"type": ["number", "null"]},
                    "horizon": {"type": "string", "description": "e.g. 'FY27', 'next quarter', 'H2 FY27', 'medium term'"},
                    "quote": {"type": "string", "description": "verbatim, at most 45 words"},
                    "page": {"type": "integer"},
                    "speaker": {"type": "string"},
                },
                "required": ["metric", "kind", "low", "high", "horizon", "quote", "page", "speaker"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["statements"],
    "additionalProperties": False,
}

EXTRACT_SYSTEM = (
    "You extract forward-looking guidance from Indian listed-company earnings-call transcripts. "
    "Only statements made by company management (never analysts or the moderator). Only explicit, "
    "checkable commitments: growth rates, margin bands, revenue/profit targets, capex amounts, debt targets, "
    "capacity timelines. Skip vague optimism. Quote verbatim. Use the [p.N] markers for page numbers. "
    "Percentages as numbers (25 for 25%). Rupee amounts in crore. Consolidated company-level metrics use "
    "revenue/ebitda_margin/pat; a single business segment's number is 'segment'."
)


def _llm_extract(pages: list[str], call: str, company: str) -> list[dict] | None:
    doc = "\n\n".join(f"[p.{i + 1}]\n{p}" for i, p in enumerate(pages) if p.strip())
    res = llm.complete(
        "guidance_extract",
        f"Company: {company}. Call: {call}. Extract management guidance statements from the transcript above.",
        tier="fast", system=EXTRACT_SYSTEM, schema=EXTRACT_SCHEMA, max_tokens=6000, cache_prefix=doc,
    )
    return None if res is None else res.get("statements", [])


_CUE = re.compile(r"\b(guid\w*|expect\w*|target\w*|aim\w*|outlook|anticipat\w*|confident|we will|we'll|we would|we're going to|we are going to|going to be|should (be|reach|grow|come)|project\w*|envisag\w*|estimat\w*|we feel|we see|endeavou?r)\b", re.I)
_METRIC = [
    ("ebitda_margin", re.compile(r"\b(ebitda|operating|gross)?\s*margins?\b", re.I)),
    ("pat", re.compile(r"\b(pat|net profit|bottom[- ]?line|profit after tax|earnings)\b", re.I)),
    ("ebitda", re.compile(r"\bebitda\b", re.I)),
    ("capex", re.compile(r"\bcapex|capital expenditure\b", re.I)),
    ("volume", re.compile(r"\bvolumes?\b", re.I)),
    ("revenue", re.compile(r"\b(revenue|top[- ]?line|sales|turnover|growth)\b", re.I)),
]
_RANGE_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%?\s*(?:to|-|–|and|,)\s*(\d+(?:\.\d+)?)\s*(?:%|percent|per cent)", re.I)
_ONE_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent|per cent)", re.I)
_CR = re.compile(r"(?:INR|Rs\.?|₹)?\s*([\d,]{3,}(?:\.\d+)?)\s*(?:crores?|cr\b)(?:\s*(?:to|-|–)\s*(?:INR|Rs\.?|₹)?\s*([\d,]{3,}(?:\.\d+)?)\s*(?:crores?|cr\b))?", re.I)
_TURN = re.compile(r"(?:^|\n)\s*([A-Z][A-Za-z\.']+(?:\s[A-Z][A-Za-z\.']+){0,3})\s*:\s")
_ANALYST = re.compile(r"(?:question|line)\s+(?:is\s+)?(?:from|of)\s+(?:the line of\s+)?(?:Mr\.?\s+|Ms\.?\s+)?([A-Z][a-z]+(?:\s[A-Z][a-z]+){0,2})", re.I)


def _fix(t: str) -> str:
    # pypdf often splits words ("g rowth", "quart er"); glue the common ones back.
    t = re.sub(r"\s+", " ", t)
    for a, b in [("g rowth", "growth"), ("quart er", "quarter"), ("reven ue", "revenue"), ("mar gin", "margin")]:
        t = t.replace(a, b)
    return t


def _horizon(s: str, call_fy: int) -> str:
    m = re.search(r"FY\s?'?(\d{2,4})", s, re.I)
    if m:
        y = int(m.group(1))
        return f"FY{y % 100:02d}"
    if re.search(r"next (fiscal|financial year|year)", s, re.I):
        return f"FY{(call_fy + 1) % 100:02d}"
    if re.search(r"this (fiscal|financial year|year)|current (fiscal|year)|full[- ]year|for the year", s, re.I):
        return f"FY{call_fy % 100:02d}"
    if re.search(r"next quarter|coming quarter|quarter \d against|this quarter|q[1-4]\b", s, re.I):
        return "next quarter"
    if re.search(r"medium[- ]term|long[- ]term|next (two|three|2|3|few) years", s, re.I):
        return "medium term"
    return f"FY{call_fy % 100:02d}"


_NOISE = re.compile(r"capacity|utili[sz]ation|market share|tax rate|dividend|payout|attrition", re.I)
_SEGMENT = re.compile(r"(segment|division|vertical|category|categories|exports?|domestic business|mobile|telecom|lighting|appliances|"
                      r"cdmo|csm|biologicals?|subsidiar\w*|jv|joint venture|new business|this business|that business|product line)", re.I)


def _heuristic_extract(pages: list[str], call_fy: int, ttm: float | None = None) -> list[dict]:
    full = "\n".join(f"\n[[p{i + 1}]]\n{p}" for i, p in enumerate(pages))
    analysts = {m.group(1).strip().lower() for m in _ANALYST.finditer(_fix(full))}
    # Walk speaker turns; keep management turns only.
    out = []
    marks = list(_TURN.finditer(full))
    for k, m in enumerate(marks):
        speaker = m.group(1).strip()
        sl = speaker.lower()
        if sl == "moderator" or any(a in sl or sl in a for a in analysts if len(a) > 3):
            continue
        body = full[m.end(): marks[k + 1].start() if k + 1 < len(marks) else len(full)]
        page_marks = [(pm.start(), int(pm.group(1))) for pm in re.finditer(r"\[\[p(\d+)\]\]", full[: m.end()])]
        page = page_marks[-1][1] if page_marks else 1
        for sent in re.split(r"(?<=[.!?])\s+", _fix(body)):
            if len(sent) < 30 or sent.strip().endswith("?") or not _CUE.search(sent):
                continue
            if _NOISE.search(sent):
                continue
            rng, one, cr = _RANGE_PCT.search(sent), _ONE_PCT.search(sent), _CR.search(sent)
            dd = re.search(r"double[- ]digit", sent, re.I)
            hit = rng or one or cr or dd
            if not hit:
                continue
            # Judge the metric from the words *near* the number, not anywhere in a long sentence.
            win = sent[max(0, hit.start() - 80): hit.end() + 40]
            if re.search(r"gross|GP|contribution", win, re.I):
                metric = "other"
            elif re.search(r"(ebitda|operating)\s+margin", win, re.I) or (re.search(r"margin", win, re.I) and re.search(r"ebitda", sent, re.I)):
                metric = "ebitda_margin"
            elif re.search(r"pat|net profit|bottom[- ]?line|profit after tax", win, re.I):
                metric = "pat"
            elif re.search(r"revenue|top[- ]?line|sales|turnover", win, re.I):
                metric = "revenue"
            elif re.search(r"capex|capital expenditure", win, re.I):
                metric = "capex"
            elif re.search(r"volume", win, re.I):
                metric = "volume"
            elif re.search(r"growth", win, re.I):
                metric = "revenue"  # bare "growth" from management almost always means sales
            else:
                continue
            if metric == "ebitda_margin" and not (rng or one):
                metric = "other"
            if metric == "ebitda_margin" and (rng or one):
                kind = "margin_pct"
            elif metric in ("revenue", "pat", "volume") and (rng or one or dd):
                kind = "growth_pct"
            elif cr:
                kind = "absolute_cr"
            else:
                kind = "qualitative"
            if kind == "absolute_cr" and cr:
                low = float(cr.group(1).replace(",", ""))
                high = float(cr.group(2).replace(",", "")) if cr.group(2) else None
            elif rng:
                low, high = float(rng.group(1)), float(rng.group(2))
            elif one:
                low, high = float(one.group(1)), None
            elif dd:
                low, high = 10.0, None
            else:
                low, high = None, None
            if kind in ("growth_pct", "margin_pct") and low is not None and (low > 150 or (high or 0) > 150):
                continue
            confident = metric in ("revenue", "ebitda_margin", "pat") and not _SEGMENT.search(sent)
            if metric in ("revenue", "pat") and kind == "absolute_cr" and ttm and low and low < 0.5 * ttm:
                metric, confident = "segment", False
            out.append({"metric": metric if confident or metric in ("capex", "volume", "segment", "other") else "segment",
                        "kind": kind, "low": low, "high": high, "confident": confident,
                        "horizon": _horizon(sent, call_fy), "quote": sent.strip()[:320], "page": page,
                        "speaker": speaker})
    return out


def transcripts(c: dict, n: int = 4) -> list[dict]:
    calls = [x for x in c.get("documents", {}).get("concalls", []) if x.get("transcript")]
    return calls[:n]


def extract_all(c: dict, n: int = 4) -> tuple[list[dict], str]:
    stmts, method = [], "model" if llm.enabled() else "heuristic"
    rev = headline_rows(c.get("annual") or {})["revenue"] or []
    ttm = rev[-1] if rev else None
    for call in transcripts(c, n):
        pages = documents.pdf_pages(call["transcript"])
        if not pages:
            continue
        period = call.get("period") or ""
        try:
            mon, yr = period.split()
            call_fy = fy_of(int(yr), MONTHS[mon[:3]])
        except (ValueError, KeyError):
            continue
        found = _llm_extract(pages, period, c["name"]) if llm.enabled() else None
        if found is None:
            found, method = _heuristic_extract(pages, call_fy, ttm), "heuristic"
        for s in found:
            s.update({"call": period, "call_fy": call_fy, "ref_quarter": ref_quarter(period), "url": call["transcript"]})
            stmts.append(s)
    return stmts, method


# ---------------------------------------------------------------------------
# 3. Verification (deterministic)
# ---------------------------------------------------------------------------

def _series(c: dict) -> dict[str, dict[str, float | None]]:
    q = c.get("quarters") or {}
    a = c.get("annual") or {}
    qr, ar = headline_rows(q), headline_rows(a)
    out = {"q": {}, "a": {}}
    for key in ("revenue", "ebitda", "pat"):
        out["q"][key] = dict(zip(q.get("periods", []), qr[key] or []))
        out["a"][key] = dict(zip(a.get("periods", []), ar[key] or []))
    return out


def _yoy_over(qs: list[str], s: dict, key: str) -> float | None:
    cur = [s["q"][key].get(p) for p in qs]
    prev = [s["q"][key].get(f"{p.split()[0]} {int(p.split()[1]) - 1}") for p in qs]
    if any(v is None for v in cur + prev) or not cur:
        return None
    return round((sum(cur) / sum(prev) - 1) * 100, 1)


def _margin_over(qs: list[str], s: dict) -> float | None:
    r = [s["q"]["revenue"].get(p) for p in qs]
    e = [s["q"]["ebitda"].get(p) for p in qs]
    if not qs or any(v is None for v in r + e) or not sum(r):
        return None
    return round(sum(e) / sum(r) * 100, 1)


def verify(st: dict, c: dict, s: dict) -> dict:
    """Attach {verdict, actual, window, note} to a statement."""
    metric, kind, low, high = st["metric"], st["kind"], st.get("low"), st.get("high")
    hz = st.get("horizon", "")
    out = {"verdict": "unverifiable", "actual": None, "window": None, "note": None}
    if st.get("confident") is False and metric not in ("capex", "volume"):
        out["note"] = "Ambiguous scope (segment or non-standard metric): needs a read"
        return out
    if metric in ("volume", "capex", "debt", "segment", "other") or kind == "qualitative" or low is None:
        out["note"] = {"segment": "Segment-level target; reported numbers are company-wide",
                       "other": "Metric not in the reported P&L (e.g. gross margin)",
                       "volume": "Volumes aren't in the reported P&L",
                       "capex": "Check against cash-flow capex at year end"}.get(metric, "Not checkable from reported numbers")
        return out
    q_periods = list(s["q"]["revenue"].keys())
    m = re.match(r"FY(\d{2})", hz)
    if m:
        fy = 2000 + int(m.group(1))
        window = [p for p in fy_quarters(fy) if p in q_periods]
        complete = len(window) == 4
        out["window"] = f"FY{fy % 100:02d}" + ("" if complete else f" YTD ({len(window)}Q)")
    elif hz == "next quarter":
        rq = st.get("ref_quarter")
        if rq not in q_periods:
            return out
        idx = q_periods.index(rq)
        window = q_periods[idx + 1: idx + 2]
        complete = bool(window)
        out["window"] = window[0] if window else None
    else:
        out["verdict"] = "pending"
        out["note"] = "Medium-term target; too early to judge"
        return out
    if not window:
        out["verdict"] = "pending"
        out["note"] = "No reported quarter in the window yet"
        return out

    key = {"revenue": "revenue", "ebitda": "ebitda", "pat": "pat", "ebitda_margin": "ebitda"}[metric]
    if kind == "growth_pct":
        if hz == "next quarter" and re.search(r"quarter[- ]on[- ]quarter|q-?o-?q|sequential|against quarter", st["quote"], re.I):
            rq = st.get("ref_quarter")
            a, b = s["q"][key].get(window[0]), s["q"][key].get(rq)
            actual = round((a / b - 1) * 100, 1) if a and b else None
            out["note"] = "QoQ growth" + (" (checked against revenue)" if metric != "revenue" else "")
        else:
            actual = _yoy_over(window, s, key)
        target = low
    elif kind == "margin_pct":
        actual = _margin_over(window, s)
        target = low
    else:  # absolute_cr: annualise YTD
        vals = [s["q"][key].get(p) for p in window]
        if any(v is None for v in vals):
            return out
        actual = round(sum(vals) * (4 / len(window)) if not complete else sum(vals), 0)
        target = low
        out["note"] = "annualised from YTD" if not complete else None
    if actual is None or target is None:
        return out
    out["actual"] = actual
    tol = 1.0 if kind in ("growth_pct", "margin_pct") else 0.03 * target
    if complete:
        out["verdict"] = "delivered" if actual >= target - tol else ("partly" if actual >= target * 0.8 else "missed")
    else:
        out["verdict"] = "on_track" if actual >= target - tol else "behind"
    return out


# ---------------------------------------------------------------------------
# 4. Grouping + 5. optional deep judgement
# ---------------------------------------------------------------------------

def _fmt_target(st: dict) -> str:
    lo, hi, k = st.get("low"), st.get("high"), st["kind"]
    unit = "%" if k in ("growth_pct", "margin_pct") else " cr"
    if lo is None:
        return "qualitative"
    return f"{lo:g}–{hi:g}{unit}" if hi else f"{lo:g}{unit}"


METRIC_LABEL = {"revenue": "Revenue", "ebitda_margin": "EBITDA margin", "ebitda": "EBITDA", "pat": "PAT",
                "volume": "Volume", "capex": "Capex", "debt": "Debt", "segment": "Segment", "other": "Other"}


def build_ledger(c: dict, n_calls: int = 4) -> dict[str, Any]:
    stmts, method = extract_all(c, n_calls)
    s = _series(c)
    for st in stmts:
        if method == "heuristic":
            # Regex can find guidance-shaped sentences but can't tell a promise from a past fact or a
            # segment from the company. Showing a verdict on that would be confidently wrong, so we don't.
            st.update({"verdict": "mention", "actual": None, "window": None,
                       "note": "Pattern-matched; add a model key to extract and verify"})
        else:
            st.update(verify(st, c, s))
    # Group the same promise (metric + kind + horizon) across calls; earliest call is the promise.
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for st in stmts:
        groups[(st["metric"], st["kind"], st["horizon"])].append(st)
    items = []
    order = {c_["period"]: i for i, c_ in enumerate(transcripts(c, n_calls))}
    for (metric, kind, hz), g in groups.items():
        g.sort(key=lambda x: -order.get(x["call"], 0))  # oldest first
        first, last = g[0], g[-1]
        revised = None
        if (method == "model" and first.get("low") and last.get("low") and first is not last and abs(last["low"] - first["low"]) >= 1
                and first.get("confident", True) and last.get("confident", True) and 0.5 <= last["low"] / first["low"] <= 2):
            revised = {"from": _fmt_target(first), "to": _fmt_target(last), "in": last["call"],
                       "direction": "up" if last["low"] > first["low"] else "down"}
        judged = last if last["verdict"] not in ("unverifiable",) else first
        items.append({
            "metric": METRIC_LABEL.get(metric, metric), "metric_key": metric, "kind": kind, "horizon": hz,
            "promised": _fmt_target(first), "promised_in": first["call"],
            "quote": first["quote"], "page": first.get("page"), "speaker": first.get("speaker"), "url": first.get("url"),
            "reiterated": len({x["call"] for x in g}) - 1, "revised": revised,
            "verdict": judged["verdict"], "actual": judged.get("actual"), "window": judged.get("window"),
            "note": judged.get("note"), "latest_target": _fmt_target(last),
        })
    rank = {"missed": 0, "behind": 1, "partly": 2, "delivered": 3, "on_track": 4, "pending": 5, "mention": 6, "unverifiable": 7}
    items.sort(key=lambda x: (rank.get(x["verdict"], 9), x["metric"]))
    judged = [i for i in items if i["verdict"] in ("delivered", "missed", "partly")]
    score = {
        "delivered": sum(i["verdict"] == "delivered" for i in items),
        "missed": sum(i["verdict"] == "missed" for i in items),
        "partly": sum(i["verdict"] == "partly" for i in items),
        "on_track": sum(i["verdict"] == "on_track" for i in items),
        "behind": sum(i["verdict"] == "behind" for i in items),
        "judged": len(judged),
        "revised_down": sum(1 for i in items if (i["revised"] or {}).get("direction") == "down"),
    }
    calls = [x["period"] for x in transcripts(c, n_calls)]
    ledger = {"items": items[:14], "score": score, "calls": calls, "method": method,
              "observations": _observations(items, score)}
    if llm.enabled() and items:
        deep = _deep_judgement(c, items, s)
        if deep:
            ledger["observations"] = deep.get("observations") or ledger["observations"]
            ledger["deep"] = True
    return ledger


def _observations(items: list[dict], score: dict) -> list[str]:
    obs = []
    for i in items:
        if i["verdict"] == "missed":
            obs.append(f"Guided {i['metric'].lower()} {i['promised']} for {i['window'] or i['horizon']} in the {i['promised_in']} call; delivered {i['actual']:g}{'%' if i['kind'] != 'absolute_cr' else ' cr'}.")
        elif i["verdict"] == "behind":
            obs.append(f"{i['metric']} {i['promised']} for {i['horizon']} is running behind: {i['actual']:g}{'%' if i['kind'] != 'absolute_cr' else ' cr'} {i['window']}.")
        if i["revised"] and i["revised"]["direction"] == "down":
            obs.append(f"{i['metric']} guidance for {i['horizon']} was cut from {i['revised']['from']} to {i['revised']['to']} ({i['revised']['in']}).")
    if not obs and score["judged"]:
        obs.append(f"Delivered {score['delivered']} of {score['judged']} checkable promises; no misses in the window.")
    return obs[:4]


DEEP_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {"type": "array", "items": {"type": "string"},
                          "description": "2-4 sentences on where management's narrative and the numbers agree or diverge. Each cites a number."},
    },
    "required": ["observations"],
    "additionalProperties": False,
}


def _deep_judgement(c: dict, items: list[dict], s: dict) -> dict | None:
    qs = list(s["q"]["revenue"].keys())[-8:]
    table = "\n".join(
        f"{p}: revenue {s['q']['revenue'].get(p)}, EBITDA {s['q']['ebitda'].get(p)}, PAT {s['q']['pat'].get(p)}" for p in qs)
    lines = "\n".join(
        f"- [{i['promised_in']}] {i['metric']} {i['promised']} for {i['horizon']} | verdict {i['verdict']} | actual {i['actual']} {i['window'] or ''} | "
        f"revised: {i['revised']} | quote: \"{i['quote'][:220]}\"" for i in items[:14])
    prompt = (
        f"Company: {c['name']} ({'lender' if is_lender(c) else 'operating company'}). Reported quarterly numbers (INR crore):\n{table}\n\n"
        f"Management guidance from the last four earnings calls, already checked against the numbers:\n{lines}\n\n"
        "Write 2-4 sharp observations for a portfolio manager on where management's narrative and the delivered "
        "numbers agree or diverge: credibility, moved goalposts, what to press management on next call. Use only the "
        "data above. No outside knowledge. Every observation must cite a specific number from above."
    )
    return llm.complete("narrative_vs_numbers", prompt, tier="deep", schema=DEEP_SCHEMA, max_tokens=3000)
