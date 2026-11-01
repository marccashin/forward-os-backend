"""Showing tour plans for buyers (Marc, Oct 30, 2026).

An agent types the addresses of the homes a buyer will see. FORWARD OS then:

  lookup   finds each live listing with Claude web search and returns price, beds, baths,
           square feet, days on market and status, EVERY figure with the page it came
           from, plus a one or two sentence overview of the home in our own words. The
           agent must confirm every figure in the app before anything reaches a client
           (Marc: "give a way for the agent to confirm the material data before sending
           it to the client"). Figures are copied as the page shows them, never rounded.
           Two pages that disagree are both returned and marked, so the agent picks.
           zillow.com and the other Zillow sites are blocked from the search: Zillow's
           Terms of Use forbid automated queries (checked Oct 30, 2026).
  route    plan mode: the drive-time matrix (Google Routes API) and the fastest order of
           the homes, tried exhaustively (8 homes = 40,320 orders), with suggested start
           times rounded UP to 5 minutes so a schedule is never tighter than the drive.
           confirmed mode: the agent's own times, kept in time order, with each drive
           and a warning wherever the gap is shorter than the drive plus a buffer, or two
           showings overlap. Both modes return two map images: the agent's with the route
           drawn, the client's with numbered pins only (Marc: the client copy has no route).
  compare  one or two sentences per home on how it compares with the others, written
           ONLY from the figures the agent confirmed. Any number in a sentence that is not
           one of those figures sends it back once; if it still has one, it is returned
           marked for the agent to fix, never silently printed.

Every address is geocoded first and must land on a street address (ROOFTOP or
RANGE_INTERPOLATED, not a partial match). A route through a guessed point is worse than
no route, so an address Google can only place approximately is refused with the reason.

The Google key lives on the server (GOOGLE_MAPS_SERVER_KEY), never in the browser; the
map images come back as image bytes.

Everything here is pure or takes its HTTP functions as arguments, so the tests run with
no network.
"""
from __future__ import annotations

import base64
import itertools
import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Awaitable, Callable, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MAX_HOMES = 8                 # map labels are single digits; 8! orders are tried in full
BLOCKED_DOMAINS = ["zillow.com", "trulia.com", "hotpads.com"]
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
STATIC_URL = "https://maps.googleapis.com/maps/api/staticmap"
NAVY = "0x0d1f35"
GOLD = "0xb8955a"
BANNED = ("stunning", "luxurious", "impeccable", "nestled", "boasts", "charming")
WHO_RX = re.compile(
    r"\b(?:family[- ]friendly|famil(?:y|ies)|kids?|children|young professionals?|empty[- ]nesters?|"
    r"retirees?|newlyweds?|bachelor|singles?|couples?|first[- ]time (?:home ?)?buyers?|"
    r"schools?|church(?:es)?|synagogues?|mosques?|handicap(?:ped)?|perfect for|ideal for)\b", re.I)
# Marc, Oct 30 2026: the tour sheet is objective ("here's where we're going, here's what they
# look like"). No negotiating or value commentary, e.g. "room to negotiate" from days on market.
ADVICE_RX = re.compile(
    r"\b(?:negotiat\w*|leverage|bargain|good deal|great deal|best deal|(?:best|good|great) value|"
    r"underpriced|overpriced|priced to sell|motivated|price (?:cut|drop|reduction)|reduced|offers? (?:below|under|over|above)|"
    r"below asking|over asking|bidding war|compet\w+ offers?|move quickly|act fast|won'?t last|sit(?:s|ting)? on the market|stale|"
    r"worth (?:it|the)|investment|appreciat\w*|resale|should (?:offer|buy|consider))\b", re.I)

PostJson = Callable[[str, dict, dict], Awaitable[object]]
GetJson = Callable[[str, dict], Awaitable[object]]
GetBytes = Callable[[str], Awaitable[bytes]]
PostClaude = Callable[[dict], Awaitable[dict]]


class TourError(Exception):
    """Something that stops a step, in words the agent can act on."""


# ── text checks shared by everything the AI writes for a client ─────────────────

def text_issues(t: str) -> list[str]:
    """What is wrong with client-facing text, in plain words. [] when nothing is."""
    text, out = str(t or ""), []
    low = text.lower()
    for w in BANNED:
        if re.search(r"(^|[^a-z])" + w + r"([^a-z]|$)", low):
            out.append('uses "{0}", a word FORWARD does not use'.format(w))
    if re.search("[—–]", text):
        out.append("has a long dash")
    m = WHO_RX.search(text)
    if m:
        out.append('"{0}" says who should live there or names a protected group (fair housing)'.format(m.group(0)))
    a = ADVICE_RX.search(text)
    if a:
        out.append('"{0}" is commentary on price or negotiating; the tour sheet states facts only'.format(a.group(0)))
    return out


# ── listing lookup ──────────────────────────────────────────────────────────────

FIELDS = ("list_price", "beds", "baths_full", "baths_half", "sqft", "days_on_market", "mls_number")
FIELD_LABELS = {"list_price": "List price", "beds": "Bedrooms", "baths_full": "Full baths",
                "baths_half": "Half baths", "sqft": "Square feet", "days_on_market": "Days on market",
                "mls_number": "MLS number"}

LOOKUP_SYSTEM = (
    "You find the live MLS listing for ONE home in Washington DC, Maryland or Virginia, using web search, "
    "for a real estate agent planning a showing tour. Prefer the listing brokerage's own page, realtor.com, "
    "homes.com and redfin.com. Do not use zillow.com, trulia.com or hotpads.com.\n\n"
    "Rules:\n"
    "- Report ONLY what a page you can name shows, each figure with that page's address.\n"
    "- Copy every figure exactly as the page shows it. Never round, estimate, convert or calculate.\n"
    "- If two pages show different values for the same figure, list both.\n"
    "- The listing must be for this exact address, including the unit number when there is one. "
    "A different unit in the same building is NOT this home.\n"
    "- overview: one or two sentences in your own words, from the listing's description, saying what the "
    "home is and its most notable features. Never copy more than four words in a row from the description. "
    "Describe the home and its location only, never who should live there or any group of people, and "
    "never comment on price, value or negotiating. "
    "No long dashes. Never use the words stunning, luxurious, impeccable, nestled, boasts or charming.\n\n"
    "Return ONLY valid JSON in this exact shape, nothing before or after it:\n"
    '{"matched_address":"","status":"","list_price":[{"value":"","source_url":""}],'
    '"beds":[{"value":"","source_url":""}],"baths_full":[{"value":"","source_url":""}],'
    '"baths_half":[{"value":"","source_url":""}],"sqft":[{"value":"","source_url":""}],'
    '"days_on_market":[{"value":"","source_url":""}],"mls_number":[{"value":"","source_url":""}],'
    '"overview":"","overview_source_url":""}\n'
    "status is the listing status as the page states it (for example Active, Coming Soon, Under Contract). "
    "Leave a list empty rather than guess. If you cannot find a live listing for this exact address, return "
    '{"matched_address":"","not_found_reason":"why, in one sentence"}.'
)

_URL_RX = re.compile(r"^https?://\S+\.\S+")
_UNIT_RX = re.compile(r"(?:#|\bunit\b|\bapt\.?\b|\bapartment\b|\bsuite\b|\bste\.?\b)\s*([a-z0-9-]+)", re.I)


def _norm_unit(u: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (u or "").lower()).lstrip("0") or "0"


def address_problem(typed: str, matched: str) -> str:
    """'' when the page's address is plausibly the typed one, else why not. The street
    number must match; when the typed address has a unit, the page's must carry the same
    unit (a different unit in the same building is a different home)."""
    t, m = (typed or "").strip(), (matched or "").strip()
    if not m:
        return "the search did not say which address it found"
    tn, mn = re.match(r"\s*(\d+[a-z]?)\b", t, re.I), re.match(r"\s*(\d+[a-z]?)\b", m, re.I)
    if not tn or not mn or tn.group(1).lower() != mn.group(1).lower():
        return "the listing found is at {0}, not this street number".format(m)
    tu = _UNIT_RX.search(t)
    if tu:
        mu = _UNIT_RX.search(m)
        if not mu or _norm_unit(mu.group(1)) != _norm_unit(tu.group(1)):
            return "the listing found is {0}, not unit {1}".format(m, tu.group(1))
    return ""


def _number(field: str, raw: str) -> Optional[float]:
    s = str(raw or "").strip()
    if field == "mls_number":
        return None
    s2 = re.sub(r"[,$\s]", "", s)
    if not re.fullmatch(r"\d+(?:\.\d+)?", s2):
        return None
    return float(s2)


def clean_lookup(raw: dict, typed_address: str) -> dict:
    """The lookup as the app shows it. Each field: {value, display, sources:[...],
    conflict, values:[...]} or missing. Values without a real page address are dropped
    and counted. `problems` lists what stops the home from being used as found."""
    if not isinstance(raw, dict):
        raise TourError("the listing search gave no readable answer")
    out = {"address": typed_address, "fields": {}, "dropped_without_source": 0, "problems": []}
    if raw.get("not_found_reason") and not raw.get("matched_address"):
        out["found"] = False
        out["problems"].append("No live listing found: {0}".format(str(raw["not_found_reason"]).strip()))
        return out
    out["matched_address"] = str(raw.get("matched_address") or "").strip()
    out["status"] = str(raw.get("status") or "").strip()
    ap = address_problem(typed_address, out["matched_address"])
    if ap:
        out["problems"].append("Check the address: " + ap)
    for f in FIELDS:
        items = raw.get(f)
        kept = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                out["dropped_without_source"] += 1
                continue
            url, val = str(it.get("source_url") or "").strip(), str(it.get("value") or "").strip()
            if not val:
                continue
            if not _URL_RX.match(url) or any(d in url.lower() for d in BLOCKED_DOMAINS):
                out["dropped_without_source"] += 1
                continue
            kept.append({"display": val, "number": _number(f, val), "source_url": url})
        if not kept:
            continue
        distinct = []
        for k in kept:
            key = k["number"] if k["number"] is not None else k["display"].lower()
            if key not in [d[0] for d in distinct]:
                distinct.append((key, k))
        first = kept[0]
        out["fields"][f] = {"label": FIELD_LABELS[f], "display": first["display"], "value": first["number"],
                            "sources": kept, "conflict": len(distinct) > 1,
                            "values": [d[1]["display"] for d in distinct]}
    ov = str(raw.get("overview") or "").strip()
    ov_url = str(raw.get("overview_source_url") or "").strip()
    if ov:
        out["overview"] = {"text": ov, "source_url": ov_url if _URL_RX.match(ov_url) else "",
                           "issues": text_issues(ov)}
    st = out["status"].lower()
    if st and not re.search(r"\b(active|coming soon|for sale)\b", st):
        out["problems"].append("The listing status is {0}, not active".format(out["status"]))
    missing = [FIELD_LABELS[f] for f in ("list_price", "beds", "baths_full", "sqft", "days_on_market")
               if f not in out["fields"]]
    if missing:
        out["problems"].append("Not found on any page: " + ", ".join(missing) + ". Fill these in yourself")
    out["found"] = True
    return out


def _parse_json_answer(content: list, keys: tuple) -> dict:
    """The last JSON object in the model's text that has one of `keys` (notes can sit
    around it when web search is on; same approach as avatar_research)."""
    text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    dec, found = json.JSONDecoder(), None
    for m in re.finditer(r"\{", text):
        try:
            val, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(val, dict) and any(k in val for k in keys):
            found = val
    if found is None:
        raise TourError("the AI answer could not be read")
    return found


MAX_PAUSES = 3


async def lookup(address: str, model: str, post: PostClaude) -> dict:
    """One home, searched on the web. Raises TourError when the search cannot run."""
    address = (address or "").strip()
    if len(address) < 8 or not re.match(r"\s*\d", address):
        raise TourError("Type the full address, starting with the street number")
    messages = [{"role": "user", "content": "Find the live listing for this home.\nAddress: " + address}]
    body = {"model": model, "max_tokens": 4000, "system": LOOKUP_SYSTEM, "messages": messages,
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5,
                       "blocked_domains": BLOCKED_DOMAINS}]}
    content, stops = [], []
    for _ in range(MAX_PAUSES + 1):
        try:
            data = await post(dict(body, messages=list(messages)))
        except Exception as e:
            raise TourError("The listing search did NOT run: {0}".format(e))
        part = (data or {}).get("content") or []
        content += [b for b in part if isinstance(b, dict)]
        stops.append((data or {}).get("stop_reason"))
        if stops[-1] != "pause_turn":
            break
        messages.append({"role": "assistant", "content": part})
    try:
        raw = _parse_json_answer(content, ("matched_address", "not_found_reason"))
    except TourError:
        if stops and stops[-1] == "pause_turn":
            raise TourError("The listing search did not finish (still searching after {0} rounds)".format(len(stops)))
        raise TourError("The listing search answer could not be read")
    return clean_lookup(raw, address)


# ── comparison sentences ────────────────────────────────────────────────────────

COMPARE_SYSTEM = (
    "You write for a real estate agent's client: a short tour sheet of homes the client will see. "
    "For EACH home, write one or two sentences giving a brief overview of the home and how it compares "
    "with the other homes on this tour.\n\n"
    "Rules:\n"
    "- Use ONLY the facts given. Any number you write must appear in the facts exactly as written. "
    "Never calculate anything (no differences, no price per square foot, no percentages).\n"
    "- Comparisons in words are fine: the largest, the only one with a half bath, the newest on the market.\n"
    "- Objective facts only. No advice and no commentary on price, value or negotiating (never say a home "
    "may leave room to negotiate, is a good value, is priced to sell, or should get an offer). Days on market "
    "is stated as a fact, never interpreted.\n"
    "- Describe the homes only, never who should live there or any group of people (fair housing). "
    "No schools.\n"
    "- No long dashes. Never use the words stunning, luxurious, impeccable, nestled, boasts or charming.\n\n"
    'Return ONLY valid JSON: {"homes":[{"stop":1,"comparison":""}]} with one entry per home, in the same '
    "order and with the same stop numbers."
)


def _numbers_in(text: str) -> set:
    """Every number written in a text, normalized ('$1,250,000' -> '1250000', '2.5' -> '2.5')."""
    out = set()
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", str(text or "")):
        s = m.group(0).replace(",", "")
        s = s.rstrip(".")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        out.add(s)
    return out


def compare_issues(sentence: str, allowed: set, stop_no: int) -> list[str]:
    issues = text_issues(sentence)
    extra = sorted(n for n in _numbers_in(sentence) if n not in allowed)
    if extra:
        issues.append("has a number that is not in the confirmed facts: " + ", ".join(extra))
    if not str(sentence or "").strip():
        issues.append("is empty")
    return issues


def compare_facts(homes: list[dict]) -> tuple[str, set]:
    """The facts block the AI sees, and the set of numbers it may use."""
    lines, allowed = [], set()
    for h in homes:
        parts = ["Stop {0}: {1}".format(h["stop"], h.get("address", ""))]
        for k in ("list_price", "beds", "baths_full", "baths_half", "sqft", "days_on_market"):
            v = h.get(k)
            if v not in (None, ""):
                parts.append("{0}: {1}".format(FIELD_LABELS[k], v))
        if h.get("overview"):
            parts.append("Description: " + str(h["overview"]))
        lines.append("; ".join(parts))
        allowed |= _numbers_in(json.dumps({k: h.get(k) for k in h if k != "overview"}))
        allowed |= _numbers_in(h.get("overview", ""))
    return "\n".join(lines), allowed


async def compare(homes: list[dict], model: str, post: PostClaude) -> dict:
    """Comparison sentences for the confirmed homes. Each comes back with its issues
    ([] when clean). One retry, told exactly what was wrong."""
    if not homes or len(homes) > MAX_HOMES:
        raise TourError("A tour needs 1 to {0} homes".format(MAX_HOMES))
    facts, allowed = compare_facts(homes)
    msgs = [{"role": "user", "content": "The homes on this tour, as the agent confirmed them:\n" + facts}]
    result = {}
    for attempt in range(2):
        try:
            data = await post({"model": model, "max_tokens": 2000, "system": COMPARE_SYSTEM, "messages": msgs})
        except Exception as e:
            raise TourError("The comparison writer did NOT run: {0}".format(e))
        try:
            raw = _parse_json_answer((data or {}).get("content") or [], ("homes",))
        except TourError:
            raw = {"homes": []}
        by_stop = {}
        for it in raw.get("homes") or []:
            if isinstance(it, dict):
                try:
                    by_stop[int(it.get("stop"))] = str(it.get("comparison") or "").strip()
                except (TypeError, ValueError):
                    pass
        result = {h["stop"]: {"text": by_stop.get(h["stop"], ""),
                              "issues": compare_issues(by_stop.get(h["stop"], ""), allowed, h["stop"])}
                  for h in homes}
        bad = {s: r["issues"] for s, r in result.items() if r["issues"]}
        if not bad or attempt == 1:
            break
        fix = "\n".join("Stop {0} {1}.".format(s, "; ".join(i)) for s, i in bad.items())
        msgs = msgs + [{"role": "assistant", "content": json.dumps(raw)},
                       {"role": "user", "content": "Rewrite. These were wrong:\n" + fix + "\nSame JSON shape."}]
    return {"homes": [{"stop": s, "comparison": r["text"], "issues": r["issues"]} for s, r in result.items()]}


# ── geocoding, times and routes ─────────────────────────────────────────────────

def geocode_result(payload: dict, address: str) -> dict:
    """{lat, lng, formatted} for a street-level match, else TourError saying why."""
    st = (payload or {}).get("status")
    if st == "ZERO_RESULTS":
        raise TourError("Google could not find {0}. Check the spelling, city and ZIP".format(address))
    if st != "OK":
        msg = (payload or {}).get("error_message") or st or "no answer"
        raise TourError("The address check did NOT run ({0})".format(msg))
    r = (payload.get("results") or [{}])[0]
    lt = ((r.get("geometry") or {}).get("location_type")) or ""
    if r.get("partial_match") or lt not in ("ROOFTOP", "RANGE_INTERPOLATED"):
        raise TourError("Google could only place {0} approximately (it matched {1}). Check the spelling, "
                        "city and ZIP".format(address, r.get("formatted_address") or "an area, not a street address"))
    loc = (r.get("geometry") or {}).get("location") or {}
    return {"lat": float(loc["lat"]), "lng": float(loc["lng"]), "formatted": r.get("formatted_address", "")}


def seconds(d) -> int:
    """A Routes API duration ('1234.5s') in whole seconds, rounded UP."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(d or ""))
    if not m:
        raise TourError("Google gave a drive time that could not be read")
    return int(math.ceil(float(m.group(1))))


def parse_hhmm(s: str) -> int:
    """'10:05' or '14:30' -> minutes after midnight."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(s or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise TourError('Times are written like 10:30 or 14:00, not "{0}"'.format(s))
    return int(m.group(1)) * 60 + int(m.group(2))


def hhmm(mins: int) -> str:
    return "{0:02d}:{1:02d}".format(mins // 60, mins % 60)


def ceil5(mins: float) -> int:
    return int(math.ceil(mins / 5.0) * 5)


def best_order(matrix: list[list[int]], n: int, origin: Optional[int] = None) -> list[int]:
    """The order of homes 0..n-1 with the least total driving, every order tried.
    matrix[i][j] = seconds from point i to point j; with an origin (the agent's starting
    point, index n) the drive from it to the first home counts. Ties go to the order that
    sorts first, so the answer never changes between runs."""
    best, best_cost = None, None
    for perm in itertools.permutations(range(n)):
        cost = matrix[origin][perm[0]] if origin is not None else 0
        for a, b in zip(perm, perm[1:]):
            cost += matrix[a][b]
        if best_cost is None or cost < best_cost:
            best, best_cost = list(perm), cost
    return best


ALLOWED_MINUTES = (15, 30, 45, 60)   # the agent's dropdown, per home (Marc, Oct 30 2026)


def plan_schedule(order: list[int], legs_sec: list[int], start: int, minutes: list[int], buffer_min: int) -> list[dict]:
    """Suggested times: the first home at `start`; each next one at the end of the last
    plus the drive plus the buffer, rounded UP to 5 minutes. minutes[k] is the time at the
    k-th home in tour order (each home has its own: an hour at one, 30 minutes at the next)."""
    out, t = [], start
    for k, i in enumerate(order):
        if k > 0:
            t = ceil5(out[-1]["end_min"] + legs_sec[k - 1] / 60.0 + buffer_min)
        out.append({"home": i, "start_min": t, "end_min": t + minutes[k],
                    "start": hhmm(t), "end": hhmm(t + minutes[k])})
    return out


def check_times(stops: list[dict], legs_sec: list[int], buffer_min: int) -> list[dict]:
    """Confirmed mode. `stops` in time order with start_min/end_min; legs_sec[k] is the
    drive from stop k to stop k+1. Returns one row per leg with a warning when the gap is
    short or the showings overlap."""
    rows = []
    for k in range(len(stops) - 1):
        a, b = stops[k], stops[k + 1]
        drive = int(math.ceil(legs_sec[k] / 60.0))
        gap = b["start_min"] - a["end_min"]
        warn = ""
        if gap < 0:
            warn = "Stop {0} starts at {1}, before stop {2} ends at {3}".format(k + 2, b["start"], k + 1, a["end"])
        elif gap < drive + buffer_min:
            warn = ("Leaving stop {0} at {1}, the drive is about {2} min, so you reach stop {3} around {4}; "
                    "its showing starts at {5}").format(k + 1, a["end"], drive, k + 2,
                                                         hhmm(a["end_min"] + drive), b["start"])
        rows.append({"from": k + 1, "to": k + 2, "drive_min": drive, "gap_min": gap, "warning": warn})
    return rows


def departure_rfc3339(tour_date: str, mins: int, now: Optional[datetime] = None) -> Optional[str]:
    """The departure time for traffic-aware driving, or None when it is not in the future
    (Google refuses a past departure for driving; then traffic is not considered)."""
    try:
        d = date.fromisoformat(tour_date)
    except (TypeError, ValueError):
        raise TourError("The tour date must be a date like 2026-10-31")
    dt = datetime(d.year, d.month, d.day, tzinfo=ET) + timedelta(minutes=mins)
    now = now or datetime.now(ET)
    if dt <= now + timedelta(minutes=1):
        return None
    return dt.isoformat()


def _wp(p: dict) -> dict:
    return {"waypoint": {"location": {"latLng": {"latitude": p["lat"], "longitude": p["lng"]}}}}


def matrix_body(points: list[dict], departure: Optional[str]) -> dict:
    body = {"origins": [_wp(p) for p in points], "destinations": [_wp(p) for p in points],
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE" if departure else "TRAFFIC_UNAWARE"}
    if departure:
        body["departureTime"] = departure
    return body


def read_matrix(payload, size: int) -> list[list[int]]:
    rows = payload if isinstance(payload, list) else []
    m = [[0] * size for _ in range(size)]
    seen = set()
    for el in rows:
        if not isinstance(el, dict):
            continue
        i, j = el.get("originIndex", 0), el.get("destinationIndex", 0)
        if i == j:
            seen.add((i, j))
            continue
        if (el.get("status") or {}).get("code") or el.get("condition") != "ROUTE_EXISTS":
            raise TourError("Google found no driving route between two of the addresses")
        m[i][j] = seconds(el.get("duration"))
        seen.add((i, j))
    if len(seen) < size * size:
        raise TourError("Google's drive times came back incomplete")
    return m


def routes_body(points: list[dict], departure: Optional[str]) -> dict:
    body = {"origin": _wp(points[0])["waypoint"], "destination": _wp(points[-1])["waypoint"],
            "intermediates": [_wp(p)["waypoint"] for p in points[1:-1]],
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE" if departure else "TRAFFIC_UNAWARE",
            "polylineEncoding": "ENCODED_POLYLINE"}
    if departure:
        body["departureTime"] = departure
    return body


def read_route(payload: dict, n_legs: int) -> tuple[list[int], str]:
    routes = (payload or {}).get("routes") or []
    if not routes:
        raise TourError("Google found no driving route through these addresses")
    r = routes[0]
    legs = r.get("legs") or []
    if len(legs) != n_legs:
        raise TourError("Google's route came back with the wrong number of drives")
    return [seconds(l.get("duration")) for l in legs], ((r.get("polyline") or {}).get("encodedPolyline") or "")


def static_map_url(homes: list[dict], key: str, polyline: str = "", start: Optional[dict] = None) -> str:
    """The map image address. Homes are numbered 1..n in tour order (navy pins); the
    agent's copy adds the route line (gold) and the starting point (S)."""
    parts = ["size=640x400", "scale=2", "format=png", "maptype=roadmap"]
    for i, h in enumerate(homes, start=1):
        parts.append("markers=" + quote("color:{0}|label:{1}|{2:.6f},{3:.6f}".format(NAVY, i, h["lat"], h["lng"]), safe=":,"))
    if start:
        parts.append("markers=" + quote("color:{0}|label:S|{1:.6f},{2:.6f}".format(GOLD, start["lat"], start["lng"]), safe=":,"))
    if polyline:
        parts.append("path=" + quote("color:{0}ff|weight:4|enc:{1}".format(GOLD, polyline), safe=":,"))
    parts.append("key=" + quote(key, safe=""))
    return STATIC_URL + "?" + "&".join(parts)


async def route(req: dict, key: str, get: GetJson, post: PostJson, get_bytes: GetBytes,
                now: Optional[datetime] = None) -> dict:
    """Plan or confirmed mode, see the module docstring. `req`:
    {mode, date, start_time (plan), buffer_minutes, start_address (optional),
     stops:[{address, minutes (plan: 15, 30, 45 or 60), start?, end? (confirmed)}]}"""
    mode = req.get("mode")
    if mode not in ("plan", "confirmed"):
        raise TourError("mode must be plan or confirmed")
    stops = [s for s in (req.get("stops") or []) if isinstance(s, dict) and str(s.get("address") or "").strip()]
    if not stops or len(stops) > MAX_HOMES:
        raise TourError("A tour needs 1 to {0} homes".format(MAX_HOMES))
    buffer_min = int(req.get("buffer_minutes", 5) or 0)
    if buffer_min < 0 or buffer_min > 60:
        raise TourError("The buffer must be 0 to 60 minutes")

    if mode == "confirmed":
        for s in stops:
            s["start_min"], s["end_min"] = parse_hhmm(s.get("start")), parse_hhmm(s.get("end"))
            if s["end_min"] <= s["start_min"]:
                raise TourError("{0}: the end time must be after the start time".format(s["address"]))
            s["start"], s["end"] = hhmm(s["start_min"]), hhmm(s["end_min"])
        order_in = sorted(range(len(stops)), key=lambda i: (stops[i]["start_min"], i))
        stops = [stops[i] for i in order_in]
    else:
        start = parse_hhmm(req.get("start_time"))
        for s in stops:
            try:
                s["minutes"] = int(s.get("minutes"))
            except (TypeError, ValueError):
                s["minutes"] = None
            if s["minutes"] not in ALLOWED_MINUTES:
                raise TourError("{0}: pick the time at this home (15, 30, 45 min or 1 hour)".format(s["address"].strip()))

    async def geo(addr):
        try:
            payload = await get(GEOCODE_URL, {"address": addr, "key": key})
        except TourError:
            raise
        except Exception as e:
            raise TourError("The address check did NOT run: {0}".format(e))
        return geocode_result(payload, addr)

    pts = [await geo(s["address"].strip()) for s in stops]
    start_pt = await geo(req["start_address"].strip()) if str(req.get("start_address") or "").strip() else None

    if mode == "plan":
        n = len(pts)
        if n == 1:
            order = [0]
        else:
            allpts = pts + ([start_pt] if start_pt else [])
            dep = departure_rfc3339(req.get("date"), start, now)
            try:
                mpay = await post(MATRIX_URL, matrix_body(allpts, dep),
                                  {"X-Goog-FieldMask": "originIndex,destinationIndex,status,condition,duration"})
            except TourError:
                raise
            except Exception as e:
                raise TourError("The drive times did NOT load: {0}".format(e))
            order = best_order(read_matrix(mpay, len(allpts)), n, n if start_pt else None)
        stops = [stops[i] for i in order]
        pts = [pts[i] for i in order]
        first_dep_min = start
    else:
        first_dep_min = stops[0]["end_min"]

    legs = []
    poly = ""
    if len(pts) > 1:
        dep = departure_rfc3339(req.get("date"), first_dep_min, now)
        try:
            rpay = await post(ROUTES_URL, routes_body(pts, dep),
                              {"X-Goog-FieldMask": "routes.legs.duration,routes.polyline.encodedPolyline"})
        except TourError:
            raise
        except Exception as e:
            raise TourError("The route did NOT load: {0}".format(e))
        legs, poly = read_route(rpay, len(pts) - 1)

    if mode == "plan":
        sched = plan_schedule(list(range(len(pts))), legs, start, [s["minutes"] for s in stops], buffer_min)
        for s, t in zip(stops, sched):
            s.update({k: t[k] for k in ("start", "end", "start_min", "end_min")})
    rows = check_times(stops, legs, buffer_min)

    out_stops = [{"stop": i + 1, "address": s["address"].strip(), "start": s["start"], "end": s["end"],
                  "minutes": s["end_min"] - s["start_min"],
                  "lat": p["lat"], "lng": p["lng"], "google_address": p["formatted"]}
                 for i, (s, p) in enumerate(zip(stops, pts))]

    async def img(url):
        try:
            b = await get_bytes(url)
        except Exception as e:
            raise TourError("The map image did NOT load: {0}".format(e))
        if not b.startswith(b"\x89PNG"):
            raise TourError("The map image did NOT load (Google did not send an image)")
        return "data:image/png;base64," + base64.b64encode(b).decode("ascii")

    return {"mode": mode, "date": req.get("date"), "buffer_minutes": buffer_min,
            "stops": out_stops, "legs": rows,
            "warnings": [r["warning"] for r in rows if r["warning"]],
            "traffic": departure_rfc3339(req.get("date"), first_dep_min, now) is not None,
            "maps": {"agent": await img(static_map_url(pts, key, poly, start_pt)),
                     "client": await img(static_map_url(pts, key))}}
