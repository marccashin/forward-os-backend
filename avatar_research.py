"""Research behind a listing's buyer avatars (Marc, Oct 24, 2026).

Until now the avatars were written from what the model already knew: the writer's
instructions said "information you can find online", but no request it made could look
anything up. Marc: census data, moving trends for the ZIP code, job clusters and salary
ranges "should all be considerations for the avatars and can be found/verified online".

What this gathers, every part with its source, and every part that could not be read
SAID, never filled in:

  census  American Community Survey 5-year, by ZIP Code Tabulation Area (api.census.gov,
          needs CENSUS_API_KEY since 2026): median household income, households earning
          $200,000 or more, median home value, owner-occupied homes, and where residents
          lived a year earlier (same house, same county, another county in the state,
          another state, abroad).
  jobs    County Business Patterns for the listing's county: employees by industry
          sector (the job clusters). County from the Census geocoder (no key).
  web     Claude with web search: major employers within commuting distance, moving
          trends into the area, and salary ranges for the occupations those employers
          hire. Every item must carry a source address; an item without one is dropped
          and counted.

Deliberately NOT gathered (fair housing): age, race, ethnicity, religion, national
origin, sex, disability, household or family make-up, children, schools. Avatars are
built from work, income, housing and movement, never from who a buyer is.

The research is for the agent and the AI. It is not printed on the seller's campaign
page (Marc's choice, Oct 24, 2026).

Everything here is pure or takes its HTTP functions as arguments, so the tests run with
no network.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Awaitable, Callable, Optional

ACS_YEARS = (2024, 2023)          # newest first; the first one that answers is used
CBP_YEARS = (2023, 2022)
ACS_URL = "https://api.census.gov/data/{year}/acs/acs5"
CBP_URL = "https://api.census.gov/data/{year}/cbp"
GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"

# Census codes, each checked against the Census's own variable list (Oct 24, 2026).
ACS_VARS = {
    "B19013_001E": "Median household income",
    "B19001_001E": "Households",
    "B19001_017E": "Households with income of $200,000 or more",
    "B25077_001E": "Median value of owner-occupied homes",
    "B25003_001E": "Occupied homes",
    "B25003_002E": "Owner-occupied homes",
    "B07001_001E": "Residents 1 year and over",
    "B07001_017E": "Same house 1 year ago",
    "B07001_033E": "Moved within the same county",
    "B07001_049E": "Moved from a different county in the same state",
    "B07001_065E": "Moved from a different state",
    "B07001_081E": "Moved from abroad",
}

FAIR_HOUSING_EXCLUDED = ("age", "race", "ethnicity", "religion", "national origin", "sex",
                         "disability", "familial status", "household or family make-up",
                         "children", "schools")

_log = logging.getLogger("forward-cc")

ZIP_RX = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

GetJson = Callable[[str, dict], Awaitable[object]]
PostClaude = Callable[[dict], Awaitable[dict]]


class ResearchError(Exception):
    """A part of the research that could not be read, in words for the agent."""


def zip_of(address: str) -> str:
    """The listing's ZIP: the LAST 5-digit group in the address (a street number can
    also have five digits, '12345 Main St ... 20815')."""
    hits = ZIP_RX.findall(address or "")
    return hits[-1] if hits else ""


def _num(v) -> Optional[float]:
    """A Census value, or None when the Census publishes no estimate (its negative
    placeholders such as -666666666, an empty cell, or text)."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return None if n < 0 else n


def _pct(part: Optional[float], whole: Optional[float]) -> Optional[float]:
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def _money(n: Optional[float]) -> str:
    return "not published" if n is None else "${:,.0f}".format(n)


def _count(n: Optional[float]) -> str:
    return "not published" if n is None else "{:,.0f}".format(n)


def _rows(payload) -> list[dict]:
    """Census answers are a list of lists: the first is the column names."""
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[0], list):
        raise ResearchError("the Census answer was not a table")
    head = payload[0]
    return [dict(zip(head, r)) for r in payload[1:] if isinstance(r, list)]


def acs_figures(row: dict, year: int, zip5: str) -> dict:
    """The ZIP's figures, each as the Census number plus, where it helps, a share the
    backend worked out from two Census counts (said so in the label)."""
    v = {k: _num(row.get(k)) for k in ACS_VARS}
    movers = None
    if v["B07001_001E"] is not None and v["B07001_017E"] is not None:
        movers = v["B07001_001E"] - v["B07001_017E"]
    figs = [
        {"key": "median_income", "label": "Median household income", "value": v["B19013_001E"], "display": _money(v["B19013_001E"])},
        {"key": "income_200k", "label": "Households earning $200,000 or more", "value": v["B19001_017E"],
         "display": _count(v["B19001_017E"]) + _share(v["B19001_017E"], v["B19001_001E"], "of " + _count(v["B19001_001E"]) + " households")},
        {"key": "median_value", "label": "Median value of owner-occupied homes", "value": v["B25077_001E"], "display": _money(v["B25077_001E"])},
        {"key": "owner_occupied", "label": "Owner-occupied homes", "value": v["B25003_002E"],
         "display": _count(v["B25003_002E"]) + _share(v["B25003_002E"], v["B25003_001E"], "of " + _count(v["B25003_001E"]) + " occupied homes")},
        {"key": "moved_last_year", "label": "Residents who moved here in the last year", "value": movers,
         "display": _count(movers) + _share(movers, v["B07001_001E"], "of " + _count(v["B07001_001E"]) + " residents")},
        {"key": "from_same_county", "label": "Of those, from the same county", "value": v["B07001_033E"], "display": _count(v["B07001_033E"]) + _share(v["B07001_033E"], movers, "of movers")},
        {"key": "from_other_county", "label": "Of those, from another county in the state", "value": v["B07001_049E"], "display": _count(v["B07001_049E"]) + _share(v["B07001_049E"], movers, "of movers")},
        {"key": "from_other_state", "label": "Of those, from another state", "value": v["B07001_065E"], "display": _count(v["B07001_065E"]) + _share(v["B07001_065E"], movers, "of movers")},
        {"key": "from_abroad", "label": "Of those, from abroad", "value": v["B07001_081E"], "display": _count(v["B07001_081E"]) + _share(v["B07001_081E"], movers, "of movers")},
    ]
    return {
        "year": year,
        "zip": zip5,
        "source": "U.S. Census Bureau, American Community Survey {0}-{1} 5-year estimates, ZIP Code Tabulation Area {2}".format(year - 4, year, zip5),
        "source_url": "https://data.census.gov/profile?g=860XX00US" + zip5,
        "note": "Shares are worked out by FORWARD OS from two Census counts; the counts are the Census's.",
        "figures": figs,
    }


def _share(part, whole, of_what) -> str:
    p = _pct(part, whole)
    return "" if p is None else " ({0}% {1})".format(p, of_what)


async def census_zip(zip5: str, key: str, get: GetJson) -> dict:
    if not key:
        raise ResearchError("Census figures were NOT pulled: no Census key is set on the backend (CENSUS_API_KEY)")
    if not zip5:
        raise ResearchError("Census figures were NOT pulled: the listing's address has no 5-digit ZIP code")
    last = ""
    for year in ACS_YEARS:
        try:
            payload = await get(ACS_URL.format(year=year), {
                "get": ",".join(ACS_VARS), "for": "zip code tabulation area:" + zip5, "key": key})
            rows = _rows(payload)
        except ResearchError as e:
            last = str(e)
            continue
        except Exception as e:  # network, 4xx/5xx: try the year before
            last = str(e)
            continue
        if rows:
            return acs_figures(rows[0], year, zip5)
        last = "the Census has no figures for ZIP " + zip5
    raise ResearchError("Census figures were NOT pulled: " + (last or "no answer"))


async def county_of(address: str, get: GetJson) -> dict:
    payload = await get(GEOCODER_URL, {"address": address, "benchmark": "Public_AR_Current",
                                       "vintage": "Current_Current", "format": "json"})
    try:
        match = payload["result"]["addressMatches"][0]
        c = match["geographies"]["Counties"][0]
        return {"state": c["STATE"], "county": c["COUNTY"], "name": c.get("NAME") or c.get("BASENAME") or ""}
    except (KeyError, IndexError, TypeError):
        raise ResearchError("the Census address lookup did not find this address")


def top_sectors(rows: list[dict], n: int = 6) -> list[dict]:
    """Employees by 2-digit industry sector for the whole county: rows for all
    establishments only (EMPSZES 001, LFO 001 where those columns are present), the
    all-industries total ('00') left out. A sector the Census suppressed (0 employees
    with establishments) is kept out of the ranking rather than guessed."""
    out = []
    for r in rows:
        code = str(r.get("NAICS2017") or "")
        if code in ("", "00") or not re.fullmatch(r"\d{2}(-\d{2})?", code):
            continue
        if "EMPSZES" in r and str(r["EMPSZES"]) != "001":
            continue
        if "LFO" in r and str(r["LFO"]) != "001":
            continue
        emp = _num(r.get("EMP"))
        if not emp:
            continue
        out.append({"code": code, "sector": r.get("NAICS2017_LABEL") or code,
                    "employees": int(emp), "establishments": int(_num(r.get("ESTAB")) or 0)})
    out.sort(key=lambda x: -x["employees"])
    return out[:n]


async def county_jobs(county: dict, key: str, get: GetJson) -> dict:
    if not key:
        raise ResearchError("Job clusters were NOT pulled: no Census key is set on the backend (CENSUS_API_KEY)")
    last = ""
    for year in CBP_YEARS:
        try:
            rows = _rows(await get(CBP_URL.format(year=year), {
                "get": "NAICS2017,NAICS2017_LABEL,EMP,ESTAB",
                "for": "county:" + county["county"], "in": "state:" + county["state"], "key": key}))
        except Exception as e:
            last = str(e)
            continue
        sectors = top_sectors(rows)
        if sectors:
            return {"year": year, "county": county.get("name", ""),
                    "source": "U.S. Census Bureau, County Business Patterns {0}, {1}".format(year, county.get("name", "")),
                    "source_url": "https://www.census.gov/programs-surveys/cbp.html",
                    "sectors": sectors}
        last = "no sector figures were published"
    raise ResearchError("Job clusters were NOT pulled: " + (last or "no answer"))


WEB_SYSTEM = (
    "You research the market around a home for FORWARD, a luxury real estate team in Washington, DC, "
    "Maryland and Virginia. The findings help the team picture the likely buyers of this home. "
    "Search the web. Report ONLY what you found on a page you can name, each item with that page's address.\n\n"
    "Find:\n"
    "1. employers: the largest employers and employment centers within a reasonable commute of the address "
    "(name, what they are, roughly where).\n"
    "2. moving: recent trends in who is moving to this area and from where, and why (relocations, new offices, "
    "government or military moves, return-to-office), from news or official reports.\n"
    "3. salaries: pay ranges for the senior occupations those employers hire, for this metro area, from "
    "official or reputable sources (for example the Bureau of Labor Statistics). Copy figures exactly as the "
    "source states them, with the year; never round or widen them.\n\n"
    "FAIR HOUSING, REQUIRED: never search for, report or infer age, race, ethnicity, religion, national origin, "
    "sex, disability, family or household make-up, children or schools. Work, pay, housing and movement only.\n\n"
    "Return ONLY valid JSON in this exact shape, nothing before or after it:\n"
    '{"employers":[{"name":"","what":"","where":"","source_url":""}],'
    '"moving":[{"finding":"","source_url":""}],'
    '"salaries":[{"occupation":"","range":"","year":"","source_url":""}]}\n'
    "Leave a list empty rather than guess. Every item needs a source_url that starts with https:// or http://."
)


def clean_web(raw: dict) -> dict:
    """Keep only items with a real source address; count what was dropped."""
    out, dropped = {}, 0
    keys = {"employers": ("name", "what", "where", "source_url"),
            "moving": ("finding", "source_url"),
            "salaries": ("occupation", "range", "year", "source_url")}
    for k, fields in keys.items():
        items = raw.get(k) if isinstance(raw, dict) else None
        kept = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                dropped += 1
                continue
            url = str(it.get("source_url") or "").strip()
            if not re.match(r"^https?://\S+\.\S+", url):
                dropped += 1
                continue
            kept.append({f: str(it.get(f) or "").strip() for f in fields})
        out[k] = kept[:8]
    out["dropped_without_source"] = dropped
    return out


WEB_KEYS = ("employers", "moving", "salaries")


def parse_claude_json(data: dict) -> dict:
    """The findings object in the model's answer. With web search on, the answer is many
    text blocks with notes around the JSON ("I'll search for...", a closing remark), and a
    note can hold a brace of its own. So every "{" is tried as the start of a JSON value
    (json's raw_decode, which stops where the value ends) and the last one that is an
    object with at least one of the expected lists wins. Live on Oct 24, 2026 the first
    version (first "{" to last "}") failed on 219 N Pitt St with "not valid JSON"."""
    text = "".join(b.get("text", "") for b in (data or {}).get("content", [])
                   if isinstance(b, dict) and b.get("type") == "text")
    dec, found, seen_brace = json.JSONDecoder(), None, False
    for m in re.finditer(r"\{", text):
        seen_brace = True
        try:
            val, _end = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(val, dict) and any(k in val for k in WEB_KEYS):
            found = val
    if found is not None:
        return found
    if not seen_brace:
        raise ResearchError("the web research gave no readable answer")
    raise ResearchError("the web research answer was not valid JSON")


async def web_research(address: str, zip5: str, county_name: str, model: str, post: PostClaude) -> dict:
    user = ("Research the market around this home.\nAddress: {0}\nZIP: {1}\nCounty: {2}"
            .format(address, zip5 or "unknown", county_name or "unknown"))
    body = {"model": model, "max_tokens": 6000,
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
            "system": WEB_SYSTEM, "messages": [{"role": "user", "content": user}]}
    try:
        data = await post(body)
    except Exception as e:
        raise ResearchError("Web research did NOT run: {0}".format(e))
    try:
        return clean_web(parse_claude_json(data))
    except ResearchError:
        texts = [b.get("text", "") for b in (data or {}).get("content", []) if isinstance(b, dict) and b.get("type") == "text"]
        _log.warning("[avatar-research] unreadable web answer, stop_reason=%s, %d text blocks, start: %r, end: %r",
                     (data or {}).get("stop_reason"), len(texts), "".join(texts)[:600], "".join(texts)[-300:])
        raise


async def research(address: str, census_key: str, model: str, get: GetJson, post: PostClaude) -> dict:
    """All three parts at once. Each part is either its findings or {"error": why}."""
    zip5 = zip_of(address)
    result = {"address": address, "zip": zip5, "excluded": list(FAIR_HOUSING_EXCLUDED)}

    async def _county():
        try:
            return await county_of(address, get)
        except ResearchError:
            raise
        except Exception as e:
            raise ResearchError("the Census address lookup failed: {0}".format(e))

    async def _jobs():
        c = await _county()
        result["county"] = c
        return await county_jobs(c, census_key, get)

    parts = await asyncio.gather(census_zip(zip5, census_key, get), _jobs(),
                                 web_research(address, zip5, "", model, post), return_exceptions=True)
    for name, val in zip(("census", "jobs", "web"), parts):
        if isinstance(val, ResearchError):
            result[name] = {"error": str(val)}
        elif isinstance(val, Exception):
            result[name] = {"error": "{0} could not be read: {1}".format(name, val)}
        else:
            result[name] = val
    result["ok"] = any("error" not in result[k] for k in ("census", "jobs", "web"))
    return result


def prompt_block(r: dict) -> str:
    """The research as plain text for the avatar writer, figures copied as stated."""
    if not isinstance(r, dict):
        return ""
    lines = ["RESEARCH FOR THESE AVATARS (gathered by FORWARD OS; use these figures exactly as written, never round, widen or invent one):"]
    c = r.get("census") or {}
    if c.get("figures"):
        lines.append("Census, " + c.get("source", "") + ":")
        lines += ["- {0}: {1}".format(f["label"], f["display"]) for f in c["figures"]]
    elif c.get("error"):
        lines.append("Census: " + c["error"] + ".")
    j = r.get("jobs") or {}
    if j.get("sectors"):
        lines.append("Largest employers by industry, " + j.get("source", "") + ":")
        lines += ["- {0}: {1:,} employees at {2:,} establishments".format(s["sector"], s["employees"], s["establishments"]) for s in j["sectors"]]
    elif j.get("error"):
        lines.append("Job clusters: " + j["error"] + ".")
    w = r.get("web") or {}
    if "error" in w:
        lines.append("Web research: " + w["error"] + ".")
    else:
        for e in w.get("employers", []):
            lines.append("- Employer: {0}, {1} ({2}) [{3}]".format(e["name"], e["what"], e["where"], e["source_url"]))
        for m in w.get("moving", []):
            lines.append("- Moving trend: {0} [{1}]".format(m["finding"], m["source_url"]))
        for s in w.get("salaries", []):
            lines.append("- Pay: {0}: {1} ({2}) [{3}]".format(s["occupation"], s["range"], s["year"], s["source_url"]))
    return "\n".join(lines)
