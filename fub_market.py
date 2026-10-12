"""A listing's market (DC, MD or VA) from what Follow Up Boss and the address say.

Marc, Oct 12, 2026: a listing's state is never assumed. Until then a seller deal
whose FUB Market was empty, and whose address named no state, was created as DC
("last resort"), and the address fallback matched plain text: " va" is inside
"300 Valley Rd", so a Bethesda, MD house could be created as VA.

Now:
- normalise_market() reads FUB's Market value. It accepts the same names as the
  OS's fosMarketCode() in index.html (forward-os): DC / MD / VA, Maryland,
  Virginia, Northern Virginia, NoVA, District of Columbia, Washington, DC, in any
  case or punctuation. Change one, change the other.
- market_from_address() answers only when the address names the state itself, as
  a whole word (MD, VA, DC, D.C., Maryland, Virginia, District of Columbia). A
  city or county name alone decides nothing. Two different states named: nothing.
- Anything else gives "": the listing is created with no state, the OS shows
  "Set state" on its badge, and the net sheet will not run until the agent sets it.
"""
from __future__ import annotations

import re

_NAMES = {
    "dc": "DC", "d c": "DC", "district of columbia": "DC", "washington dc": "DC",
    "washington d c": "DC", "washington district of columbia": "DC",
    "md": "MD", "maryland": "MD",
    "va": "VA", "virginia": "VA", "northern virginia": "VA", "nova": "VA",
}


def normalise_market(value) -> str:
    """FUB's Market value to 'DC', 'MD', 'VA', or '' when it names none of them."""
    t = re.sub(r"[^a-z]+", " ", str(value or "").lower()).strip()
    return _NAMES.get(t, "")


_STATE_IN_ADDRESS = [
    ("DC", re.compile(r"(?<![A-Za-z])(?:D\.?C\.?|District of Columbia)(?![A-Za-z])", re.I)),
    ("MD", re.compile(r"(?<![A-Za-z])(?:MD|Maryland)(?![A-Za-z])", re.I)),
    ("VA", re.compile(r"(?<![A-Za-z])(?:VA|Virginia)(?![A-Za-z])", re.I)),
]


def market_from_address(address) -> str:
    """The state an address names, as a whole word; '' when it names none, or more than one."""
    a = str(address or "")
    found = {code for code, rx in _STATE_IN_ADDRESS if rx.search(a)}
    return found.pop() if len(found) == 1 else ""


def listing_market(fub_market, address) -> str:
    """What a new listing is created with: FUB's Market, else the address's own state, else ''."""
    return normalise_market(fub_market) or market_from_address(address)
