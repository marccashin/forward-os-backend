"""Offer Strategy Generator: every MLS sheet reader it uses.

SEPARATION RULE (Marc, Sept 21 2026): the Offer Strategy Generator and the CMA
builder must stay completely separate, so a change to one can never change
the other. This module is the Offer Strategy side and owns everything it
needs: its own model setting, its own prompts, its own PDF reading and its
own Claude call. It does not import main.py and main.py's CMA code does not
import it. Do not "de-duplicate" this against the CMA reader.

Two readers live here:

1. parse_comps: Step 2 comp sheets. OSG_COMP_PARSE_PROMPT started life as a
   verbatim copy of main.CMA_PARSE_PROMPT on Sept 21 2026 (the two were
   proven equal at the fork), so comps read exactly as they did the day
   before. From here on they are free to diverge.
2. parse_subject: the subject property's own sheet on Step 1. Claude reads,
   code verifies (verify_subject). Lockbox, access codes, phones, emails,
   owner names and compensation never leave the server.
"""

import io
import gc
import json
import logging
import re

import httpx

# Owned by the Offer Strategy Generator. Changing BMR_MODEL in main.py (used
# by the CMA reader and the Buyer Market Report) does not move this.
OSG_MODEL = "claude-sonnet-4-5"
MAX_BYTES = 20 * 1024 * 1024
MLS_MARKERS = ("bright mls", "mls #", "mls#", "listing agrmnt", "agent full")

OSG_COMP_PARSE_PROMPT = 'You are reading a real estate MLS report (usually a Bright MLS "Agent Full" export).\n\nIMPORTANT: One PDF often contains SEVERAL properties, one after another. Each new property begins with a header line holding an address, a status word, and a price. Find EVERY property in the document and return one entry for each. Do not stop after the first.\n\nExtract ONLY facts printed on the sheet. Never estimate, infer, or calculate a value that is not stated. If a field is absent, use an empty string "".\n\nReturn ONLY a valid JSON object of the form {"listings": [ ... ]}, where every element has exactly these keys:\n\n{\n  "status": "one of: active, pending, closed, off_market",\n  "status_raw": "the status word exactly as printed (Active, Under Contract, Closed, Canceled, Expired, Withdrawn, etc.)",\n  "mlsNumber": "MLS #",\n  "address": "street address only, no city/state/zip",\n  "city": "city name only",\n  "state": "2-letter state",\n  "zip": "5-digit zip",\n  "county": "county name if shown",\n  "propType": "one of: Single Family, Condo, Townhouse, Multi-Family, Land, Other",\n  "beds": "number of bedrooms, digits only",\n  "fullBaths": "number of full baths, digits only",\n  "halfBaths": "number of half baths, digits only",\n  "gla": "Above Grade Finished SQFT, digits only, no commas. This is the appraiser\'s gross living area. NEVER use Total SQFT or Tax Total Fin SQFT, which include below-grade space.",\n  "glaSource": "the label printed after the Above Grade Fin SQFT figure, exactly as shown: Assessor, Estimated, or blank",\n  "assessorGla": "the separate \'Assessor AbvGrd Fin SQFT\' figure if the sheet prints one, digits only",\n  "below": "Below Grade FINISHED SQFT, digits only. If the sheet gives only unfinished sqft, or gives a percentage instead of a number, leave this EMPTY and set flag below_grade_unclear.",\n  "lotSize": "lot size in SQUARE FEET, digits only. If given in acres, convert (1 acre = 43560 sqft).",\n  "yearBuilt": "4-digit year",\n  "garageSpaces": "number of GARAGE spaces, digits only. If the sheet says Garage: No, use 0. If total parking is listed as Unknown, leave EMPTY and set flag parking_unknown.",\n  "hoaMonthly": "HOA fee converted to a MONTHLY dollar amount, digits only",\n  "condoFee": "condo fee converted to a MONTHLY dollar amount, digits only",\n  "listPrice": "current or original list price, digits only",\n  "salePrice": "CLOSE/SOLD price, digits only. Only for closed sales. Empty otherwise.",\n  "soldDate": "close date as YYYY-MM-DD. Only for closed sales.",\n  "listDate": "listing entry date as YYYY-MM-DD",\n  "dom": "days on market, digits only",\n  "concessions": "the dollar figure from \'Total Amount Paid by Seller Towards Closing Costs\', digits only, no commas or dollar sign. Use 0 if that line prints $0.00. Leave EMPTY only if the line is absent from the sheet. IGNORE the yes/no \'Seller Concessions\' field entirely - it is often wrong. A sheet can say Seller Concessions: No and still show a dollar amount here; the dollar amount wins.",\n  "concessionsRaw": "the \'Seller Concessions\' yes/no field exactly as printed, for reference only",\n  "annualTax": "annual property tax amount, digits only",\n  "flags": {\n    "gla_needs_check": true/false,\n    "lot_estimated": true/false,\n    "parking_unknown": true/false,\n    "below_grade_unclear": true/false,\n    "price_is_list_not_sold": true/false\n  }\n}\n\nRules:\n- status: Active -> "active". Pending / Under Contract / Active Under Contract -> "pending". Closed / Sold -> "closed". Canceled / Expired / Withdrawn / Temporarily Off Market -> "off_market".\n- Set gla_needs_check true ONLY when the square footage is genuinely uncertain: the label reads "Estimated", OR the Above Grade Fin SQFT differs from the Assessor AbvGrd Fin SQFT printed on the same sheet. A plain "Assessor" label that matches is normal and must NOT be flagged.\n- Set lot_estimated true when the lot size is labelled "Estimated".\n- Set parking_unknown true when total parking spaces reads "Unknown".\n- Set price_is_list_not_sold true whenever salePrice is empty but listPrice is present.\n- Never output a condition, quality, or proximity rating. Those are the agent\'s call.\n- A closed sale\'s price is its Close Price, not its list price, and its soldDate is the Close Date.\n- Return one entry per property. A report holding 7 properties returns 7 entries.\n\nReturn ONLY the JSON object {"listings": [...]}. No explanation, no markdown, no code fences.'

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


# ---------------------------------------------------------------------------
# PDF reading and the Claude call (Offer Strategy only)
# ---------------------------------------------------------------------------

async def read_pdf(upload):
    """Return (text, error). error is a plain-English reason or None."""
    from pypdf import PdfReader
    if upload.content_type not in ("application/pdf", "application/octet-stream"):
        return "", "Not a PDF file."
    pdf_bytes = await upload.read()
    if len(pdf_bytes) > MAX_BYTES:
        return "", "PDF is larger than 20 MB."
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception:
        text = ""
    finally:
        del pdf_bytes
        gc.collect()
    if len(text.strip()) < 200:
        return "", ("This PDF looks like a scan and has no readable text. "
                    "Print the MLS sheet to PDF again, or enter it by hand.")
    if not any(m in text.lower() for m in MLS_MARKERS):
        return "", ("This does not look like an MLS listing report, so nothing "
                    "was imported. Export the sheet from Bright MLS and try again.")
    return text, None


async def ask_claude(api_key: str, content: str, max_tokens: int, temperature=None):
    payload = {"model": OSG_MODEL, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": content}]}
    if temperature is not None:
        payload["temperature"] = temperature
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post("https://api.anthropic.com/v1/messages",
                                 headers=headers, json=payload)
        resp.raise_for_status()
    raw = resp.json()["content"][0]["text"].strip()
    raw = re.sub(r"^```(?:json)?\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Step 2: comp sheets. Same response shape the page already expects.
# ---------------------------------------------------------------------------

MAX_FILES = 12
MAX_LISTINGS_PER_FILE = 30


async def parse_comps(files, api_key: str) -> dict:
    results = []
    for f in files:
        name = f.filename or "listing.pdf"
        try:
            text, err = await read_pdf(f)
            if err:
                results.append({"file": name, "ok": False, "error": err})
                continue
            data = await ask_claude(
                api_key, f"MLS LISTING SHEET:\n\n{text}\n\n---\n\n{OSG_COMP_PARSE_PROMPT}", 16000)
            del text
            if isinstance(data, dict) and isinstance(data.get("listings"), list):
                listings = data["listings"]
            elif isinstance(data, dict):
                listings = [data]
            elif isinstance(data, list):
                listings = data
            else:
                raise ValueError("unexpected shape")
            if not listings:
                results.append({"file": name, "ok": False,
                                "error": "No properties were found in this sheet."})
                continue
            for item in listings[:MAX_LISTINGS_PER_FILE]:
                if not isinstance(item, dict):
                    continue
                item.setdefault("flags", {})
                item["file"] = name
                item["ok"] = True
                results.append(item)
        except Exception:
            logging.exception("osg parse_comps failed for %s", name)
            results.append({"file": name, "ok": False,
                            "error": "Could not read this sheet. Try re-downloading it from the MLS."})
    return {"success": True, "results": results}


# ---------------------------------------------------------------------------
# Step 1: the subject property's own sheet
# ---------------------------------------------------------------------------

async def parse_subject(upload, api_key: str) -> dict:
    name = upload.filename or "listing.pdf"

    def fail(msg):
        return {"success": True, "ok": False, "file": name, "error": msg}

    try:
        text, err = await read_pdf(upload)
        if err:
            return fail(err)
        data = await ask_claude(
            api_key, f"MLS LISTING SHEET:\n\n{text}\n\n---\n\n{SUBJECT_PARSE_PROMPT}", 4000, 0)
        if not isinstance(data, dict):
            raise ValueError("unexpected shape")
        found = count_properties(text, data.get("propertiesFound"))
        if found > 1:
            return fail(f"This PDF holds {found} properties. Drop the sheet for the home "
                        "you are writing the offer on here. Comps go in Step 2.")
        checked = verify_subject(data.get("subject") or {}, text)
        if not checked["subject"].get("address"):
            return fail("Could not find the property address on this sheet, so nothing "
                        "was filled in. Type the details in, or re-export the sheet.")
        if checked["unverified"] or checked["dropped_notes"]:
            logging.info("osg parse_subject %s: blanked %s, dropped %d notes",
                         name, checked["unverified"], checked["dropped_notes"])
        return {"success": True, "ok": True, "file": name,
                "properties_found": found, **checked}
    except Exception:
        logging.exception("osg parse_subject failed for %s", name)
        return fail("Could not read this sheet. Try re-downloading it from the MLS.")
