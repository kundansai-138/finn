"""Accounts: sign up, sign in, sessions.

  passwords   scrypt (Python stdlib), random 16-byte salt, stored as "scrypt$n$r$p$salt$hash" in a table
              under row-level security, readable only inside that user's own scope
  sessions    a random 32-byte token in an HttpOnly, SameSite=Lax cookie; the database stores only its
              SHA-256, so a leaked sessions table can't be replayed; 30 days, revoked on sign-out
  identity    per request: session cookie, else the X-FINN-User header (development and tests; off
              when FINN_AUTH_STRICT=on), else the guest demo user, so the demo still opens without an account
  limits      5 failed sign-ins per username per 10 minutes, then 429

Every personal table is already isolated by user in the database; this module only decides who the
user is.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque

from . import store
from .context import DEFAULT_USER, reset_user, set_user

COOKIE = "finn_session"
TTL = 30 * 86400
STRICT = os.getenv("FINN_AUTH_STRICT", "off") == "on"
USERNAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{2,39}$")
RESERVED = {DEFAULT_USER, "admin", "root", "system", "finn", "api", "test", "guest", "self"}
MIN_PASSWORD = 8
_N, _R, _P = 2 ** 14, 8, 1


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---- passwords ------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b = lambda x: base64.urlsafe_b64encode(x).decode().rstrip("=")  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b(salt)}${b(h)}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        hashlib.scrypt(b"x", salt=b"0" * 16, n=_N, r=_R, p=_P, dklen=32)   # same work either way: no user-exists timing leak
        return False
    try:
        _, n, r, p, salt, h = stored.split("$")
        pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
        got = hashlib.scrypt(password.encode(), salt=pad(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, pad(h))
    except (ValueError, TypeError):
        return False


# ---- rate limit -----------------------------------------------------------------
_fails: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
_lock = threading.Lock()
MAX_FAILS, WINDOW = 5, 600


def _limited(key: str) -> bool:
    now = time.time()
    with _lock:
        q = _fails[key]
        while q and now - q[0] > WINDOW:
            q.popleft()
        return len(q) >= MAX_FAILS


def _fail(key: str) -> None:
    with _lock:
        _fails[key].append(time.time())


# ---- sessions -------------------------------------------------------------------
_cache: dict[str, tuple[str | None, float]] = {}   # token hash -> (user, cached at): skip a DB read per request


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(user_id: str, ua: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    store.session_create(_token_hash(token), user_id, TTL, ua)
    return token


def user_for_token(token: str | None) -> str | None:
    if not token or len(token) > 100:
        return None
    th = _token_hash(token)
    hit = _cache.get(th)
    if hit and time.time() - hit[1] < 60:
        return hit[0]
    uid = store.session_user(th)
    if len(_cache) > 5000:
        _cache.clear()
    _cache[th] = (uid, time.time())
    return uid


def end_session(token: str | None) -> None:
    if token:
        th = _token_hash(token)
        store.session_delete(th)
        _cache.pop(th, None)


def resolve(cookie_token: str | None, header_user: str | None) -> tuple[str, str]:
    """(user id, how): the session wins; the dev header only when not strict; else the guest demo."""
    uid = user_for_token(cookie_token)
    if uid:
        return uid, "session"
    if header_user and not STRICT:
        h = header_user.lower()
        if re.match(r"^[a-z0-9_-]{1,40}$", h):
            return h, "header"
    return DEFAULT_USER, "guest"


# ---- sign up / sign in ------------------------------------------------------------
def _in_scope(uid: str, fn):
    tok = set_user(uid)
    try:
        return fn()
    finally:
        reset_user(tok)


def register(username: str, password: str, name: str | None = None, start_from_demo: bool = False) -> str:
    u = (username or "").strip().lower()
    if not USERNAME_RX.match(u) or u in RESERVED:
        raise AuthError(422, "Usernames are 3-40 letters, digits, - or _, and some names are reserved.")
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(422, f"Use a password of at least {MIN_PASSWORD} characters.")
    if store.user_exists(u):
        raise AuthError(409, "That username is taken.")

    def make():
        store.user_create(u, (name or u).strip()[:80], start_from_demo=start_from_demo)
        store.credentials_put(hash_password(password))
        store.notify_users_sync()   # in the scheduler's list from day one (in-app inbox even without a phone)
    _in_scope(u, make)
    from .network import graph

    graph.invalidate(u)
    return u


def login(username: str, password: str) -> str:
    u = (username or "").strip().lower()
    key = f"user:{u}"
    if _limited(key):
        raise AuthError(429, "Too many attempts. Try again in a few minutes.")
    stored = _in_scope(u, store.credentials_get) if USERNAME_RX.match(u) else None
    if not check_password(password or "", stored):
        _fail(key)
        raise AuthError(401, "Wrong username or password.")
    return u


def set_password(user_id: str, password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(422, f"Use a password of at least {MIN_PASSWORD} characters.")
    _in_scope(user_id, lambda: store.credentials_put(hash_password(password)))
