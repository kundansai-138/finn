"""Copy reference data (public market data) from one database to another, e.g. local → Neon.

    python -m scripts.copy_reference --to "postgresql+psycopg://...neon.tech/finn?sslmode=require"

Why: cloud hosts are often blocked by Yahoo/screener, and re-downloading every transcript from a
fresh deployment is slow and impolite. Shipping the already-fetched public data means the deployed
demo works on its first request. Only reference tables move; personal data never leaves a user's
database (production users seed their own via the app).
"""
from __future__ import annotations

import argparse

from sqlalchemy import create_engine, select

from finn import store
from finn.config import DATABASE_URL

TABLES = [store.companies_t, store.eod_prices, store.announcements, store.documents, store.doc_pages,
          store.doc_chunks, store.orgs, store.http_cache]


def main(src: str, dst: str, batch: int = 500) -> None:
    s, d = create_engine(src), create_engine(dst)
    store.md.create_all(d)
    for t in TABLES:
        keys = [c.name for c in t.primary_key.columns]
        n = 0
        with s.connect() as sc:
            rows = [dict(r._mapping) for r in sc.execute(select(t))]
        for i in range(0, len(rows), batch):
            with d.begin() as dc:
                store._upsert(dc, t, rows[i:i + batch], keys)
            n += len(rows[i:i + batch])
        print(f"{t.name:15s} {n:6d} rows")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", default=DATABASE_URL)
    ap.add_argument("--to", dest="dst", required=True)
    a = ap.parse_args()
    main(a.src, a.dst)
