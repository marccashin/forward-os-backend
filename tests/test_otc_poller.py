"""otc_poller: tick times are first-seen, never invented; removals only on complete lists."""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import otc_poller as op
from otc_probe import OtcClient

TOKEN = "SECRETTOKEN123"
T0 = datetime(2026, 10, 17, 13, 0, tzinfo=timezone.utc)


def task(i, done=False, title=None):
    return {"id": i, "created": {"date": "2026-10-08 12:40:40.000000", "timezone_type": 3, "timezone": "UTC"},
            "title": title or f"Task {i}", "private": None, "pinned": None, "critical": None,
            "completed": True if done else None, "property": {"id": 1}}


def prop(i, status="Listing - Under Contract", addr="138 U Street Northeast, Washington, DC, 20002"):
    return {"id": i, "address": addr, "contract_status": status, "contract_client_type": "Seller",
            "agent_name": "Marc Cashin", "team_user_name": "Sofia Burton", "created": "2026-10-08 12:39:59"}


class FakeOtc:
    """Routes by path; honours limit/offset unless ignore_offset."""
    def __init__(self, routes, ignore_offset=False, fail=None):
        self.routes, self.ignore_offset, self.fail = routes, ignore_offset, fail or set()
        self.seen = []

    async def __call__(self, method, url):
        self.seen.append((method, url))
        path, _, qs = url.split("https://api.opentoclose.com", 1)[1].partition("?")
        q = dict(kv.split("=", 1) for kv in qs.split("&") if "=" in kv)
        if path in self.fail:
            return 500, json.dumps({"error": "boom"}).encode()
        if path not in self.routes:
            return 404, json.dumps({"error": "not found"}).encode()
        rows = self.routes[path]
        lim = int(q.get("limit", 100))
        off = 0 if self.ignore_offset else int(q.get("offset", 0))
        return 200, json.dumps(rows[off:off + lim]).encode()


class Store:
    def __init__(self):
        self.p, self.t = {}, {}

    def properties(self):
        return {k: dict(v) for k, v in self.p.items()}

    def tasks(self, pid):
        return [dict(v) for (p, _), v in self.t.items() if p == pid]

    def upsert_tasks(self, rows):
        keys = {tuple(sorted(r)) for r in rows}
        assert len(keys) <= 1, "bulk upsert rows must share keys"
        for r in rows:
            self.t[(r["property_id"], r["task_id"])] = dict(r)

    def upsert_properties(self, rows):
        for r in rows:
            self.p[r["id"]] = dict(r)


async def _nosleep(s):
    return None


def run(fake, store, now):
    c = OtcClient(TOKEN, fake, sleep=_nosleep)
    return asyncio.run(op.poll(c, store, now)), c


passed = failed = 0


def ok(cond, msg):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL", msg)


def _all():
    routes = {"/v1.1/properties": [prop(1471375), prop(1438421, "Closed"), prop(1239310, "Transaction Cancelled")],
              "/v1/properties/1471375/tasks": [task(1, True), task(2), task(3)]}
    fake, st = FakeOtc(routes), Store()
    r, c = run(fake, st, T0)
    ok(r["ok"] and r["read"] == [1471375] and r["skipped"] == 2, "first poll reads only the open deal")
    t1 = st.t[(1471375, 1)]
    ok(t1["completed"] and t1["completed_before_tracking"] and t1["completed_seen_at"] is None,
       "already ticked on first read: no invented date")
    ok(st.p[1471375]["task_count"] == 3 and st.p[1471375]["done_count"] == 1, "counts stored")
    ok(st.t[(1471375, 2)]["otc_created"] == "2026-10-08T12:40:40+00:00", "Open To Close created time parsed as UTC")
    ok(all(m == "GET" for m, _ in fake.seen), "GET only")
    ok(all(TOKEN not in json.dumps(r) for _ in [0]), "token never in the run report")
    ok(not any("/1438421/" in u or "/1239310/" in u for _, u in fake.seen), "closed and cancelled not read")

    # Sofia ticks task 2; task 3 renamed
    routes["/v1/properties/1471375/tasks"] = [task(1, True), task(2, True), task(3, title="Renamed")]
    T1 = T0 + timedelta(minutes=15)
    r, _ = run(fake, st, T1)
    t2 = st.t[(1471375, 2)]
    ok(t2["completed_seen_at"] == T1.isoformat() and not t2["completed_before_tracking"], "tick stamped with the poll that saw it")
    ok(st.t[(1471375, 1)]["completed_before_tracking"] and st.t[(1471375, 1)]["completed_seen_at"] is None,
       "earlier unknown tick stays unknown")
    ok(st.t[(1471375, 3)]["title"] == "Renamed", "title change kept")
    ok([x["change"] for x in r["changes"]] == ["ticked"], "one change logged: " + json.dumps(r["changes"]))
    ok(st.t[(1471375, 2)]["first_seen_at"] == T0.isoformat(), "first_seen_at kept")

    # Same state again: nothing moves
    T2 = T1 + timedelta(minutes=15)
    r, _ = run(fake, st, T2)
    ok(r["changes"] == [] and st.t[(1471375, 2)]["completed_seen_at"] == T1.isoformat(), "no change, tick time unchanged")
    ok(st.t[(1471375, 2)]["last_seen_at"] == T2.isoformat(), "last_seen moves")

    # Untick 2, remove 3, add 4 already ticked
    routes["/v1/properties/1471375/tasks"] = [task(1, True), task(2), task(4, True)]
    T3 = T2 + timedelta(minutes=15)
    r, _ = run(fake, st, T3)
    t2 = st.t[(1471375, 2)]
    ok(not t2["completed"] and t2["completed_seen_at"] is None and t2["reopened_at"] == T3.isoformat(), "untick clears time, records reopened")
    ok(st.t[(1471375, 3)]["removed_at"] == T3.isoformat(), "removed task marked")
    t4 = st.t[(1471375, 4)]
    ok(t4["completed_seen_at"] == T3.isoformat() and not t4["completed_before_tracking"],
       "new task already ticked on a known deal: seen now (not before tracking)")
    ok(st.p[1471375]["task_count"] == 3 and st.p[1471375]["done_count"] == 2, "counts exclude removed")
    ok(sorted(x["change"] for x in r["changes"]) == ["removed", "ticked", "unticked"], "changes: " + json.dumps(r["changes"]))

    # Removed task comes back
    routes["/v1/properties/1471375/tasks"] = [task(1, True), task(2), task(3), task(4, True)]
    T4 = T3 + timedelta(minutes=15)
    r, _ = run(fake, st, T4)
    ok(st.t[(1471375, 3)]["removed_at"] is None and st.t[(1471375, 3)]["first_seen_at"] == T0.isoformat(), "returning task restored")

    # Deal closes: read once more, then not again
    routes["/v1.1/properties"][0] = prop(1471375, "Closed")
    routes["/v1/properties/1471375/tasks"] = [task(1, True), task(2, True), task(3), task(4, True)]
    T5 = T4 + timedelta(minutes=15)
    n0 = len(fake.seen)
    r, _ = run(fake, st, T5)
    ok(1471375 in r["read"] and st.t[(1471375, 2)]["completed_seen_at"] == T5.isoformat(), "closing poll keeps the last ticks")
    r, _ = run(fake, st, T5 + timedelta(minutes=15))
    ok(r["read"] == [] and st.p[1471375]["status"] == "Closed", "closed deal no longer read")

    # One deal's tasks fail: others still read, nothing removed, run not ok
    routes2 = {"/v1.1/properties": [prop(10), prop(11)],
               "/v1/properties/10/tasks": [task(1)], "/v1/properties/11/tasks": [task(5)]}
    fake2, st2 = FakeOtc(routes2), Store()
    run(fake2, st2, T0)
    fake2.fail = {"/v1/properties/10/tasks"}
    r, _ = run(fake2, st2, T1)
    ok(not r["ok"] and r["read"] == [11] and r["errors"][0]["property_id"] == 10, "one failure reported, others read")
    ok(st2.t[(10, 1)]["removed_at"] is None, "failed read removes nothing")
    ok(TOKEN not in json.dumps(r), "token not in error text")

    # Listing fails entirely
    fake2.fail = {"/v1.1/properties"}
    r, _ = run(fake2, st2, T2)
    ok(not r["ok"] and r["properties"] == 0 and r["errors"], "list failure reported")

    # Paging: 150 tasks, offset honoured -> all read
    many = [task(i) for i in range(1, 151)]
    routes3 = {"/v1.1/properties": [prop(20)], "/v1/properties/20/tasks": many}
    st3 = Store()
    r, _ = run(FakeOtc(routes3), st3, T0)
    ok(r["ok"] and st3.p[20]["task_count"] == 150, "150 tasks read across pages")
    # Offset ignored by the API -> refuse, and do not mark the rest removed
    routes3["/v1/properties/20/tasks"] = many[:100] + [task(999)]
    r, _ = run(FakeOtc(routes3, ignore_offset=True), st3, T1)
    ok(not r["ok"] and all(v["removed_at"] is None for v in st3.t.values()), "unconfirmed paging: error, nothing removed")
    ok("ignored the offset" in r["errors"][0]["error"], "paging error says why: " + r["errors"][0]["error"])

    # Unknown status counts as open
    ok(op.is_open(None) and op.is_open("") and op.is_open("Listing- Active") and not op.is_open("Transaction Cancelled"), "is_open")


if __name__ == "__main__":
    _all()
    print(f"{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


def test_pytest_entry():
    global passed, failed
    passed = failed = 0
    _all()
    assert failed == 0
