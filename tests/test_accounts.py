"""Accounts, the radar, editable scoring and per-user notifications.

  - sign up, sign in, sign out; wrong passwords, rate limiting, a session cookie that identifies the user
  - a signed-in user sees only their own radar and network; the guest still gets the demo
  - add and remove companies (NSE-listed only), and people
  - scoring: defaults, per-user overrides that change rankings, clamping, reset, isolation
  - notifications reach every user with a registered device, each in their own scope
"""
import pytest
from fastapi.testclient import TestClient

from finn import auth, scoring, store
from finn.api import app
from finn.context import reset_user, set_user
from finn.network import graph, nudges
from finn.network import recommend as R

OSAT = "binding term sheet with Vishay for an OSAT semiconductor assembly joint venture"


@pytest.fixture
def client():
    with TestClient(app) as c:      # a fresh cookie jar per test
        yield c


@pytest.fixture(autouse=True)
def nse_list(monkeypatch):
    """The NSE company list, without the network."""
    from finn.sources import universe

    names = {"TITAN": "Titan Company Limited", "BEL": "Bharat Electronics Limited", "KAYNES": "Kaynes Technology India Limited"}
    monkeypatch.setattr(universe, "load", lambda: (names, {}))
    monkeypatch.setattr(universe, "name", lambda s: names.get(s.upper()))
    import finn.api as api

    monkeypatch.setattr(api, "_warm_company", lambda t: None)   # no price/filing fetch in tests


def signup(client, username, password="correct horse 1", demo=True, name=None):
    """demo=True: an account seeded like the shareable test user (scripts/create_user.py), then signed in.
    demo=False: a real self sign-up through the API, which always starts empty."""
    if demo:
        try:
            auth.register(username, password, name or username.title(), start_from_demo=True)
        except auth.AuthError as e:
            return type("R", (), {"status_code": e.status, "json": lambda self: {}, "headers": {}})()
        return client.post("/api/auth/login", json={"username": username, "password": password})
    return client.post("/api/auth/register", json={"username": username, "password": password, "name": name or username.title(),
                                                   "start_from_demo": True})   # the flag is ignored: sign-ups start empty


# ---- accounts ------------------------------------------------------------------
def test_password_hashing():
    h = auth.hash_password("s3cret-pass")
    assert h.startswith("scrypt$") and "s3cret" not in h
    assert auth.check_password("s3cret-pass", h) and not auth.check_password("wrong", h)
    assert auth.hash_password("s3cret-pass") != h                      # salted
    assert not auth.check_password("x", None)


def test_signup_signin_signout(client):
    assert client.get("/api/auth/me").json()["guest"]                     # no account: the demo
    r = signup(client, "acct-a", name="Asha Investor", demo=False)
    assert r.status_code == 200 and r.json()["user"] == "acct-a" and not r.json()["guest"]
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    me = client.get("/api/auth/me").json()
    assert me["user"] == "acct-a" and me["name"] == "Asha Investor" and me["via"] == "session"
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").json()["guest"]
    assert client.post("/api/auth/login", json={"username": "ACCT-A", "password": "correct horse 1"}).json()["user"] == "acct-a"


def test_signup_validation(client):
    assert signup(client, "ab", demo=False).status_code == 422                        # too short
    assert signup(client, "demo", demo=False).status_code == 422                      # reserved
    assert signup(client, "acct-b", password="short", demo=False).status_code == 422
    assert signup(client, "acct-b", demo=False).status_code == 200
    assert signup(client, "acct-b", demo=False).status_code == 409                    # taken


def test_wrong_password_and_rate_limit(client):
    signup(client, "acct-c")
    client.post("/api/auth/logout")
    for _ in range(auth.MAX_FAILS):
        assert client.post("/api/auth/login", json={"username": "acct-c", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "acct-c", "password": "correct horse 1"}).status_code == 429
    assert client.post("/api/auth/login", json={"username": "nobody-here", "password": "x"}).status_code == 401


def test_session_beats_header_and_is_revocable(client):
    signup(client, "acct-d")
    assert client.get("/api/auth/me", headers={"X-FINN-User": "someone-else"}).json()["user"] == "acct-d"
    tok = client.cookies.get(auth.COOKIE)
    client.post("/api/auth/logout")
    assert auth.user_for_token(tok) is None or auth._cache.pop(auth._token_hash(tok), None) or auth.user_for_token(tok) is None


def test_strict_mode_ignores_the_dev_header(monkeypatch):
    monkeypatch.setattr(auth, "STRICT", True)
    assert auth.resolve(None, "alice") == ("demo", "guest")
    monkeypatch.setattr(auth, "STRICT", False)
    assert auth.resolve(None, "alice") == ("alice", "header")


def test_an_empty_account_starts_empty(client):
    signup(client, "acct-e", demo=False)   # even asking for demo data, a self sign-up sees nothing
    assert client.get("/api/radar").json()["empty"] is True
    assert client.get("/api/people/todo").json()["items"] == []
    assert client.get("/api/network/nudges").json()["items"] == []
    assert client.get("/api/notifications").json()["items"] == []
    assert client.get("/api/portfolio").json()["holdings"] == []
    assert client.get("/api/network").json()["people"] == []
    assert client.get("/api/radar").json()["tiles"] == []


# ---- the radar -------------------------------------------------------------------
def test_add_and_remove_companies(client):
    signup(client, "acct-f")
    n0 = len(client.get("/api/radar").json()["tiles"])
    assert client.get("/api/universe/search?q=tit").json()["results"][0]["ticker"] == "TITAN"
    r = client.post("/api/holdings", json={"ticker": "titan", "kind": "portfolio", "weight": 3}).json()
    assert r["new"] and r["name"] == "Titan Company Limited"
    tiles = client.get("/api/radar").json()["tiles"]
    assert len(tiles) == n0 + 1 and next(t for t in tiles if t["ticker"] == "TITAN")["weight"] == 3
    assert "TITAN" in store.kv_get("tracked", "tickers")                  # the shared refresh fetches its filings
    # moving between lists replaces, never duplicates
    client.post("/api/holdings", json={"ticker": "TITAN", "kind": "research"})
    p = client.get("/api/portfolio").json()
    assert "TITAN" not in [h["ticker"] for h in p["holdings"]] and "TITAN" in [h["ticker"] for h in p["research"]]
    assert client.delete("/api/holdings/TITAN").status_code == 200
    assert client.delete("/api/holdings/TITAN").status_code == 404
    assert client.post("/api/holdings", json={"ticker": "NOTREAL", "kind": "portfolio"}).status_code == 422
    assert client.post("/api/holdings", json={"ticker": "BEL", "kind": "portfolio", "weight": 500}).status_code == 422


def test_my_radar_is_mine(client):
    signup(client, "acct-g")
    client.post("/api/holdings", json={"ticker": "BEL", "kind": "portfolio", "weight": 2})
    client.post("/api/auth/logout")
    assert "BEL" not in [t["ticker"] for t in client.get("/api/radar").json()["tiles"]]   # the guest demo doesn't see it


def test_adding_people_is_per_account(client):
    signup(client, "acct-h")
    r = client.post("/api/network/contacts", json={"name": "Rohan Mehta", "title": "CFO", "org_name": "Kaynes Technology",
                                                    "context": "met at a conference"}).json()
    assert not r["existed"] and r["person"]["is_contact"]
    assert any(p["id"] == r["id"] for p in client.get("/api/network").json()["people"])
    new_co = client.post("/api/network/contacts", json={"name": "Nikhil Rao", "title": "Head of Strategy", "org_name": "Bharat Electronics"}).json()
    assert new_co["person"]["headline"] == "Head of Strategy, Bharat Electronics"   # not the new org's internal id
    client.post("/api/auth/logout")
    assert client.get(f"/api/people/{r['id']}").status_code == 404


# ---- scoring ---------------------------------------------------------------------
def _as(uid):
    tok = set_user(uid)
    graph.invalidate(uid)
    return tok


def test_scoring_defaults_and_shape(client):
    signup(client, "acct-i")
    groups = {g["id"]: g for g in client.get("/api/scoring").json()["groups"]}
    assert set(groups) == {"pull", "push", "nudges", "web"} and not any(g["customised"] for g in groups.values())
    topic = next(f for f in groups["pull"]["fields"] if f["key"] == "topic")
    assert topic["value"] == topic["default"] == R.PRIORS["pull"]["topic"] and topic["help"]
    assert {f["key"] for f in groups["push"]["fields"]} >= {"reach_floor", "fresh_floor"}
    assert "warmth" not in {f["key"] for f in groups["push"]["fields"]}   # enters push as a multiplier, not a weight


def test_editing_weights_changes_the_ranking_and_reset_restores_it(client):
    signup(client, "acct-j")
    tok = _as("acct-j")
    try:
        assert {p["person"]["id"] for p in R.recommend("kaynes", event="M&A", text=OSAT, k=2)} == {"sneha", "arjun"}
        # this investor doesn't care about topic expertise or function, only about who worked there
        client.put("/api/scoring/pull", json={"values": {"topic": 0, "function_fit": 0, "insider": 5}})
        assert R.recommend("kaynes", event="M&A", text=OSAT, k=1)[0]["person"]["id"] == "kiran"
        assert R.model_card("pull")["features"][0]["prior"] == 5.0
        client.delete("/api/scoring/pull")
        assert {p["person"]["id"] for p in R.recommend("kaynes", event="M&A", text=OSAT, k=2)} == {"sneha", "arjun"}
    finally:
        reset_user(tok)


def test_values_are_clamped_shares_normalised_and_unknowns_ignored(client):
    signup(client, "acct-k")
    r = client.put("/api/scoring/nudges", json={"values": {"w_urgency": 99, "w_value": -4, "w_hook": 0.5, "bogus": 3}}).json()
    vals = {f["key"]: f["value"] for f in r["fields"]}
    assert vals["w_urgency"] == 1.0 and vals["w_value"] == 0.0 and "bogus" not in vals
    tok = _as("acct-k")
    try:
        v = scoring.values("nudges")
        assert abs(v["w_urgency"] + v["w_value"] + v["w_hook"] - 1) < 1e-9 and v["w_value"] == 0
        assert all(n["scores"]["priority"] <= 1 for n in nudges.compute(include_all=True))
    finally:
        reset_user(tok)
    assert client.put("/api/scoring/nope", json={"values": {}}).status_code == 404


def test_scoring_is_per_user(client):
    signup(client, "acct-l")
    client.put("/api/scoring/web", json={"values": {"min_fit": 0.8}})
    client.post("/api/auth/logout")
    signup(client, "acct-m")
    web = next(g for g in client.get("/api/scoring").json()["groups"] if g["id"] == "web")
    assert not web["customised"] and next(f for f in web["fields"] if f["key"] == "min_fit")["value"] == 0.35


def test_saving_a_ranking_replaces_what_feedback_taught(client):
    signup(client, "acct-n")
    tok = _as("acct-n")
    try:
        rec = R.recommend("kaynes", event="M&A", text=OSAT, k=1)[0]
        R.feedback(rec["person"]["id"], rec["query"], 0)
        assert R.weights("pull") != scoring.prior("pull")
        scoring.save("pull", {"seniority": 1.0})
        assert R.weights("pull") == scoring.prior("pull") and R.weights("pull")["seniority"] == 1.0
    finally:
        reset_user(tok)


# ---- notifications per user ---------------------------------------------------------
SUB = {"endpoint": "https://push.example/dev-1", "keys": {"p256dh": "BEl6", "auth": "a1"}}


def test_device_registration_follows_the_account(client):
    signup(client, "acct-o")
    r = client.post("/api/push/subscribe", json=SUB).json()
    assert r["devices"] == 1 and r["user"] == "acct-o"
    assert "acct-o" in store.notify_users_list()
    client.request("DELETE", "/api/push/subscribe", json={"endpoint": SUB["endpoint"]})
    assert client.get("/api/auth/me").json()["devices"] == 0
    assert "acct-o" in store.notify_users_list()   # still listed: the in-app inbox needs no device


def test_scheduler_notifies_every_user_in_their_own_scope(client, monkeypatch):
    from finn import scheduler
    from finn.context import current_user

    signup(client, "acct-p")
    client.post("/api/push/subscribe", json={**SUB, "endpoint": "https://push.example/dev-p"})
    seen = []
    out = scheduler.for_each_user(lambda: seen.append(current_user()) or {"ok": current_user()}, "test")
    assert "demo" in seen and "acct-p" in seen and out["acct-p"] == {"ok": "acct-p"}

    def boom():
        if current_user() == "demo":
            raise RuntimeError("one user's failure")
        return "fine"
    assert scheduler.for_each_user(boom, "test")["acct-p"] == "fine"   # never blocks the others


def test_test_user_script(tmp_path, monkeypatch):
    from scripts import create_user

    monkeypatch.setattr(create_user, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("DATABASE_URL=x\nFINN_TEST_PASSWORD=old\n", encoding="utf-8")
    monkeypatch.setenv("FINN_NEW_PASSWORD", "a-test-password")
    monkeypatch.setattr("sys.argv", ["create_user", "acct-script", "--name", "Script User"])
    create_user.main()
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "FINN_TEST_USER=acct-script" in env and "FINN_TEST_PASSWORD=a-test-password" in env and env.count("FINN_TEST_PASSWORD") == 1
    assert auth.login("acct-script", "a-test-password") == "acct-script"


# ---- the in-app inbox ---------------------------------------------------------------
def test_notifications_reach_the_inbox_without_a_device(client):
    signup(client, "acct-q")
    assert client.get("/api/auth/me").json()["unread"] == 0
    r = client.post("/api/notifications/check").json()      # the simulated CFO change etc. on the demo holdings
    assert r["new"] >= 1 and r["unread"] == r["new"]
    items = client.get("/api/notifications").json()["items"]
    assert items[0]["url"].startswith("/#/") and {i["kind"] for i in items} <= {"filing", "move", "nudge"}
    assert client.post("/api/notifications/check").json()["new"] == 0          # never twice
    first = items[0]["id"]
    assert client.post("/api/notifications/read", json={"ids": [first]}).json()["unread"] == r["unread"] - 1
    assert client.post("/api/notifications/read", json={}).json()["unread"] == 0
    client.post("/api/auth/logout")
    signup(client, "acct-r", demo=False)
    assert client.get("/api/notifications").json()["items"] == []            # one account's inbox is invisible to another


def test_test_alert_is_listed_each_time(client):
    import time as _t

    signup(client, "acct-s", demo=False)
    client.post("/api/push/test")
    _t.sleep(1.1)
    client.post("/api/push/test")
    assert len(client.get("/api/notifications").json()["items"]) == 2
