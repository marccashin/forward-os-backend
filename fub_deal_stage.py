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


def pick_by_address(address: str, deals: list[dict]) -> tuple[list[dict], str]:
    """Sellers deals that are this listing. Returns (hits, how).

    1. strict address rule (same as FORWARD OS and the Command Center);
    2. else the same rule without street types, single hit only;
    3. else, for a listing whose "address" is not an address (a card named after the
       seller, as the Seller Engaged webhook does when the deal card keeps the
       seller's name), a deal whose name is exactly that name, single hit only.
    Two or more hits at any step are reported as ambiguous, never guessed."""
    deals = [d for d in (deals or []) if isinstance(d, dict)]
    hits = [d for d in deals if same_address(address, d.get("name"))]
    if len(hits) == 1:
        return hits, "address"
    if len(hits) > 1:
        return hits, "ambiguous"
    hits = [d for d in deals if same_address_loose(address, d.get("name"))]
    if len(hits) == 1:
        return hits, "address"
    if len(hits) > 1:
        return hits, "ambiguous"
    if not name_is_address(address) and _norm_name(address):
        hits = [d for d in deals if _norm_name(d.get("name")) == _norm_name(address)]
        if len(hits) == 1:
            return hits, "name"
        if len(hits) > 1:
            return hits, "ambiguous"
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
