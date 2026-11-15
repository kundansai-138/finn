"""Data layer: per-user isolation, enforced twice (application filters + Postgres row-level security)."""
import json

import pytest
from sqlalchemy import create_engine, text

from finn import store
from finn.config import DATABASE_URL
from tests.conftest import POSTGRES, ROOT


@pytest.fixture(scope="module")
def two_users(tmp_path_factory, as_user=None):
    # alice gets the full demo network; bob has a portfolio but no contacts
    d = tmp_path_factory.mktemp("seed")
    empty = d / "empty_network.json"
    nw = json.loads((ROOT / "data" / "network.json").read_text(encoding="utf-8"))
    empty.write_text(json.dumps({**nw, "people": []}), encoding="utf-8")
    from finn.context import reset_user, set_user

    for uid, net in (("alice", None), ("bob", empty)):
        tok = set_user(uid)
        store.seed_user(uid, network_path=net)
        reset_user(tok)
    return "alice", "bob"


def test_networks_are_private(two_users, as_user):
    with as_user("alice"):
        assert len(store.network_raw()["people"]) == 24
    with as_user("bob"):
        raw = store.network_raw()
        assert raw["people"] == []                                          # none of alice's contacts
        assert [p["name"] for p in raw["public_profiles"]] == ["Siddharth Kapadia"]  # only public profiles


def test_threads_are_private(two_users, as_user):
    with as_user("alice"):
        store.thread_create({"id": "t-alice", "created": 0.0})
        t = store.thread_get("t-alice")
        t["messages"].append({"role": "user", "content": "my private question"})
        store.thread_save(t)
    with as_user("bob"):
        assert store.thread_get("t-alice") is None
        with pytest.raises(PermissionError):
            store.thread_save({"id": "t-alice", "messages": []})


def test_learned_ranking_is_per_user(two_users, as_user):
    from finn.network import recommend as R

    with as_user("alice"):
        R.feedback("zara", {"org": "piind", "event": "M&A"}, 1)
        assert store.reco_feedback_count() == 1
    with as_user("demo"):
        assert store.reco_feedback_count() == 0 and store.reco_weights_get() is None


def test_upsert_is_idempotent():
    row = {"id": "t1", "ticker": "TEST", "ts": "2026-11-01T10:00:00+05:30", "title": "a", "source": "test",
           "category": "results", "subtype": "filing", "score": 92, "extra": "{}"}
    store.ann_upsert([row])
    store.ann_upsert([{**row, "title": "b"}])
    rows = store.ann_for_ticker("TEST")
    assert len(rows) == 1 and rows[0]["title"] == "b"


@pytest.mark.skipif(not POSTGRES, reason="row-level security needs Postgres")
def test_database_enforces_isolation_even_without_app_filters(two_users):
    """Bypass the app entirely: raw SQL as the app role still can't see or write another user's rows."""
    eng = create_engine(DATABASE_URL)
    with eng.connect() as c:
        c.execute(text("SELECT set_config('app.user_id', 'bob', false)"))
        assert c.execute(text("SELECT count(*) FROM contacts")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM holdings WHERE user_id = 'alice'")).scalar() == 0
        with pytest.raises(Exception, match="row-level security"):
            c.execute(text("INSERT INTO holdings(user_id, ticker, kind) VALUES ('alice', 'HACK', 'portfolio')"))
        c.rollback()
        c.execute(text("SELECT set_config('app.user_id', '', false)"))
        assert c.execute(text("SELECT count(*) FROM holdings")).scalar() == 0   # no identity, no rows
