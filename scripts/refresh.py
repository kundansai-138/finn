"""Scheduled job: pull EOD prices + announcements, then pre-build dossiers for the demo set.

    python -m scripts.refresh            # brief data only
    python -m scripts.refresh --dossiers # also pre-compute every portfolio + research dossier

In production: announcements every 5 min (NSE RSS), prices + full refresh after market close.
"""
from __future__ import annotations

import argparse
import time

from finn import portfolio, pull, push


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dossiers", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    stats = push.refresh(verbose=True)
    print(f"refresh: {stats} ({time.time() - t0:.0f}s)")
    if args.dossiers:
        for t in portfolio.tickers() + [r["ticker"] for r in portfolio.research()]:
            t1 = time.time()
            try:
                d = pull.dossier(t, force=True)
                print(f"  dossier {t}: ledger={d['ledger']['method']} items={len(d['ledger']['items'])} ({time.time() - t1:.0f}s)")
            except Exception as e:
                print(f"  dossier {t}: FAILED {e}")


if __name__ == "__main__":
    main()
