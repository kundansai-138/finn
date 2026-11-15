"""Company data access: snapshot first, live fetch on miss (works for any NSE ticker)."""
from __future__ import annotations

from typing import Any

from . import store
from .config import TTL_SCREENER
from .sources import screener


def get(ticker: str, max_age: float = TTL_SCREENER) -> dict[str, Any]:
    t = ticker.upper().strip()
    hit = store.company_get(t, max_age=max_age)
    if hit:
        return hit
    try:
        c = screener.fetch_company(t)
    except Exception:
        stale = store.company_get(t)  # any age beats failing the demo
        if stale:
            return stale
        raise
    store.company_put(c)
    return c


def cached(ticker: str) -> dict[str, Any] | None:
    return store.company_get(ticker.upper())
