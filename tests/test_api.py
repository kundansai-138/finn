"""HTTP contract tests: every screen's endpoint, user isolation through headers, and latency budgets."""
import json
import statistics
import time

import pytest
from fastapi.testclient import TestClient

from finn.api import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    h = client.get("/api/health").json()
    assert h["ok"] and h["db"] in ("postgresql", "sqlite")


def test_brief_ranks_and_explains(client):
    b = client.get("/api/brief?window=24h&level=material").json()
    assert b["items"], "simulated filings should be in the 24h window"
    scores = [i["score"] for i in b["items"]]
    assert scores == sorted(scores, reverse=True)                      # ranked
    cfo = next(i for i in b["items"] if i["ticker"] == "DEEPAKNTR")
    assert cfo["tier"] == "critical" and cfo["person"]["name"] == "Siddharth Kapadia"
    assert cfo["paths"][0]["via"]["name"] == "Ananya Rao"              # every signal ends in a person
    assert any(a["ticker"] == "DIXON" for a in b["alerts"])           # the synthetic +6% / 3x volume day


def test_swipe_to_handle_round_trip(client):
    b = client.get("/api/brief?window=24h").json()
    first = b["items"][0]["id"]
    client.post("/api/brief/dismiss", json={"id": first})
    after = client.get("/api/brief?window=24h").json()
    assert first not in [i["id"] for i in after["items"]] and after["counts"]["done"] == 1
    assert "left" in after["headline"]
    client.post("/api/brief/dismiss", json={"id": first, "undo": True})
    assert client.get("/api/brief?window=24h").json()["counts"]["done"] == 0


def test_recommender_endpoint(client):
    r = client.get("/api/network/recommend", params={"ticker": "LAURUSLABS", "event": "capex"}).json()["results"]
    assert r[0]["person"]["id"] == "priya" and r[0]["graph"]["nodes"] and r[0]["contributions"]


def test_users_are_isolated_over_http(client):
    other = {"X-FINN-User": "intruder"}
    assert client.get("/api/portfolio", headers=other).json()["holdings"] == []
    assert client.get("/api/brief?window=24h", headers=other).json()["items"] == []   # no holdings, no brief
    # A demo thread is invisible to another user
    events = _chat(client, "Who knows semiconductors?")
    tid = next(e["thread_id"] for e in events if e["type"] == "done")
    assert client.get(f"/api/chat/{tid}").status_code == 200
    assert client.get(f"/api/chat/{tid}", headers=other).status_code == 404


def _chat(client, message, **kw):
    with client.stream("POST", "/api/chat", json={"message": message, **kw}) as r:
        return [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]


def test_agent_stream_contract(client):
    ev = _chat(client, "Who can intro me to the new CFO of Deepak Nitrite?")
    kinds = [e["type"] for e in ev]
    assert kinds[0] == "thread" and kinds[-1] == "done"
    nodes = [e["node"] for e in ev if e["type"] == "node" and e["status"] == "end"]
    assert nodes == ["understand", "plan", "execute", "answer", "verify", "remember"]
    plan = next(e for e in ev if e["type"] == "plan")
    assert plan["steps"][0]["tool"] == "route_to_person"
    verify = next(e for e in ev if e["type"] == "verify")
    assert verify["ok"]


def test_push_key_is_a_p256_point(client):
    import base64

    k = client.get("/api/push/key").json()["publicKey"]
    raw = base64.urlsafe_b64decode(k + "=" * (-len(k) % 4))
    assert len(raw) == 65 and raw[0] == 4   # uncompressed EC point, as browsers require


def test_radar_home_screen(client):
    """Every holding is a tile; only the flagged ones become rows, each with one person attached."""
    r = client.get("/api/radar?window=24h").json()
    assert len(r["tiles"]) == len(client.get("/api/portfolio").json()["holdings"])
    needs = {n["ticker"]: n for n in r["needs"]}
    assert {t["ticker"] for t in r["tiles"] if t["status"]} == set(needs)       # map and list agree
    assert [n["score"] for n in r["needs"]] == sorted((n["score"] for n in r["needs"]), reverse=True)
    cfo = needs["DEEPAKNTR"]
    assert cfo["kind"] == "filing" and cfo["hook"]["intent"] == "intro"
    assert cfo["hook"]["person"]["name"] == "Ananya Rao" and cfo["hook"]["target"]["name"] == "Siddharth Kapadia"
    assert needs["DIXON"]["move"]["volume_x"] >= 2                               # the synthetic 3x-volume day
    assert r["headline"].startswith(f"{len(needs)} of {len(r['tiles'])}")
    assert client.get("/api/radar?window=1y").status_code == 422


def test_people_todo_news_first_one_row_per_person(client):
    items = client.get("/api/people/todo").json()["items"]
    assert items and items[0]["kind"] == "news"
    kinds = [x["kind"] for x in items]
    assert kinds == sorted(kinds, key=lambda k: k != "news")
    assert len({x["person"]["id"] for x in items}) == len(items)
    ananya = next(x for x in items if x["person"]["id"] == "ananya")
    assert ananya["intent"] == "intro" and ananya["title"].startswith("Ask Ananya to introduce you")
    # Done clears the row (without touching the contact, which other tests rely on)
    client.post("/api/people/todo/done", json={"id": ananya["id"], "person_id": None})
    assert ananya["id"] not in [x["id"] for x in client.get("/api/people/todo").json()["items"]]
    client.post("/api/brief/dismiss", json={"id": ananya["id"], "undo": True})


def test_drafter_grounded_and_channel_aware(client):
    cfo = next(n for n in client.get("/api/radar").json()["needs"] if n["ticker"] == "DEEPAKNTR")
    body = {"person_id": "ananya", "intent": "intro", "event_id": cfo["event_id"], "target_id": cfo["hook"]["target"]["id"]}
    wa = client.post("/api/draft", json={**body, "channel": "whatsapp"}).json()
    assert wa["source"] == "template" and wa["intent"] == "intro"            # model is off in tests
    assert wa["text"].startswith("Hi Ananya, ") and "Siddharth" in wa["text"]
    assert any(g.startswith("How you know Ananya") for g in wa["grounds"])    # the draft says what it rests on
    assert {i["id"] for i in wa["intents"]} == {"intro", "view", "catchup"}
    assert len(client.post("/api/draft", json={**body, "channel": "linkedin"}).json()["text"]) <= 300
    em = client.post("/api/draft", json={"person_id": "meera", "intent": "view", "channel": "email", "ticker": "PIIND"}).json()
    assert em["subject"] and em["text"].startswith("Hi Meera,\n\n")
    assert client.post("/api/draft", json={"person_id": "nobody"}).status_code == 404


@pytest.mark.perf
def test_latency_budgets(client):
    """Regression guard, not a benchmark: warm p95 must stay inside the budget."""
    budgets = {"/api/brief?window=7d&level=material": 300, "/api/network/recommend?ticker=PIIND&event=M%26A": 150,
               "/api/portfolio": 300, "/api/health": 100}
    for url, budget_ms in budgets.items():
        client.get(url)  # warm
        ts = []
        for _ in range(15):
            t = time.perf_counter()
            assert client.get(url).status_code == 200
            ts.append((time.perf_counter() - t) * 1000)
        p95 = sorted(ts)[int(0.95 * len(ts)) - 1]
        assert p95 < budget_ms, f"{url}: p95 {p95:.0f}ms > {budget_ms}ms (median {statistics.median(ts):.0f}ms)"
