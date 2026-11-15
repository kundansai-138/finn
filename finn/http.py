"""Polite, cached HTTP fetching.

- Identifies itself honestly (no browser impersonation, no bot-check workarounds).
- Rate-limits per host.
- Caches bodies in SQLite with a TTL so a refresh job and the API never hammer a source.
"""
from __future__ import annotations

import hashlib
import threading
import time
from urllib.parse import urlparse

import requests

from . import store
from .config import HOST_MIN_INTERVAL, USER_AGENT

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})
_last_hit: dict[str, float] = {}
_host_lock = threading.Lock()


class FetchError(RuntimeError):
    pass


def _throttle(host: str) -> None:
    with _host_lock:
        wait = _last_hit.get(host, 0) + HOST_MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.time()


def fetch(url: str, ttl: float, *, binary: bool = False, timeout: float = 30) -> bytes | str:
    """GET with cache. Returns cached body if younger than ttl; stale cache is used if the source fails."""
    key = hashlib.sha256(url.encode()).hexdigest()
    cached = store.http_get(key)
    if cached and time.time() - cached.fetched_at < ttl and cached.status == 200:
        body = cached.body
        return body if binary else body.decode("utf-8", errors="replace")

    host = urlparse(url).netloc
    try:
        _throttle(host)
        r = _session.get(url, timeout=timeout)
        if r.status_code == 429:
            time.sleep(5)
            _throttle(host)
            r = _session.get(url, timeout=timeout)
        r.raise_for_status()
        body = r.content
        store.http_put(key, r.status_code, body)
        return body if binary else body.decode(r.encoding or "utf-8", errors="replace")
    except requests.RequestException as e:
        if cached and cached.status == 200:  # serve stale rather than fail the demo
            body = cached.body
            return body if binary else body.decode("utf-8", errors="replace")
        raise FetchError(f"{url}: {e}") from e
