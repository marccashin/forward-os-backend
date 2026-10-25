"""Research behind a listing's buyer avatars: the pure rules with made-up Census and AI
answers, then the real route with Supabase replaced. No network."""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import avatar_research as avr  # noqa: E402

ADDR = "5000 Edgemoor Ln, Bethesda, MD 20814"
ACS_HEAD = ["B19013_001E", "B19001_001E", "B19001_017E", "B25077_001E", "B25003_001E", "B25003_002E",
            "B07001_001E", "B07001_017E", "B07001_033E", "B07001_049E", "B07001_065E", "B07001_081E",
            "zip code tabulation area"]
ACS_ROW = ["187500", "10000", "4120", "1250000", "10000", "6200",
           "20000", "16000", "1500", "700", "1500", "300", "20814"]
GEO = {"result": {"addressMatches": [{"geographies": {"Counties": [
    {"STATE": "24", "COUNTY": "031", "NAME": "Montgomery County", "BASENAME": "Montgomery"}]}}]}}
CBP = [["NAICS2017", "NAICS2017_LABEL", "EMP", "ESTAB", "EMPSZES", "LFO", "state", "county"],
       ["00", "Total for all sectors", "500000", "30000", "001", "001", "24", "031"],
       ["54", "Professional, scientific, and technical services", "90000", "6000", "001", "001", "24", "031"],
       ["54", "Professional, scientific, and technical services", "4000", "900", "212", "001", "24", "031"],
       ["62", "Health care and social assistance", "80000", "3000", "001", "001", "24", "031"],
       ["44-45", "Retail trade", "40000", "2500", "001", "001", "24", "031"],
       ["52", "Finance and insurance", "0", "800", "001", "001", "24", "031"],
       ["541", "Legal services", "9999999", "1", "001", "001", "24", "031"]]
WEB_JSON = ('{"employers":[{"name":"Walter Reed National Military Medical Center","what":"military hospital",'
            '"where":"Bethesda","source_url":"https://example.org/wr"},{"name":"No Source Inc","what":"x","where":"y","source_url":""}],'
            '"moving":[{"finding":"Agency relocations to Bethesda in 2025","source_url":"https://example.org/move"}],'
            '"salaries":[{"occupation":"Physicians","range":"$239,200 median","year":"2024","source_url":"https://www.bls.gov/oes/x"},'
            '{"occupation":"Lawyers","range":"high","year":"","source_url":"not a link"}]}')


class Net:
    def __init__(self, census_ok=True, web_ok=True, acs_fail_years=()):
        self.calls, self.census_ok, self.web_ok, self.acs_fail = [], census_ok, web_ok, set(acs_fail_years)
        self.bodies = []

    async def get(self, url, params):
        self.calls.append((url, dict(params)))
        if "geocoder" in url:
            return GEO
        if "/acs/acs5" in url:
            year = int(re.search(r"/data/(\d{4})/", url).group(1))
            if not self.census_ok or year in self.acs_fail:
                raise avr.ResearchError("api.census.gov answered 404")
            return [ACS_HEAD, ACS_ROW]
        if "/cbp" in url:
            return CBP
        raise AssertionError(url)

    async def post(self, body):
        self.bodies.append(body)
        if not self.web_ok:
            raise RuntimeError("Claude answered 529: overloaded")
        return {"content": [{"type": "server_tool_use"}, {"type": "text", "text": "Here you go. " + WEB_JSON}]}


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_zip_is_the_last_five_digits():
    assert avr.zip_of(ADDR) == "20814"
    assert avr.zip_of("12345 Main St, Bethesda, MD 20817-1234") == "20817", "a five-digit street number is not the ZIP"
    assert avr.zip_of("41 V St NW, Washington, DC") == ""


def test_census_figures_are_the_census_numbers():
    f = avr.acs_figures(dict(zip(ACS_HEAD, ACS_ROW)), 2024, "20814")
    by = {x["key"]: x for x in f["figures"]}
    assert by["median_income"]["display"] == "$187,500"
    assert by["median_value"]["display"] == "$1,250,000"
    assert by["income_200k"]["display"] == "4,120 (41.2% of 10,000 households)"
    assert by["owner_occupied"]["display"] == "6,200 (62.0% of 10,000 occupied homes)"
    assert by["moved_last_year"]["value"] == 4000 and by["moved_last_year"]["display"] == "4,000 (20.0% of 20,000 residents)"
    assert by["from_other_state"]["display"] == "1,500 (37.5% of movers)"
    assert f["source"] == "U.S. Census Bureau, American Community Survey 2020-2024 5-year estimates, ZIP Code Tabulation Area 20814"
    # The Census's "no estimate" placeholders are said, never shown as a number
    row = dict(zip(ACS_HEAD, ACS_ROW)); row["B19013_001E"] = "-666666666"; row["B19001_001E"] = None
    by = {x["key"]: x for x in avr.acs_figures(row, 2024, "20814")["figures"]}
    assert by["median_income"]["display"] == "not published" and by["median_income"]["value"] is None
    assert by["income_200k"]["display"] == "4,120", "no share without the total"


def test_nothing_about_who_a_buyer_is():
    words = " ".join(avr.ACS_VARS.values()).lower() + " " + avr.WEB_SYSTEM.split("FAIR HOUSING")[0].lower()
    for w in ("age", "race", "religion", "child", "school", "famil", "sex", "disab", "ethnic", "origin"):
        assert not re.search(r"\b" + w, words), w
    assert all(w in avr.WEB_SYSTEM for w in ("never search for, report or infer age, race", "children or schools"))
    assert not any(v.startswith(("B01", "B02", "B03", "B11", "B09", "B14", "B18")) for v in avr.ACS_VARS), "no age, race, household-type, children, school or disability tables"


def test_sectors_rank_all_establishments_only():
    rows = avr._rows(CBP)
    s = avr.top_sectors(rows)
    assert [x["code"] for x in s] == ["54", "62", "44-45"], s
    assert s[0]["employees"] == 90000 and s[0]["establishments"] == 6000
    assert all(x["code"] != "52" for x in s), "a suppressed sector (0 employees) is not ranked"


def test_research_all_parts():
    net = Net()
    r = run(avr.research(ADDR, "KEY", "m", net.get, net.post))
    assert r["ok"] and r["zip"] == "20814" and r["county"]["name"] == "Montgomery County"
    assert r["census"]["year"] == 2024 and r["jobs"]["year"] == 2023
    assert all(p.get("key") == "KEY" for u, p in net.calls if "api.census.gov" in u)
    assert not any("key" in p for u, p in net.calls if "geocoder" in u), "the address lookup needs no key"
    w = r["web"]
    assert [e["name"] for e in w["employers"]] == ["Walter Reed National Military Medical Center"]
    assert [s["occupation"] for s in w["salaries"]] == ["Physicians"] and w["salaries"][0]["range"] == "$239,200 median"
    assert w["dropped_without_source"] == 2, "items without a real source address are dropped and counted"
    b = net.bodies[0]
    assert b["tools"][0]["type"] == "web_search_20250305" and "FAIR HOUSING" in b["system"] and ADDR in b["messages"][0]["content"]
    p = avr.prompt_block(r)
    assert "use these figures exactly as written" in p and "Median household income: $187,500" in p
    assert "Professional, scientific, and technical services: 90,000 employees at 6,000 establishments" in p
    assert "Pay: Physicians: $239,200 median (2024) [https://www.bls.gov/oes/x]" in p
    assert "No Source Inc" not in p


def test_no_census_key_is_said_and_nothing_is_sent_to_the_census():
    net = Net()
    r = run(avr.research(ADDR, "", "m", net.get, net.post))
    assert r["census"] == {"error": "Census figures were NOT pulled: no Census key is set on the backend (CENSUS_API_KEY)"}
    assert r["jobs"]["error"].startswith("Job clusters were NOT pulled: no Census key")
    assert not any("api.census.gov" in u for u, _ in net.calls)
    assert r["ok"] and r["web"]["employers"], "the web research still runs"
    assert "Census: Census figures were NOT pulled" in avr.prompt_block(r)


def test_newest_year_first_then_the_year_before():
    net = Net(acs_fail_years={2024})
    r = run(avr.research(ADDR, "K", "m", net.get, net.post))
    assert r["census"]["year"] == 2023
    years = [re.search(r"/data/(\d{4})/acs", u).group(1) for u, _ in net.calls if "/acs/" in u]
    assert years == ["2024", "2023"]


def test_every_failure_is_said():
    net = Net(census_ok=False, web_ok=False)
    r = run(avr.research(ADDR, "K", "m", net.get, net.post))
    assert r["census"]["error"].startswith("Census figures were NOT pulled: api.census.gov answered 404")
    assert r["web"]["error"] == "Web research did NOT run: Claude answered 529: overloaded"
    assert r["ok"], "jobs still came through"
    r = run(avr.research("41 V St NW, Washington, DC", "K", "m", net.get, net.post))
    assert r["census"]["error"] == "Census figures were NOT pulled: the listing's address has no 5-digit ZIP code"


def test_bad_web_answer_is_said():
    async def post(body):
        return {"content": [{"type": "text", "text": "I could not find anything."}]}
    r = run(avr.research(ADDR, "", "m", Net().get, post))
    assert r["web"]["error"] == "the web research gave no readable answer"
    assert r["web"]["debug"]["text_start"] == "I could not find anything." and set(r["web"]) == {"error", "debug"}
    assert r["ok"] is False


# ── The real route ──────────────────────────────────────────────────────────────
P = "11111111-1111-4111-8111-111111111111"
P_NOADDR = "22222222-2222-4222-8222-222222222222"
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
    monkeypatch.setattr(main, "supabase", FakeSB([{"id": P, "address": ADDR}, {"id": P_NOADDR, "address": "  "}]))
    net = Net()
    monkeypatch.setattr(main, "_avr_get", net.get)
    monkeypatch.setattr(main, "_avr_post", net.post)
    monkeypatch.setattr(main, "CENSUS_API_KEY", "K")
    c = TestClient(main.app)
    assert c.post("/api/avatar-research", json={"property_id": P}).status_code == 403, "only from FORWARD OS"
    assert c.post("/api/avatar-research", json={"property_id": "nope"}, headers=ORIGIN).status_code == 400
    assert c.post("/api/avatar-research", json={"property_id": "33333333-3333-4333-8333-333333333333"}, headers=ORIGIN).status_code == 404
    assert c.post("/api/avatar-research", json={"property_id": P_NOADDR}, headers=ORIGIN).status_code == 422
    r = c.post("/api/avatar-research", json={"property_id": P, "address": "1 Fake St, Nowhere 99999"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["address"] == ADDR and j["zip"] == "20814", "the address comes from the database, not the caller"
    assert j["census"]["figures"] and j["jobs"]["sectors"] and j["web"]["employers"]
    assert j["prompt"].startswith("RESEARCH FOR THESE AVATARS") and j["researched_at"]
    assert any(ADDR in b["messages"][0]["content"] for b in net.bodies)
    monkeypatch.setattr(main, "CENSUS_API_KEY", "")
    j = c.post("/api/avatar-research", json={"property_id": P}, headers=ORIGIN).json()
    assert "no Census key" in j["census"]["error"]


def test_the_findings_are_found_among_notes_and_stray_braces():
    def ans(*texts):
        return {"content": [{"type": "text", "text": x} for x in texts]}
    j = '{"employers":[{"name":"Inova","what":"hospital","where":"Alexandria","source_url":"https://example.org/i"}],"moving":[],"salaries":[]}'
    # notes before and after, a brace in a note, the JSON split across text blocks (web search answers come in many blocks)
    got = avr.parse_claude_json(ans("I'll search for employers {near the address}. ", j[:40], j[40:], " Note: figures are from 2024 {see sources}."))
    assert got["employers"][0]["name"] == "Inova"
    got = avr.parse_claude_json(ans("```json\n" + j + "\n```"))
    assert got["employers"][0]["source_url"] == "https://example.org/i"
    # an object that is not the findings is not taken for them
    try:
        avr.parse_claude_json(ans('Searching {"query": "employers"} now.'))
        raise AssertionError("should refuse")
    except avr.ResearchError as e:
        assert str(e) == "the web research answer was not valid JSON"
    try:
        avr.parse_claude_json(ans("No braces at all."))
        raise AssertionError("should refuse")
    except avr.ResearchError as e:
        assert str(e) == "the web research gave no readable answer"


def test_a_paused_search_is_sent_back_until_it_finishes():
    j = '{"employers":[{"name":"Inova","what":"hospital","where":"Alexandria","source_url":"https://example.org/i"}],"moving":[],"salaries":[]}'
    turns = [
        {"stop_reason": "pause_turn", "content": [{"type": "text", "text": "I'll search for employers."}, {"type": "server_tool_use", "id": "s1"}]},
        {"stop_reason": "pause_turn", "content": [{"type": "web_search_tool_result", "tool_use_id": "s1"}, {"type": "text", "text": "Now pay ranges."}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Here are the findings. " + j}]},
    ]
    bodies = []

    async def post(body):
        bodies.append(body)
        return turns[len(bodies) - 1]
    r = run(avr.web_research(ADDR, "20814", "", "m", post))
    assert r["employers"][0]["name"] == "Inova" and len(bodies) == 3
    # each round sends the conversation so far, with the paused turn as the assistant's reply, unchanged
    assert [m["role"] for m in bodies[1]["messages"]] == ["user", "assistant"]
    assert bodies[1]["messages"][1]["content"] == turns[0]["content"]
    assert [m["role"] for m in bodies[2]["messages"]] == ["user", "assistant", "assistant"]
    assert bodies[0]["messages"] == [bodies[0]["messages"][0]] and len(bodies[0]["messages"]) == 1, "the first request is not changed afterwards"


def test_a_search_that_never_finishes_is_said_with_a_debug_note():
    calls = []

    async def post(body):
        calls.append(body)
        return {"stop_reason": "pause_turn", "content": [{"type": "text", "text": "Still searching {"}]}
    r = run(avr.research(ADDR, "", "m", Net().get, post))
    assert len(calls) == avr.MAX_PAUSES + 1
    assert r["web"]["error"] == "the web research did not finish (it was still searching after 4 rounds)"
    d = r["web"]["debug"]
    assert d["stop_reasons"] == ["pause_turn"] * 4 and d["text_blocks"] == 4 and "Still searching" in d["text_start"]


def test_a_cut_off_answer_is_said():
    async def post(body):
        return {"stop_reason": "max_tokens", "content": [{"type": "text", "text": '{"employers":[{"name":"Ino'}]}
    r = run(avr.research(ADDR, "", "m", Net().get, post))
    assert r["web"]["error"] == "the web research answer was cut off before it finished"
    assert r["web"]["debug"]["stop_reasons"] == ["max_tokens"]

