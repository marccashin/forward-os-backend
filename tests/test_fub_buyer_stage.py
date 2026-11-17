"""Where a FORWARD OS buyer stands in Follow Up Boss (Nov 16, 2026): the pure rules,
then the real route with Follow Up Boss and Supabase replaced."""
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fub_deal_stage as fds  # noqa: E402
from test_fub_listing_stage import ORIGIN, load_main  # noqa: E402

AGENT_MAP = {"Ash McGowan": "Ashling McGowan"}  # Follow Up Boss name -> OS name, as in main.py


def deal(i, name, stage, entered, people=("Private Person",), users=("Marc Cashin",), pipeline="Buyers"):
    return {"id": i, "name": name, "pipelineName": pipeline, "stageName": stage, "enteredStageAt": entered,
            "price": 999999, "commissionValue": 12345,
            "people": [{"id": 1, "name": p, "emails": [{"value": "person-on-deal"}]} for p in people],
            "users": [{"id": 9, "name": u} for u in users]}


BUYERS = [
    deal(501, "Jordan Alvarez", "Actively Showing", "2026-11-11T19:40:00Z", people=("Jordan Alvarez",)),
    deal(502, "4410 Garrison St NW", "Pending", "2026-11-12T14:05:00Z", people=("Sam Rivers",)),
    deal(503, "Pat Lee", "Buyer Engaged", "2026-11-02T14:00:00Z", people=("Pat Lee",), users=("Ash McGowan",)),
    deal(504, "Pat Lee", "BA Signed", "2026-11-03T14:00:00Z", people=("Pat Lee",), users=("Marc Cashin",)),
    deal(505, "Chris Day", "Closed This Quarter", "2026-06-01T00:00:00Z", people=("Chris Day",)),
    deal(506, "Chris Day", "Actively Showing", "2026-11-01T00:00:00Z", people=("Chris Day",)),
    deal(507, "Two Live", "Actively Showing", None, people=("Two Live",)),
    deal(508, "Two Live", "Pending", None, people=("Two Live",)),
]


def test_pick_buyer_deal():
    hits, how = fds.pick_buyer_deal("Jordan  alvarez", {"Marc Cashin"}, BUYERS, AGENT_MAP)
    assert how == "name" and [d["id"] for d in hits] == [501], "same words, any case and spacing"
    hits, how = fds.pick_buyer_deal("Sam Rivers", {"Marc Cashin"}, BUYERS, AGENT_MAP)
    assert how == "name" and hits[0]["id"] == 502, "a deal renamed to the address is found by its person"
    hits, how = fds.pick_buyer_deal("Pat Lee", {"Ashling McGowan"}, BUYERS, AGENT_MAP)
    assert how == "name" and hits[0]["id"] == 503, "the buyer's own agent only (FUB name mapped to the OS name)"
    hits, how = fds.pick_buyer_deal("Pat Lee", {"Marc Cashin"}, BUYERS, AGENT_MAP)
    assert hits[0]["id"] == 504
    hits, how = fds.pick_buyer_deal("Chris Day", {"Marc Cashin"}, BUYERS, AGENT_MAP)
    assert how == "name" and [d["id"] for d in hits] == [506], "a closed purchase and a new search: the live one"
    assert fds.pick_buyer_deal("Two Live", {"Marc Cashin"}, BUYERS, AGENT_MAP)[1] == "ambiguous", "two live deals are never guessed"
    assert fds.pick_buyer_deal("Jordan", {"Marc Cashin"}, BUYERS, AGENT_MAP) == ([], "none"), "part of a name never matches"
    assert fds.pick_buyer_deal("Jordan Alvarez", {"Niki Lang"}, BUYERS, AGENT_MAP) == ([], "none"), "another agent's deal is not this buyer"
    assert fds.pick_buyer_deal("", {"Marc Cashin"}, BUYERS, AGENT_MAP) == ([], "none")


def test_summarize_buyer_sends_nothing_personal():
    s = fds.summarize_buyer(BUYERS[0], "name")
    assert s == {"found": True, "how": "name", "deal_id": 501, "pipeline": "Buyers", "stage": "Actively Showing",
                 "entered_stage_at": "2026-11-11T19:40:00Z", "name_is_address": False}, s
    s = fds.summarize_buyer(BUYERS[1], "linked")
    assert s["address"] == "4410 Garrison St NW" and s["name_is_address"] is True, "an address name is sent, for Open To Close"
    for d in BUYERS:
        out = fds.summarize_buyer(d, "name")
        assert not ({"price", "commissionValue", "people", "users", "name"} & set(out)), out
        assert "Alvarez" not in str(out) and "Private Person" not in str(out) and "person-on-deal" not in str(out)
    assert fds.summarize_buyer(None, "none", 0) == {"found": False, "how": "none", "count": 0}


class FakeQ:
    def __init__(self, rows, name):
        self.rows, self.f, self.name = rows, [], name

    def select(self, *_a, **_k):
        return self

    def eq(self, k, v):
        self.f.append((k, v)); return self

    def limit(self, _n):
        return self

    def execute(self):
        return type("R", (), {"data": [dict(r) for r in self.rows if all(r.get(k) == v for k, v in self.f)]})


class FakeSB:
    def __init__(self, rows):
        self.rows, self.writes = rows, []

    def table(self, name):
        assert name == "buyers", name
        sb = self

        class Q(FakeQ):
            def update(self, *a, **k):
                sb.writes.append(a); return self

            def insert(self, *a, **k):
                sb.writes.append(a); return self
        return Q(self.rows, name)


def test_route(monkeypatch):
    from fastapi.testclient import TestClient
    main = load_main()
    rows = [
        {"id": "b-linked", "buyer_name": "Someone Renamed", "agent_name": "Marc Cashin", "subfolder_drive_ids": {"_fub_deal_id": "502"}},
        {"id": "b-name", "buyer_name": "Jordan Alvarez", "agent_name": "Marc Cashin", "subfolder_drive_ids": None},
        {"id": "b-co", "buyer_name": "Pat Lee", "agent_name": "Niki Lang", "subfolder_drive_ids": {"_co_agents": ["Ashling McGowan"]}},
        {"id": "b-two", "buyer_name": "Two Live", "agent_name": "Marc Cashin", "subfolder_drive_ids": {}},
        {"id": "b-none", "buyer_name": "Nobody Here", "agent_name": "Marc Cashin", "fub_deal_id": "501", "subfolder_drive_ids": {}},
        {"id": "b-gone", "buyer_name": "Jordan Alvarez", "agent_name": "Marc Cashin", "subfolder_drive_ids": {"_fub_deal_id": "1"}},
    ]
    sb = FakeSB(rows)
    monkeypatch.setattr(main, "supabase", sb)
    calls = []
    PIPES = [{"id": 1, "name": "Buyers"}, {"id": 2, "name": "Sellers"}]

    async def fake_fub_get(path, params=None):
        calls.append((path, dict(params or {})))
        if path == "/pipelines":
            return {"pipelines": PIPES}
        if path == "/deals":
            assert params["pipelineId"] == 1, "the Buyers pipeline"
            if params.get("next"):
                return {"deals": BUYERS[4:], "_metadata": {"next": None}}
            return {"deals": BUYERS[:4], "_metadata": {"next": "PAGE2"}}
        if path == "/deals/502":
            return BUYERS[1]
        if path == "/deals/1":
            req = httpx.Request("GET", "https://api.followupboss.com/v1/deals/1")
            raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
        raise AssertionError(path)

    monkeypatch.setattr(main, "fub_get", fake_fub_get)
    main._FDS_BUYER_CACHE.update(at=0.0, deals=[], pipeline_id=None)
    seller_cache_before = dict(main._FDS_CACHE)
    c = TestClient(main.app)

    assert c.get("/api/fub/buyer-stage", params={"buyer_id": "b-name"}).status_code == 403, "FORWARD OS pages only"
    assert c.get("/api/fub/buyer-stage", params={"buyer_id": "x;drop"}, headers=ORIGIN).status_code == 400
    assert c.get("/api/fub/buyer-stage", params={"buyer_id": "nope"}, headers=ORIGIN).status_code == 404

    r = c.get("/api/fub/buyer-stage", params={"buyer_id": "b-linked"}, headers=ORIGIN).json()
    assert r["how"] == "linked" and r["deal_id"] == 502 and r["stage"] == "Pending" and r["address"] == "4410 Garrison St NW", r
    assert not any(p == "/deals" for p, _ in calls), "a linked deal is read directly, fresh"

    r = c.get("/api/fub/buyer-stage", params={"buyer_id": "b-name"}, headers=ORIGIN).json()
    assert r["how"] == "name" and r["deal_id"] == 501 and r["stage"] == "Actively Showing" and "address" not in r, r
    assert sum(1 for p, _ in calls if p == "/deals") == 2, "both pages of Buyers deals read"

    r = c.get("/api/fub/buyer-stage", params={"buyer_id": "b-co"}, headers=ORIGIN).json()
    assert r["deal_id"] == 503, "a co-agent's deal counts"

    r = c.get("/api/fub/buyer-stage", params={"buyer_id": "b-two"}, headers=ORIGIN).json()
    assert r == {"found": False, "how": "ambiguous", "count": 2}, r

    r = c.get("/api/fub/buyer-stage", params={"buyer_id": "b-none"}, headers=ORIGIN).json()
    assert r == {"found": False, "how": "none", "count": 0}, "the legacy fub_deal_id column is not used"

    r = c.get("/api/fub/buyer-stage", params={"buyer_id": "b-gone"}, headers=ORIGIN).json()
    assert r["how"] == "name" and r["deal_id"] == 501, "a stamped deal FUB no longer has falls back to the name"
    assert sum(1 for p, _ in calls if p == "/deals") == 2, "the Buyers list is kept for 10 minutes"
    assert sum(1 for p, _ in calls if p == "/pipelines") == 1
    assert sb.writes == [], "read only: nothing is written to the buyer card"
    assert main._FDS_CACHE == seller_cache_before, "the listing route's Sellers cache is untouched"

    async def broken(path, params=None):
        raise RuntimeError("401 Unauthorized")
    monkeypatch.setattr(main, "fub_get", broken)
    main._FDS_BUYER_CACHE.update(at=0.0, deals=[], pipeline_id=None)
    r = c.get("/api/fub/buyer-stage", params={"buyer_id": "b-name"}, headers=ORIGIN)
    assert r.status_code == 502 and "Follow Up Boss could not be read" in r.json()["detail"], r.text
