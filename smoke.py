"""Whole-system check for every AI-backed tool in this backend.

WHY THIS EXISTS
On Sept 28, 2026 one change (the move to claude-opus-5 / claude-sonnet-5)
broke six tools at once: the CMA import, Buyer Market Report analyze,
regenerate, the comparison report, Multiple Offers analysis and the offer
chat. Each PR had been verified for what it touched. Nothing exercised the
tools it did not touch, the failures were swallowed, and the tools were used
rarely, so the breakage surfaced one tool at a time over five days, when an
agent happened to need one.

This module calls every one of those tools the way the app does, with
made-up data, and checks that a usable answer comes back. It runs:
  - once at boot, and /health/deploy reports the result, so Railway does not
    switch traffic to a build whose tools are broken;
  - every morning before the workday;
  - on demand: POST /api/smoke-test/run (GET /api/smoke-test/status to read).

RULES
- Checks call the REAL endpoint functions in main.py. Never a copy.
- Nothing here writes to the database, Drive or Netlify. The comparison check
  calls _bmr_comparison_claude directly and never deploys a page.
- All data is fictional and says so.
- A new AI tool gets a check here in the same PR that adds it.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
import zlib
from datetime import datetime, timezone

_log = logging.getLogger("forward-cc.smoke")

CHECK_TIMEOUT_SECONDS = 150


# ── Test documents ─────────────────────────────────────────────────────────
def make_pdf(lines: list[str]) -> bytes:
    """A small one-page text PDF, built by hand so no PDF library is needed."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    ops = ["BT", "/F1 10 Tf", "40 760 Td", "13 TL"]
    for ln in lines:
        ops.append(f"({esc(ln)}) Tj T*")
    ops.append("ET")
    stream = zlib.compress("\n".join(ops).encode("latin-1"))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(b"trailer\n<< /Size " + str(len(objs) + 1).encode() + b" /Root 1 0 R >>\n"
              b"startxref\n" + str(xref).encode() + b"\n%%EOF\n")
    return out.getvalue()


MLS_LINES = [
    "Agent Full",
    "100 Smoke Test Ln, Testville, VA 22000  Closed  Residential  $805,000",
    "FICTIONAL TEST LISTING FOR AN AUTOMATED SYSTEM CHECK. NOT A REAL PROPERTY.",
    "MLS #: VATT0000001",
    "Beds: 3   Baths: 2 / 0",
    "Above Grade Fin SQFT: 1,750 / Assessor",
    "Assessor AbvGrd Fin SQFT: 1,750",
    "Year Built: 1998",
    "County: Testshire, VA",
    "Lot Size SqFt: 6,500",
    "Tax Annual Amt / Year: $7,200 / 2026",
    "Parking",
    "Attached Garage - # of Spaces 2",
    "Total Parking Spaces 2",
    "Listing Details",
    "Original Price: $790,000   DOM: 6",
    "Listing Agrmnt Type: Exclusive Right",
    "Sale / Lease Contract",
    "Close Date: 08/15/26   Close Price: $805,000.00",
    "Total Amount Paid by Seller Towards Closing Costs: $0.00",
    "Copyright 2026 Bright MLS (test data, not a real Bright MLS record).",
]

BUYER_REPORT_LINES = [
    "TEST DOCUMENT FOR AN AUTOMATED SYSTEM CHECK. NOT A REAL LISTING REPORT.",
    "Bright MLS Agent Full (fictional data)",
    "SUBJECT: 1 Smoke Test Ln, Testville, VA 22000  Active  List $800,000  Beds 3  Baths 2  SqFt 1,800  DOM 5",
    "COMP: 2 Smoke Test Ln  Closed  List $790,000  Sold $805,000  Beds 3  Baths 2  SqFt 1,750  DOM 6",
    "COMP: 3 Smoke Test Ln  Closed  List $830,000  Sold $820,000  Beds 3  Baths 2  SqFt 1,900  DOM 14",
    "COMP: 4 Smoke Test Ln  Closed  List $815,000  Sold $815,000  Beds 4  Baths 2  SqFt 1,850  DOM 9",
]

OFFER_LINES = [
    "FICTIONAL TEST CONTRACT FOR AN AUTOMATED SYSTEM CHECK. NOT A REAL OFFER.",
    "RESIDENTIAL SALES CONTRACT",
    "Property: 1 Smoke Test Ln, Testville, VA 22000",
    "Buyer: Test Buyer One.  Seller: Test Seller One.",
    "Sales Price: $800,000.  Earnest Money Deposit: $25,000.",
    "Financing: Conventional loan, 20 percent down payment.",
    "Settlement Date: 30 days from ratification.",
    "Contingencies: financing, appraisal, home inspection (7 days).",
    "Seller credit toward closing costs: $0.",
    "This paragraph exists so the extracted text is comfortably longer than the",
    "minimum the parser requires before it will send a document for reading.",
]

COMPS = [
    {"address": "2 Smoke Test Ln", "status": "Closed", "beds": "3", "baths": "2", "sqft": "1750",
     "list_price": "$790,000", "sale_price": "$805,000", "dom": "6", "ls_ratio": "101.9%", "notes": ""},
    {"address": "3 Smoke Test Ln", "status": "Closed", "beds": "3", "baths": "2", "sqft": "1900",
     "list_price": "$830,000", "sale_price": "$820,000", "dom": "14", "ls_ratio": "98.8%", "notes": ""},
    {"address": "4 Smoke Test Ln", "status": "Closed", "beds": "4", "baths": "2", "sqft": "1850",
     "list_price": "$815,000", "sale_price": "$815,000", "dom": "9", "ls_ratio": "100%", "notes": ""},
]

OFFERS = [
    {"offer_number": 1, "offer_data": {
        "sales_price": "$800,000", "loan_type": "Conventional", "closing_date": "30 days",
        "earnest_money": "$25,000", "appraisal_waiver": "No",
        "buyer_contingencies": "Financing, appraisal, inspection"}},
    {"offer_number": 2, "offer_data": {
        "sales_price": "$785,000", "loan_type": "Cash", "closing_date": "14 days",
        "earnest_money": "$50,000", "appraisal_waiver": "Yes", "buyer_contingencies": "None"}},
]


def _upload(name: str, data: bytes):
    from starlette.datastructures import Headers, UploadFile
    return UploadFile(io.BytesIO(data), filename=name,
                      headers=Headers({"content-type": "application/pdf"}))


class _JsonRequest:
    """Stands in for a FastAPI Request on endpoints that read request.json()."""
    def __init__(self, data): self._data = data
    async def json(self): return self._data


def _need(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)


# ── The checks. Each returns a short string describing what it saw. ─────────
async def check_cma_import(m):
    r = await m.cma_parse_listings([_upload("smoke_mls.pdf", make_pdf(MLS_LINES))])
    rows = r.get("results") or []
    good = [x for x in rows if x.get("ok")]
    _need(good, "no listing read: " + str([x.get("error") for x in rows])[:200])
    _need(any("smoke test" in str(x.get("address", "")).lower() for x in good),
          "address not read back: " + str([x.get("address") for x in good])[:120])
    return f"{len(good)} listing read"


async def check_buyer_report_analyze(m):
    r = await m.buyer_report_analyze(_upload("smoke_report.pdf", make_pdf(BUYER_REPORT_LINES)))
    _need(str((r.get("subject") or {}).get("address", "")).strip(), "no subject address")
    _need(len(r.get("comps") or []) >= 1, "no comps returned")
    return f"subject + {len(r['comps'])} comps"


async def check_buyer_report_regenerate(m):
    r = await m.buyer_report_regenerate(_JsonRequest({
        "subject": {"address": "1 Smoke Test Ln, Testville, VA", "beds": "3", "baths": "2",
                    "sqft": "1800", "list_price": "$800,000"},
        "comps": COMPS, "market_conditions": "balanced", "subject_dom": 5}))
    _need(len(str(r.get("narrative", "")).strip()) > 80, "narrative missing or too short")
    return f"narrative {len(r['narrative'])} chars"


async def check_buyer_report_comparison(m):
    # Calls the analysis only. Never deploys a page.
    req = m.BuyerReportComparisonRequest(
        candidates=[m.BuyerReportCandidate(
            address=c["address"], beds=c["beds"], baths=c["baths"], sqft=c["sqft"],
            list_price=c["list_price"], dom=c["dom"], status="Active", notes="TEST DATA")
            for c in COMPS[:2]],
        client_name="SMOKE TEST", agent_name="Smoke Test")
    r = await m._bmr_comparison_claude(req)
    _need(isinstance(r, list) and len(r) == 2 and all(isinstance(a, dict) and a for a in r),
          "expected 2 non-empty analyses, got " + str(r)[:160])
    return "2 analyses"


async def check_offers_analyze(m):
    r = await m.analyze_offers(m.AnalyzeOffersRequest(
        offers=OFFERS, property_address="1 Smoke Test Ln, Testville, VA", list_price="$800,000"))
    _need(len(str(r.get("analysis", "")).strip()) > 200, "analysis missing or too short")
    return f"analysis {len(r['analysis'])} chars"


async def check_offers_chat(m):
    r = await m.chat_offers(m.ChatOffersRequest(
        offers=OFFERS, question="TEST: which of these two offers is stronger for the seller?",
        property_address="1 Smoke Test Ln", list_price="$800,000"))
    _need(len(str(r.get("answer", "")).strip()) > 20, "answer missing")
    return f"answer {len(r['answer'])} chars"


async def check_offer_parse(m):
    r = await m.parse_offer(_upload("smoke_offer.pdf", make_pdf(OFFER_LINES)))
    _need(isinstance(r, dict) and r, "empty result")
    _need("800" in str(r), "sales price not read back: " + str(r)[:160])
    return "offer read"


async def check_meeting_prep_research(m):
    r = await m.meeting_prep_research(m.MeetingPrepResearchRequest(
        name="Zzyx Smoketest", company="Nonexistent Test Company",
        agent_context="Automated system check. This person does not exist."))
    _need(isinstance(r, dict), "no result")
    # This endpoint swallows failures into {"found": False, "error": ...}.
    _need(not r.get("error"), "research call failed: " + str(r.get("error"))[:200])
    return "research call answered"


async def check_offer_strategy_mls(m):
    r = await m.osg_parse_comps([_upload("smoke_mls.pdf", make_pdf(MLS_LINES))])
    rows = (r.get("results") if isinstance(r, dict) else None) or []
    good = [x for x in rows if x.get("ok")]
    _need(good, "no comp read: " + str([x.get("error") for x in rows])[:200])
    return f"{len(good)} comp read"


CHECKS = [
    ("CMA Builder: MLS import", check_cma_import),
    ("Buyer Market Report: analyze", check_buyer_report_analyze),
    ("Buyer Market Report: regenerate", check_buyer_report_regenerate),
    ("Buyer Market Report: comparison analysis", check_buyer_report_comparison),
    ("Multiple Offers: analysis", check_offers_analyze),
    ("Multiple Offers: chat", check_offers_chat),
    ("Multiple Offers: read an offer PDF", check_offer_parse),
    ("Meeting Prep: research", check_meeting_prep_research),
    ("Offer Strategy: MLS comp import", check_offer_strategy_mls),
]


def _describe(e: BaseException) -> str:
    detail = getattr(e, "detail", None)
    return (f"{type(e).__name__}: {detail if detail else e}")[:400]


async def _run_one(name, fn, m) -> dict:
    t0 = time.monotonic()
    last = ""
    # One retry: a single slow or overloaded API call must not fail a deploy.
    for attempt in (1, 2):
        try:
            detail = await asyncio.wait_for(fn(m), timeout=CHECK_TIMEOUT_SECONDS)
            return {"name": name, "ok": True, "attempts": attempt, "detail": detail,
                    "seconds": round(time.monotonic() - t0, 1)}
        except asyncio.TimeoutError:
            last = f"timed out after {CHECK_TIMEOUT_SECONDS}s"
        except BaseException as e:  # HTTPException, AssertionError, anything
            if isinstance(e, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            last = _describe(e)
        _log.warning("smoke check failed (attempt %s) %s: %s", attempt, name, last)
    return {"name": name, "ok": False, "attempts": 2, "detail": last,
            "seconds": round(time.monotonic() - t0, 1)}


async def run_smoke(m, trigger: str = "manual") -> dict:
    """Run every check at once. `m` is the main module."""
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    results = await asyncio.gather(*[_run_one(n, f, m) for n, f in CHECKS])
    failed = [r for r in results if not r["ok"]]
    out = {
        "ok": not failed,
        "trigger": trigger,
        "started_at": started.isoformat(),
        "seconds": round(time.monotonic() - t0, 1),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "checks": results,
    }
    if failed:
        _log.error("SMOKE TEST FAILED trigger=%s failed=%s", trigger,
                   [(r["name"], r["detail"]) for r in failed])
    else:
        _log.info("smoke test passed trigger=%s checks=%s seconds=%s",
                  trigger, len(results), out["seconds"])
    return out
