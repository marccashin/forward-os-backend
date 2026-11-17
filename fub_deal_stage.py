"""Where a FORWARD OS listing stands in Follow Up Boss (Oct 17, 2026).

FORWARD OS's Deal Progress tab showed only "not in the Command Center yet" for a
listing before Listing Agreement Signed, though the deal is at a real stage in
Follow Up Boss (Marc: "it should have a deal stage in FUB which means it is in
process somewhere"). This module finds the listing's Sellers deal and returns its
stage and when it entered it. Nothing else of the deal leaves the backend, and the
FUB key never reaches the browser.

Which deal:
  1. the deal id the Seller Engaged webhook stamped on the card
     (subfolder_drive_ids._fub_deal_id), the same key the webhook's duplicate guard
     reads. The top-level fub_deal_id column is NOT used: it holds legacy Command
     Center ids.
  2. else a Sellers deal whose name is the listing's street address (the guide tells
     agents to put the address in the deal card's name field). The address rule is a
     copy of FORWARD OS's dpSameAddress / the Command Center's addressTokens: keep the
     three the same.
Two or more address matches are reported as ambiguous, never guessed.

Pure functions here; the route and the FUB calls live in main.py.
"""
from __future__ import annotations

import re

SUFFIXES = {
    "street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "court": "ct",
    "place": "pl", "boulevard": "blvd", "lane": "ln", "terrace": "ter", "circle": "cir",
    "highway": "hwy", "parkway": "pkwy", "square": "sq", "trail": "trl", "way": "way",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}


def addr_tokens(raw) -> list[str]:
    s = str(raw or "").lower().replace("#", " unit ")
    s = re.sub(r"\b(apt|apartment|suite|ste|unit)\b\.?", " unit ", s)
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return []
    out: list[str] = []
    for t in s.split(" "):
        tok = SUFFIXES.get(t, t)
        if tok == "unit" and out and out[-1] == "unit":
            continue
        out.append(tok)
    return out


def same_address(a, b) -> bool:
    x, y = addr_tokens(a), addr_tokens(b)
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    if len(short) < 3 or not re.search(r"\d", short[0]):
        return False
    return all(i < len(long_) and long_[i] == tok for i, tok in enumerate(short))


def stamped_deal_id(prop: dict) -> str:
    sdi = (prop or {}).get("subfolder_drive_ids") or {}
    if not isinstance(sdi, dict):
        return ""
    return str(sdi.get("_fub_deal_id") or "").strip()


_STREET_TYPES = {"st", "ave", "rd", "dr", "ct", "pl", "blvd", "ln", "ter", "cir", "hwy", "pkwy", "sq", "trl", "way"}


def same_address_loose(a, b) -> bool:
    """same_address with street types left out ("St", "Ave", ...), for deal names
    typed without one: "2500 N Van Dorn #205" is "2500 N Van Dorn St, Unit 205".
    Used only when the strict rule finds nothing, and only a single hit counts."""
    x = [t for t in addr_tokens(a) if t not in _STREET_TYPES]
    y = [t for t in addr_tokens(b) if t not in _STREET_TYPES]
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    if len(short) < 3 or not re.search(r"\d", short[0]):
        return False
    return all(i < len(long_) and long_[i] == tok for i, tok in enumerate(short))


def _norm_name(v) -> str:
    return " ".join(str(v or "").lower().replace(".", "").replace(",", " ").split())


FINISHED_STAGES = {"closed this quarter", "total closed this year", "previous year", "total", "terminated"}


def _one_open(hits: list[dict]) -> list[dict]:
    """Of several deals at one address, the single one still in progress, if there is
    exactly one. A home sold earlier and listed again (2909 7th St NE, Oct 2026: bought,
    flipped, now for sale) has a finished deal and a live one; the listing card is the
    live sale."""
    live = [d for d in hits if str(d.get("stageName") or "").strip().lower() not in FINISHED_STAGES]
    return live if len(live) == 1 else []


def pick_by_address(address: str, deals: list[dict]) -> tuple[list[dict], str]:
    """Sellers deals that are this listing. Returns (hits, how).

    1. strict address rule (same as FORWARD OS and the Command Center);
    2. else the same rule without street types, single hit only;
    3. else, for a listing whose "address" is not an address (a card named after the
       seller, as the Seller Engaged webhook does when the deal card keeps the
       seller's name), a deal whose name is exactly that name, single hit only.
    Two or more hits at any step are ambiguous, unless exactly one of them is still in
    progress (not closed or terminated): that one is the listing's live sale."""
    deals = [d for d in (deals or []) if isinstance(d, dict)]
    hits = [d for d in deals if same_address(address, d.get("name"))]
    if len(hits) == 1:
        return hits, "address"
    if len(hits) > 1:
        return (_one_open(hits), "address") if _one_open(hits) else (hits, "ambiguous")
    hits = [d for d in deals if same_address_loose(address, d.get("name"))]
    if len(hits) == 1:
        return hits, "address"
    if len(hits) > 1:
        return (_one_open(hits), "address") if _one_open(hits) else (hits, "ambiguous")
    if not name_is_address(address) and _norm_name(address):
        hits = [d for d in deals if _norm_name(d.get("name")) == _norm_name(address)]
        if len(hits) == 1:
            return hits, "name"
        if len(hits) > 1:
            return (_one_open(hits), "name") if _one_open(hits) else (hits, "ambiguous")
    return [], "none"


def name_is_address(name) -> bool:
    """True when a deal's name reads as a street address: a house number, then a word
    ("6300 Stevenson Ave #1120", "1415 Riverside Ave."). The guide tells agents to put
    the address in the deal card's name, not the seller's name (Seller Engaged step);
    FORWARD OS reminds them when the name is a person's ("Abby Wenk")."""
    return bool(re.match(r"^\s*\d+[a-z]?(-\d+)?\s+[a-z0-9]", str(name or ""), re.I))


def summarize(deal: dict | None, how: str, count: int = 0) -> dict:
    """What FORWARD OS gets: stage and when it entered it, nothing personal."""
    if not deal:
        return {"found": False, "how": how, "count": count}
    return {
        "found": True,
        "how": how,
        "deal_id": deal.get("id"),
        "pipeline": deal.get("pipelineName") or "",
        "stage": deal.get("stageName") or "",
        "entered_stage_at": deal.get("enteredStageAt") or None,
        "name_is_address": name_is_address(deal.get("name")),
    }


# ---------------------------------------------------------------------------
# Buyers (Nov 16, 2026): where a FORWARD OS buyer stands in Follow Up Boss, for the
# buyer folder's Deal Progress tab (the listing one, mirrored).
#
# Which deal:
#   1. the deal id the Buyer Engaged webhook stamped on the OS buyer card
#      (subfolder_drive_ids._fub_deal_id). The top-level fub_deal_id column is not
#      used, the same as for listings.
#   2. else a Buyers deal whose name, or one of whose people, is the buyer's name
#      exactly (same words, any case or punctuation), handled by the buyer's agent
#      or a co-agent. The OS buyer card is named after the first person on the deal
#      (the webhook copies it), so this is how an unstamped card is found.
# Two or more hits are reported as ambiguous unless exactly one is still in progress.
# ---------------------------------------------------------------------------
def _name_tokens(name) -> list[str]:
    toks = re.split(r"[^a-z]+", str(name or "").lower().replace("&", " "))
    return [t for t in toks if t and t != "and"]


def pick_buyer_deal(buyer_name: str, agents: set[str], deals: list[dict], agent_map: dict | None = None) -> tuple[list[dict], str]:
    """Buyers deals that are this OS buyer. Returns (hits, how): how is "name",
    "ambiguous" or "none". agents: the buyer's agent and co-agents (OS names);
    agent_map turns a Follow Up Boss user name into the OS name."""
    want = " ".join(_name_tokens(buyer_name))
    if not want:
        return [], "none"
    agent_map = agent_map or {}

    def deal_agents(d: dict) -> set[str]:
        return {agent_map.get(u.get("name") or "", u.get("name") or "") for u in (d.get("users") or []) if isinstance(u, dict) and u.get("name")}

    hits = []
    for d in deals or []:
        if not isinstance(d, dict):
            continue
        names = [d.get("name")] + [p.get("name") for p in (d.get("people") or []) if isinstance(p, dict)]
        if not any(" ".join(_name_tokens(n)) == want for n in names):
            continue
        if agents and not (deal_agents(d) & agents):
            continue
        hits.append(d)
    if len(hits) == 1:
        return hits, "name"
    if len(hits) > 1:
        return (_one_open(hits), "name") if _one_open(hits) else (hits, "ambiguous")
    return [], "none"


def summarize_buyer(deal: dict | None, how: str, count: int = 0) -> dict:
    """summarize(), plus the deal card's name ONLY when it is a street address: the
    guide has the agent rename the card to the property address when the offer is
    accepted, and that address is how Open To Close's file is found. A name that is a
    person's never leaves the backend."""
    out = summarize(deal, how, count)
    if deal and out.get("name_is_address"):
        out["address"] = str(deal.get("name") or "").strip()
    return out
