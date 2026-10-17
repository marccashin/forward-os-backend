"""Where a FORWARD OS listing stands in Follow Up Boss: the pure rules, then the real
route with Follow Up Boss and Supabase replaced."""
import asyncio
import os
import re
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fub_deal_stage as fds  # noqa: E402

ORIGIN = {"origin": "https://forward-os-staging.netlify.app"}
P_LINKED = "11111111-1111-4111-8111-111111111111"
P_ADDRESS = "22222222-2222-4222-8222-222222222222"
P_TWO = "33333333-3333-4333-8333-333333333333"
P_NONE = "44444444-4444-4444-8444-444444444444"
P_GONE = "55555555-5555-4555-8555-555555555555"
P_NAMED = "77777777-7777-4777-8777-777777777777"


def deal(i, name, stage, entered, pipeline="Sellers"):
    return {"id": i, "name": name, "pipelineName": pipeline, "stageName": stage, "enteredStageAt": entered,
            "price": 999999, "commissionValue": 12345, "people": [{"id": 1, "name": "Private Person"}],
            "users": [{"id": 9, "name": "Ashling McGowan"}]}


SELLERS = [
    deal(940, "7532 Coxton Ct, Unit G-127, Alexandria VA 22306", "Listing App. Scheduled", "2026-10-09T15:00:00Z"),
    deal(941, "12 Twin Way", "Seller Engaged", "2026-10-02T15:00:00Z"),
    deal(942, "12 Twin Way NW", "Listed", "2026-09-02T15:00:00Z"),
    deal(917, "Kylie & Brian Dowling", "Seller Engaged", "2026-10-06T12:42:29Z"),
]
PIPES = [{"id": 1, "name": "Buyers"}, {"id": 2, "name": "Sellers"}]


def test_address_rule_matches_the_os_copy():
    assert fds.same_address("7532 Coxton Ct, Unit G-127, Alexandria VA 22306", "7532 Coxton Court #G-127")
    assert fds.same_address("1926 Ruxton Rd", "1926 Ruxton Road, Towson, MD, 21204"), "a deal name without the city"
    assert not fds.same_address("1926 Ruxton Rd, Baltimore MD", "1926 Ruxton Road, Towson, MD"), "a different city is not assumed equal (same as FORWARD OS)"
    assert not fds.same_address("1926 Ruxton Rd", "1928 Ruxton Rd")
    assert not fds.same_address("Kylie & Brian Dowling", "Kylie & Brian Dowling"), "a person's name is never an address"
    assert not fds.same_address("12 Main", "12 Main St"), "fewer than three tokens never matches"


def test_pick_and_summary():
    hits, how = fds.pick_by_address("7532 Coxton Ct Unit G-127", SELLERS)
    assert how == "address" and hits[0]["id"] == 940
    hits, how = fds.pick_by_address("12 Twin Way", SELLERS)
    assert how == "ambiguous" and len(hits) == 2
    assert fds.pick_by_address("9 Nowhere Rd", SELLERS) == ([], "none")
    # Live cases (Oct 17, 2026): a deal name typed without "St", and a card named after the sellers
    live = [deal(777, "2500 N Van Dorn #205", "Listed", "2026-09-01T00:00:00Z"),
            deal(917, "Kylie & Brian Dowling", "Seller Engaged", "2026-10-06T12:42:29Z"),
            deal(833, "1300 Army Navy Dr #1019", "Seller Engaged", "2026-09-01T00:00:00Z"),
            deal(918, "2909 7th st NE", "Listed", "2026-10-15T14:40:14Z")]
    hits, how = fds.pick_by_address("2500 N Van Dorn St, Unit 205, Alexandria VA 22302", live)
    assert how == "address" and hits[0]["id"] == 777, (how, hits)
    assert fds.pick_by_address("2500 N Van Dorn St, Unit #304, Alexandria VA 22302", live) == ([], "none"), "another unit is another home"
    assert fds.pick_by_address("1300 Army Navy Dr #518", live) == ([], "none"), "another unit is another home"
    assert fds.pick_by_address("2909 7th st NW, 20017", live) == ([], "none"), "NW is not NE"
    hits, how = fds.pick_by_address("Kylie & Brian Dowling", live)
    assert how == "name" and hits[0]["id"] == 917
    assert fds.summarize(hits[0], how)["name_is_address"] is False, "so the agent is reminded to rename it"
    assert fds.pick_by_address("Kylie Dowling", live) == ([], "none"), "a name must match exactly"
    flip = [deal(918, "2909 7th st NE", "Listed", "2026-10-15T14:40:14Z"), deal(729, "2909 7th St NE", "Total Closed This Year", "2026-05-01T00:00:00Z")]
    hits, how = fds.pick_by_address("2909 7th st NE, 20017", flip)
    assert how == "address" and [d["id"] for d in hits] == [918], "bought, flipped, listed again: the live sale wins"
    assert fds.pick_by_address("2909 7th st NE", flip + [deal(950, "2909 7th St NE", "Pending", None)])[1] == "ambiguous", "two live deals are never guessed"
    assert fds.pick_by_address("2909 7th st NE", [deal(729, "2909 7th St NE", "Total Closed This Year", None), deal(730, "2909 7th St NE", "Terminated", None)])[1] == "ambiguous", "no live deal: not guessed"
    assert fds.pick_by_address("12 Main St NW", [deal(1, "12 Main Ave NW", "Listed", None), deal(2, "12 Main Rd NW", "Listed", None)])[1] == "ambiguous", "two loose hits are never guessed"
    s = fds.summarize(SELLERS[0], "address")
    assert s == {"found": True, "how": "address", "deal_id": 940, "pipeline": "Sellers",
                 "stage": "Listing App. Scheduled", "entered_stage_at": "2026-10-09T15:00:00Z", "name_is_address": True}
    assert not ({"price", "commissionValue", "people", "users", "name"} & set(s)), "nothing personal or financial"
    for n in ["6300 Stevenson Ave #1120", "1415 Riverside Ave.", "41 V st. NW", "7532 Coxton Ct, Unit G-127", "1926 Ruxton Rd", "12-14 Main St"]:
        assert fds.name_is_address(n), n
    for n in ["Abby Wenk", "Kristina Poplin", "Joseph D'Angelo", "Robert & Julie Brown", "Kylie & Brian Dowling", "", None, "  "]:
        assert not fds.name_is_address(n), n
    assert fds.summarize(SELLERS[3], "linked")["name_is_address"] is False, "a deal named after the seller is flagged"
    assert fds.stamped_deal_id({"subfolder_drive_ids": {"_fub_deal_id": 917}}) == "917"
    assert fds.stamped_deal_id({"fub_deal_id": "123", "subfolder_drive_ids": {}}) == "", "legacy column ignored"


def load_main():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")).read()
    for k in set(re.findall(r'os\.environ\["([A-Z0-9_]+)"\]', src)):
        os.environ.setdefault(k, "test")
    os.environ["SUPABASE_URL"] = "https://x.supabase.co"
    os.environ["SMOKE_ON_BOOT"] = "off"
    os.environ["SMOKE_GATE"] = "off"
    os.environ.setdefault("ANTHROPIC_API_KEY", "test")
    import main
    return main


class FakeQ:
    def __init__(self, rows):
        self.rows, self.f = rows, []

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
        self.rows = rows

    def table(self, name):
        assert name == "properties", name
        return FakeQ(self.rows)


def test_route(monkeypatch):
    from fastapi.testclient import TestClient
    main = load_main()
    props = [
        {"id": P_LINKED, "address": "Something typed differently", "subfolder_drive_ids": {"_fub_deal_id": "917"}},
        {"id": P_ADDRESS, "address": "7532 Coxton Ct, Unit G-127, Alexandria VA 22306", "subfolder_drive_ids": {}},
        {"id": P_TWO, "address": "12 Twin Way NW, Washington, DC", "subfolder_drive_ids": None},
        {"id": P_NONE, "address": "9 Nowhere Rd, Bethesda, MD", "fub_deal_id": "917", "subfolder_drive_ids": {}},
        {"id": P_GONE, "address": "7532 Coxton Ct Unit G-127", "subfolder_drive_ids": {"_fub_deal_id": "1"}},
        {"id": P_NAMED, "address": "Kylie & Brian Dowling", "subfolder_drive_ids": {}},
    ]
    monkeypatch.setattr(main, "supabase", FakeSB(props))
    calls = []

    async def fake_fub_get(path, params=None):
        calls.append((path, dict(params or {})))
        if path == "/pipelines":
            return {"pipelines": PIPES}
        if path == "/deals":
            assert params["pipelineId"] == 2
            if params.get("next"):
                return {"deals": SELLERS[2:], "_metadata": {"next": None}}
            return {"deals": SELLERS[:2], "_metadata": {"next": "PAGE2"}}
        if path == "/deals/917":
            return SELLERS[3]
        if path == "/deals/1":
            req = httpx.Request("GET", "https://api.followupboss.com/v1/deals/1")
            raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
        raise AssertionError(path)

    monkeypatch.setattr(main, "fub_get", fake_fub_get)
    main._FDS_CACHE.update(at=0.0, deals=[], pipeline_id=None)
    c = TestClient(main.app)

    assert c.get("/api/fub/listing-stage", params={"property_id": P_LINKED}).status_code == 403, "FORWARD OS pages only"
    assert c.get("/api/fub/listing-stage", params={"property_id": "x"}, headers=ORIGIN).status_code == 400
    assert c.get("/api/fub/listing-stage", params={"property_id": "66666666-6666-4666-8666-666666666666"}, headers=ORIGIN).status_code == 404

    r = c.get("/api/fub/listing-stage", params={"property_id": P_LINKED}, headers=ORIGIN).json()
    assert r["how"] == "linked" and r["stage"] == "Seller Engaged" and r["entered_stage_at"] == "2026-10-06T12:42:29Z", r
    assert r["name_is_address"] is False and "name" not in r, "says the name is a person's without sending the name"
    assert ("/deals", {"pipelineId": 2, "limit": 100}) not in calls, "a linked deal is read directly, fresh"

    r = c.get("/api/fub/listing-stage", params={"property_id": P_ADDRESS}, headers=ORIGIN).json()
    assert r["how"] == "address" and r["deal_id"] == 940 and r["stage"] == "Listing App. Scheduled", r
    assert sum(1 for p, q in calls if p == "/deals") == 2, "both pages of Sellers deals read"

    r = c.get("/api/fub/listing-stage", params={"property_id": P_TWO}, headers=ORIGIN).json()
    assert r == {"found": False, "how": "ambiguous", "count": 2}, r

    r = c.get("/api/fub/listing-stage", params={"property_id": P_NONE}, headers=ORIGIN).json()
    assert r == {"found": False, "how": "none", "count": 0}, "the legacy fub_deal_id column is not a FUB deal"

    r = c.get("/api/fub/listing-stage", params={"property_id": P_GONE}, headers=ORIGIN).json()
    assert r["how"] == "address" and r["deal_id"] == 940, "a stamped deal FUB no longer has falls back to the address"
    assert sum(1 for p, q in calls if p == "/deals") == 2, "the Sellers list is kept for 10 minutes"
    assert sum(1 for p, q in calls if p == "/pipelines") == 1

    r = c.get("/api/fub/listing-stage", params={"property_id": P_NAMED}, headers=ORIGIN).json()
    assert r["found"] is True and r["how"] == "name" and r["deal_id"] == 917 and r["name_is_address"] is False, \
        "a card named after the sellers finds its deal by name, through the route (missed in #67)"

    async def broken(path, params=None):
        raise RuntimeError("401 Unauthorized")
    monkeypatch.setattr(main, "fub_get", broken)
    main._FDS_CACHE.update(at=0.0, deals=[], pipeline_id=None)
    r = c.get("/api/fub/listing-stage", params={"property_id": P_ADDRESS}, headers=ORIGIN)
    assert r.status_code == 502 and "Follow Up Boss could not be read" in r.json()["detail"], r.text
