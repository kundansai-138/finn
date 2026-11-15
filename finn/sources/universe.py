"""The NSE equity universe (NSE's public list of every listed company), for name → ticker resolution.

Lets "How is Titan's cash conversion?" or "Navin Fluorine" resolve to TITAN / NAVINFLUOR, so the
ask bar and the agent work for any listed company, not just the ones in the portfolio file.
"""
from __future__ import annotations

import csv
import io
import re
from functools import lru_cache

from ..http import FetchError, fetch

URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
WEEK = 7 * 86400
_DROP = re.compile(r"\b(limited|ltd|the|company|co|corporation|corp|india|of|and|&)\b\.?", re.I)
COMMON = set("""what which when where why how who whom this that these those their there then than with from into about over under
after before again also just like make made more most much many some such very will would could should might must only even
good great best better high low new old next last first second third year years quarter results india indian market share
shares stock stocks price prices sector company companies business growth margin margins cash debt capital power energy""".split())


def _norm(name: str) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", _DROP.sub(" ", name.lower())).split() if w]


@lru_cache(maxsize=1)
def load() -> tuple[dict[str, str], dict[tuple[str, ...], set[str]]]:
    """(symbol -> name, name-prefix tuple -> symbols)."""
    try:
        raw = fetch(URL, WEEK)
    except FetchError:
        return {}, {}
    rows = list(csv.reader(io.StringIO(raw)))
    sym_name, prefixes = {}, {}
    for r in rows[1:]:
        if len(r) < 3 or r[2].strip() not in ("EQ", "BE", "BZ", "SM"):
            continue
        sym, name = r[0].strip(), r[1].strip()
        sym_name[sym] = name
        words = _norm(name)
        for k in range(1, min(3, len(words)) + 1):
            prefixes.setdefault(tuple(words[:k]), set()).add(sym)
    return sym_name, prefixes


def resolve(text: str) -> str | None:
    """Best ticker mentioned in free text, or None. Longest unambiguous name match wins."""
    sym_name, prefixes = load()
    if not sym_name:
        return None
    for tok in re.findall(r"\b[A-Z][A-Z0-9&\-]{1,14}\b", text):  # explicit symbols, e.g. "TITAN"
        if tok in sym_name and tok.lower() not in COMMON:
            return tok
    words = re.findall(r"[A-Za-z][A-Za-z0-9&]*", re.sub(r"'s\b", "", text))
    best = None
    for i in range(len(words)):
        for k in (3, 2, 1):
            seg = words[i: i + k]
            if len(seg) < k or not seg[0][0].isupper():
                continue
            key = tuple(_norm(" ".join(seg)))
            if not key or len(key) != k:
                continue
            hits = prefixes.get(key)
            if not hits or len(hits) != 1:
                continue
            if k == 1 and (len(key[0]) < 4 or key[0] in COMMON):
                continue
            if best is None or k > best[0]:
                best = (k, next(iter(hits)))
        if best and best[0] == 3:
            break
    return best[1] if best else None


def name(symbol: str) -> str | None:
    return load()[0].get(symbol.upper())
