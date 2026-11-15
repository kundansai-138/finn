"""HTTP API + static frontend. Run: uvicorn finn.api:app --reload"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import queue
import re
import threading
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import ask as ask_mod
from . import clock, db, portfolio, pull, push, scheduler, store
from . import enrich as web_enrich
from .context import DEFAULT_USER, current_user, reset_user, set_user
from .config import WEB
from .http import FetchError
from .intel import llm
from .network import discover, graph, nudges
from .network import recommend as reco
from .sources import prices

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = FastAPI(title="FINN", version="0.1")
app.add_middleware(GZipMiddleware, minimum_size=1000)
ADMIN_TOKEN = os.getenv("FINN_ADMIN_TOKEN", "")
_USER_RX = re.compile(r"^[a-z0-9_-]{1,40}$")

# ---- request context + latency metrics ---------------------------------------
_latency: dict[str, deque] = defaultdict(lambda: deque(maxlen=2000))
_errors: dict[str, int] = defaultdict(int)


@app.middleware("http")
async def user_and_timing(request: Request, call_next):
    """Bind the caller's identity for every DB read/write, and time the request.

    Prototype identity is the X-FINN-User header (default: the demo user). Production replaces this
    with real authentication; the database enforces the same boundary with row-level security."""
    uid = request.headers.get("x-finn-user", DEFAULT_USER).lower()
    if not _USER_RX.match(uid):
        uid = DEFAULT_USER
    token = set_user(uid)
    t0 = time.perf_counter()
    try:
        resp = await call_next(request)
    finally:
        reset_user(token)
    ms = (time.perf_counter() - t0) * 1000
    route = getattr(request.scope.get("route"), "path", request.url.path)
    if route.startswith("/api"):
        _latency[f"{request.method} {route}"].append(ms)
        if resp.status_code >= 500:
            _errors[f"{request.method} {route}"] += 1
    resp.headers["Server-Timing"] = f"app;dur={ms:.1f}"
    return resp


@app.on_event("startup")
def _startup() -> None:
    store.init()  # tables, row-level security policies, demo user (idempotent)
    # Warm the demo user's recommender index off the request path (first build is ~2s).
    threading.Thread(target=lambda: (set_user(DEFAULT_USER), reco.index()), daemon=True).start()
    if os.getenv("FINN_SCHEDULER", "on") != "off":
        scheduler.start()


def _require_admin(token: str | None) -> None:
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(403, "admin token required")


@app.get("/api/brief")
def brief(window: str = "24h", level: str = Query("material", pattern="^(critical|material|all)$")) -> dict[str, Any]:
    return push.brief(window, level)


@app.post("/api/refresh")
def refresh(x_finn_admin: str | None = Header(default=None)) -> dict[str, Any]:
    _require_admin(x_finn_admin)
    out = scheduler.run_refresh()
    return out if out is not None else {"status": "a refresh is already running"}


@app.get("/api/health")
def health() -> dict[str, Any]:
    from sqlalchemy import text

    t0 = time.perf_counter()
    with store.ro() as c:
        c.execute(text("SELECT 1"))
    return {"ok": True, "db": store.engine().dialect.name, "db_ms": round((time.perf_counter() - t0) * 1000, 1),
            "last_refresh_age_s": round(db.kv_age("meta", "last_refresh") or -1), "llm": llm.enabled(),
            "scheduler": scheduler.status()}


@app.get("/api/metrics")
def metrics() -> dict[str, Any]:
    """Live latency percentiles per route since process start (last 2,000 requests each)."""
    def pct(xs: list[float], p: float) -> float:
        return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1)

    out = {}
    for k, d in sorted(_latency.items()):
        xs = sorted(d)
        out[k] = {"n": len(xs), "p50_ms": pct(xs, .5), "p95_ms": pct(xs, .95), "p99_ms": pct(xs, .99),
                  "max_ms": round(xs[-1], 1), "errors": _errors.get(k, 0)}
    return {"routes": out, "llm": llm.spend_summary()}


@app.get("/api/rules")
def get_rules() -> dict[str, Any]:
    return portfolio.rules()


@app.put("/api/rules")
def put_rules(update: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return portfolio.save_rules(update)


@app.get("/api/portfolio")
def holdings() -> dict[str, Any]:
    eod = prices.fetch_eod(portfolio.tickers())
    rows = []
    for h in portfolio.holdings():
        m = (eod.get(h["ticker"]) or {}).get("metrics") or {}
        rows.append({**h, "metrics": m})
    return {"holdings": rows, "research": portfolio.research()}


@app.get("/api/company/{ticker}")
def company(ticker: str, refresh: bool = False) -> dict[str, Any]:
    try:
        return pull.dossier(ticker, force=refresh)
    except FetchError as e:
        raise HTTPException(404, f"Couldn't find {ticker.upper()} on the exchange data sources ({e}).")


@app.get("/api/ask")
def ask(q: str) -> dict[str, Any]:
    return ask_mod.ask(q)


@app.get("/api/people/{pid}")
def person(pid: str) -> dict[str, Any]:
    net = graph.get()
    if pid not in net.people or pid == "you":
        raise HTTPException(404, "Unknown person")
    full = net.person_full(pid)
    if full.get("paths"):
        reco.with_graphs(net, full["paths"], pid)
    return full


@app.get("/api/network")
def network() -> dict[str, Any]:
    net = graph.get()
    people = []
    for cid in net.contacts:
        card = net.person_card(cid)
        card["warmth"] = net.warmth(cid)
        card["tags"] = net.contacts[cid].get("tags", [])
        people.append(card)
    people.sort(key=lambda p: -p["warmth"]["score"])
    themes = []
    for t in net.themes():
        ex = reco.recommend(theme=t, k=3, min_score=0.3)
        if ex:
            themes.append({"theme": t, "people": ex})
    themes.sort(key=lambda x: -sum(e["score"] for e in x["people"]))
    return {"people": people, "themes": themes}


@app.get("/api/network/recommend")
def recommend(org: str | None = None, ticker: str | None = None, theme: str | None = None,
              event: str | None = None, text: str = "", k: int = 5) -> dict[str, Any]:
    net = graph.get()
    org = org or (net.org_for_ticker(ticker) if ticker else None)
    return {"results": reco.recommend(org, theme, event, text, k=k)}


@app.post("/api/network/feedback")
def network_feedback(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return reco.feedback(body["person_id"], body.get("query") or {}, int(body["label"]))
    except KeyError:
        raise HTTPException(404, "Unknown person")


@app.get("/api/network/model")
def network_model(mode: str = Query("pull", pattern="^(pull|push)$")) -> dict[str, Any]:
    return reco.model_card(mode)


# ---- Flow 1: no one in the network? find people on the public web ----------
@app.get("/api/network/discover")
def network_discover(org: str | None = None, ticker: str | None = None, event: str | None = None, text: str = "",
                     theme: str | None = None, company: str | None = None, force: bool = False) -> dict[str, Any]:
    net = graph.get()
    org = org or (net.org_for_ticker(ticker) if ticker else None)
    return discover.discover(org=org, event=event, text=text[:600], theme=theme, company=company, force=force)


# ---- Flow 3: grow the network, keep it warm ---------------------------------
def _network_changed() -> None:
    uid = current_user()
    graph.invalidate(uid)
    store.kv_delete_prefix("dossier", f"{uid}:")   # dossiers carry people; rebuild on next open


@app.post("/api/network/contacts")
def add_contact(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Add someone to your network: by hand, or a public-web lead from discovery ('to-meet')."""
    name = (body.get("name") or "").strip()
    if len(name) < 3:
        raise HTTPException(422, "A name is required")
    net = graph.get()
    org_name = (body.get("org_name") or "").strip()
    oid = body.get("org_id") or (discover.resolve_org(net, org_name) if org_name else None)
    if not oid and org_name:
        oid = store.org_ensure(org_name)
    hit = net.resolve(name, org_hint=oid)
    if hit and hit["id"] in net.contacts:   # dedupe: same person already in your network
        return {"id": hit["id"], "existed": True, "person": net.person_card(hit["id"])}
    title = (body.get("title") or "").strip()
    lead = bool(body.get("lead"))
    today = clock.now().date().isoformat()
    person = {"name": name, "headline": f"{title}, {net.org_name(oid) if oid else org_name}".strip(", "),
              "source": body.get("source_url"), "tags": body.get("tags") or [],
              "roles": [{"org": oid, "title": title or "—", "function": discover.infer_function(title),
                         "from": body.get("since_role"), "to": None}] if oid else []}
    tie = {"type": "to-meet" if lead else (body.get("tie_type") or "met"),
           "context": (body.get("context") or ("Found on the public web" if lead else ""))[:300],
           "since": today, "last_contact": None if lead else (body.get("last_contact") or today),
           "interactions_12m": 0 if lead else 1}
    pid, existed = store.contact_add(person, tie)
    _network_changed()
    return {"id": pid, "existed": existed, "person": graph.get().person_card(pid)}


@app.post("/api/network/contacts/{pid}/touch")
def touch_contact(pid: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """'I spoke to them': resets the keep-in-touch clock."""
    on = body.get("date") or clock.now().date().isoformat()
    r = store.contact_touch(pid, on, body.get("note"))
    if r is None:
        raise HTTPException(404, "Not in your network")
    _network_changed()
    return {**r, "warmth": graph.get().warmth(pid)}


@app.get("/api/network/nudges")
def network_nudges(limit: int = 10, all: bool = False) -> dict[str, Any]:
    items = nudges.compute(limit=limit, include_all=all)
    return {"items": items, "method": nudges.__doc__.strip().split("\n\n")[1] if nudges.__doc__ else ""}


@app.post("/api/network/nudges/{pid}/draft")
def nudge_draft(pid: str) -> dict[str, Any]:
    try:
        return nudges.draft(pid)
    except KeyError:
        raise HTTPException(404, "Not in your network")


# ---- Flow 2: web context for a pushed filing --------------------------------
@app.get("/api/brief/{item_id}/web")
def brief_item_web(item_id: str, force: bool = False) -> dict[str, Any]:
    a = store.ann_get(item_id)
    if a is None:
        raise HTTPException(404, "Unknown filing")
    item = {**a, "company": next((h["name"] for h in portfolio.holdings() + portfolio.research() if h["ticker"] == a["ticker"]), a["ticker"])}
    web = web_enrich.enrich(item, force=force)
    if web is None:
        return {"status": "unavailable" if not llm.enabled() else "not eligible", "facts": [], "people": []}
    return {"status": "ok", **web}


# ---- Ask FINN agent ---------------------------------------------------------
@app.post("/api/chat")
def chat(body: dict[str, Any] = Body(...)) -> StreamingResponse:
    """Runs the agent graph and streams its events (node progress, plan, evidence, answer, verification)."""
    from .agent import core

    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "message is required")
    q: queue.Queue = queue.Queue()

    def worker() -> None:
        try:
            core.run(msg, body.get("thread_id"), body.get("context"), q.put)
        except Exception as e:  # surface, don't hang the stream
            logging.exception("agent failed")
            q.put({"type": "error", "error": str(e)[:300]})
        finally:
            q.put(None)

    # The worker keeps the caller's identity (contextvars don't cross threads on their own).
    threading.Thread(target=contextvars.copy_context().run, args=(worker,), daemon=True).start()

    def stream():
        while True:
            ev = q.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/chat/{tid}")
def chat_thread(tid: str) -> dict[str, Any]:
    from .agent import memory

    t = memory.get(tid)
    if not t:
        raise HTTPException(404, "Unknown thread")
    return t


@app.get("/api/agent")
def agent_info() -> dict[str, Any]:
    from .agent import core, memory, tools

    return {"graph": core.GRAPH.describe(), "tools": [{"name": t.name, "description": t.description} for t in tools.available()],
            "facts": memory.facts(), "model": {"enabled": llm.enabled(), "agent": llm.MODEL_DEEP}}


@app.delete("/api/agent/memory/{fact_id}")
def forget(fact_id: int) -> dict[str, Any]:
    from .agent import memory

    memory.delete_fact(fact_id)
    return {"facts": memory.facts()}


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    return {"llm": llm.spend_summary(), "last_refresh": db.kv_get("meta", "last_refresh")}


# ---- mobile: push notifications, swipe-to-handle ----------------------------------
@app.get("/api/push/key")
def push_key() -> dict[str, Any]:
    from . import notify

    return {"publicKey": notify.keys()[1]}


@app.post("/api/push/subscribe")
def push_subscribe(request: Request, sub: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not sub.get("endpoint") or not (sub.get("keys") or {}).get("p256dh"):
        raise HTTPException(400, "not a push subscription")
    store.push_sub_add(sub, ua=request.headers.get("user-agent", "")[:200])
    return {"ok": True, "devices": len(store.push_subs_for())}


@app.delete("/api/push/subscribe")
def push_unsubscribe(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    store.push_sub_remove(body.get("endpoint", ""))
    return {"ok": True}


@app.post("/api/push/test")
def push_test() -> dict[str, Any]:
    from . import notify

    r = notify.send("FINN is connected", "Alerts on your portfolio will arrive here: results, management changes, unusual moves.",
                    url="/", tag="finn-test")
    return {**r, "devices": len(store.push_subs_for())}


@app.post("/api/brief/dismiss")
def brief_dismiss(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    store.dismiss(str(body["id"]), undo=bool(body.get("undo")))
    return {"ok": True}


# ---- static frontend --------------------------------------------------------
app.mount("/static", StaticFiles(directory=WEB), name="static")


def _asset_version() -> str:
    """Hash of the frontend files: any change to them gives the service worker a new cache name,
    so phones pick up a new release on their next visit instead of serving a stale shell."""
    import hashlib

    h = hashlib.sha1()
    for p in sorted(WEB.rglob("*")):
        if p.is_file() and p.suffix in (".js", ".css", ".html", ".webmanifest", ".png"):
            h.update(p.name.encode())
            h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()[:10]


@app.get("/sw.js")
def service_worker():
    # Served from the root so its scope covers the whole app; versioned by content.
    from fastapi.responses import Response

    body = (WEB / "sw.js").read_text(encoding="utf-8").replace('"finn-v3"', f'"finn-{_asset_version()}"')
    return Response(body, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/")
@app.get("/c/{_path:path}")
@app.get("/p/{_path:path}")
def index(_path: str = "") -> FileResponse:
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})
