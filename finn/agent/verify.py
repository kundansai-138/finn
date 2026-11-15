"""Grounding check: every figure in an answer must trace back to the evidence it cites.

Deterministic and cheap, so it runs on every answer:
  - every number with substance (not a year, not a date, not list numbering) must appear in the
    evidence, allowing for rounding (12.46 → 12.5, 1,234.6 → 1,235)
  - or be arithmetic on figures shown in the same sentence that do appear in the evidence
    ("PBT ex other income ₹460 cr (₹532 cr − ₹72 cr)"): the answer prompt allows a derived figure
    only with its inputs shown, and this checks the sum, difference, ratio or % change
  - sentences that state figures must carry a citation
  - citations must point at evidence that exists
A failure sends the draft back through one repair pass; whatever's left is shown to the investor.
"""
from __future__ import annotations

import re
from typing import Any

_NUM = re.compile(r"(?<![\w.\[])(?:₹\s?)?(-?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)\s*(%|x\b|×|bps|cr\b|crore|mn|bn)?", re.I)
_CITE = re.compile(r"\[E\d+(?:\s*,\s*E?\d+)*\]")
# Day-of-month in a date ("Nov 10–15", "12 Nov", "Nov 13, 2026") is a date, not a figure.
# Whole month names only ("margin 15 bps" is not a date), and a day that isn't part of a figure
# ("Sep 12.5%", "2.7 Sep" stay figures).
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
          r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?(?![a-z])")
_DAY = r"\d{1,2}(?:\s*[–-]\s*\d{1,2})?"
_NOT_FIGURE = r"(?![.,]?\d)(?!\s*(?:%|bps|x\b|×|cr\b|crore|mn\b|bn\b))"
_DATE = re.compile(rf"\b{_MONTH}\s+{_DAY}{_NOT_FIGURE}(?:,?\s+\d{{4}}\b)?"
                   rf"|(?<![\d.,₹])\b{_DAY}\s+{_MONTH}(?:,?\s+\d{{4}}\b)?", re.I)


def numbers(text: str) -> list[tuple[float, str, str]]:
    out = []
    clean = _CITE.sub(" ", text)
    for m in _NUM.finditer(clean):
        raw, unit = m.group(1), (m.group(2) or "").lower()
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        out.append((v, unit, m.group(0).strip()))
    return out


def _meaningful(v: float, unit: str, sentence: str) -> bool:
    if unit:
        return True
    if 1990 <= v <= 2100 and float(v).is_integer():
        return False  # a year
    if abs(v) <= 10 and float(v).is_integer():
        return False  # counts, list numbering, "3 points"
    if re.search(rf"\b(?:Q[1-4]|FY|H[12])\s?'?{int(abs(v))}\b", sentence, re.I):
        return False
    return True


def _supported(v: float, pool: list[float]) -> bool:
    for e in pool:
        if abs(v - e) <= max(0.051, 0.005 * abs(e)) or round(e) == v or round(e, 1) == v or abs(abs(v) - abs(e)) < 1e-9:
            return True
    return False


def _derived(v: float, inputs: list[float]) -> bool:
    """Is v the sum, difference, ratio or % change of two traced figures shown beside it?"""
    cands = []
    for i, a in enumerate(inputs):
        for b in inputs[i + 1:]:
            cands += [a + b, a - b]
            for x, y in ((a, b), (b, a)):
                if y:
                    cands += [x / y, x / y * 100, (x - y) / y * 100]
    return _supported(abs(v), [abs(c) for c in cands])


def check(answer: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    known = {e["id"] for e in evidence}
    pool = [v for e in evidence for v, _, _ in numbers(f"{e.get('title', '')} {e.get('text', '')}")]
    cited = {f"E{n}" for c in _CITE.findall(answer) for n in re.findall(r"\d+", c)}
    unknown = sorted(cited - known)
    total = supported = derived = 0
    unsupported, uncited = [], []
    # A bullet or paragraph that ends with its citation covers every sentence in it.
    for line in answer.split("\n"):
        line_cited = bool(_CITE.search(line))
        for sent in re.split(r"(?<=[.!?])\s+", line):
            body = _DATE.sub(" ", sent)
            figs = [(v, u, raw) for v, u, raw in numbers(body) if _meaningful(v, u, body)]
            if not figs:
                continue
            if not (line_cited or _CITE.search(sent)):
                uncited.append(sent.strip()[:160])
            traced = [v for v, _, _ in figs if _supported(v, pool)]
            for v, u, raw in figs:
                total += 1
                if _supported(v, pool):
                    supported += 1
                elif _derived(v, traced):
                    supported += 1
                    derived += 1
                else:
                    unsupported.append(raw)
    return {"figures": total, "supported": supported, "derived": derived, "unsupported": unsupported[:8],
            "uncited": uncited[:5], "unknown_citations": unknown,
            "ok": not unsupported and not unknown and not uncited}
