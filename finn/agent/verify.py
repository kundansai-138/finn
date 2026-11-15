"""Grounding check: every figure in an answer must trace back to the evidence it cites.

Deterministic and cheap, so it runs on every answer:
  - every number with substance (not a year, not list numbering) must appear in the evidence,
    allowing for rounding (12.46 → 12.5, 1,234.6 → 1,235)
  - sentences that state figures must carry a citation
  - citations must point at evidence that exists
A failure sends the draft back through one repair pass; whatever's left is shown to the investor.
"""
from __future__ import annotations

import re
from typing import Any

_NUM = re.compile(r"(?<![\w.\[])(?:₹\s?)?(-?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)\s*(%|x\b|×|bps|cr\b|crore|mn|bn)?", re.I)
_CITE = re.compile(r"\[E\d+(?:\s*,\s*E?\d+)*\]")


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


def check(answer: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    known = {e["id"] for e in evidence}
    pool = [v for e in evidence for v, _, _ in numbers(f"{e.get('title', '')} {e.get('text', '')}")]
    cited = {f"E{n}" for c in _CITE.findall(answer) for n in re.findall(r"\d+", c)}
    unknown = sorted(cited - known)
    total = supported = 0
    unsupported, uncited = [], []
    # A bullet or paragraph that ends with its citation covers every sentence in it.
    for line in answer.split("\n"):
        line_cited = bool(_CITE.search(line))
        for sent in re.split(r"(?<=[.!?])\s+", line):
            figs = [(v, u, raw) for v, u, raw in numbers(sent) if _meaningful(v, u, sent)]
            if not figs:
                continue
            if not (line_cited or _CITE.search(sent)):
                uncited.append(sent.strip()[:160])
            for v, u, raw in figs:
                total += 1
                if _supported(v, pool):
                    supported += 1
                else:
                    unsupported.append(raw)
    return {"figures": total, "supported": supported, "unsupported": unsupported[:8],
            "uncited": uncited[:5], "unknown_citations": unknown,
            "ok": not unsupported and not unknown and not uncited}
