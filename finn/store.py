"""FINN's database: one schema, SQLite locally, Postgres in production (DATABASE_URL).

Three kinds of data, kept apart on purpose:

  REFERENCE (shared by all users; public market data)
    companies       parsed company snapshot (financial tables, document links)
    eod_prices      one row per ticker per trading day
    announcements   exchange filings, classified
    documents / doc_pages / doc_chunks   primary documents, page text, retrieval chunks
    orgs, people, roles, education       the people-company graph (profiles of *public* people)

  PERSONAL (scoped by user_id; never visible to another user)
    users, holdings, alert_rules
    contacts        the investor's relationships (tie type, last contact): the private part of the network
    people.owner_user_id  profiles a user imported from their own contacts are visible only to them
    chat_threads, chat_messages, memory_facts
    reco_weights, reco_feedback

  CACHES (key-value: dossiers, model responses, tool results, HTTP bodies)

In Postgres, the personal tables get row-level security on user_id; the code already passes
user_id on every personal read and write (see finn/context.py).
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterable

from sqlalchemy import (JSON, Boolean, Column, Float, Index, Integer, LargeBinary, MetaData, String, Table, Text,
                        create_engine, delete, event, func, or_, select, text, update)
from sqlalchemy.engine import Engine

from .config import DATA, DATABASE_URL
from .context import current_user

log = logging.getLogger(__name__)
md = MetaData()

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# -- caches (kept identical to the original prototype tables so existing data survives)
http_cache = Table("http_cache", md, Column("key", String(64), primary_key=True), Column("fetched_at", Float, nullable=False),
                   Column("status", Integer), Column("body", LargeBinary))
kv = Table("kv", md, Column("ns", String(40), primary_key=True), Column("key", String(255), primary_key=True),
           Column("updated_at", Float, nullable=False), Column("value", Text, nullable=False))
llm_ledger = Table("llm_ledger", md, Column("at", Float, nullable=False), Column("model", String(60), nullable=False),
                   Column("purpose", String(60), nullable=False), Column("prompt_hash", String(64), nullable=False),
                   Column("input_tokens", Integer), Column("output_tokens", Integer), Column("cache_read_tokens", Integer),
                   Column("cost_usd", Float), Column("cached", Integer, nullable=False))

# -- reference data
announcements = Table(
    "announcements", md,
    Column("id", String(40), primary_key=True), Column("ticker", String(20), nullable=False), Column("ts", String(40), nullable=False),
    Column("title", Text, nullable=False), Column("summary", Text), Column("url", Text), Column("source", String(20), nullable=False),
    Column("category", String(30)), Column("subtype", String(30)), Column("score", Float), Column("extra", Text),
    Index("ix_ann_ts", "ts"), Index("ix_ann_ticker", "ticker"))
companies_t = Table("companies", md, Column("ticker", String(20), primary_key=True), Column("name", Text),
                    Column("fetched_at", Float, nullable=False), Column("snapshot", JSON, nullable=False))
eod_prices = Table("eod_prices", md, Column("ticker", String(20), primary_key=True), Column("date", String(10), primary_key=True),
                   Column("close", Float, nullable=False), Column("volume", Float))
documents = Table("documents", md, Column("id", String(64), primary_key=True), Column("url", Text), Column("ticker", String(20)),
                  Column("kind", String(20)), Column("label", String(60)), Column("n_pages", Integer), Column("fetched_at", Float))
doc_pages = Table("doc_pages", md, Column("doc_id", String(64), primary_key=True), Column("page", Integer, primary_key=True),
                  Column("text", Text))
doc_chunks = Table("doc_chunks", md, Column("id", String(160), primary_key=True), Column("doc_key", String(255), nullable=False),
                   Column("ticker", String(20), nullable=False), Column("doc", String(20)), Column("label", String(60)),
                   Column("url", Text), Column("page", Integer), Column("speaker", String(120)), Column("qa", Boolean),
                   Column("seq", Integer), Column("text", Text, nullable=False), Index("ix_chunk_doc", "doc_key"))
orgs = Table("orgs", md, Column("id", String(40), primary_key=True), Column("name", Text, nullable=False), Column("ticker", String(20)),
             Column("kind", String(20)), Column("themes", JSON), Column("peers", JSON), Column("customers", JSON),
             Column("suppliers", JSON), Column("aliases", JSON))
people = Table("people", md, Column("id", String(60), primary_key=True), Column("owner_user_id", String(40)),
               Column("name", Text, nullable=False), Column("headline", Text), Column("location", Text),
               Column("simulated", Boolean, default=False), Column("source", Text), Column("credentials", JSON), Column("tags", JSON))
roles = Table("roles", md, Column("id", Integer, primary_key=True, autoincrement=True), Column("person_id", String(60), nullable=False),
              Column("org_id", String(40), nullable=False), Column("title", Text), Column("function", String(40)),
              Column("start", String(10)), Column("end", String(10)), Index("ix_roles_person", "person_id"))
education = Table("education", md, Column("id", Integer, primary_key=True, autoincrement=True), Column("person_id", String(60), nullable=False),
                  Column("org_id", String(40), nullable=False), Column("degree", Text), Column("start", String(10)), Column("end", String(10)))

# -- personal data (every row carries user_id)
users = Table("users", md, Column("id", String(40), primary_key=True), Column("name", Text), Column("self_person_id", String(60)),
              Column("created_at", Float))
holdings_t = Table("holdings", md, Column("user_id", String(40), primary_key=True), Column("ticker", String(20), primary_key=True),
                   Column("kind", String(12), nullable=False), Column("name", Text), Column("theme", Text), Column("weight", Float),
                   Column("position", Integer))
contacts = Table("contacts", md, Column("user_id", String(40), primary_key=True), Column("person_id", String(60), primary_key=True),
                 Column("tie_type", String(20)), Column("context", Text), Column("since", String(10)),
                 Column("last_contact", String(10)), Column("interactions_12m", Integer))
explicit_links = Table("explicit_links", md, Column("id", Integer, primary_key=True, autoincrement=True), Column("user_id", String(40)),
                       Column("a", String(60)), Column("b", String(60)), Column("context", Text), Column("strength", Float))
alert_rules = Table("alert_rules", md, Column("user_id", String(40), primary_key=True), Column("rules", JSON, nullable=False))
reco_weights = Table("reco_weights", md, Column("user_id", String(40), primary_key=True), Column("weights", JSON, nullable=False),
                     Column("updated_at", Float))
reco_feedback = Table("reco_feedback", md, Column("id", Integer, primary_key=True, autoincrement=True), Column("user_id", String(40), nullable=False),
                      Column("person_id", String(60)), Column("query_key", String(20)), Column("query", JSON), Column("label", Integer),
                      Column("p_before", Float), Column("at", Float))
chat_threads = Table("chat_threads", md, Column("id", String(20), primary_key=True), Column("user_id", String(40), nullable=False),
                     Column("created_at", Float), Column("title", Text), Column("summary", Text), Column("summarised_upto", Integer, default=0),
                     Column("focus", JSON), Column("pinned", JSON), Index("ix_threads_user", "user_id"))
chat_messages = Table("chat_messages", md, Column("id", Integer, primary_key=True, autoincrement=True), Column("thread_id", String(20), nullable=False),
                      Column("seq", Integer, nullable=False), Column("role", String(12), nullable=False), Column("content", Text, nullable=False),
                      Column("meta", JSON), Column("at", Float), Index("ix_msgs_thread", "thread_id"))
memory_facts = Table("memory_facts", md, Column("id", Integer, primary_key=True, autoincrement=True), Column("user_id", String(40), nullable=False),
                     Column("text", Text, nullable=False), Column("source", String(30)), Column("at", Float))

# Mobile: Web Push subscriptions (one per device), what was already pushed, what the investor swiped away.
push_subs = Table("push_subscriptions", md, Column("endpoint", Text, primary_key=True), Column("user_id", String(40), nullable=False),
                  Column("p256dh", Text, nullable=False), Column("auth", Text, nullable=False), Column("ua", Text), Column("created_at", Float))
notified = Table("notifications_sent", md, Column("user_id", String(40), primary_key=True), Column("item_id", String(80), primary_key=True),
                 Column("at", Float))
dismissed = Table("dismissed_items", md, Column("user_id", String(40), primary_key=True), Column("item_id", String(80), primary_key=True),
                  Column("at", Float))

# Row-level security (Postgres). Each personal table admits only rows for app.user_id, which every
# connection sets (see _scope). FORCE makes the policies bind the table owner too; only superusers
# and BYPASSRLS roles skip them, which is why the app connects as a plain role.
_ME = "current_setting('app.user_id', true)"
RLS_POLICIES = {
    "users": f"id = {_ME}",
    "holdings": f"user_id = {_ME}",
    "contacts": f"user_id = {_ME}",
    "explicit_links": f"user_id = {_ME}",
    "alert_rules": f"user_id = {_ME}",
    "reco_weights": f"user_id = {_ME}",
    "reco_feedback": f"user_id = {_ME}",
    "chat_threads": f"user_id = {_ME}",
    "memory_facts": f"user_id = {_ME}",
    "push_subscriptions": f"user_id = {_ME}",
    "notifications_sent": f"user_id = {_ME}",
    "dismissed_items": f"user_id = {_ME}",
    "chat_messages": "thread_id IN (SELECT id FROM chat_threads)",        # inherits the thread's isolation
    "people": f"owner_user_id IS NULL OR owner_user_id = {_ME}",          # public profiles + your own imports
    "roles": "person_id IN (SELECT id FROM people)",                      # inherits the person's visibility
    "education": "person_id IN (SELECT id FROM people)",
}


def apply_rls(conn) -> None:
    for table, rule in RLS_POLICIES.items():
        conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        conn.execute(text(f"DROP POLICY IF EXISTS user_isolation ON {table}"))
        conn.execute(text(f"CREATE POLICY user_isolation ON {table} USING ({rule}) WITH CHECK ({rule})"))
    # Text search over transcript chunks, maintained by Postgres (used for lexical retrieval at scale).
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chunks_fts ON doc_chunks USING gin (to_tsvector('english', text))"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_people_name_trgm ON people USING gin (name gin_trgm_ops)"))

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        url = DATABASE_URL
        if url.startswith("sqlite"):
            _engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

            @event.listens_for(_engine, "connect")
            def _pragmas(conn, _):  # WAL: readers don't block the writer
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()
        else:
            # Sized to the web server's worker threads (anyio's pool is 40): a request should wait on
            # work, never on a free connection. Managed Postgres poolers (Neon/PgBouncer) sit behind this.
            _engine = create_engine(url, pool_pre_ping=True, pool_size=int(os.getenv("FINN_DB_POOL", "20")),
                                    max_overflow=20, pool_timeout=15, pool_recycle=1800)
        md.create_all(_engine)
    return _engine


def _scope(conn) -> None:
    """Tell Postgres who is asking. Row-level security policies read this setting."""
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT set_config('app.user_id', :u, false)"), {"u": current_user()})


@contextmanager
def tx():
    """A write transaction, scoped to the current user."""
    with engine().begin() as conn:
        _scope(conn)
        yield conn


@contextmanager
def ro():
    """A read connection, scoped to the current user."""
    with engine().connect() as conn:
        _scope(conn)
        yield conn


def _upsert(conn, table: Table, rows: list[dict] | dict, keys: list[str]) -> None:
    rows = [rows] if isinstance(rows, dict) else rows
    if not rows:
        return
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    ins = insert(table)
    cols = [c.name for c in table.columns if c.name not in keys and c.name in rows[0]]
    stmt = ins.on_conflict_do_update(index_elements=keys, set_={c: ins.excluded[c] for c in cols}) if cols else ins.on_conflict_do_nothing()
    conn.execute(stmt, rows)


def _uid(user_id: str | None) -> str:
    return user_id or current_user()


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------
def kv_get(ns: str, key: str, max_age: float | None = None) -> Any | None:
    with ro() as c:
        row = c.execute(select(kv.c.value, kv.c.updated_at).where(kv.c.ns == ns, kv.c.key == key)).first()
    if not row or (max_age is not None and time.time() - row.updated_at > max_age):
        return None
    return json.loads(row.value)


def kv_put(ns: str, key: str, value: Any) -> None:
    with tx() as c:
        _upsert(c, kv, {"ns": ns, "key": key, "updated_at": time.time(), "value": json.dumps(value, default=str)}, ["ns", "key"])


def kv_age(ns: str, key: str) -> float | None:
    with ro() as c:
        t = c.execute(select(kv.c.updated_at).where(kv.c.ns == ns, kv.c.key == key)).scalar()
    return None if t is None else time.time() - t


def kv_clear(*namespaces: str) -> None:
    with tx() as c:
        c.execute(delete(kv).where(kv.c.ns.in_(namespaces)))


def http_get(key: str):
    with ro() as c:
        return c.execute(select(http_cache).where(http_cache.c.key == key)).first()


def http_put(key: str, status: int, body: bytes) -> None:
    with tx() as c:
        _upsert(c, http_cache, {"key": key, "fetched_at": time.time(), "status": status, "body": body}, ["key"])


def ledger_add(row: dict) -> None:
    with tx() as c:
        c.execute(llm_ledger.insert().values(**row))


def ledger_summary() -> list[dict]:
    q = (select(llm_ledger.c.model, llm_ledger.c.purpose, func.count().label("n"), func.sum(llm_ledger.c.cached).label("hits"),
                func.sum(llm_ledger.c.cost_usd).label("cost"), func.sum(llm_ledger.c.input_tokens).label("it"),
                func.sum(llm_ledger.c.output_tokens).label("ot"), func.sum(llm_ledger.c.cache_read_tokens).label("cr"))
         .group_by(llm_ledger.c.model, llm_ledger.c.purpose).order_by(func.sum(llm_ledger.c.cost_usd).desc()))
    with ro() as c:
        return [dict(r._mapping) for r in c.execute(q)]


# ---------------------------------------------------------------------------
# Reference: announcements
# ---------------------------------------------------------------------------
def _ann(r) -> dict:
    return dict(r._mapping)


def ann_upsert(rows: list[dict]) -> int:
    with tx() as c:
        _upsert(c, announcements, rows, ["id"])
    return len(rows)


def ann_delete_source(source: str) -> None:
    with tx() as c:
        c.execute(delete(announcements).where(announcements.c.source == source))


def ann_since(since_iso: str, tickers: Iterable[str] | None = None) -> list[dict]:
    q = select(announcements).where(announcements.c.ts >= since_iso)
    if tickers is not None:
        q = q.where(announcements.c.ticker.in_(list(tickers)))
    with ro() as c:
        return [_ann(r) for r in c.execute(q.order_by(announcements.c.ts.desc()))]


def ann_for_ticker(ticker: str, limit: int = 40, category: str | None = None) -> list[dict]:
    q = select(announcements).where(announcements.c.ticker == ticker)
    if category:
        q = q.where(announcements.c.category == category)
    with ro() as c:
        return [_ann(r) for r in c.execute(q.order_by(announcements.c.ts.desc()).limit(limit))]


def ann_recent(limit: int = 200) -> list[dict]:
    with ro() as c:
        return [_ann(r) for r in c.execute(select(announcements).order_by(announcements.c.ts.desc()).limit(limit))]


# ---------------------------------------------------------------------------
# Reference: companies, prices, documents
# ---------------------------------------------------------------------------
def company_get(ticker: str, max_age: float | None = None) -> dict | None:
    with ro() as c:
        r = c.execute(select(companies_t).where(companies_t.c.ticker == ticker)).first()
    if not r or (max_age is not None and time.time() - r.fetched_at > max_age):
        return None
    return r.snapshot


def company_put(snapshot: dict) -> None:
    with tx() as c:
        _upsert(c, companies_t, {"ticker": snapshot["ticker"], "name": snapshot.get("name"), "fetched_at": time.time(),
                                 "snapshot": snapshot}, ["ticker"])


def prices_put(ticker: str, dates: list[str], close: list[float], volume: list[float]) -> None:
    with tx() as c:
        _upsert(c, eod_prices, [{"ticker": ticker, "date": d, "close": cl, "volume": v} for d, cl, v in zip(dates, close, volume)],
                ["ticker", "date"])


def prices_get(ticker: str, limit: int = 260) -> dict | None:
    with ro() as c:
        rows = list(c.execute(select(eod_prices).where(eod_prices.c.ticker == ticker).order_by(eod_prices.c.date.desc()).limit(limit)))
    if not rows:
        return None
    rows.reverse()
    return {"dates": [r.date for r in rows], "close": [r.close for r in rows], "volume": [int(r.volume or 0) for r in rows]}


def prices_many(tickers: list[str], limit: int = 260) -> dict[str, dict]:
    """All series in one round trip (the brief needs every holding at once)."""
    if not tickers:
        return {}
    with ro() as c:
        rows = list(c.execute(select(eod_prices).where(eod_prices.c.ticker.in_(tickers))
                              .order_by(eod_prices.c.ticker, eod_prices.c.date)))
    out: dict[str, dict] = {}
    for r in rows:
        s = out.setdefault(r.ticker, {"dates": [], "close": [], "volume": []})
        s["dates"].append(r.date)
        s["close"].append(r.close)
        s["volume"].append(int(r.volume or 0))
    for s in out.values():
        for k in s:
            s[k] = s[k][-limit:]
    return out


def kv_ages(ns: str, keys: list[str]) -> dict[str, float]:
    with ro() as c:
        rows = c.execute(select(kv.c.key, kv.c.updated_at).where(kv.c.ns == ns, kv.c.key.in_(keys)))
        now = time.time()
        return {r.key: now - r.updated_at for r in rows}


def doc_pages_get(doc_id: str) -> list[str] | None:
    with ro() as c:
        if c.execute(select(documents.c.id).where(documents.c.id == doc_id)).first() is None:
            return None
        return [r.text for r in c.execute(select(doc_pages.c.text).where(doc_pages.c.doc_id == doc_id).order_by(doc_pages.c.page))]


def doc_pages_put(doc_id: str, url: str, pages: list[str], ticker: str | None = None, kind: str | None = None, label: str | None = None) -> None:
    with tx() as c:
        _upsert(c, documents, {"id": doc_id, "url": url, "ticker": ticker, "kind": kind, "label": label, "n_pages": len(pages),
                               "fetched_at": time.time()}, ["id"])
        c.execute(delete(doc_pages).where(doc_pages.c.doc_id == doc_id))
        if pages:
            c.execute(doc_pages.insert(), [{"doc_id": doc_id, "page": i + 1, "text": p} for i, p in enumerate(pages)])


def chunks_get(doc_key: str) -> list[dict] | None:
    with ro() as c:
        rows = list(c.execute(select(doc_chunks).where(doc_chunks.c.doc_key == doc_key).order_by(doc_chunks.c.seq)))
    if not rows:
        return None
    return [{"id": r.id, "ticker": r.ticker, "doc": r.doc, "call": r.label, "url": r.url, "page": r.page, "speaker": r.speaker,
             "qa": bool(r.qa), "text": r.text} for r in rows]


def chunks_put(doc_key: str, chunks: list[dict]) -> None:
    with tx() as c:
        c.execute(delete(doc_chunks).where(doc_chunks.c.doc_key == doc_key))
        if chunks:
            c.execute(doc_chunks.insert(), [{"id": ch["id"], "doc_key": doc_key, "ticker": ch["ticker"], "doc": ch.get("doc"),
                                             "label": ch.get("call"), "url": ch.get("url"), "page": ch.get("page"),
                                             "speaker": ch.get("speaker"), "qa": bool(ch.get("qa")), "seq": i, "text": ch["text"]}
                                            for i, ch in enumerate(chunks)])


# ---------------------------------------------------------------------------
# Network: reference graph + this user's private relationships
# ---------------------------------------------------------------------------
def network_raw(user_id: str | None = None) -> dict[str, Any]:
    """The network in the shape graph.Network expects, assembled for one user."""
    uid = _uid(user_id)
    with ro() as c:
        u = c.execute(select(users).where(users.c.id == uid)).first()
        org_rows = list(c.execute(select(orgs)))
        visible = list(c.execute(select(people).where(or_(people.c.owner_user_id.is_(None), people.c.owner_user_id == uid))))
        ids = [p.id for p in visible] + ([u.self_person_id] if u and u.self_person_id else [])
        role_rows = list(c.execute(select(roles).where(roles.c.person_id.in_(ids)).order_by(roles.c.id)))
        edu_rows = list(c.execute(select(education).where(education.c.person_id.in_(ids)).order_by(education.c.id)))
        ties = {r.person_id: r for r in c.execute(select(contacts).where(contacts.c.user_id == uid))}
        links = [dict(r._mapping) for r in c.execute(select(explicit_links).where(explicit_links.c.user_id == uid))]
        self_row = c.execute(select(people).where(people.c.id == u.self_person_id)).first() if u and u.self_person_id else None
    pre = f"{uid}:"
    view = lambda pid: pid[len(pre):] if pid.startswith(pre) else pid  # noqa: E731
    rmap: dict[str, list] = {}
    for r in role_rows:
        rmap.setdefault(r.person_id, []).append({"org": r.org_id, "title": r.title, "function": r.function, "from": r.start, "to": r.end})
    emap: dict[str, list] = {}
    for e in edu_rows:
        emap.setdefault(e.person_id, []).append({"org": e.org_id, "degree": e.degree, "from": e.start, "to": e.end})

    def person(p) -> dict:
        d = {"id": view(p.id), "name": p.name, "headline": p.headline, "location": p.location, "roles": rmap.get(p.id, []),
             "education": emap.get(p.id, []), "tags": p.tags or [], "credentials": p.credentials or []}
        if p.simulated:
            d["simulated"] = True
        if p.source:
            d["source"] = p.source
        return d

    org_map = {}
    for o in org_rows:
        d = {"name": o.name}
        for k in ("ticker", "kind"):
            if getattr(o, k):
                d[k] = getattr(o, k)
        for k in ("themes", "peers", "customers", "suppliers", "aliases"):
            if getattr(o, k):
                d[k] = getattr(o, k)
        org_map[o.id] = d
    contacts_out, public = [], []
    for p in visible:
        if p.id == (u.self_person_id if u else None):
            continue
        d = person(p)
        t = ties.get(p.id)
        if t:
            d["tie"] = {"type": t.tie_type, "context": t.context, "since": t.since, "last_contact": t.last_contact,
                        "interactions_12m": t.interactions_12m or 0}
            contacts_out.append(d)
        else:
            public.append(d)
    you = {"id": "you", "name": "You", "headline": self_row.headline if self_row else "Investor",
           "roles": rmap.get(u.self_person_id, []) if u else [], "education": emap.get(u.self_person_id, []) if u else []}
    return {"orgs": org_map, "you": you, "people": contacts_out, "public_profiles": public,
            "explicit_links": [{"a": l["a"], "b": l["b"], "context": l["context"], "strength": l["strength"]} for l in links]}


# ---------------------------------------------------------------------------
# Personal: holdings, rules
# ---------------------------------------------------------------------------
def holdings(kind: str = "portfolio", user_id: str | None = None) -> list[dict]:
    with ro() as c:
        rows = c.execute(select(holdings_t).where(holdings_t.c.user_id == _uid(user_id), holdings_t.c.kind == kind)
                         .order_by(holdings_t.c.position))
        return [{"ticker": r.ticker, "name": r.name, "theme": r.theme, **({"weight": r.weight} if kind == "portfolio" else {})} for r in rows]


def rules_get(user_id: str | None = None) -> dict | None:
    with ro() as c:
        return c.execute(select(alert_rules.c.rules).where(alert_rules.c.user_id == _uid(user_id))).scalar()


def rules_put(rules: dict, user_id: str | None = None) -> None:
    with tx() as c:
        _upsert(c, alert_rules, {"user_id": _uid(user_id), "rules": rules}, ["user_id"])


# ---------------------------------------------------------------------------
# Personal: recommender state
# ---------------------------------------------------------------------------
def reco_weights_get(user_id: str | None = None) -> dict | None:
    with ro() as c:
        return c.execute(select(reco_weights.c.weights).where(reco_weights.c.user_id == _uid(user_id))).scalar()


def reco_weights_put(w: dict, user_id: str | None = None) -> None:
    with tx() as c:
        _upsert(c, reco_weights, {"user_id": _uid(user_id), "weights": w, "updated_at": time.time()}, ["user_id"])


def reco_feedback_add(row: dict, user_id: str | None = None) -> int:
    with tx() as c:
        c.execute(reco_feedback.insert().values(user_id=_uid(user_id), at=time.time(), **row))
        return c.execute(select(func.count()).select_from(reco_feedback).where(reco_feedback.c.user_id == _uid(user_id))).scalar()


def reco_feedback_count(user_id: str | None = None) -> int:
    with ro() as c:
        return c.execute(select(func.count()).select_from(reco_feedback).where(reco_feedback.c.user_id == _uid(user_id))).scalar()


def reco_reset(user_id: str | None = None) -> None:
    with tx() as c:
        c.execute(delete(reco_weights).where(reco_weights.c.user_id == _uid(user_id)))
        c.execute(delete(reco_feedback).where(reco_feedback.c.user_id == _uid(user_id)))


# ---------------------------------------------------------------------------
# Personal: agent threads and memory
# ---------------------------------------------------------------------------
def thread_create(t: dict, user_id: str | None = None) -> None:
    with tx() as c:
        c.execute(chat_threads.insert().values(id=t["id"], user_id=_uid(user_id), created_at=t["created"], title=t.get("title"),
                                               summary=t.get("summary", ""), summarised_upto=0, focus=t.get("focus") or {},
                                               pinned=t.get("pinned") or {}))


def thread_get(tid: str, user_id: str | None = None) -> dict | None:
    with ro() as c:
        r = c.execute(select(chat_threads).where(chat_threads.c.id == tid, chat_threads.c.user_id == _uid(user_id))).first()
        if not r:
            return None
        msgs = [{"role": m.role, "content": m.content, "meta": m.meta} if m.meta else {"role": m.role, "content": m.content}
                for m in c.execute(select(chat_messages).where(chat_messages.c.thread_id == tid).order_by(chat_messages.c.seq))]
    return {"id": r.id, "created": r.created_at, "title": r.title, "summary": r.summary or "", "summarised_upto": r.summarised_upto or 0,
            "focus": r.focus or {}, "pinned": r.pinned or {}, "messages": msgs}


def thread_save(t: dict, user_id: str | None = None) -> None:
    """Append-only for messages: only rows beyond what's stored are inserted."""
    uid = _uid(user_id)
    with tx() as c:
        owned = c.execute(select(chat_threads.c.id).where(chat_threads.c.id == t["id"], chat_threads.c.user_id == uid)).first()
        if not owned:
            raise PermissionError("thread does not belong to this user")
        c.execute(update(chat_threads).where(chat_threads.c.id == t["id"]).values(
            title=t.get("title"), summary=t.get("summary", ""), summarised_upto=t.get("summarised_upto", 0),
            focus=t.get("focus") or {}, pinned=t.get("pinned") or {}))
        n = c.execute(select(func.count()).select_from(chat_messages).where(chat_messages.c.thread_id == t["id"])).scalar()
        new = t["messages"][n:]
        if new:
            c.execute(chat_messages.insert(), [{"thread_id": t["id"], "seq": n + i, "role": m["role"], "content": m["content"],
                                                "meta": m.get("meta"), "at": time.time()} for i, m in enumerate(new)])


def threads_list(user_id: str | None = None, limit: int = 20) -> list[dict]:
    with ro() as c:
        return [{"id": r.id, "title": r.title, "created": r.created_at} for r in
                c.execute(select(chat_threads).where(chat_threads.c.user_id == _uid(user_id)).order_by(chat_threads.c.created_at.desc()).limit(limit))]


def facts_get(user_id: str | None = None) -> list[dict]:
    with ro() as c:
        return [{"id": r.id, "text": r.text, "source": r.source, "at": r.at} for r in
                c.execute(select(memory_facts).where(memory_facts.c.user_id == _uid(user_id)).order_by(memory_facts.c.id))]


def fact_add(text: str, source: str, user_id: str | None = None) -> None:
    with tx() as c:
        c.execute(memory_facts.insert().values(user_id=_uid(user_id), text=text, source=source, at=time.time()))


def fact_delete(fact_id: int, user_id: str | None = None) -> None:
    with tx() as c:
        c.execute(delete(memory_facts).where(memory_facts.c.id == fact_id, memory_facts.c.user_id == _uid(user_id)))


# ---------------------------------------------------------------------------
# Personal: mobile (push subscriptions, sent notifications, dismissed brief items)
# ---------------------------------------------------------------------------
def push_sub_add(sub: dict, ua: str | None = None, user_id: str | None = None) -> None:
    with tx() as c:
        _upsert(c, push_subs, {"endpoint": sub["endpoint"], "user_id": _uid(user_id), "p256dh": sub["keys"]["p256dh"],
                               "auth": sub["keys"]["auth"], "ua": ua, "created_at": time.time()}, ["endpoint"])


def push_sub_remove(endpoint: str, user_id: str | None = None) -> None:
    with tx() as c:
        c.execute(delete(push_subs).where(push_subs.c.endpoint == endpoint, push_subs.c.user_id == _uid(user_id)))


def push_subs_for(user_id: str | None = None) -> list[dict]:
    with ro() as c:
        return [{"endpoint": r.endpoint, "keys": {"p256dh": r.p256dh, "auth": r.auth}} for r in
                c.execute(select(push_subs).where(push_subs.c.user_id == _uid(user_id)))]


def notified_filter(item_ids: list[str], user_id: str | None = None) -> set[str]:
    """Which of these were already pushed to this user."""
    if not item_ids:
        return set()
    with ro() as c:
        return {r.item_id for r in c.execute(select(notified.c.item_id).where(notified.c.user_id == _uid(user_id),
                                                                              notified.c.item_id.in_(item_ids)))}


def notified_mark(item_ids: list[str], user_id: str | None = None) -> None:
    with tx() as c:
        _upsert(c, notified, [{"user_id": _uid(user_id), "item_id": i, "at": time.time()} for i in item_ids], ["user_id", "item_id"])


def dismissed_ids(user_id: str | None = None) -> set[str]:
    with ro() as c:
        return {r.item_id for r in c.execute(select(dismissed.c.item_id).where(dismissed.c.user_id == _uid(user_id)))}


def dismiss(item_id: str, undo: bool = False, user_id: str | None = None) -> None:
    with tx() as c:
        if undo:
            c.execute(delete(dismissed).where(dismissed.c.user_id == _uid(user_id), dismissed.c.item_id == item_id))
        else:
            _upsert(c, dismissed, {"user_id": _uid(user_id), "item_id": item_id, "at": time.time()}, ["user_id", "item_id"])


# ---------------------------------------------------------------------------
# Seeding and migration
# ---------------------------------------------------------------------------
def seed_user(user_id: str, portfolio_path=None, network_path=None, name: str = "Demo investor") -> None:
    """Load a user's holdings and network from the JSON seed files (idempotent)."""
    portfolio_path = portfolio_path or DATA / "portfolio.json"
    network_path = network_path or DATA / "network.json"
    pf = json.loads(open(portfolio_path, encoding="utf-8").read())
    nw = json.loads(open(network_path, encoding="utf-8").read())
    self_id = f"{user_id}:self"
    own = lambda pid: f"{user_id}:{pid}"  # noqa: E731  private profile ids are namespaced by owner
    with tx() as c:
        old = [r.id for r in c.execute(select(people.c.id).where(people.c.owner_user_id == user_id))]
        if old:
            c.execute(delete(roles).where(roles.c.person_id.in_(old)))
            c.execute(delete(education).where(education.c.person_id.in_(old)))
            c.execute(delete(contacts).where(contacts.c.user_id == user_id))
            c.execute(delete(people).where(people.c.id.in_(old)))
        _upsert(c, users, {"id": user_id, "name": name, "self_person_id": self_id, "created_at": time.time()}, ["id"])
        rows = [{"user_id": user_id, "ticker": h["ticker"], "kind": "portfolio", "name": h["name"], "theme": h.get("theme"),
                 "weight": h.get("weight"), "position": i} for i, h in enumerate(pf["portfolio"])]
        rows += [{"user_id": user_id, "ticker": h["ticker"], "kind": "research", "name": h["name"], "theme": h.get("theme"),
                  "weight": None, "position": i} for i, h in enumerate(pf.get("research", []))]
        c.execute(delete(holdings_t).where(holdings_t.c.user_id == user_id))
        c.execute(holdings_t.insert(), rows)
        _upsert(c, orgs, [{"id": oid, "name": o["name"], "ticker": o.get("ticker"), "kind": o.get("kind"), "themes": o.get("themes"),
                           "peers": o.get("peers"), "customers": o.get("customers"), "suppliers": o.get("suppliers"),
                           "aliases": o.get("aliases")} for oid, o in nw["orgs"].items()], ["id"])
        everyone = [(self_id, {**nw["you"], "id": self_id}, user_id)]
        everyone += [(own(p["id"]), p, user_id) for p in nw["people"]]        # the user's own contacts: private profiles
        everyone += [(p["id"], p, None) for p in nw.get("public_profiles", [])]  # public profiles: shared
        for pid, p, owner in everyone:
            _upsert(c, people, {"id": pid, "owner_user_id": owner, "name": p["name"], "headline": p.get("headline"),
                                "location": p.get("location"), "simulated": bool(p.get("simulated")), "source": p.get("source"),
                                "credentials": p.get("credentials"), "tags": p.get("tags")}, ["id"])
            c.execute(delete(roles).where(roles.c.person_id == pid))
            c.execute(delete(education).where(education.c.person_id == pid))
            if p.get("roles"):
                c.execute(roles.insert(), [{"person_id": pid, "org_id": r["org"], "title": r.get("title"), "function": r.get("function"),
                                            "start": r.get("from"), "end": r.get("to")} for r in p["roles"]])
            if p.get("education"):
                c.execute(education.insert(), [{"person_id": pid, "org_id": e["org"], "degree": e.get("degree"),
                                                "start": str(e.get("from")), "end": str(e.get("to"))} for e in p["education"]])
        c.execute(delete(contacts).where(contacts.c.user_id == user_id))
        ties = [{"user_id": user_id, "person_id": own(p["id"]), "tie_type": p["tie"].get("type"),
                 "context": p["tie"].get("context"), "since": str(p["tie"].get("since")),
                 "last_contact": p["tie"].get("last_contact"), "interactions_12m": p["tie"].get("interactions_12m", 0)}
                for p in nw["people"] if p.get("tie")]
        if ties:  # a new investor may start with no contacts at all
            c.execute(contacts.insert(), ties)
        c.execute(delete(explicit_links).where(explicit_links.c.user_id == user_id))
        if nw.get("explicit_links"):
            c.execute(explicit_links.insert(), [{"user_id": user_id, **l} for l in nw["explicit_links"]])


def migrate_legacy() -> dict[str, int]:
    """Move domain data the first prototype kept as KV blobs into proper tables (idempotent)."""
    moved = {}
    with tx() as c:
        rows = list(c.execute(select(kv).where(kv.c.ns.in_(["eod", "company", "pdf_text", "chunks", "thread", "memory", "reco", "settings"]))))
    by_ns: dict[str, list] = {}
    for r in rows:
        by_ns.setdefault(r.ns, []).append(r)
    for r in by_ns.get("eod", []):
        v = json.loads(r.value)
        prices_put(r.key, v["dates"], v["close"], v["volume"])
    for r in by_ns.get("company", []):
        company_put(json.loads(r.value))
    for r in by_ns.get("pdf_text", []):
        doc_pages_put(r.key, url="", pages=json.loads(r.value))
    for r in by_ns.get("chunks", []):
        chunks_put(r.key, json.loads(r.value))
    rr = [r for r in by_ns.get("settings", []) if r.key == "rules"]
    if rr:
        rules_put(json.loads(rr[0].value), "demo")
    for ns, items in by_ns.items():
        moved[ns] = len(items)
    kv_clear("eod", "company", "pdf_text", "chunks", "thread", "memory", "reco", "settings")
    return moved


def init(seed: bool = True) -> None:
    """Create tables, apply Postgres policies, seed the demo user on first run (all idempotent)."""
    from .context import reset_user, set_user

    eng = engine()
    if eng.dialect.name == "postgresql":
        for ext in ("vector", "pg_trgm"):  # present via docker/initdb locally; managed hosts allow the owner to add them
            try:
                with eng.begin() as c:
                    c.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
            except Exception as e:
                log.info("extension %s not created here (%s); expected when it already exists", ext, type(e).__name__)
        with eng.begin() as c:
            apply_rls(c)
    token = set_user("demo")
    try:
        with ro() as c:
            has_demo = c.execute(select(users.c.id).where(users.c.id == "demo")).first() is not None
        if seed and not has_demo:
            seed_user("demo")
            moved = migrate_legacy() if eng.dialect.name == "sqlite" else {}
            log.info("seeded demo user; migrated legacy data: %s", moved)
    finally:
        reset_user(token)
