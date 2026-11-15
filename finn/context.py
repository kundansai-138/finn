"""Per-request user identity.

Every personal read/write in finn/store.py defaults to the current user, set here by the API
middleware from the request. Background work (the agent's worker thread, parallel tool calls)
runs inside a copied context so it keeps the caller's identity.

The prototype has one demo user and selects users by the `X-FINN-User` header. In production
this is replaced by real authentication (OAuth/session), and Postgres row-level security
enforces the same user_id boundary in the database itself.
"""
from __future__ import annotations

import contextvars

DEFAULT_USER = "demo"
_user: contextvars.ContextVar[str] = contextvars.ContextVar("finn_user", default=DEFAULT_USER)


def current_user() -> str:
    return _user.get()


def set_user(user_id: str) -> contextvars.Token:
    return _user.set(user_id or DEFAULT_USER)


def reset_user(token: contextvars.Token) -> None:
    _user.reset(token)
