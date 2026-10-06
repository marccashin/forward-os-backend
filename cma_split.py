"""Split a multi-property MLS export into its properties, before it is read.

Why this exists (Oct 6, 2026): Marc uploaded one Bright "Agent Full" PDF holding
16 properties to the CMA Builder, twice. Both times Railway logged
httpx.ReadTimeout inside claude_post(where="cma-import"): the whole file went to
the AI service as ONE request, the reply for 16 properties did not finish inside
the 120 second limit, and the agent saw "FORWARD OS could not finish reading this
sheet". The system check passed the same morning, because its test sheet holds
one property. Nothing was wrong with the PDF or with the service: one request
was simply too big. It would also have hit the reply size limit before long.

The fix is to never send more than a few properties in one request. A Bright
export is the single-property sheets one after another, and every property's
first page carries exactly one "MLS #:" line near the top; its continuation
pages (remarks, history) carry none. Verified on a real sheet read with the same
library the backend uses (pypdf): page 1 has one "MLS #:" line, page 2 has none.

Pure functions, no network, so they can be tested without the AI service.
The prompt and the per-request code in main.py are unchanged: a file small
enough for one request is sent exactly as it was before.
"""
from __future__ import annotations

import re

# A property's first page: a line that starts with "MLS #:" (pypdf prints the
# field label at the start of its own line).
_MLS_LINE = re.compile(r"(?im)^\s*MLS\s*#\s*:")

# Properties per request. A single-property reply takes roughly 10 to 20
# seconds, so 4 leaves a wide margin inside the 120 second limit.
PER_REQUEST = 4
# Requests in flight at once for one file.
CONCURRENCY = 3


def split_listings(page_texts):
    """Group a PDF's pages into properties.

    Returns a list of {"label": str, "text": str}, one per property, in order;
    or None when the pages cannot be split with confidence, in which case the
    caller reads the whole file in one request, as before:
      - no page has an "MLS #:" line (not the layout this was written for);
      - a page has more than one (several properties share a page, so a page
        boundary is not a property boundary).
    Pages before the first "MLS #:" page stay with the first property, so no
    text is ever dropped.
    """
    pages = [p or "" for p in (page_texts or [])]
    counts = [len(_MLS_LINE.findall(p)) for p in pages]
    if not pages or max(counts) != 1:
        return None
    groups, current = [], None
    lead = []
    for text, n in zip(pages, counts):
        if n == 1:
            current = {"label": _label(text), "pages": lead + [text]}
            lead = []
            groups.append(current)
        elif current is None:
            lead.append(text)
        else:
            current["pages"].append(text)
    return [{"label": g["label"], "text": "\n".join(g["pages"])} for g in groups]


def _label(first_page_text):
    """The header line of a property, for naming it in a message.

    On an Agent Full sheet the first line under the "Agent Full" title reads
    "13717 Martin Rd, Brandywine, MD 20613 Active Residential $1,050,000".
    Other lines can sit between it and "MLS #:" (a closed sale prints
    "Recent Change: ..." there), so the FIRST line above "MLS #:" is used,
    not the nearest. Falls back to an empty string; the caller then says
    "a property".
    """
    lines = [l.strip() for l in (first_page_text or "").split("\n")]
    for i, line in enumerate(lines):
        if _MLS_LINE.match(line):
            for prev in lines[:i]:
                if prev and prev.lower() != "agent full":
                    return prev[:70]
            break
    return ""


def chunk(listings, per_request=PER_REQUEST):
    """Consecutive groups of at most per_request properties."""
    return [listings[i:i + per_request] for i in range(0, len(listings), per_request)]
