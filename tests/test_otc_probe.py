"""otc_probe: read only, token never leaks, one call per second, real shape reported."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import otc_probe as otc

TOKEN = "SECRETTOKEN123"


class FakeOtc:
    def __init__(self, routes, raise_on=None):
        self.routes = routes          # path -> (status, obj)
        self.seen = []                # (method, url)
        self.raise_on = raise_on

    async def __call__(self, method, url):
        self.seen.append((method, url))
        path = url.split("https://api.opentoclose.com", 1)[1].split("?", 1)[0]
        if self.raise_on and self.raise_on in path:
            raise RuntimeError("Connection reset for url " + url)
        st, obj = self.routes.get(path, (404, {"error": "not found"}))
        return st, json.dumps(obj).encode()


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


def client(fake, clock=None):
    clock = clock or Clock()
    return otc.OtcClient(TOKEN, fake, sleep=clock.sleep, clock=clock.now), clock


def run(c):
    return asyncio.get_event_loop().run_until_complete(c) if False else asyncio.run(c)


def prop(pid, title, status="Active"):
    return [{"id": pid, "created": "2026-10-01 10:00:00", "field_values": [
        {"key": "contract_title", "value": title}, {"key": "contract_status", "value": status},
        {"key": "contract_client_type", "value": "Seller"}]}]


def test_list_recent_reads_titles_and_only_gets():
    fake = FakeOtc({
        "/v1.1/properties": (200, [{"id": 7, "created": "x", "agent_name": "Niki Lang"},
                                   {"id": 6, "created": "y", "agent_name": "Marc Cashin"}]),
        "/v1.1/properties/7": (200, prop(7, "1 Main St")),
        "/v1.1/properties/6": (200, prop(6, "2 Oak Ave", "Pending")),
    })
    c, _ = client(fake)
    res = run(otc.list_recent(c))
    assert [p["title"] for p in res["properties"]] == ["1 Main St", "2 Oak Ave"]
    assert res["properties"][1]["status"] == "Pending"
    assert res["sorted_newest_first"] is True
    assert all(m == "GET" for m, _ in fake.seen)
    assert TOKEN not in json.dumps(res)


def test_sort_refused_falls_back_once():
    calls = {"n": 0}

    async def http(method, url):
        path = url.split("opentoclose.com", 1)[1].split("?", 1)[0]
        if path == "/v1.1/properties":
            calls["n"] += 1
            if "sort_by" in url:
                return 400, b'{"error":"bad sort"}'
            return 200, b'[{"id": 1}]'
        return 200, json.dumps(prop(1, "A")).encode()
    c, _ = client(http)
    res = run(otc.list_recent(c))
    assert calls["n"] == 2 and res["sorted_newest_first"] is False
    assert res["properties"][0]["title"] == "A"


def test_tasks_shape_completed_values_and_detail():
    tasks = [
        {"id": 1, "title": "Order HOA docs", "completed": "2026-10-10 14:03:00", "property": {"id": 5}},
        {"id": 2, "title": "Collect disclosures", "completed": None, "critical": True, "property": {"id": 5}},
    ]
    fake = FakeOtc({
        "/v1/properties/5/tasks": (200, tasks),
        "/v1/properties/5/tasks/1": (200, [dict(tasks[0], completed_by="Sofia Burton")]),
        "/v1/properties/5/tasks/2": (200, [tasks[1]]),
    })
    c, _ = client(fake)
    res = run(otc.probe_tasks(c, 5))
    assert res["ok"] and res["count"] == 2 and res["completed_count"] == 1
    assert res["keys"]["completed"] == ["null", "str"]
    assert "completed_by" in res["detail_keys"]
    assert res["detail"]["completed_example"]["completed_by"] == "Sofia Burton"
    assert all(m == "GET" for m, _ in fake.seen)


def test_tasks_falls_back_to_v11_and_reports_attempts():
    fake = FakeOtc({"/v1.1/properties/5/tasks": (200, [])})
    c, _ = client(fake)
    res = run(otc.probe_tasks(c, 5))
    assert res["ok"] and res["endpoint"] == "/v1.1/properties/:id/tasks"
    assert res["attempts"][0]["status"] == 404


def test_tasks_both_fail_is_not_ok():
    c, _ = client(FakeOtc({}))
    res = run(otc.probe_tasks(c, 9))
    assert res["ok"] is False and len(res["attempts"]) == 2


def test_token_never_in_errors_even_when_http_raises_with_url():
    fake = FakeOtc({}, raise_on="/v1.1/properties")
    c, _ = client(fake)
    try:
        run(otc.list_recent(c))
        assert False, "should raise"
    except otc.OtcError as e:
        assert TOKEN not in str(e)
        assert "api_token=***" in str(e)


def test_token_scrubbed_from_api_error_text():
    fake = FakeOtc({"/v1.1/properties": (401, {"error": "token " + TOKEN + " is invalid"})})
    c, _ = client(fake)
    try:
        run(otc.list_recent(c))
        assert False
    except otc.OtcError as e:
        assert TOKEN not in str(e) and "401" in str(e)


def test_waits_between_calls():
    fake = FakeOtc({
        "/v1.1/properties": (200, [{"id": 1}, {"id": 2}]),
        "/v1.1/properties/1": (200, prop(1, "A")),
        "/v1.1/properties/2": (200, prop(2, "B")),
    })
    clock = Clock()
    c, _ = client(fake, clock)
    run(otc.list_recent(c))
    assert len(fake.seen) == 3
    assert len(clock.slept) == 2 and all(s >= 1.0 for s in clock.slept)


def test_token_sent_as_api_token_query_param():
    fake = FakeOtc({"/v1.1/properties": (200, [])})
    c, _ = client(fake)
    run(otc.list_recent(c))
    assert "api_token=" + TOKEN in fake.seen[0][1]


def test_no_token_refuses_without_calling():
    fake = FakeOtc({})
    c = otc.OtcClient("", fake)
    try:
        run(c.get("/v1.1/properties"))
        assert False
    except otc.OtcError:
        pass
    assert fake.seen == []
