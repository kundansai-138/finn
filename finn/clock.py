"""One clock for the whole app, so a demo can be pinned to a moment (FINN_NOW) and replayed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import PINNED_NOW

IST = timezone(timedelta(hours=5, minutes=30))


def now() -> datetime:
    if PINNED_NOW:
        dt = datetime.fromisoformat(PINNED_NOW)
        return dt if dt.tzinfo else dt.replace(tzinfo=IST)
    return datetime.now(IST)
