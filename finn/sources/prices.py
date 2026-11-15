"""End-of-day price & volume via yfinance, plus the derived signals Push cares about."""
from __future__ import annotations

import logging
import math
from typing import Any

from .. import db, store
from ..config import TTL_PRICES

log = logging.getLogger(__name__)


def _yf_symbol(ticker: str) -> str:
    return ticker if "." in ticker else f"{ticker}.NS"


def fetch_eod(tickers: list[str], period: str = "6mo", force: bool = False) -> dict[str, dict[str, Any]]:
    """Return {ticker: {dates, close, volume, metrics}}; cached per ticker for TTL_PRICES."""
    out: dict[str, dict[str, Any]] = {}
    missing = []
    ages = {} if force else store.kv_ages("eod_fetched", tickers)
    fresh = [t for t in tickers if ages.get(t, 1e12) < TTL_PRICES]
    series = store.prices_many(fresh)          # one query for every fresh ticker
    for t in tickers:
        hit = series.get(t)
        if hit:
            hit["metrics"] = metrics(hit)
            out[t] = hit
        else:
            missing.append(t)
    if missing:
        try:
            import yfinance as yf

            df = yf.download(
                [_yf_symbol(t) for t in missing], period=period, interval="1d",
                group_by="ticker", auto_adjust=False, progress=False, threads=True,
            )
        except Exception as e:  # network down: fall back to whatever is cached, however old
            log.warning("yfinance failed: %s", e)
            df = None
        for t in missing:
            series = None
            if df is not None:
                try:
                    sub = df[_yf_symbol(t)] if len(missing) > 1 or hasattr(df.columns, "levels") else df
                    sub = sub.dropna(subset=["Close"])
                    series = {
                        "dates": [d.strftime("%Y-%m-%d") for d in sub.index],
                        "close": [round(float(x), 2) for x in sub["Close"]],
                        "volume": [int(x) if not math.isnan(x) else 0 for x in sub["Volume"]],
                    }
                except Exception as e:
                    log.warning("no price data for %s: %s", t, e)
            if series and series["close"]:
                store.prices_put(t, series["dates"], series["close"], series["volume"])
                db.kv_put("eod_fetched", t, series["dates"][-1])
                full = store.prices_get(t) or series   # the table keeps history beyond this download
                full["metrics"] = metrics(full)
                out[t] = full
            else:
                stale = store.prices_get(t)  # network down: whatever history we have, however old
                if stale:
                    stale["metrics"] = metrics(stale)
                    out[t] = stale
    return out


def metrics(s: dict[str, Any], vol_window: int = 20) -> dict[str, Any]:
    """Day change, volume multiple vs trailing average, and a 30-day sparkline."""
    c, v = s["close"], s["volume"]
    if len(c) < 2:
        return {}
    chg = (c[-1] / c[-2] - 1) * 100
    hist = [x for x in v[-(vol_window + 1):-1] if x]
    avg = sum(hist) / len(hist) if hist else 0
    vol_x = (v[-1] / avg) if avg else None
    # 20-day realised volatility of daily returns, to express a move in "sigmas"
    rets = [(c[i] / c[i - 1] - 1) * 100 for i in range(max(1, len(c) - 21), len(c) - 1)]
    sd = (sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / len(rets)) ** 0.5 if len(rets) > 2 else None
    return {
        "date": s["dates"][-1],
        "close": c[-1],
        "prev_close": c[-2],
        "change_pct": round(chg, 2),
        "volume": v[-1],
        "avg_volume_20d": int(avg),
        "volume_x": round(vol_x, 2) if vol_x else None,
        "sigma": round(chg / sd, 1) if sd else None,
        "spark": c[-30:],
        "spark_vol": v[-30:],
        "high_52w": max(c[-250:]),
        "low_52w": min(c[-250:]),
    }
