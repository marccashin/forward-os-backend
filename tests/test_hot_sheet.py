"""Hot sheet rules (hot_sheet.py) and the /api/hot-sheet route through the real app."""
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hot_sheet as hs

ET = ZoneInfo("America/New_York")
SAT_OCT17 = datetime(2026, 10, 17, 17, 0, tzinfo=timezone.utc)   # Saturday 1pm ET
MON_OCT19 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)   # Monday 8am ET


def et(y, m, d, h=10):
    return datetime(y, m, d, h, tzinfo=ET).astimezone(timezone.utc).isoformat()


# Independent business-day count: whole weeks plus a remainder, no day-by-day loop.
def ref_bdays(start: date, end: date) -> int:
    if end <= start:
        return 0
    total = (end - start).days
    weeks, rem = divmod(total, 7)
    n = weeks * 5
    wd = start.weekday()
    for k in range(1, rem + 1):
        if (wd + k) % 7 < 5:
            n += 1
    return n


def test_business_days_match_independent_count_over_a_year():
    now = datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc)
    for offset in range(0, 400):
        start = date(2026, 1, 1) + timedelta(days=offset)
        for end_off in range(0, 15):
            end = start + timedelta(days=end_off)
            now = datetime(end.year, end.month, end.day, 15, 0, tzinfo=ET)
            got = hs.business_days_open(datetime(start.year, start.month, start.day, 9, 0, tzinfo=ET), now)
            assert got == ref_bdays(start, end), (start, end, got)


def test_business_days_examples():
    # Opened Monday: Thursday is 3. Opened Friday: Monday is 1. Opened Wednesday: Saturday is 2.
    assert hs.business_days_open(et(2026, 10, 12), datetime(2026, 10, 15, 14, tzinfo=timezone.utc)) == 3
    assert hs.business_days_open(et(2026, 10, 16), MON_OCT19) == 1
    assert hs.business_days_open(et(2026, 10, 14), SAT_OCT17) == 2
    assert hs.business_days_open(None, SAT_OCT17) is None
    assert hs.business_days_open("not a date", SAT_OCT17) is None


def test_eastern_day_boundary():
    # 11pm ET Monday is 3am UTC Tuesday: still Monday's item in Eastern time.
    late_monday = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc).isoformat()
    thursday = datetime(2026, 10, 15, 14, tzinfo=timezone.utc)
    assert hs.business_days_open(late_monday, thursday) == 3


def L(id, status="active", address=None):
    return {"id": id, "address": address or f"{id} Main St", "agent": "Niki Lang", "stage": "Listed", "status": status}


def LI(lid, title, created, completed=False, owner="concierge"):
    return {"listing_id": lid, "title": title, "completed": completed, "owner": owner, "stage": "Listed", "created_at": created}


def test_concierge_rules():
    listings = [L("a"), L("b", status="archived")]
    items = [
        LI("a", "Old item", et(2026, 10, 12)),                       # Mon -> Mon 19 = 5
        LI("a", "Fresh item", et(2026, 10, 16)),                     # Fri -> Mon = 1, out
        LI("a", "Two day item", et(2026, 10, 15)),                   # Thu -> Mon = 2, out (the line)
        LI("a", "Three day item", et(2026, 10, 14)),                 # Wed -> Mon = 3, in (the line)
        LI("a", "Done item", et(2026, 10, 1), completed=True),       # ticked, out
        LI("a", "Agent item", et(2026, 10, 1), owner="agent"),       # agent's, out
        LI("a", "All contingencies cleared", et(2026, 10, 1)),       # waiting, out
        LI("a", "  Closing gift ready in office ", et(2026, 10, 1)), # waiting with spaces, out
        LI("a", "No date item", None),                               # kept, unknown date
        LI("b", "Archived listing item", et(2026, 10, 1)),           # listing not active, out
        LI("a", "Owner blank item", et(2026, 10, 1), owner=None),    # Concierge's by default
    ]
    deals = [
        {"id": 1, "client_name": "Jane Buyer", "agent_name": "Cesar Rivera", "pipeline": "buyer", "stage": "BA Signed", "closed_at": None},
        {"id": 2, "client_name": "Done Buyer", "agent_name": "x", "pipeline": "buyer", "stage": "Total Closed This Year", "closed_at": None},
        {"id": 3, "client_name": "Closed Buyer", "agent_name": "x", "pipeline": "buyer", "stage": "Pending", "closed_at": "2026-10-01"},
        {"id": 4, "client_name": "Seller deal", "agent_name": "x", "pipeline": "seller", "stage": "Listed", "closed_at": None},
        {"id": 5, "client_name": "No stage", "agent_name": "x", "pipeline": "buyer", "stage": None, "closed_at": None},
        {"id": 6, "client_name": "Terminated", "agent_name": "x", "pipeline": "buyer", "stage": "Terminated", "closed_at": None},
    ]
    tasks = [
        {"deal_id": 1, "title": "Send Welcome", "owner": "concierge", "completed": False, "stage_triggered": "BA Signed", "created_at": et(2026, 10, 7)},
        {"deal_id": 1, "title": "Agent task", "owner": "agent", "completed": False, "stage_triggered": "BA Signed", "created_at": et(2026, 10, 7)},
    ] + [{"deal_id": d, "title": "T", "owner": "concierge", "completed": False, "stage_triggered": "x", "created_at": et(2026, 10, 1)} for d in (2, 3, 4, 5, 6)]
    s = hs.concierge_sheet(listings, items, deals, tasks, MON_OCT19)
    titles = sorted(i["title"] for g in s["groups"] for i in g["items"])
    assert titles == ["No date item", "Old item", "Owner blank item", "Send Welcome", "Three day item"], titles
    assert s["count"] == 5
    names = {g["name"] for g in s["groups"]}
    assert names == {"a Main St", "Jane Buyer"}


def T(pid, tid, pos, created, completed=False, removed=None):
    return {"property_id": pid, "task_id": tid, "title": f"Task {tid}", "position": pos,
            "otc_created": created, "completed": completed, "removed_at": removed}


def test_transactions_next_four_only():
    props = [{"id": 1, "address": "138 U St NE", "status": "Listing - Under Contract"},
             {"id": 2, "address": "Closed Rd", "status": "Closed"},
             {"id": 3, "address": "Cancelled Ave", "status": "Transaction Cancelled"},
             {"id": 4, "address": "New Deal Ct", "status": "Listing- Active"}]
    old = et(2026, 9, 1)
    tasks = [T(1, k, k, old) for k in range(1, 45)]
    tasks[0]["completed"] = True                      # task 1 done
    tasks[2]["removed_at"] = "2026-10-10T00:00:00Z"   # task 3 gone from Open To Close
    tasks += [T(2, 100, 1, old), T(3, 200, 1, old)]
    tasks += [T(4, 300, 1, et(2026, 10, 16)), T(4, 301, 2, et(2026, 10, 16))]   # 1 business day
    s = hs.transactions_sheet(props, tasks, MON_OCT19)
    assert [g["name"] for g in s["groups"]] == ["138 U St NE"]
    assert [i["title"] for i in s["groups"][0]["items"]] == ["Task 2", "Task 4", "Task 5", "Task 6"]
    assert s["count"] == 4


def test_transactions_next_four_counted_before_age_filter():
    # Next 4 are chosen first, then flagged by age: a young first task does not pull in task 5.
    props = [{"id": 1, "address": "X", "status": "Open"}]
    tasks = [T(1, 1, 1, et(2026, 10, 16))] + [T(1, k, k, et(2026, 9, 1)) for k in range(2, 8)]
    s = hs.transactions_sheet(props, tasks, MON_OCT19)
    assert [i["title"] for i in s["groups"][0]["items"]] == ["Task 2", "Task 3", "Task 4"]


def test_render():
    s = hs.concierge_sheet([L("a", address="1 <b>Main</b> St")], [LI("a", "Order & sign", et(2026, 10, 12))], [], [], MON_OCT19)
    out = hs.render(s, MON_OCT19)
    assert out["subject"] == "Concierge hot sheet, Mon Oct 19: 1 item open 3+ business days"
    assert "1 &lt;b&gt;Main&lt;/b&gt; St" in out["html"] and "<b>Main</b>" not in out["html"]
    assert "Order &amp; sign" in out["html"] and "open 5 business days" in out["text"]
    assert "tick each item in the Command Center" in out["html"]
    assert "—" not in out["html"] + out["text"] + out["subject"]
    empty = hs.render(hs.transactions_sheet([], [], MON_OCT19), MON_OCT19)
    assert empty["subject"] == "Transactions hot sheet, Mon Oct 19: nothing open 3+ business days"
    assert "tick each task in Open To Close" in empty["text"] and empty["count"] == 0


def test_waiting_titles_match_command_center():
    path = os.environ.get("CC_CONCIERGE_TASKS")
    if not path or not os.path.exists(path):
        return
    src = open(path).read()
    found = set(re.findall(r"title:\s*'((?:[^'\\]|\\.)*)'[^}\n]*waiting:\s*true", src))
    assert found == set(hs.WAITING_TITLES), found


# ── The route, through the real app ──────────────────────────────────────────

from fastapi.testclient import TestClient


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


class FakeQuery:
    def __init__(self, db, name, log):
        self.db, self.name, self.f, self.log, self.rng = db, name, [], log, None

    def select(self, *_a, **_k):
        return self

    def eq(self, k, v):
        self.f.append((k, v)); return self

    def range(self, a, b):
        self.rng = (a, b); return self

    def execute(self):
        rows = [r for r in self.db.get(self.name, []) if all(r.get(k) == v for k, v in self.f)]
        a, b = self.rng
        self.log.append((self.name, a, b))
        return type("R", (), {"data": rows[a:b + 1]})


class FakeSupabase:
    def __init__(self, db):
        self.db, self.log = db, []

    def table(self, name):
        return FakeQuery(self.db, name, self.log)


def test_route():
    m = load_main()
    many = [LI("a", f"Item {k}", et(2026, 9, 1)) for k in range(2500)]   # crosses two page edges
    fake = FakeSupabase({"listings": [L("a")], "listing_checklist_items": many, "deals": [], "deal_tasks": [],
                         "otc_properties": [{"id": 1, "address": "138 U St NE", "status": "Open"}],
                         "otc_tasks": [T(1, k, k, et(2026, 9, 1)) for k in range(1, 10)]})
    m.supabase = fake
    c = TestClient(m.app)
    os.environ.pop("HOT_SHEET_SECRET", None)
    assert c.get("/api/hot-sheet/concierge").status_code == 503
    os.environ["HOT_SHEET_SECRET"] = "s3cret"
    assert c.get("/api/hot-sheet/concierge").status_code == 403
    assert c.get("/api/hot-sheet/concierge", headers={"x-hot-sheet-secret": "wrong"}).status_code == 403
    assert c.get("/api/hot-sheet/agents", headers={"x-hot-sheet-secret": "s3cret"}).status_code == 404
    r = c.get("/api/hot-sheet/concierge", headers={"x-hot-sheet-secret": "s3cret"})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2500, r.json()["count"]
    assert ("listing_checklist_items", 2000, 2999) in fake.log
    r = c.get("/api/hot-sheet/transactions", headers={"x-hot-sheet-secret": "s3cret"})
    assert r.status_code == 200 and r.json()["count"] == 4
    assert set(r.json()) == {"team", "subject", "html", "text", "count"}

    class Boom:
        def table(self, name):
            raise RuntimeError("database down")
    m.supabase = Boom()
    r = c.get("/api/hot-sheet/transactions", headers={"x-hot-sheet-secret": "s3cret"})
    assert r.status_code == 502 and "could not be built" in r.text
    m.supabase = fake
