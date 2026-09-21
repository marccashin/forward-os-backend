"""Offer Strategy Generator: read the SUBJECT property's own MLS sheet.

The comp importer (/api/cma/parse-listings) answers "what did similar homes
sell for". This module answers a different question: "what does the listing
sheet for the house we are writing an offer on tell us". Original price and
reductions, DOM against CDOM, sale type, possession, financing limits in the
remarks, a seller credit already on offer, private sewer or well, the tax
assessment. Those move an offer strategy, and the agent should not have to
retype them.

Two rules carried over from the CMA reader and the market reports module:

1. Claude reads, code verifies. Every figure and every quoted remark Claude
   returns is checked against the PDF text. Anything that is not printed on
   the sheet is blanked and reported, never passed on.
2. Some of what a Bright Agent Full sheet prints must never reach a strategy:
   lockbox type and location, showing service phone numbers, agent emails,
   owner names, compensation. The prompt says so, and a deterministic scrub
   enforces it, because a prompt instruction alone is not a guarantee.
"""

import re

SUBJECT_PARSE_PROMPT = """You are reading the MLS listing sheet (usually a Bright MLS "Agent Full" export) for ONE property: the home a buyer is about to make an offer on.

Extract ONLY what is printed on the sheet. Never estimate, infer, calculate or convert. Copy numbers as digits only (no $ or commas). Copy text values exactly as printed. If something is not printed, use an empty string "".

First count how many separate properties the document contains. Each property begins with a header line holding an address, a status word and a price.

Return ONLY a JSON object of this exact shape:

{
  "propertiesFound": <integer>,
  "subject": {
    "status": "one of: active, pending, closed, off_market",
    "status_raw": "the status word exactly as printed in the header (Active, Pending, Coming Soon, Closed, Canceled...)",
    "mlsNumber": "MLS #",
    "address": "street address only, including any unit number, exactly as printed",
    "city": "",
    "state": "2-letter state",
    "zip": "5-digit zip",
    "county": "county as printed, without the state",
    "propType": "one of: Single Family, Condo, Townhouse, Multi-Family, Land, Other. Condo when Ownership Interest is Condominium or Cooperative. Townhouse when the structure is a row or townhouse and it is not a condo. Single Family when detached.",
    "structureType": "Structure Type as printed",
    "ownership": "Ownership Interest as printed (Fee Simple, Condominium, Cooperative, Ground Rent...)",
    "beds": "",
    "fullBaths": "the number before the slash in Baths",
    "halfBaths": "the number after the slash in Baths",
    "gla": "Above Grade Fin SQFT, digits only. NEVER Total SQFT or Total Fin SQFT.",
    "glaSource": "the label printed after the Above Grade Fin SQFT figure (Assessor, Estimated...)",
    "yearBuilt": "",
    "lotSizeText": "Lot Acres / SQFT exactly as printed",
    "listPrice": "the CURRENT list price in the header",
    "originalListPrice": "Original Price",
    "previousListPrice": "Previous List Price",
    "listDate": "Listing Entry Date exactly as printed",
    "dom": "the DOM figure (the number before the slash in DOM / CDOM)",
    "cdom": "the CDOM figure (the number after the slash in DOM / CDOM)",
    "saleType": "Sale Type as printed (Standard, Short Sale, REO, Estate...)",
    "possession": "Possession as printed",
    "hoaFee": "HOA fee amount as printed, digits only",
    "hoaFrequency": "the frequency printed with the HOA fee (Monthly, Quarterly, Annually)",
    "condoFee": "Condo/Coop Fee amount as printed, digits only",
    "condoFrequency": "the frequency printed with the condo fee",
    "annualTax": "the amount in Tax Annual Amt / Year",
    "taxYear": "the year in Tax Annual Amt / Year",
    "taxAssessedValue": "Tax Assessed Value amount",
    "garage": "the Parking line as printed",
    "basement": "Basement Type as printed, or the Basement Yes/No if no type is printed",
    "waterSource": "Water Source as printed",
    "sewer": "Sewer as printed",
    "heating": "Heating as printed",
    "cooling": "Utilities cooling as printed (Central A/C...)",
    "floodZone": "Flood zone if printed",
    "inclusions": "Inclusions if printed",
    "exclusions": "Exclusions if printed",
    "offerNotes": [
      {"quote": "one sentence copied WORD FOR WORD from the Agent or Public remarks", "topic": "one of: financing, seller_credit, offer_deadline, offer_instructions, possession, as_is, condition, hoa_condo, tenant, incentive, other"}
    ]
  }
}

What belongs in offerNotes: sentences from the remarks that would change how a buyer's agent writes an offer. Examples: a loan type the seller or association cannot accept, a credit the seller is offering, an offer deadline or instructions for submitting offers, a rent-back or possession request, an as-is sale, a pre-inspection or disclosures being available, a tenant in place, a condition claim such as a new roof or a system age, a special assessment, a buyer incentive program. Copy each sentence exactly, including the seller's wording. Do not summarise. At most 12. Leave out anything that is only marketing description.

NEVER include, anywhere in your answer: lockbox type or location, door, alarm or gate codes, showing instructions, showing service details, any phone number, any email address, owner names, or anything about agent compensation.

Return ONLY the JSON object. No explanation, no markdown, no code fences."""


# Fields Claude returns, grouped by how they are checked against the sheet.
NUMERIC_FIELDS = (
    "beds", "fullBaths", "halfBaths", "gla", "yearBuilt", "listPrice",
    "originalListPrice", "previousListPrice", "dom", "cdom", "hoaFee",
    "condoFee", "annualTax", "taxYear", "taxAssessedValue",
)
TEXT_FIELDS = (
    "status_raw", "mlsNumber", "address", "city", "state", "zip", "county",
    "structureType", "ownership", "glaSource", "lotSizeText", "listDate",
    "saleType", "possession", "hoaFrequency", "condoFrequency", "garage",
    "basement", "waterSource", "sewer", "heating", "cooling", "floodZone",
    "inclusions", "exclusions",
)
# Categorical answers. Not printed verbatim, so they are checked against a
# fixed list instead.
ENUMS = {
    "status": ("active", "pending", "closed", "off_market"),
    "propType": ("Single Family", "Condo", "Townhouse", "Multi-Family", "Land", "Other"),
}
NOTE_TOPICS = ("financing", "seller_credit", "offer_deadline", "offer_instructions",
               "possession", "as_is", "condition", "hoa_condo", "tenant",
               "incentive", "other")
MAX_NOTES = 12
MAX_QUOTE = 400

# Anything matching these never leaves the server, whatever Claude returned.
_SENSITIVE = [
    re.compile(r"\(?\b\d{3}\)?[\s.\-]?\d{3}[\s.\-]\d{4}\b"),        # phone numbers
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),  # emails
    # Access details. Narrow on purpose: "code violations", "gated community"
    # and "co-op" are real offer facts and must survive.
    re.compile(r"lock\s*-?\s*box|\bcombo\b|supra\b|"
               r"(door|lock|alarm|gate|entry|access|garage|building|fob|key)\s*-?\s*codes?\b|"
               r"\bcodes?\s*(is|are|:|=)|\balarm\b|\bkeys?\s+(is|are|in|under|at|with)\b|"
               r"showing\s*time|showing\s+(instruction|requirement|contact|method|service)|"
               r"owner\s+name|compensation|buyer\s+agency|cooperating\s+broker|\bbac\b",
               re.IGNORECASE),
]


def _squash(s: str) -> str:
    """Lowercase and drop ALL whitespace.

    pypdf joins words across line breaks ("eachyear", "makesure" on real
    Bright sheets), so a word-for-word check has to ignore spacing entirely
    or it would reject genuine quotes.
    """
    return re.sub(r"\s+", "", str(s or "")).lower()


def _digits(v) -> str:
    """Normalise a numeric answer to digits with an optional decimal part."""
    s = str(v if v is not None else "").strip().replace(",", "").replace("$", "")
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?", s)
    if not m:
        return ""
    whole, frac = m.group(1), m.group(2)
    if frac and int(frac) != 0:
        return whole + "." + frac
    return whole


def _flat(s: str) -> str:
    """Squashed text with commas removed, for matching figures like 1,000."""
    return _squash(s).replace(",", "")


# Where each value must be printed. Every pattern runs over the flattened
# sheet (lowercase, no whitespace, no commas) and must be followed directly
# by the value. Anchoring matters: on a real sheet "Estate" appears inside
# "Long & Foster Real Estate", and a previous list price would verify a
# wrong current one if figures were matched anywhere on the page.
# "HEADER" means the property header, the text before the first "MLS #".
_NUM = r"\$?"
ANCHORS = {
    "listPrice":         ["HEADER"],
    "originalListPrice": [r"original(?:list)?price:" + _NUM],
    "previousListPrice": [r"previouslistprice:" + _NUM],
    "dom":               [r"dom/cdom:"],
    "cdom":              [r"dom/cdom:\d+/"],
    "beds":              [r"beds:"],
    "fullBaths":         [r"baths:"],
    "halfBaths":         [r"baths:\d+/"],
    "gla":               [r"abovegradefinsqft:"],
    "yearBuilt":         [r"yearbuilt:"],
    "annualTax":         [r"taxannualamt/year:" + _NUM],
    "taxYear":           [r"taxannualamt/year:\$?[\d.]+/"],
    "taxAssessedValue":  [r"taxassessedvalue:" + _NUM],
    "hoaFee":            [r"(?:hoa|association)[a-z/]*fee:" + _NUM],
    "condoFee":          [r"condo/coopfee:" + _NUM, r"condofee:" + _NUM],
    "status_raw":        ["HEADER"],
    "mlsNumber":         [r"mls#:"],
    "address":           ["HEADER"],
    "city":              ["HEADER"],
    "state":             ["HEADER"],
    "zip":               ["HEADER"],
    "county":            [r"county:"],
    "structureType":     [r"structuretype:"],
    "ownership":         [r"ownershipinterest:"],
    "glaSource":         [r"abovegradefinsqft:\d+/"],
    "lotSizeText":       [r"lotacres/sqft:"],
    "listDate":          [r"listingentrydate:"],
    "saleType":          [r"saletype:"],
    "possession":        [r"possession:"],
    "hoaFrequency":      [r"(?:hoa|association)[a-z/]*fee:\$?[\d.]+/"],
    "condoFrequency":    [r"condo/coopfee:\$?[\d.]+/", r"condofee:\$?[\d.]+/"],
    "garage":            [r"parking"],
    "basement":          [r"basementtype:", r"basement:"],
    "waterSource":       [r"watersource:"],
    "sewer":             [r"sewer:"],
    "heating":           [r"heating:"],
    "cooling":           [r"utilities:", r"cooling:"],
    "floodZone":         [r"floodzone:"],
    "inclusions":        [r"inclusions:"],
    "exclusions":        [r"exclusions:"],
}


def _header(flat: str) -> str:
    at = flat.find("mls#")
    return flat[:at] if at > -1 else flat[:300]


def number_on_sheet(value, text: str) -> bool:
    """True when the figure is printed on the sheet as a whole number.

    A printed ".00" is allowed after the figure, and the figure must not be
    part of a longer number, so 69000 does not verify against $169,000.
    """
    d = _digits(value)
    if not d:
        return False
    flat = str(text or "").replace(",", "")
    pat = r"(?<![\d.])" + re.escape(d) + r"(?:\.0+)?(?![\d])"
    return re.search(pat, flat) is not None


def text_on_sheet(value, text: str) -> bool:
    v = _squash(value)
    return bool(v) and v in _squash(text)


def field_on_sheet(field: str, value, text: str, numeric: bool) -> bool:
    """True when VALUE is printed directly after FIELD's own label."""
    flat = _flat(text)
    v = _digits(value) if numeric else _flat(value)
    if not v:
        return False
    tail = r"(?:\.0+)?(?![\d])" if numeric else ""
    for label in ANCHORS.get(field, []):
        if label == "HEADER":
            hdr = _header(flat)
            pat = (r"(?<![\d.])" if numeric else "") + re.escape(v) + tail
            if re.search(pat, hdr):
                return True
        elif re.search(label + re.escape(v) + tail, flat):
            return True
    return False


def is_sensitive(s: str) -> bool:
    return any(p.search(str(s or "")) for p in _SENSITIVE)


def _remark_bounds(text: str):
    """Where the Agent and Public remarks sit in the squashed sheet text.

    Returns (agent_start, public_start, remarks_end), any of which may be -1.
    On a Bright Agent Full sheet the block reads "RemarksAgent: ... Public:
    ... Listing Office".
    """
    sq = _squash(text)
    a = sq.find("remarksagent:")
    p = sq.find("public:", a if a > -1 else 0)
    end = sq.find("listingoffice", max(a, p, 0))
    return a, p, end


def note_source(quote: str, text: str) -> str:
    """'agent', 'public', or '' when the quote is not inside the remarks.

    Decided from where the quote sits on the sheet, never from Claude's
    label. When the remark markers cannot be found the answer is 'agent',
    the conservative choice, because agent remarks are confidential between
    agents and must not end up in anything a buyer reads.
    """
    sq, q = _squash(text), _squash(quote)
    at = sq.find(q)
    if at < 0:
        return ""
    a, p, end = _remark_bounds(text)
    if a < 0 and p < 0:
        return "agent"
    if end > -1 and at >= end:
        return ""
    if p > -1 and at >= p:
        return "public"
    if a > -1 and at >= a:
        return "agent"
    return ""


def count_properties(text: str, claude_count) -> int:
    """How many properties the PDF holds. Takes the larger of Claude's count
    and the number of 'MLS #:' labels, so a comp packet dropped on the subject
    zone is caught even if Claude reports 1."""
    labels = len(re.findall(r"MLS\s*#\s*:", str(text or "")))
    try:
        c = int(claude_count)
    except (TypeError, ValueError):
        c = 0
    return max(c, labels, 1)


def verify_subject(sub: dict, text: str) -> dict:
    """Keep only what the sheet actually prints.

    Returns {"subject": clean, "unverified": [field names blanked],
             "dropped_notes": int}.
    """
    sub = sub if isinstance(sub, dict) else {}
    clean, unverified = {}, []

    for k in NUMERIC_FIELDS:
        raw = sub.get(k, "")
        d = _digits(raw)
        if str(raw or "").strip() == "":
            clean[k] = ""
        elif d and field_on_sheet(k, d, text, numeric=True):
            clean[k] = d
        else:
            clean[k] = ""
            unverified.append(k)

    for k in TEXT_FIELDS:
        raw = str(sub.get(k, "") or "").strip()
        if not raw:
            clean[k] = ""
        elif field_on_sheet(k, raw, text, numeric=False) and not is_sensitive(raw):
            clean[k] = raw
        else:
            clean[k] = ""
            unverified.append(k)

    for k, allowed in ENUMS.items():
        v = str(sub.get(k, "") or "").strip()
        clean[k] = v if v in allowed else ""

    notes, dropped, seen = [], 0, set()
    for n in (sub.get("offerNotes") or []):
        q = str((n or {}).get("quote", "") if isinstance(n, dict) else n or "").strip()
        if not q:
            continue
        key = _squash(q)
        src = note_source(q, text) if q else ""
        if (len(q) > MAX_QUOTE or not src or is_sensitive(q) or key in seen):
            dropped += 1
            continue
        seen.add(key)
        topic = str((n or {}).get("topic", "") if isinstance(n, dict) else "").strip()
        notes.append({"quote": q, "source": src,
                      "topic": topic if topic in NOTE_TOPICS else "other"})
        if len(notes) >= MAX_NOTES:
            break
    clean["offerNotes"] = notes

    return {"subject": clean, "unverified": unverified, "dropped_notes": dropped}
