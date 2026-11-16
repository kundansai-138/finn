"""Test setup: a fresh schema in the dedicated test database for every run.

  docker compose up -d db      # Postgres with a finn_test database (see docker/initdb)
  python -m pytest -q

TEST_DATABASE_URL comes from the environment or .env. Without it, tests fall back to a throwaway
SQLite file and the Postgres-only tests (row-level security) are skipped. The model is always off
in tests (nothing here spends money) unless FINN_TEST_LLM=on for the live suite, and the scheduler never starts.
"""
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _from_env_file(key: str) -> str | None:
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return None


_url = os.environ.get("TEST_DATABASE_URL") or _from_env_file("TEST_DATABASE_URL")
os.environ["DATABASE_URL"] = _url or f"sqlite:///{(ROOT / 'data' / 'test.sqlite3').as_posix()}"
os.environ["FINN_LLM"] = os.environ.get("FINN_TEST_LLM", "off")   # "on" only for `-m live` runs that should spend
if os.environ["FINN_LLM"] == "on":
    os.environ["FINN_LLM_SERVER_KEY"] = "on"   # the live suite calls the model with the key in .env
os.environ["FINN_SCHEDULER"] = "off"
os.environ["FINN_SCENARIOS"] = "on"

import pytest  # noqa: E402

from finn import store  # noqa: E402

POSTGRES = store.engine().dialect.name == "postgresql"


@pytest.fixture(scope="session", autouse=True)
def fresh_database():
    store.md.drop_all(store.engine())
    store._engine.dispose()
    store._engine = None
    store.init()  # schema, RLS policies, demo user
    from finn import push

    push._store(push.load_scenarios())  # three simulated filings, no network needed
    _synthetic_prices()
    yield


def _synthetic_prices():
    """A flat 60-day series for every holding, with one +6% / 3x-volume day on DIXON."""
    from datetime import date, timedelta

    from finn import db, portfolio
    from finn.context import set_user

    set_user("demo")
    days = [(date(2026, 8, 1) + timedelta(days=i)).isoformat() for i in range(60)]
    for h in portfolio.holdings():
        close = [100.0] * 60
        vol = [1000.0] * 60
        if h["ticker"] == "DIXON":
            close[-1], vol[-1] = 106.0, 3000.0
        store.prices_put(h["ticker"], days, close, vol)
        db.kv_put("eod_fetched", h["ticker"], days[-1])


@pytest.fixture
def as_user():
    """Run a block as a given user: `with as_user("alice"): ...`."""
    from contextlib import contextmanager

    from finn.context import reset_user, set_user

    @contextmanager
    def _as(uid: str):
        tok = set_user(uid)
        try:
            yield
        finally:
            reset_user(tok)

    return _as
