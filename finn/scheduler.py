"""Background ingestion.

  every 5 min   poll NSE's announcements RSS (it's a rolling ~20-item window, so it must be polled)
  18:45 IST     full end-of-day refresh: prices, per-company filings, fundamentals
  on startup    full refresh if the last one is older than 6 hours

Runs as a daemon thread inside the web process, which is right for one small instance. On a host
that sleeps when idle (free tiers), the same jobs are triggered externally by a scheduled
GitHub Action calling POST /api/refresh with the admin token; see .github/workflows/refresh.yml.
At larger scale this moves to a separate worker process with a job queue.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

from . import clock, db, notify, push, store

log = logging.getLogger(__name__)
RSS_EVERY = 300
EOD_AT = (18, 45)
_lock = threading.Lock()
_state = {"started": None, "last_rss": None, "last_eod": None, "errors": 0}


def run_refresh(**kw) -> dict | None:
    """Single-flight: a refresh already in progress isn't started twice."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        out = push.refresh(**kw)
        out["push"] = for_each_user(notify.dispatch, "push dispatch")
        threading.Thread(target=_after_refresh, daemon=True).start()
        return out
    finally:
        _lock.release()


def _after_refresh() -> None:
    """Off the request path: web context for the new important filings (budgeted), the weekly
    keep-in-touch push, then warm every dossier."""
    from . import enrich
    from .network import nudges

    # Web context is cached per filing and shared, so enriching each user's brief costs nothing extra
    # for filings another user already triggered; the daily budget is global.
    _state["enrich"] = for_each_user(lambda: enrich.enrich_pending(push.brief("72h", "material")["items"]), "enrich")
    _state["nudges"] = for_each_user(nudges.dispatch, "nudge push")   # capped at two a week per user inside
    prebuild_dossiers()


def for_each_user(fn, what: str) -> dict:
    """Run fn in each notifiable user's own scope (the demo user plus everyone with a device registered).
    One user's failure never blocks the others."""
    from .context import DEFAULT_USER, reset_user, set_user

    out = {}
    for uid in dict.fromkeys([DEFAULT_USER] + store.notify_users_list()):
        tok = set_user(uid)
        try:
            out[uid] = fn()
        except Exception as e:  # notifications must never break ingestion
            log.warning("%s failed for %s: %s", what, uid, e)
        finally:
            reset_user(tok)
    return out


def prebuild_dossiers() -> int:
    """Rebuild every holding's dossier off the request path, so no investor waits on a cold one."""
    from . import portfolio, pull

    n = 0
    for h in portfolio.holdings() + portfolio.research():
        try:
            pull.dossier(h["ticker"], force=True)
            n += 1
        except Exception as e:
            log.warning("prebuild %s failed: %s", h["ticker"], e)
    _state["dossiers_built"] = n
    return n


def _loop() -> None:
    age = db.kv_age("meta", "last_refresh")
    if age is None or age > 6 * 3600:
        _safe(run_refresh)
        _state["last_eod"] = clock.now().isoformat()
    while True:
        now = clock.now()
        try:
            if push.poll_rss():
                for_each_user(notify.dispatch, "push dispatch")   # something new may deserve a buzz
            _state["last_rss"] = now.isoformat()
        except Exception as e:
            _state["errors"] += 1
            log.warning("rss poll failed: %s", e)
        target = now.replace(hour=EOD_AT[0], minute=EOD_AT[1], second=0, microsecond=0)
        last = _state["last_eod"]
        if now >= target and (last is None or last[:10] != now.date().isoformat()):
            _safe(run_refresh)
            _state["last_eod"] = now.isoformat()
        time.sleep(RSS_EVERY)


def _safe(fn) -> None:
    try:
        fn()
    except Exception as e:
        _state["errors"] += 1
        log.warning("scheduled job failed: %s", e)


_leader_conn = None


def _become_leader() -> bool:
    """With several web processes (or instances), exactly one runs the jobs. A Postgres advisory
    lock, held for the life of a dedicated connection, elects it; if that process dies the lock is
    released and the next one to start takes over. SQLite (single process) always leads."""
    global _leader_conn
    from sqlalchemy import text

    from . import store

    eng = store.engine()
    if eng.dialect.name != "postgresql":
        return True
    conn = eng.connect()
    got = conn.execute(text("SELECT pg_try_advisory_lock(424242)")).scalar()
    conn.commit()
    if got:
        _leader_conn = conn   # keep it open: closing would release the lock
        return True
    conn.close()
    return False


def start() -> None:
    if _state["started"]:
        return
    if not _become_leader():
        _state["role"] = "follower"
        log.info("scheduler: another process is the leader; this one only serves requests")
        return
    _state["role"] = "leader"
    _state["started"] = clock.now().isoformat()
    threading.Thread(target=_loop, name="finn-scheduler", daemon=True).start()


def status() -> dict:
    nxt = clock.now().replace(hour=EOD_AT[0], minute=EOD_AT[1], second=0, microsecond=0)
    if clock.now() >= nxt:
        nxt += timedelta(days=1)
    return {**_state, "running": bool(_state["started"]), "next_eod": nxt.isoformat(), "rss_every_s": RSS_EVERY}
