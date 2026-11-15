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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import ask as ask_mod
from . import auth, clock, db, portfolio, pull, push, scheduler, scoring, store
from . import enrich as web_enrich
from . import radar as radar_mod
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

    Identity (finn/auth.py): the session cookie of a signed-in investor; else the X-FINN-User header
    (development and tests, disabled by FINN_AUTH_STRICT=on); else the guest demo user. The database
    enforces the same per-user boundary with row-level security."""
    uid, how = auth.resolve(request.cookies.get(auth.COOKIE), request.headers.get("x-finn-user"))
    request.state.auth = how
    token = set_user(uid)
    h = request.headers   # a visitor's own model config, this request only (never stored)
    ktoken = llm.use_key(h.get("x-finn-model-key"), h.get("x-finn-model-provider"), h.get("x-finn-model-fast"),
                         h.get("x-finn-model-deep"), h.get("x-finn-model-base"))
    t0 = time.perf_counter()
    try:
        resp = await call_next(request)
    finally:
        llm.reset_key(ktoken)
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
            "last_refresh_age_s": round(db.kv_age("meta", "last_refresh") or -1), "llm": llm.enabled(), "llm_status": llm.status(),
            "scheduler": scheduler.status()}


@app.post("/api/llm/check")
def llm_check(body: dict = Body(...)) -> dict[str, Any]:
    """Check a key the visitor pasted (lists the provider's models; costs nothing). The key isn't stored."""
    return llm.check_key(str(body.get("key", ""))[:400], body.get("provider"), body.get("base"))


@app.get("/api/llm")
def llm_info() -> dict[str, Any]:
    """Whose key this request would use: the visitor's, the server's, or none (fallbacks)."""
    from .intel import providers

    return {"source": llm.source(), "status": llm.status(), "server_key": llm.LLM_ENABLED, "web_search": llm.web_search_available(),
            "providers": providers.public()}


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


@app.get("/api/radar")
def radar_view(window: str = Query("24h", pattern="^(24h|3d|7d|30d)$")) -> dict[str, Any]:
    """Home screen: every holding as a tile, and the few that need a look."""
    return radar_mod.radar(window)


@app.get("/api/people/todo")
def people_todo(window: str = Query("7d", pattern="^(24h|3d|7d|30d)$")) -> dict[str, Any]:
    """Who to contact now, verb first: news-driven calls, then keep-in-touch nudges."""
    return radar_mod.todo(window)


@app.post("/api/people/todo/done")
def people_todo_done(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    out = radar_mod.todo_done(str(body.get("id") or ""), body.get("person_id"))
    if out["touched"]:
        _network_changed()
    return out


@app.post("/api/draft")
def draft_message(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """A first message to someone in the network; FINN drafts, the investor sends it themselves."""
    from .network import drafter

    try:
        return drafter.draft(str(body.get("person_id") or ""), intent=body.get("intent") or "view",
                             channel=body.get("channel") or "whatsapp", ticker=body.get("ticker"),
                             event_id=body.get("event_id"), target_id=body.get("target_id"),
                             variant=int(body.get("variant") or 0))
    except KeyError:
        raise HTTPException(404, "Unknown person")


@app.get("/api/people/{pid}")
def person(pid: str) -> dict[str, Any]:
    net = graph.get()
    if pid not in net.people or pid == "you":
        raise HTTPException(404, "Unknown person")
    full = net.person_full(pid)
    if full.get("paths"):
        reco.with_graphs(net, full["paths"], pid)
    if pid in net.contacts:   # why get in touch now, if anything says so
        try:
            n = next((x for x in nudges.compute(include_all=True) if x["person"]["id"] == pid), None)
            full["nudge"] = {k: n[k] for k in ("reason", "due", "hook", "days_since", "cadence_days")} if n else None
        except Exception as e:
            logging.warning("nudge for %s failed: %s", pid, e)
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
    org_label = net.orgs.get(oid, {}).get("name") or org_name   # a company new to the map isn't in this (cached) graph yet
    person = {"name": name, "headline": f"{title}, {org_label}".strip(", "),
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
            "facts": memory.facts(), "model": {"enabled": llm.enabled(), "agent": llm.models()["deep"]}}


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
    try:
        store.push_sub_add(sub, ua=request.headers.get("user-agent", "")[:200])
    except Exception:  # the device is registered to another account (its row is invisible here)
        raise HTTPException(409, "This device gets alerts for another account. Turn alerts off there first.")
    n = store.notify_users_sync()   # the scheduler pushes to every user in this registry
    return {"ok": True, "devices": n, "user": current_user()}


@app.delete("/api/push/subscribe")
def push_unsubscribe(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    store.push_sub_remove(body.get("endpoint", ""))
    store.notify_users_sync()
    return {"ok": True}


# ---- Accounts -------------------------------------------------------------------
def _set_cookie(request: Request, resp: JSONResponse, token: str | None) -> JSONResponse:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    if token:
        resp.set_cookie(auth.COOKIE, token, max_age=auth.TTL, httponly=True, samesite="lax", secure=secure, path="/")
    else:
        resp.delete_cookie(auth.COOKIE, path="/")
    return resp


def _me() -> dict[str, Any]:
    uid = current_user()
    with store.ro() as c:
        row = c.execute(store.select(store.users.c.name).where(store.users.c.id == uid)).first()
    return {"user": uid, "name": row.name if row else uid, "guest": uid == DEFAULT_USER,
            "devices": len(store.push_subs_for()), "unread": store.inbox_unread()}


# ---- Notifications: the in-app inbox (every alert lands here; phones with alerts on also get a push)
@app.get("/api/notifications")
def notifications(limit: int = 50) -> dict[str, Any]:
    return {"items": store.inbox_list(min(limit, 200)), "unread": store.inbox_unread(), "devices": len(store.push_subs_for())}


@app.post("/api/notifications/read")
def notifications_read(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    store.inbox_mark_read([int(i) for i in (body.get("ids") or [])] or None)
    return {"unread": store.inbox_unread()}


@app.post("/api/notifications/check")
def notifications_check() -> dict[str, Any]:
    """Run this user's alert and keep-in-touch checks now, instead of waiting for the scheduler."""
    from . import notify
    from .network import nudges

    a, n = notify.dispatch(), nudges.dispatch()
    return {"new": a.get("items", 0) + n.get("items", 0), "unread": store.inbox_unread()}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    return {**_me(), "via": getattr(request.state, "auth", "guest")}


@app.post("/api/auth/register")
def auth_register(request: Request, body: dict[str, Any] = Body(...)) -> JSONResponse:
    try:
        # A new account starts empty: no demo holdings, no demo network, nothing to see until you add it.
        uid = auth.register(body.get("username", ""), body.get("password", ""), body.get("name"), start_from_demo=False)
    except auth.AuthError as e:
        raise HTTPException(e.status, str(e))
    tok = auth.new_session(uid, request.headers.get("user-agent"))
    reset = set_user(uid)
    try:
        return _set_cookie(request, JSONResponse(_me()), tok)
    finally:
        reset_user(reset)


@app.post("/api/auth/login")
def auth_login(request: Request, body: dict[str, Any] = Body(...)) -> JSONResponse:
    try:
        uid = auth.login(body.get("username", ""), body.get("password", ""))
    except auth.AuthError as e:
        raise HTTPException(e.status, str(e))
    tok = auth.new_session(uid, request.headers.get("user-agent"))
    reset = set_user(uid)
    try:
        return _set_cookie(request, JSONResponse(_me()), tok)
    finally:
        reset_user(reset)


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    auth.end_session(request.cookies.get(auth.COOKIE))
    return _set_cookie(request, JSONResponse({"ok": True}), None)


# ---- The radar: add and remove companies ----------------------------------------
@app.get("/api/universe/search")
def universe_search(q: str = "", limit: int = 8) -> dict[str, Any]:
    """NSE-listed companies by ticker or name (NSE's public EQUITY_L list, cached weekly)."""
    from .sources import universe

    sym_name, _ = universe.load()
    ql = q.strip().lower()
    if len(ql) < 2:
        return {"results": []}
    held = {h["ticker"]: "portfolio" for h in portfolio.holdings()} | {h["ticker"]: "research" for h in portfolio.research()}
    scored = []
    for sym, name in sym_name.items():
        s, n = sym.lower(), name.lower()
        rank = 0 if s == ql else 1 if s.startswith(ql) else 2 if n.startswith(ql) else 3 if f" {ql}" in f" {n}" else None
        if rank is not None:
            scored.append((rank, len(name), sym, name))
    scored.sort()
    return {"results": [{"ticker": sym, "name": name, "on": held.get(sym)} for _, _, sym, name in scored[:limit]]}


@app.post("/api/holdings")
def holdings_add(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Put a company on the radar (kind=portfolio, with a weight) or the research list (kind=research)."""
    from .sources import universe

    t = (body.get("ticker") or "").strip().upper()
    kind = body.get("kind", "portfolio")
    if kind not in ("portfolio", "research"):
        raise HTTPException(422, "kind is portfolio or research")
    name = universe.name(t)
    if not name:
        raise HTTPException(422, f"{t or 'That'} isn't an NSE-listed equity.")
    w = body.get("weight")
    if kind == "portfolio":
        try:
            w = round(float(w), 2) if w not in (None, "") else 1.0
        except (TypeError, ValueError):
            raise HTTPException(422, "weight is a percentage")
        if not 0 < w <= 100:
            raise HTTPException(422, "weight is a percentage between 0 and 100")
    new = store.holding_add(t, name, kind, w, (body.get("theme") or "").strip()[:60] or None)
    if kind == "portfolio":
        portfolio.track(t)   # the shared refresh now fetches its filings too
    store.kv_delete_prefix("dossier", f"{current_user()}:{t}")
    # Fetch its filings and prices off the request path, so the next radar has the tile filled in.
    threading.Thread(target=contextvars.copy_context().run, args=(_warm_company, t), daemon=True).start()
    total = sum(h.get("weight") or 0 for h in portfolio.holdings())
    return {"ok": True, "new": new, "ticker": t, "name": name, "kind": kind, "portfolio_weight_total": round(total, 2)}


def _warm_company(t: str) -> None:
    from . import companies

    try:
        prices.fetch_eod([t])
        companies.get(t)
    except Exception as e:  # a slow source must not surface as an error: the radar shows the tile without data
        logging.info("warming %s failed: %s", t, e)


@app.delete("/api/holdings/{ticker}")
def holdings_remove(ticker: str) -> dict[str, Any]:
    if not store.holding_remove(ticker.upper()):
        raise HTTPException(404, "Not on your radar or research list")
    return {"ok": True}


# ---- Scoring: FINN's defaults, editable per investor ----------------------------
@app.get("/api/scoring")
def scoring_get() -> dict[str, Any]:
    return {"groups": scoring.schema()}


@app.put("/api/scoring/{group}")
def scoring_put(group: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return scoring.save(group, body.get("values") or {})
    except KeyError:
        raise HTTPException(404, "Unknown scoring section")
    except (TypeError, ValueError):
        raise HTTPException(422, "Values must be numbers")


@app.delete("/api/scoring/{group}")
def scoring_reset(group: str) -> dict[str, Any]:
    try:
        return scoring.reset(group)
    except KeyError:
        raise HTTPException(404, "Unknown scoring section")


@app.post("/api/push/test")
def push_test() -> dict[str, Any]:
    from . import notify

    r = notify.send("FINN is connected", "Alerts on your portfolio will arrive here: results, management changes, unusual moves.",
                    url="/", tag=f"finn-test-{int(time.time())}", kind="test")
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
