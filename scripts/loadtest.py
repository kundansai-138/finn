"""Load test: N concurrent simulated investors hitting the read paths for D seconds.

    python -m scripts.loadtest --base http://localhost:8000 --users 20 --duration 30

Reports client-side throughput and latency percentiles per endpoint, then the server's own view
(/api/metrics, measured inside the app). The traffic mix mirrors a morning check-in:
mostly the brief, then a dossier or two, the portfolio, and a "who should I call".
The agent (/api/chat) is excluded by default: with a model key it's bound by the model's
latency and costs money; add --chat to include it.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
from collections import defaultdict

import httpx

MIX = [
    (40, "brief", lambda: f"/api/brief?window={random.choice(['24h', '7d'])}&level=material"),
    (15, "portfolio", lambda: "/api/portfolio"),
    (20, "dossier", lambda: f"/api/company/{random.choice(['DIXON', 'PIIND', 'LAURUSLABS', 'DEEPAKNTR', 'HAL'])}"),
    (15, "recommend", lambda: f"/api/network/recommend?ticker={random.choice(['PIIND', 'DIXON', 'CHOLAFIN'])}&event=M%26A"),
    (10, "health", lambda: "/api/health"),
]


def pick(chat: bool):
    mix = MIX + ([(5, "chat", None)] if chat else [])
    r = random.uniform(0, sum(w for w, *_ in mix))
    for w, name, url in mix:
        r -= w
        if r <= 0:
            return name, url
    return mix[0][1], mix[0][2]


async def user(client: httpx.AsyncClient, stop: float, stats: dict, errors: dict, chat: bool) -> None:
    while time.perf_counter() < stop:
        name, url = pick(chat)
        t = time.perf_counter()
        try:
            if name == "chat":
                async with client.stream("POST", "/api/chat", json={"message": "How is Dixon's margin holding up?"}) as r:
                    async for _ in r.aiter_lines():
                        pass
                    ok = r.status_code == 200
            else:
                r = await client.get(url())
                ok = r.status_code == 200
        except Exception:
            ok = False
        stats[name].append((time.perf_counter() - t) * 1000)
        if not ok:
            errors[name] += 1
        await asyncio.sleep(random.uniform(0.05, 0.3))  # think time between taps


async def main(base: str, users: int, duration: int, chat: bool) -> None:
    stats: dict = defaultdict(list)
    errors: dict = defaultdict(int)
    limits = httpx.Limits(max_connections=users * 2)
    async with httpx.AsyncClient(base_url=base, timeout=60, limits=limits) as client:
        await client.get("/api/brief")  # warm
        stop = time.perf_counter() + duration
        t0 = time.perf_counter()
        await asyncio.gather(*(user(client, stop, stats, errors, chat) for _ in range(users)))
        wall = time.perf_counter() - t0
        server = (await client.get("/api/metrics")).json()["routes"]

    total = sum(len(v) for v in stats.values())
    errs = sum(errors.values())
    print(f"\n{users} users × {duration}s → {total} requests, {total / wall:.1f} req/s, errors {errs} ({100 * errs / max(1, total):.2f}%)\n")
    print(f"{'endpoint':12s} {'n':>6s} {'p50':>8s} {'p95':>8s} {'p99':>8s} {'max':>8s}  (client ms, incl. network + queueing)")
    for name, xs in sorted(stats.items()):
        xs = sorted(xs)
        p = lambda q: xs[min(len(xs) - 1, int(q * len(xs)))]  # noqa: E731
        print(f"{name:12s} {len(xs):6d} {statistics.median(xs):8.1f} {p(.95):8.1f} {p(.99):8.1f} {xs[-1]:8.1f}")
    print("\nserver-side (inside the app):")
    for route, m in server.items():
        if m["n"] >= 5:
            print(f"  {route:44s} n={m['n']:5d} p50={m['p50_ms']:7.1f} p95={m['p95_ms']:7.1f} p99={m['p99_ms']:7.1f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--chat", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.base, a.users, a.duration, a.chat))
