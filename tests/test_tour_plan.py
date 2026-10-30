"""Showing tour plans: listing lookup, drive order and times, maps, comparisons.
The pure rules against independent checks, then the real routes with Google and the AI
replaced. No network."""
import asyncio
import itertools
import json
import os
import random
import re
import sys
from datetime import datetime
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tour_plan as tp  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
NOW = datetime(2026, 10, 30, 8, 0, tzinfo=tp.ET)


def run(c):
    return asyncio.run(c)


# ── text checks ─────────────────────────────────────────────────────────────────
def test_text_issues():
    assert tp.text_issues("A bright two-level home with a renovated kitchen and a deck.") == []
    assert any("stunning" in i for i in tp.text_issues("A Stunning home."))
    assert any("long dash" in i for i in tp.text_issues("Open plan — bright"))
    for who in ("Great for families.", "Near top schools.", "Ideal for young professionals.", "Kids will love the yard."):
        assert any("fair housing" in i for i in tp.text_issues(who)), who
    assert tp.text_issues("Close to the Metro and Rock Creek Park.") == []
    # Marc, Oct 30: facts only, no negotiating or value commentary
    for adv in ("It has been on the market the longest, which may leave room to negotiate.",
                "The best value on the tour.", "Priced to sell.", "A motivated seller.", "Expect a bidding war."):
        assert any("facts only" in i for i in tp.text_issues(adv)), adv
    for fact in ("On the market 22 days, the longest of the four.", "The lowest price on the tour.",
                 "The newest listing on the tour.", "An open main level with room to entertain."):
        assert tp.text_issues(fact) == [], fact


# ── address matching ────────────────────────────────────────────────────────────
def test_address_problem():
    assert tp.address_problem("4200 Massachusetts Ave NW #106, Washington, DC 20016",
                              "4200 Massachusetts Ave NW Unit 106, Washington, DC 20016") == ""
    assert "not unit 106" in tp.address_problem("4200 Massachusetts Ave NW #106, Washington, DC", "4200 Massachusetts Ave NW #506")
    assert "not unit" in tp.address_problem("4200 Massachusetts Ave NW #106", "4200 Massachusetts Ave NW"), "a building page is not the unit"
    assert "street number" in tp.address_problem("219 N Pitt St, Alexandria, VA", "291 N Pitt St, Alexandria, VA")
    assert tp.address_problem("219 N Pitt St", "") != ""
    assert tp.address_problem("12 Oak Ct Apt 0012", "12 Oak Ct #12") == "", "leading zeros in a unit"


# ── lookup ──────────────────────────────────────────────────────────────────────
def raw_listing(**over):
    r = {"matched_address": "219 N Pitt St, Alexandria, VA 22314", "status": "Active",
         "list_price": [{"value": "$1,495,000", "source_url": "https://www.realtor.com/a"},
                        {"value": "$1,495,000", "source_url": "https://www.homes.com/b"}],
         "beds": [{"value": "4", "source_url": "https://www.realtor.com/a"}],
         "baths_full": [{"value": "3", "source_url": "https://www.realtor.com/a"}],
         "baths_half": [{"value": "1", "source_url": "https://www.realtor.com/a"}],
         "sqft": [{"value": "2,610", "source_url": "https://www.realtor.com/a"},
                  {"value": "2,480", "source_url": "https://www.redfin.com/c"}],
         "days_on_market": [{"value": "12", "source_url": "https://www.zillow.com/z"},
                            {"value": "9", "source_url": "https://www.homes.com/b"}],
         "mls_number": [{"value": "VAAX2051234", "source_url": "not a link"}],
         "overview": "A brick federal townhouse in Old Town with a walled garden and a renovated kitchen.",
         "overview_source_url": "https://www.realtor.com/a"}
    r.update(over)
    return r


def test_clean_lookup():
    o = tp.clean_lookup(raw_listing(), "219 N Pitt St, Alexandria, VA 22314")
    f = o["fields"]
    assert o["found"] and o["problems"] == []
    assert f["list_price"]["display"] == "$1,495,000" and f["list_price"]["value"] == 1495000 and not f["list_price"]["conflict"]
    assert f["sqft"]["conflict"] and f["sqft"]["values"] == ["2,610", "2,480"], "two pages disagree: both shown"
    assert f["days_on_market"]["display"] == "9" and len(f["days_on_market"]["sources"]) == 1, "zillow source dropped"
    assert "mls_number" not in f and o["dropped_without_source"] == 2
    assert o["overview"]["issues"] == []
    # copied exactly, never reformatted
    o = tp.clean_lookup(raw_listing(beds=[{"value": "4 beds", "source_url": "https://x.org/a"}]), "219 N Pitt St")
    assert o["fields"]["beds"]["display"] == "4 beds" and o["fields"]["beds"]["value"] is None


def test_clean_lookup_problems():
    o = tp.clean_lookup(raw_listing(status="Under Contract", beds=[], sqft=[]), "219 N Pitt St, Alexandria, VA")
    assert any("Under Contract" in p for p in o["problems"])
    assert any("Bedrooms" in p and "Square feet" in p for p in o["problems"])
    o = tp.clean_lookup(raw_listing(matched_address="219 N Pitt St #2, Alexandria"), "219 N Pitt St #3, Alexandria")
    assert any(p.startswith("Check the address") for p in o["problems"])
    o = tp.clean_lookup({"matched_address": "", "not_found_reason": "No active listing."}, "1 Oak St")
    assert o["found"] is False and "No active listing" in o["problems"][0]
    o = tp.clean_lookup(raw_listing(overview="Perfect for families, stunning views."), "219 N Pitt St")
    assert len(o["overview"]["issues"]) >= 2


class AI:
    def __init__(self, answers):
        self.answers, self.bodies = list(answers), []

    async def post(self, body):
        self.bodies.append(json.loads(json.dumps(body)))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def ans(text, stop="end_turn"):
    return {"stop_reason": stop, "content": [{"type": "server_tool_use"}, {"type": "text", "text": text}]}


def test_lookup_pause_and_blocked_domains():
    j = json.dumps(raw_listing())
    ai = AI([ans("Searching {first}", "pause_turn"), ans("Found it. " + j[:50]), ans(j[50:] + " done.")])
    # the third answer would only be read if the second paused; it did not, so make it pause
    ai.answers[0]["stop_reason"] = "pause_turn"
    ai.answers[1]["stop_reason"] = "pause_turn"
    o = run(tp.lookup("219 N Pitt St, Alexandria, VA 22314", "m", ai.post))
    assert o["fields"]["beds"]["display"] == "4"
    assert len(ai.bodies) == 3 and ai.bodies[2]["messages"][-1]["role"] == "assistant"
    tool = ai.bodies[0]["tools"][0]
    assert tool["blocked_domains"] == ["zillow.com", "trulia.com", "hotpads.com"]
    assert "zillow" in ai.bodies[0]["system"].lower() and "exactly" in ai.bodies[0]["system"]


def test_lookup_failures():
    for bad in ("", "Pitt Street", "abc"):
        try:
            run(tp.lookup(bad, "m", AI([]).post))
            raise AssertionError("should refuse " + bad)
        except tp.TourError as e:
            assert "street number" in str(e)
    try:
        run(tp.lookup("219 N Pitt St", "m", AI([RuntimeError("529 overloaded")]).post))
        raise AssertionError
    except tp.TourError as e:
        assert "did NOT run" in str(e) and "529" in str(e)
    try:
        run(tp.lookup("219 N Pitt St", "m", AI([ans("I could not tell.")]).post))
        raise AssertionError
    except tp.TourError as e:
        assert "could not be read" in str(e)
    try:
        run(tp.lookup("219 N Pitt St", "m", AI([ans("x", "pause_turn")] * 4).post))
        raise AssertionError
    except tp.TourError as e:
        assert "did not finish" in str(e)


# ── comparisons ─────────────────────────────────────────────────────────────────
HOMES = [{"stop": 1, "address": "219 N Pitt St", "list_price": "$1,495,000", "beds": "4", "baths_full": "3",
          "baths_half": "1", "sqft": "2,610", "days_on_market": "9", "overview": "A brick townhouse with a garden."},
         {"stop": 2, "address": "600 Cameron St", "list_price": "$1,250,000", "beds": "3", "baths_full": "2",
          "baths_half": "", "sqft": "1,900", "days_on_market": "31", "overview": "A detached home on a corner lot."}]


def test_numbers_in():
    assert tp._numbers_in("$1,495,000 and 2.5 baths, 2.0 and 9.") == {"1495000", "2.5", "2", "9"}


def test_compare_retries_and_flags():
    good = {"homes": [{"stop": 1, "comparison": "The largest home on the tour at 2,610 square feet, with 4 bedrooms."},
                      {"stop": 2, "comparison": "The lowest price on the tour at $1,250,000, on a corner lot."}]}
    invented = {"homes": [{"stop": 1, "comparison": "About $573 per square foot, the best value."},
                          good["homes"][1]]}
    ai = AI([ans(json.dumps(invented)), ans(json.dumps(good))])
    out = run(tp.compare(HOMES, "m", ai.post))
    assert [h["issues"] for h in out["homes"]] == [[], []]
    assert len(ai.bodies) == 2 and "573" in ai.bodies[1]["messages"][-1]["content"], "told exactly what was wrong"
    # still wrong after the retry: returned marked, never cleaned up silently
    ai = AI([ans(json.dumps(invented)), ans(json.dumps(invented))])
    out = run(tp.compare(HOMES, "m", ai.post))
    assert out["homes"][0]["comparison"].startswith("About $573") and any("573" in i for i in out["homes"][0]["issues"])
    assert out["homes"][1]["issues"] == []
    # a home the AI left out is marked empty
    ai = AI([ans(json.dumps({"homes": [good["homes"][0]]}))] * 2)
    out = run(tp.compare(HOMES, "m", ai.post))
    assert "is empty" in out["homes"][1]["issues"]
    # fair housing in a comparison
    fam = {"homes": [{"stop": 1, "comparison": "Great for families."}, good["homes"][1]]}
    out = run(tp.compare(HOMES, "m", AI([ans(json.dumps(fam))] * 2).post))
    assert any("fair housing" in i for i in out["homes"][0]["issues"])


# ── order and times, against independent checks ─────────────────────────────────
def held_karp(m, n, origin=None):
    """Least total drive over all orders, by dynamic programming (a different method from
    tour_plan.best_order's exhaustive search)."""
    INF = float("inf")
    dp = {}
    for i in range(n):
        dp[(1 << i, i)] = m[origin][i] if origin is not None else 0
    for mask in range(1, 1 << n):
        for last in range(n):
            if (mask, last) not in dp:
                continue
            for nxt in range(n):
                if mask & (1 << nxt):
                    continue
                k = (mask | 1 << nxt, nxt)
                v = dp[(mask, last)] + m[last][nxt]
                if v < dp.get(k, INF):
                    dp[k] = v
    full = (1 << n) - 1
    return min(dp[(full, i)] for i in range(n))


def cost(m, order, origin=None):
    c = m[origin][order[0]] if origin is not None else 0
    return c + sum(m[a][b] for a, b in zip(order, order[1:]))


def test_best_order_is_optimal():
    rnd = random.Random(7)
    for trial in range(60):
        n = rnd.randint(2, 7)
        size = n + (trial % 2)
        m = [[0 if i == j else rnd.randint(60, 2400) for j in range(size)] for i in range(size)]
        origin = n if size > n else None
        o = tp.best_order(m, n, origin)
        assert sorted(o) == list(range(n))
        assert cost(m, o, origin) == held_karp(m, n, origin), (trial, o)
    m = [[0, 5, 5], [5, 0, 5], [5, 5, 0]]
    assert tp.best_order(m, 3) == [0, 1, 2], "ties go to the order that sorts first"


def test_plan_schedule():
    rnd = random.Random(3)
    for _ in range(200):
        n = rnd.randint(1, 8)
        legs = [rnd.randint(0, 3600) for _ in range(n - 1)]
        start, buf = rnd.randint(480, 900), rnd.choice([0, 5, 10])
        each = [rnd.choice(tp.ALLOWED_MINUTES) for _ in range(n)]
        s = tp.plan_schedule(list(range(n)), legs, start, each, buf)
        assert s[0]["start_min"] == start
        for k in range(1, n):
            earliest = s[k - 1]["end_min"] + legs[k - 1] / 60.0 + buf
            assert earliest <= s[k]["start_min"] < earliest + 5 and s[k]["start_min"] % 5 == 0
        for k in range(n):
            assert s[k]["end_min"] - s[k]["start_min"] == each[k], "each home keeps its own time"
        assert all(r["warning"] == "" for r in tp.check_times(s, legs, buf))


def test_check_times():
    st = [{"start_min": 600, "end_min": 630, "start": "10:00", "end": "10:30"},
          {"start_min": 640, "end_min": 670, "start": "10:40", "end": "11:10"},
          {"start_min": 660, "end_min": 690, "start": "11:00", "end": "11:30"},
          {"start_min": 720, "end_min": 750, "start": "12:00", "end": "12:30"}]
    rows = tp.check_times(st, [18 * 60, 60, 25 * 60 - 30], 5)
    assert rows[0]["drive_min"] == 18 and "about 18 min" in rows[0]["warning"] and "10:48" in rows[0]["warning"]
    assert "before stop 2 ends" in rows[1]["warning"]
    assert rows[2]["drive_min"] == 25 and rows[2]["gap_min"] == 30 and rows[2]["warning"] == "", "30 >= 25 + 5"
    rows = tp.check_times(st[2:], [25 * 60 + 1], 5)
    assert rows[0]["drive_min"] == 26 and rows[0]["warning"], "a drive is rounded UP"
    # independent rule over random cases: warning exactly when gap < ceil(drive) + buffer
    rnd = random.Random(11)
    for _ in range(500):
        a0 = rnd.randint(500, 800); a1 = a0 + rnd.randint(10, 60); b0 = a1 + rnd.randint(-20, 60)
        d, buf = rnd.randint(0, 3000), rnd.choice([0, 5, 10])
        r = tp.check_times([{"start_min": a0, "end_min": a1, "start": "", "end": tp.hhmm(a1)},
                            {"start_min": b0, "end_min": b0 + 30, "start": tp.hhmm(b0), "end": ""}], [d], buf)[0]
        assert bool(r["warning"]) == ((b0 - a1) < -(-d // 60) + buf)


def test_small_parsers():
    assert tp.seconds("754s") == 754 and tp.seconds("754.1s") == 755 and tp.seconds("0s") == 0
    for bad in ("", "12", "abc", None):
        try:
            tp.seconds(bad); raise AssertionError(bad)
        except tp.TourError:
            pass
    assert tp.parse_hhmm("9:05") == 545 and tp.parse_hhmm("14:30") == 870
    for bad in ("24:00", "10:60", "10am", ""):
        try:
            tp.parse_hhmm(bad); raise AssertionError(bad)
        except tp.TourError:
            pass
    assert tp.departure_rfc3339("2026-10-31", 600, NOW) == "2026-10-31T10:00:00-04:00"
    assert tp.departure_rfc3339("2026-11-02", 600, NOW) == "2026-11-02T10:00:00-05:00", "after the clocks change"
    assert tp.departure_rfc3339("2026-10-29", 600, NOW) is None, "a past time gets no traffic"
    assert tp.departure_rfc3339("2026-10-30", 480, NOW) is None


def test_geocode_result():
    ok = {"status": "OK", "results": [{"formatted_address": "219 N Pitt St, Alexandria, VA 22314, USA",
                                       "geometry": {"location": {"lat": 38.806, "lng": -77.044}, "location_type": "ROOFTOP"}}]}
    assert tp.geocode_result(ok, "x")["lat"] == 38.806
    for payload, word in ((dict(ok, status="ZERO_RESULTS"), "could not find"),
                          ({"status": "REQUEST_DENIED", "error_message": "API not enabled"}, "API not enabled"),
                          ({"status": "OK", "results": [dict(ok["results"][0], partial_match=True)]}, "approximately"),
                          ({"status": "OK", "results": [{"formatted_address": "Alexandria, VA", "geometry": {
                              "location": {"lat": 1, "lng": 2}, "location_type": "APPROXIMATE"}}]}, "approximately")):
        try:
            tp.geocode_result(payload, "219 N Pitt St"); raise AssertionError(word)
        except tp.TourError as e:
            assert word in str(e)


def test_static_map_url():
    homes = [{"lat": 38.8, "lng": -77.04}, {"lat": 38.81, "lng": -77.05}]
    client = unquote(tp.static_map_url(homes, "KEY"))
    agent = unquote(tp.static_map_url(homes, "KEY", "abc~def", {"lat": 38.9, "lng": -77.0}))
    assert "label:1|38.800000,-77.040000" in client and "label:2|" in client
    assert "path=" not in client and "label:S" not in client, "the client map has pins only"
    assert "enc:abc~def" in agent and "label:S|38.900000" in agent


# ── the whole route with Google replaced ────────────────────────────────────────
PLACES = {"1 A St, Washington, DC 20001": (38.90, -77.00), "2 B St, Washington, DC 20001": (38.95, -77.00),
          "3 C St, Washington, DC 20001": (38.91, -77.00), "4 D St, Washington, DC 20001": (38.94, -77.00),
          "Office, 1 Main St, Washington, DC 20001": (38.89, -77.00)}


class Google:
    def __init__(self):
        self.posts, self.gets, self.imgs = [], [], []

    async def get(self, url, params):
        self.gets.append((url, params))
        a = params["address"]
        if a not in PLACES:
            return {"status": "ZERO_RESULTS", "results": []}
        lat, lng = PLACES[a]
        return {"status": "OK", "results": [{"formatted_address": a, "geometry": {
            "location": {"lat": lat, "lng": lng}, "location_type": "ROOFTOP"}}]}

    @staticmethod
    def secs(p, q):
        return int(abs(p["latitude"] - q["latitude"]) * 100000) + 60   # about a minute per km, plus parking

    async def post(self, url, body, headers):
        self.posts.append((url, body, headers))
        assert headers.get("X-Goog-FieldMask")
        if "computeRouteMatrix" in url:
            pts = [o["waypoint"]["location"]["latLng"] for o in body["origins"]]
            return [{"originIndex": i, "destinationIndex": j, "condition": "ROUTE_EXISTS",
                     "duration": "{0}s".format(self.secs(p, q) if i != j else 0)}
                    for i, p in enumerate(pts) for j, q in enumerate(pts)]
        pts = [body["origin"]] + body["intermediates"] + [body["destination"]]
        pts = [p["location"]["latLng"] for p in pts]
        return {"routes": [{"legs": [{"duration": "{0}s".format(self.secs(a, b))} for a, b in zip(pts, pts[1:])],
                            "polyline": {"encodedPolyline": "POLY"}}]}

    async def get_bytes(self, url):
        self.imgs.append(url)
        return PNG


def test_route_plan_mode():
    g = Google()
    mins = {"1": 60, "2": 30, "3": 45, "4": 15}   # Marc: an hour at one, 30 minutes at the next
    req = {"mode": "plan", "date": "2026-10-31", "start_time": "10:00", "buffer_minutes": 5,
           "start_address": "Office, 1 Main St, Washington, DC 20001",
           "stops": [{"address": a, "minutes": mins[a[0]]} for a in (
               "2 B St, Washington, DC 20001", "1 A St, Washington, DC 20001",
               "4 D St, Washington, DC 20001", "3 C St, Washington, DC 20001")]}
    out = run(tp.route(req, "KEY", g.get, g.post, g.get_bytes, NOW))
    # from the office (south), north along the line: A, C, D, B
    assert [s["address"][0] for s in out["stops"]] == ["1", "3", "4", "2"]
    # A 10:00-11:00; +17.7 drive +5 -> 11:25 C 11:25-12:10; +51 +5 -> 13:10 D 13:10-13:25; +17.7 +5 -> 13:50 B
    assert [(s["start"], s["end"]) for s in out["stops"]] == [("10:00", "11:00"), ("11:25", "12:10"),
                                                              ("13:10", "13:25"), ("13:50", "14:20")]
    assert [s["minutes"] for s in out["stops"]] == [60, 45, 15, 30], "each home's own time follows it into the order"
    assert out["warnings"] == [] and out["traffic"] is True
    assert g.posts[0][1]["departureTime"] == "2026-10-31T10:00:00-04:00"
    assert g.posts[0][1]["routingPreference"] == "TRAFFIC_AWARE"
    assert out["maps"]["agent"].startswith("data:image/png;base64,") and out["maps"]["client"].startswith("data:image/png")
    assert "path=" in g.imgs[0] and "path=" not in g.imgs[1]
    assert "KEY" not in json.dumps(out), "the server key never reaches the browser"


def test_route_confirmed_mode():
    g = Google()
    req = {"mode": "confirmed", "date": "2026-10-31", "buffer_minutes": 5,
           "stops": [{"address": "2 B St, Washington, DC 20001", "start": "10:40", "end": "11:10"},
                     {"address": "1 A St, Washington, DC 20001", "start": "10:00", "end": "10:30"},
                     {"address": "3 C St, Washington, DC 20001", "start": "11:05", "end": "11:30"}]}
    out = run(tp.route(req, "KEY", g.get, g.post, g.get_bytes, NOW))
    assert [s["address"][0] for s in out["stops"]] == ["1", "2", "3"], "kept in time order, never reordered"
    assert [s["start"] for s in out["stops"]] == ["10:00", "10:40", "11:05"]
    assert len(out["warnings"]) == 2 and "before stop 2 ends" in out["warnings"][1]
    assert out["legs"][0]["drive_min"] == 85 and "about 85 min" in out["warnings"][0]
    assert not any("computeRouteMatrix" in p[0] for p in g.posts), "confirmed mode does not reorder"
    assert g.posts[0][1]["departureTime"] == "2026-10-31T10:30:00-04:00", "leaves when the first showing ends"


def test_route_refusals():
    g = Google()
    base = {"mode": "plan", "date": "2026-10-31", "start_time": "10:00"}
    cases = [(dict(base, stops=[]), "1 to 8"),
             (dict(base, stops=[{"address": "1 A St, Washington, DC 20001"}] * 9), "1 to 8"),
             (dict(base, mode="x", stops=[{"address": "a"}]), "plan or confirmed"),
             (dict(base, stops=[{"address": "1 A St, Washington, DC 20001"}]), "pick the time at this home"),
             (dict(base, stops=[{"address": "1 A St, Washington, DC 20001", "minutes": 20}]), "pick the time at this home"),
             (dict(base, stops=[{"address": "9 Nowhere Rd", "minutes": 30}]), "could not find"),
             ({"mode": "confirmed", "date": "2026-10-31", "stops": [{"address": "1 A St, Washington, DC 20001", "start": "10:30", "end": "10:00"}]}, "after the start"),
             ({"mode": "confirmed", "date": "2026-10-31", "stops": [{"address": "1 A St, Washington, DC 20001", "start": "10am", "end": "11"}]}, "Times are written")]
    for req, word in cases:
        try:
            run(tp.route(json.loads(json.dumps(req)), "K", g.get, g.post, g.get_bytes, NOW))
            raise AssertionError(word)
        except tp.TourError as e:
            assert word in str(e), (word, str(e))

    async def not_png(url):
        return b"<html>error</html>"
    try:
        run(tp.route(dict(base, stops=[{"address": "1 A St, Washington, DC 20001", "minutes": 30}]), "K", g.get, g.post, not_png, NOW))
        raise AssertionError
    except tp.TourError as e:
        assert "map image did NOT load" in str(e)


# ── the real routes ─────────────────────────────────────────────────────────────
ORIGIN = {"origin": "https://forward-os.netlify.app"}


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


def test_http_routes(monkeypatch):
    from fastapi.testclient import TestClient
    main = load_main()
    c = TestClient(main.app)
    g = Google()
    monkeypatch.setattr(main, "_tour_get", g.get)
    monkeypatch.setattr(main, "_tour_post", g.post)
    monkeypatch.setattr(main, "_tour_get_bytes", g.get_bytes)
    calls = []

    async def fake_claude(body, web):
        calls.append(web)
        if web:
            return ans(json.dumps(raw_listing()))
        return ans(json.dumps({"homes": [{"stop": 1, "comparison": "A brick townhouse with 4 bedrooms."}]}))
    monkeypatch.setattr(main, "_tour_claude", fake_claude)
    for path, body in (("/api/tour/lookup", {"address": "219 N Pitt St"}), ("/api/tour/route", {}),
                       ("/api/tour/compare", {"homes": []})):
        assert c.post(path, json=body).status_code == 403, path + " only from FORWARD OS"
    r = c.post("/api/tour/lookup", json={"address": "219 N Pitt St, Alexandria, VA 22314"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["fields"]["beds"]["display"] == "4" and r.json()["looked_up_at"]
    assert c.post("/api/tour/lookup", json={"address": "Pitt"}, headers=ORIGIN).status_code == 502
    req = {"mode": "plan", "date": "2026-10-31", "start_time": "10:00",
           "stops": [{"address": "1 A St, Washington, DC 20001", "minutes": 60},
                     {"address": "2 B St, Washington, DC 20001", "minutes": 30}]}
    monkeypatch.setattr(main, "GOOGLE_MAPS_SERVER_KEY", "")
    r = c.post("/api/tour/route", json=req, headers=ORIGIN)
    assert r.status_code == 503 and "GOOGLE_MAPS_SERVER_KEY" in r.json()["detail"]
    monkeypatch.setattr(main, "GOOGLE_MAPS_SERVER_KEY", "SERVERKEY")
    r = c.post("/api/tour/route", json=req, headers=ORIGIN)
    assert r.status_code == 200 and len(r.json()["stops"]) == 2 and "SERVERKEY" not in r.text
    r = c.post("/api/tour/route", json=dict(req, stops=[{"address": "9 Nowhere Rd", "minutes": 30}]), headers=ORIGIN)
    assert r.status_code == 422 and "could not find" in r.json()["detail"]
    r = c.post("/api/tour/compare", json={"homes": HOMES[:1]}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["homes"][0]["issues"] == []
    assert calls == [True, False]
