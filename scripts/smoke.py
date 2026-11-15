"""End-to-end smoke test: walk every screen's API the way the app does, against a running FINN.

    python -m scripts.smoke                                  # http://localhost:8000
    python -m scripts.smoke https://finn-kjsg.onrender.com   # the deployed app

Read-only except for chat (creates a conversation thread for the guest) and the notifications check.
Every company on the radar and research list, plus a few listed names that aren't, gets its page
loaded; every person in the network gets their sheet loaded; Ask FINN answers a handful of questions.
Exits non-zero if anything fails, and prints what did.
"""
from __future__ import annotations

import json
import sys
import time

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
c = httpx.Client(base_url=BASE, timeout=120.0, headers={"User-Agent": "FINN-smoke/1.0"})
fails: list[str] = []
slow: list[str] = []
n = 0


def hit(method: str, path: str, expect=(200,), check=None, **kw):
    global n
    n += 1
    t0 = time.perf_counter()
    try:
        r = c.request(method, path, **kw)
    except httpx.HTTPError as e:
        fails.append(f"{method} {path}: {type(e).__name__} {e}")
        return None
    ms = (time.perf_counter() - t0) * 1000
    if ms > 8000:
        slow.append(f"{method} {path}: {ms / 1000:.1f}s")
    if r.status_code not in expect:
        fails.append(f"{method} {path}: HTTP {r.status_code} {r.text[:160]}")
        return None
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    if check and r.status_code == 200:
        problem = check(body)
        if problem:
            fails.append(f"{method} {path}: {problem}")
    return body


def need(*keys):
    return lambda b: None if isinstance(b, dict) and all(k in b for k in keys) else f"missing {[k for k in keys if k not in (b or {})]}"


print(f"Smoke test against {BASE}")
# shell and static assets
for p in ("/", "/sw.js", "/manifest.webmanifest", "/static/app.js", "/static/styles.css", "/static/mobile.js"):
    hit("GET", p)
health = hit("GET", "/api/health", check=need("ok", "db", "llm_status"))
hit("GET", "/api/llm", check=need("source", "providers"))
hit("GET", "/api/auth/me", check=need("user"))
hit("GET", "/api/meta")

# radar (home), every window
for w in ("3d", "7d", "30d"):
    hit("GET", f"/api/radar?window={w}", check=lambda b: None if b.get("empty") or ("tiles" in b and "needs" in b) else "no tiles/needs")
radar = hit("GET", "/api/radar?window=24h", check=need("tiles", "needs")) or {}
hit("GET", "/api/brief?window=24h&level=material")
hit("GET", "/api/rules")

# holding / research pages: everything on the radar and research list, plus listed names that aren't held
port = hit("GET", "/api/portfolio") or {}
tickers = {t["ticker"] for t in radar.get("tiles", [])}
for k in ("holdings", "research", "portfolio", "watch"):
    for h in (port.get(k) or []) if isinstance(port, dict) else []:
        if isinstance(h, dict) and h.get("ticker"):
            tickers.add(h["ticker"])
tickers |= {"HYUNDAI", "MARUTI", "TMPV", "INFY"}
for t in sorted(tickers):
    hit("GET", f"/api/company/{t}", expect=(200, 404), check=need("ticker", "name"))
hit("GET", "/api/company/NOTATICKER123", expect=(404,))

# web context on the filings that need a look
for nd in radar.get("needs", [])[:3]:
    for i in nd.get("ids", [])[:1]:
        hit("GET", f"/api/brief/{i}/web", check=need("status"))

# research search
for q in ("hyundai", "kia", "tata", "hdfc", "semi"):
    hit("GET", f"/api/universe/search?q={q}", check=need("results"))
for q in ("HDFCBANK", "Who knows semiconductors?"):
    hit("GET", "/api/ask", params={"q": q})

# people
net = hit("GET", "/api/network", check=need("people")) or {}
for p in net.get("people", []):
    hit("GET", f"/api/people/{p['id']}")
hit("GET", "/api/people/todo?window=7d")
hit("GET", "/api/network/recommend", params={"ticker": next(iter(sorted(tickers)))}, expect=(200, 422))
hit("GET", "/api/network/model")
hit("GET", "/api/network/nudges")
hit("GET", "/api/network/discover", params={"company": "Hyundai Motor India", "text": "new plant"}, expect=(200, 422))

# notifications, scoring, agent
hit("GET", "/api/notifications?limit=5", check=need("unread"))
hit("POST", "/api/notifications/check", json={})
hit("GET", "/api/scoring")
hit("GET", "/api/agent")
hit("GET", "/api/push/key", expect=(200, 404))
hit("POST", "/api/llm/check", json={"key": "sk-not-real", "provider": "openai"}, check=lambda b: None if b.get("ok") is False else "fake key accepted")

# Ask FINN: stream each answer to the end
for q in ("What changed across my portfolio this week?", "How is HDFCBANK's margin holding up?", "Why did TATAPOWER move today?",
          "Who knows semiconductors?", "KIA INDIA", "Compare TCS and INFY margins"):
    n += 1
    t0 = time.perf_counter()
    try:
        events, tid = [], None
        with c.stream("POST", "/api/chat", json={"message": q}) as r:
            if r.status_code != 200:
                fails.append(f"chat {q!r}: HTTP {r.status_code}")
                continue
            for line in r.iter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    events.append(ev)
                    tid = ev.get("thread_id") or tid
        kinds = [e.get("type") for e in events]
        ans = next((e for e in events if e.get("type") == "answer"), None)
        err = next((e for e in events if e.get("type") == "error"), None)
        if err:
            fails.append(f"chat {q!r}: agent error {err.get('error')}")
        elif not ans:
            fails.append(f"chat {q!r}: no answer (events: {kinds[-6:]})")
        else:
            text = ans.get("text") or ans.get("answer") or (ans.get("data") or {}).get("text") or ""
            print(f"  chat {q!r}: {len(events)} events, {(time.perf_counter() - t0):.1f}s, answer {len(str(text))} chars")
        if tid:
            hit("GET", f"/api/chat/{tid}")
    except httpx.HTTPError as e:
        fails.append(f"chat {q!r}: {type(e).__name__} {e}")

print(f"\n{n} checks, {len(fails)} failed" + (f", llm: {health.get('llm_status')}" if isinstance(health, dict) else ""))
for s in slow:
    print("  slow:", s)
for f in fails:
    print("  FAIL:", f)
sys.exit(1 if fails else 0)
