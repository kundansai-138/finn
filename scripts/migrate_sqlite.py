"""One-off: copy the prototype's SQLite data into the database at DATABASE_URL (Postgres).

    python -m scripts.migrate_sqlite [path/to/finn.sqlite3]

Moves what's expensive or impolite to re-fetch (cached HTTP bodies, parsed PDFs, transcript chunks,
prices, company snapshots, classified filings, the model-cost ledger). Personal data is re-seeded
from the JSON seed files by store.init(), so nothing user-specific is copied blindly.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from finn import store
from finn.config import DATA, DATABASE_URL


def main(path: str) -> None:
    if DATABASE_URL.startswith("sqlite"):
        raise SystemExit("DATABASE_URL points at SQLite; set it to the Postgres URL first (see .env).")
    src = sqlite3.connect(path)
    src.row_factory = sqlite3.Row
    store.init()
    n = {}
    rows = list(src.execute("SELECT key, fetched_at, status, body FROM http_cache"))
    with store.tx() as c:
        for i in range(0, len(rows), 200):
            store._upsert(c, store.http_cache, [dict(r) for r in rows[i:i + 200]], ["key"])
    n["http_cache"] = len(rows)
    rows = [dict(r) for r in src.execute("SELECT * FROM announcements")]
    store.ann_upsert(rows)
    n["announcements"] = len(rows)
    kv = list(src.execute("SELECT ns, key, updated_at, value FROM kv"))
    for r in kv:
        v = json.loads(r["value"])
        if r["ns"] == "eod":
            store.prices_put(r["key"], v["dates"], v["close"], v["volume"])
            store.kv_put("eod_fetched", r["key"], v["dates"][-1])
        elif r["ns"] == "company":
            store.company_put(v)
        elif r["ns"] == "pdf_text":
            store.doc_pages_put(r["key"], url="", pages=v)
        elif r["ns"] == "chunks":
            store.chunks_put(r["key"], v)
        elif r["ns"] in ("llm", "meta"):
            store.kv_put(r["ns"], r["key"], v)
        n[r["ns"]] = n.get(r["ns"], 0) + 1
    led = [dict(r) for r in src.execute("SELECT * FROM llm_ledger")]
    for r in led:
        store.ledger_add(r)
    n["llm_ledger"] = len(led)
    print("migrated:", n)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path(DATA) / "finn.sqlite3"))
