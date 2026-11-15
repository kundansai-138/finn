"""The current user's portfolio, research list and alert rules (all per-user rows in the database)."""
from __future__ import annotations

from typing import Any

from . import store

DEFAULT_RULES: dict[str, Any] = {
    # Categories that should interrupt you (they get an alert badge and a score bump).
    "watch": ["results", "M&A", "management_change", "litigation", "capex", "fund_raise", "credit_rating", "pledging"],
    # Categories you never want surfaced above routine.
    "mute": ["investor_meetings", "postal_ballot", "agm_egm"],
    "price_move_pct": 4.0,   # |day move| that counts as unusual
    "volume_x": 2.0,         # volume vs 20-day average
    "include_simulated": True,
}


def holdings() -> list[dict[str, Any]]:
    return store.holdings("portfolio")


def tracked() -> set[str]:
    """Every ticker any investor holds: public reference data (a set of tickers, nothing linking them to
    who holds them), so the shared refresh fetches filings for companies only a signed-in user follows."""
    from . import db

    return set(db.kv_get("tracked", "tickers") or [])


def track(ticker: str) -> None:
    from . import db

    t = tracked()
    if ticker not in t:
        db.kv_put("tracked", "tickers", sorted(t | {ticker}))


def research() -> list[dict[str, Any]]:
    return store.holdings("research")


def tickers() -> list[str]:
    return [h["ticker"] for h in holdings()]


def weight(ticker: str) -> float:
    return next((h["weight"] for h in holdings() if h["ticker"] == ticker), 0.0)


def holding(ticker: str) -> dict[str, Any] | None:
    return next((h for h in holdings() + research() if h["ticker"] == ticker), None)


def rules() -> dict[str, Any]:
    return {**DEFAULT_RULES, **(store.rules_get() or {})}


def save_rules(update: dict[str, Any]) -> dict[str, Any]:
    cur = rules()
    for k in DEFAULT_RULES:
        if k in update:
            cur[k] = update[k]
    store.rules_put(cur)
    return cur
