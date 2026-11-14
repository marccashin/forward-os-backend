"""The /api/otc/poll route and job wiring, through the real app, with Open To Close and Supabase replaced."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient

TOKEN = "SECRETTOKEN123"


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


class FakeTable:
    def __init__(self, db, name):
        self.db, self.name, self.filters, self.op, self.rows = db, name, [], "select", None

    def select(self, *_a, **_k):
        self.op = "select"; return self

    def eq(self, k, v):
        self.filters.append((k, v)); return self

    def upsert(self, rows, on_conflict=None):
        self.op, self.rows, self.keys = "upsert", rows, on_conflict.split(","); return self

    def execute(self):
        t = self.db.setdefault(self.name, {})
        if self.op == "upsert":
            for r in self.rows:
                t[tuple(r[k] for k in self.keys)] = dict(r)
            return type("R", (), {"data": self.rows})
        rows = [dict(r) for r in t.values() if all(r.get(k) == v for k, v in self.filters)]
        return type("R", (), {"data": rows})


class FakeSupabase:
    def __init__(self):
        self.db = {}

    def table(self, name):
        return FakeTable(self.db, name)


def otc_http(routes):
    seen = []

    async def http(method, url):
        seen.append((method, url))
        path = url.split("https://api.opentoclose.com", 1)[1].split("?", 1)[0]
        if path not in routes:
            return 404, b'{"error":"nope"}'
        return 200, json.dumps(routes[path]).encode()
    return http, seen


def test_poll_route(monkeypatch):
    main = load_main()
    fake = FakeSupabase()
    monkeypatch.setattr(main, "supabase", fake)
    monkeypatch.setenv("OTC_API_TOKEN", TOKEN)
    routes = {
        "/v1.1/properties": [{"id": 1471375, "address": "138 U Street Northeast", "contract_status": "Listing - Under Contract",
                              "contract_client_type": "Seller", "agent_name": None, "team_user_name": "Sofia", "created": "2026-10-08 12:39:59"},
                             {"id": 1438421, "address": "1926 Ruxton Road", "contract_status": "Closed",
                              "contract_client_type": "Seller", "agent_name": None, "team_user_name": "Sofia", "created": "2026-09-03 10:00:00"}],
        "/v1/properties/1471375/tasks": [
            {"id": 1, "created": {"date": "2026-10-08 12:40:40.000000", "timezone": "UTC"}, "title": "A", "completed": True},
            {"id": 2, "created": {"date": "2026-10-08 12:40:40.000000", "timezone": "UTC"}, "title": "B", "completed": None}],
    }
    http, seen = otc_http(routes)
    monkeypatch.setattr(main, "_otc_http", http)
    main._OTCP_STATE.update({"running": False, "last_run": None, "fail_streak": 0, "last_manual_at": 0.0})
    c = TestClient(main.app)

    r = c.post("/api/otc/poll")
    assert r.status_code == 403, "no FORWARD OS origin: refused"

    r = c.post("/api/otc/poll", headers={"Origin": "https://forward-os.netlify.app"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["read"] == [1471375] and body["skipped"] == 1, body
    assert TOKEN not in r.text
    assert all(m == "GET" for m, _ in seen)
    assert fake.db["otc_tasks"][(1471375, 1)]["completed_before_tracking"] is True
    assert fake.db["otc_properties"][(1471375,)]["done_count"] == 1

    r = c.post("/api/otc/poll", headers={"Origin": "https://forward-os.netlify.app"})
    assert r.status_code == 429, "once a minute"

    s = c.get("/api/otc/poll/status").json()
    assert s["last_run"]["trigger"] == "manual" and s["fail_streak"] == 0

    # A scheduled run that fails three times emails once
    sent = []
    monkeypatch.setattr(main, "_otcp_email", lambda run: sent.append(run))
    routes.pop("/v1.1/properties")
    import asyncio
    for _ in range(3):
        asyncio.run(main.job_otc_poll())
    s = c.get("/api/otc/poll/status").json()
    assert s["fail_streak"] == 3 and len(sent) == 1, (s, len(sent))
    assert fake.db["otc_tasks"][(1471375, 2)]["removed_at"] is None, "a failed list removes nothing"


def test_check_now_wait_and_join(monkeypatch):
    """Oct 19, 2026: the OS shows a shared countdown, and a press during a running pass joins it."""
    import asyncio
    main = load_main()
    fake = FakeSupabase()
    monkeypatch.setattr(main, "supabase", fake)
    monkeypatch.setenv("OTC_API_TOKEN", TOKEN)
    routes = {"/v1.1/properties": [{"id": 7, "address": "1 A St", "contract_status": "Listing- Active",
                                    "contract_client_type": "Seller", "created": "2026-10-19 10:00:00"}],
              "/v1/properties/7/tasks": [{"id": 1, "created": {"date": "2026-10-19 10:00:00.000000", "timezone": "UTC"}, "title": "A", "completed": None}]}
    http, seen = otc_http(routes)
    monkeypatch.setattr(main, "_otc_http", http)
    main._OTCP_STATE.update({"running": False, "last_run": None, "fail_streak": 0, "last_manual_at": 0.0})
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(main._time, "time", lambda: clock["t"])
    c = TestClient(main.app)
    O = {"Origin": "https://forward-os.netlify.app"}

    s = c.get("/api/otc/poll/status").json()
    assert s["manual_wait_seconds"] == 0 and s["manual_gap_seconds"] == 60 and s["manual_last_at"] is None, s

    r = c.post("/api/otc/poll", headers=O)
    assert r.status_code == 200 and r.json()["ok"] and r.json()["manual_wait_seconds"] == 60, r.text

    # Another agent, 20 seconds later, sees 40 seconds left (from the server's clock)
    clock["t"] += 20
    s = c.get("/api/otc/poll/status").json()
    assert s["manual_wait_seconds"] == 40 and s["manual_last_at"], s
    r = c.post("/api/otc/poll", headers=O)
    assert r.status_code == 429 and r.headers.get("Retry-After") == "40" and "40 seconds" in r.json()["detail"], (r.status_code, r.text)
    calls = len(seen)

    # 19.5 s later: rounds up, never says 0 while still refused
    clock["t"] += 39.5
    assert c.get("/api/otc/poll/status").json()["manual_wait_seconds"] == 1
    clock["t"] += 0.5
    assert c.get("/api/otc/poll/status").json()["manual_wait_seconds"] == 0

    # A press while a pass is running waits for it, returns its result, starts nothing new,
    # and does not use up the minute
    main._OTCP_STATE["running"] = True
    main._OTCP_STATE["last_run"] = {"ok": True, "trigger": "scheduled", "read": [7], "changes": []}
    async def finish():
        await asyncio.sleep(1.2)
        main._OTCP_STATE["running"] = False
    async def press():
        import httpx
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            fin = asyncio.create_task(finish())
            resp = await ac.post("/api/otc/poll", headers=O)
            await fin
            return resp
    before = main._OTCP_STATE["last_manual_at"]
    resp = asyncio.run(press())
    j = resp.json()
    assert resp.status_code == 200 and j["joined"] is True and j["trigger"] == "scheduled" and j["read"] == [7], j
    assert len(seen) == calls, "joining a running pass reads Open To Close no more"
    assert main._OTCP_STATE["last_manual_at"] == before, "joining does not start the one-minute wait"
    assert c.get("/api/otc/poll/status").json()["manual_wait_seconds"] == 0
    assert TOKEN not in resp.text

    # Still refused without a FORWARD OS page, even while running
    main._OTCP_STATE["running"] = True
    assert c.post("/api/otc/poll").status_code == 403
    main._OTCP_STATE["running"] = False
