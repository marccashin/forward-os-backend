"""
FORWARD monthly market reports.

Source of truth: Corcoran McEnearney's "Market in a Minute & StatPak" hub page,
which always shows the current month for every region we cover. Each month this
module reads that page, pulls the numbers for each region, re-issues them as
FORWARD-branded PDFs, Instagram squares and captions, saves them to the
"FORWARD Market Stats" shared drive folder, and points the OS Market Reports
panel at the new files.

Accuracy rules (seller-facing numbers, so these are deliberately strict):
  * Numbers are extracted DETERMINISTICALLY with regexes from McEnearney's own
    sentence for each region. No model ever produces a number.
  * Every extracted number is re-checked against the source text before use.
  * A region whose sentence does not parse cleanly is NOT published. Its
    previous link stays in place and the run is marked failed, loudly.
  * The narrative (insight line, trend line, Instagram caption) may be written
    by Claude, but every number in it must appear in the source text, it must
    contain no em dashes and none of FORWARD's banned words. If any check
    fails, a plain template sentence built only from the verified numbers is
    used instead.

Nothing here runs at import time. main.py calls register(...) at startup.
"""
from __future__ import annotations

import asyncio
import html as _html
import io
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

HUB_URL = "https://corcoranmce.com/monthly-market-report"

# key used by the OS panel, display label, heading text on the hub page
REGIONS = [
    ("dc",          "Washington, DC",         "Washington, DC"),
    ("montgomery",  "Montgomery County",      "Montgomery County"),
    ("pg",          "Prince George's County", "Prince George's County"),
    ("nova",        "Northern Virginia",      "Northern Virginia"),
    ("loudoun",     "Loudoun County",         "Loudoun County"),
    ("countryside", "Virginia Countryside",   "Virginia Countryside"),
]

NOTE_PROPERTY_ID = "00000000-0000-0000-0000-000000000001"   # same sentinel the OS uses
LINKS_SUBFOLDER = "links"             # {region_key: url}, read by the OS panel
META_SUBFOLDER = "market_reports"     # run record, read by the OS panel + health checks

BANNED_WORDS = ["stunning", "luxurious", "impeccable", "nestled", "boasts", "charming"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

NAVY, GOLD, CREAM = "#0A2342", "#C8A96E", "#F7F4EF"


def _norm(s: str) -> str:
    """Normalise curly apostrophes and whitespace so headings and sentences match."""
    s = (s or "").replace("\u2019", "'").replace("\u2018", "'").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Hub page parsing
# ─────────────────────────────────────────────────────────────────────────────
def parse_hub(html_text: str) -> dict:
    """Return {'statpak_month': 'September 2026', 'regions': {key: {summary, source_url}}}.

    Region blocks are found by their heading text, and each block's text and
    first Google Drive link are read from the heading's enclosing list item (or
    its parent). Missing regions are simply absent; the caller treats that as a
    failure for that region.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")
    page_text = _norm(soup.get_text(" "))
    m = re.search(r"StatPak\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", page_text)
    statpak_month = (m.group(1) + " " + m.group(2)) if m else ""

    regions: dict = {}
    wanted = {_norm(h).lower(): (k, lbl) for k, lbl, h in REGIONS}
    for tag in soup.find_all(["h2", "h3", "h4", "h5", "h6", "strong"]):
        t = _norm(tag.get_text(" ")).lower()
        if t not in wanted:
            continue
        key, _label = wanted[t]
        if key in regions:
            continue   # first qualifying block wins
        box = tag.find_parent("li") or tag.parent
        if box is None:
            continue
        text = _norm(box.get_text(" "))
        # drop the heading itself and the "Learn More" link label
        text = _norm(text[len(_norm(tag.get_text(" "))):]) if text.lower().startswith(t) else text
        text = _norm(re.sub(r"\bLearn More\b", "", text))
        link = ""
        for a in box.find_all("a", href=True):
            if "drive.google.com" in a["href"]:
                link = a["href"]
                break
        # A real report block carries its StatPak link and a dated sentence. A
        # menu item or teaser with the same heading text does not; skip it and
        # keep looking rather than letting it shadow the real block.
        if not link or not re.search(r"\bin\s+(" + "|".join(MONTHS) + r")\s+\d{4}\b", text):
            continue
        regions[key] = {"summary": text, "source_url": link}
    return {"statpak_month": statpak_month, "regions": regions}


# ─────────────────────────────────────────────────────────────────────────────
# 2. Deterministic number extraction from McEnearney's sentence
# ─────────────────────────────────────────────────────────────────────────────
_PCT = r"(up|down)\s+(?:just\s+|only\s+)?(\d+(?:\.\d+)?)%"


def extract_metrics(summary: str) -> dict:
    """Pull the four headline figures out of one region's sentence.

    Raises ValueError naming what could not be found. Never guesses.
    Returns a dict whose every number is a substring of the summary.
    """
    s = _norm(summary)
    out: dict = {}

    m = re.search(r"\bin\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b", s)
    if not m:
        raise ValueError("data month not found")
    out["data_month"] = m.group(1) + " " + m.group(2)

    # Contract activity vs the same month last year: the first up/down X% in the sentence.
    mm = re.search(r"through the first", s, re.I)
    first_part, sep, ytd_part = (s[:mm.start()], "x", s[mm.end():]) if mm else (s, "", "")
    m = re.search(_PCT, first_part)
    if not m:
        raise ValueError("month-over-year contract change not found")
    out["contract_dir"], out["contract_pct"] = m.group(1), m.group(2)

    m = re.search(r"(?:was\s+)?(down|up)\s+(?:for|in)\s+(.+?)\s+price categor(?:y|ies)", first_part)
    if m:
        out["price_categories"] = m.group(1) + " in " + m.group(2) + " price " + (
            "category" if m.group(2).strip() in ("one", "1") else "categories")
    else:
        out["price_categories"] = ""

    # Year-to-date: the first up/down X% after "first ... months of the year".
    if not sep:
        raise ValueError("year-to-date clause not found")
    m = re.search(r"months of the year,?\s+contract activity is\s+" + _PCT, ytd_part)
    if not m:
        raise ValueError("year-to-date change not found")
    out["ytd_dir"], out["ytd_pct"] = m.group(1), m.group(2)

    # Days on market for homes receiving contracts.
    m = re.search(r"was\s+(\d+)\s*days\s*in\s+(" + "|".join(MONTHS) + r")", s)
    if not m:
        raise ValueError("days on market not found")
    out["dom"] = m.group(1)
    tail = s[m.end():]
    m2 = re.search(r"from\s+(\d+)\s+days", tail)
    if m2:
        out["dom_prior"] = m2.group(1)
        out["dom_note"] = ""
    elif re.search(r"\bunchanged\b", tail):
        out["dom_prior"] = out["dom"]
        out["dom_note"] = "unchanged from a year ago"
    else:
        m3 = re.search(r"(up|down)\s+(\d+(?:\.\d+)?)%\s+from", tail)
        out["dom_prior"] = ""
        out["dom_note"] = (m3.group(1) + " " + m3.group(2) + "% from a year ago") if m3 else ""

    verify_against_source(out, s)
    return out


def verify_against_source(metrics: dict, summary: str) -> None:
    """Independent re-check: every figure must literally appear in the source."""
    s = _norm(summary)
    for k in ("contract_pct", "ytd_pct"):
        if (metrics[k] + "%") not in s:
            raise ValueError(k + " " + metrics[k] + "% is not in the source text")
    if (metrics["dom"] + " days") not in s and (metrics["dom"] + "days") not in s:
        raise ValueError("dom " + metrics["dom"] + " is not in the source text")
    if metrics.get("dom_prior") and metrics["dom_prior"] != metrics["dom"]:
        if (metrics["dom_prior"] + " days") not in s:
            raise ValueError("dom_prior " + metrics["dom_prior"] + " is not in the source text")
    if metrics["data_month"] not in s:
        raise ValueError("data month is not in the source text")


def signed(direction: str, pct: str) -> str:
    return ("+" if direction == "up" else "-") + pct + "%"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Narrative: Claude, fenced by hard checks, with a deterministic fallback
# ─────────────────────────────────────────────────────────────────────────────
def template_narrative(label: str, m: dict, statpak_month: str) -> dict:
    cdir = "rose" if m["contract_dir"] == "up" else "fell"
    ydir = "ahead of" if m["ytd_dir"] == "up" else "behind"
    dom_line = "Homes that went under contract averaged " + m["dom"] + " days on market"
    if m.get("dom_prior") and m["dom_prior"] != m["dom"]:
        dom_line += ", compared with " + m["dom_prior"] + " days a year earlier."
    elif m.get("dom_note"):
        dom_line += ", " + m["dom_note"] + "."
    else:
        dom_line += "."
    insight = ("Contract activity " + cdir + " " + m["contract_pct"] + "% in " + m["data_month"]
               + " compared with a year earlier.")
    trend = ("Year to date, contract activity is running " + m["ytd_pct"] + "% " + ydir
             + " last year's pace. " + dom_line)
    caption = (label + " Market Update. " + statpak_month + ".\n\n" + insight + " " + trend
               + "\n\nThinking about a move? Send us a message for the full report or a private consultation."
               + "\n\n#ForwardMarketStats #RealEstateMarket #CorcoranMcEnearney")
    return {"insight": insight, "trend": trend, "caption": caption, "source": "template"}


def narrative_problems(text: str, allowed_numbers: set) -> list:
    problems = []
    if "\u2014" in text or "\u2013" in text or "&mdash;" in text:
        problems.append("contains a dash that is not allowed")
    low = text.lower()
    for w in BANNED_WORDS:
        if re.search(r"\b" + w + r"\b", low):
            problems.append("banned word: " + w)
    for n in re.findall(r"\d+(?:\.\d+)?", text):
        if n not in allowed_numbers:
            problems.append("number not in source: " + n)
    return problems


async def claude_narrative(label: str, m: dict, summary: str, statpak_month: str,
                           api_key: str, http_post: Callable) -> Optional[dict]:
    """Ask Claude for FORWARD-voice copy. Returns None if the call fails."""
    if not api_key:
        return None
    facts = {
        "region": label, "statpak": statpak_month, "data_month": m["data_month"],
        "contract_change": signed(m["contract_dir"], m["contract_pct"]),
        "price_categories": m.get("price_categories", ""),
        "ytd_change": signed(m["ytd_dir"], m["ytd_pct"]),
        "avg_days_on_market": m["dom"], "prior_year_days_on_market": m.get("dom_prior", ""),
        "dom_note": m.get("dom_note", ""),
    }
    prompt = (
        "You write monthly market updates for FORWARD Real Estate, a luxury brokerage in "
        "Washington DC, Maryland and Northern Virginia. Voice: calm, precise, understated, "
        "in the register of Rolex or the Ritz-Carlton. Short declarative sentences.\n\n"
        "Hard rules:\n"
        "- Use ONLY these facts. Do not add any other number, percentage, date or statistic.\n"
        "- Never use an em dash or en dash. Use periods and commas.\n"
        "- Never use: " + ", ".join(BANNED_WORDS) + ".\n"
        "- No emoji.\n\n"
        "FACTS (verified):\n" + json.dumps(facts, indent=2) + "\n\n"
        "Source sentence for context:\n" + summary + "\n\n"
        "Return ONLY JSON with keys: insight (one sentence), trend (one or two sentences), "
        "caption (Instagram caption, under 900 characters, ending with 3 to 6 hashtags "
        "including #ForwardMarketStats)."
    )
    body = {"model": "claude-sonnet-4-6", "max_tokens": 800,
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    try:
        data = await http_post("https://api.anthropic.com/v1/messages", headers, body)
        txt = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
        out = json.loads(txt)
        if not all(isinstance(out.get(k), str) and out[k].strip() for k in ("insight", "trend", "caption")):
            return None
        return {"insight": out["insight"].strip(), "trend": out["trend"].strip(),
                "caption": out["caption"].strip(), "source": "claude"}
    except Exception:
        return None


def allowed_numbers_for(m: dict, summary: str, statpak_month: str) -> set:
    nums = set(re.findall(r"\d+(?:\.\d+)?", _norm(summary)))
    nums |= set(re.findall(r"\d+(?:\.\d+)?", statpak_month))
    return nums


async def build_narrative(label, m, summary, statpak_month, api_key, http_post) -> dict:
    allowed = allowed_numbers_for(m, summary, statpak_month)
    c = await claude_narrative(label, m, summary, statpak_month, api_key, http_post)
    if c:
        probs = narrative_problems(c["insight"] + " " + c["trend"] + " " + c["caption"], allowed)
        if not probs:
            return c
        t = template_narrative(label, m, statpak_month)
        t["rejected_claude"] = probs
        return t
    return template_narrative(label, m, statpak_month)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Rendering (HTML; turned into PDF and PNG by headless Chromium)
# ─────────────────────────────────────────────────────────────────────────────
def _e(s: str) -> str:
    return _html.escape(s or "", quote=True)


def _kpis(m: dict) -> list:
    k = [(signed(m["contract_dir"], m["contract_pct"]), "Contract activity, " + m["data_month"] + " vs a year earlier"),
         (signed(m["ytd_dir"], m["ytd_pct"]), "Year-to-date contract activity"),
         (m["dom"] + " days", "Average days on market, " + m["data_month"])]
    if m.get("dom_prior") and m["dom_prior"] != m["dom"]:
        k.append((m["dom_prior"] + " days", "Average days on market a year earlier"))
    elif m.get("price_categories"):
        k.append((m["price_categories"].split(" in ")[0].capitalize(),
                  "Price categories: " + m["price_categories"]))
    return k


_FONTS = ('<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700'
          '&family=Montserrat:wght@500;600;700&display=swap" rel="stylesheet">')


def render_report_html(label: str, m: dict, n: dict, statpak_month: str) -> str:
    cards = "".join(
        '<div class="k"><div class="v">' + _e(v) + '</div><div class="l">' + _e(l) + "</div></div>"
        for v, l in _kpis(m))
    return ("<!doctype html><html><head><meta charset='utf-8'>" + _FONTS + "<style>"
            "@page{size:Letter;margin:0}*{box-sizing:border-box}"
            "body{margin:0;background:" + CREAM + ";color:" + NAVY + ";font-family:Montserrat,Arial,sans-serif}"
            ".top{background:" + NAVY + ";color:" + CREAM + ";padding:56px 64px 44px}"
            ".eb{font-size:11px;letter-spacing:.32em;color:" + GOLD + ";font-weight:700}"
            ".t{font-family:'Cormorant Garamond',Georgia,serif;font-size:48px;font-weight:600;margin:14px 0 6px}"
            ".s{font-size:12px;letter-spacing:.08em;opacity:.8}"
            ".body{padding:44px 64px}"
            ".h{font-size:10px;letter-spacing:.28em;font-weight:700;color:" + GOLD + ";margin:0 0 10px}"
            ".p{font-family:'Cormorant Garamond',Georgia,serif;font-size:21px;line-height:1.45;margin:0 0 30px}"
            ".g{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:0 0 34px}"
            ".k{background:#fff;border-top:3px solid " + GOLD + ";padding:22px 22px 18px}"
            ".v{font-family:'Cormorant Garamond',Georgia,serif;font-size:40px;font-weight:700}"
            ".l{font-size:11px;letter-spacing:.06em;color:#5b6474;margin-top:6px;line-height:1.4}"
            ".f{position:absolute;bottom:40px;left:64px;right:64px;border-top:1px solid #d9d2c3;padding-top:14px;"
            "font-size:10px;color:#5b6474;display:flex;justify-content:space-between}"
            "html,body{height:100%}.page{position:relative;min-height:100%}"
            "</style></head><body><div class='page'>"
            "<div class='top'><div class='eb'>F O R W A R D &nbsp; M A R K E T &nbsp; S T A T S</div>"
            "<div class='t'>" + _e(label) + "</div>"
            "<div class='s'>" + _e(statpak_month) + " StatPak &nbsp;|&nbsp; Data through " + _e(m["data_month"]) + "</div></div>"
            "<div class='body'><div class='h'>MARKET INSIGHT</div><p class='p'>" + _e(n["insight"]) + "</p>"
            "<div class='h'>KEY MARKET INDICATORS</div><div class='g'>" + cards + "</div>"
            "<div class='h'>TREND SUMMARY</div><p class='p'>" + _e(n["trend"]) + "</p></div>"
            "<div class='f'><span>Source: Corcoran McEnearney Market in a Minute</span>"
            "<span>www.FwrdRealEstate.com</span></div></div></body></html>")


def render_square_html(label: str, m: dict, statpak_month: str) -> str:
    kp = _kpis(m)[:3]
    rows = "".join('<div class="r"><div class="v">' + _e(v) + '</div><div class="l">' + _e(l) + "</div></div>"
                   for v, l in kp)
    return ("<!doctype html><html><head><meta charset='utf-8'>" + _FONTS + "<style>"
            "*{box-sizing:border-box}body{margin:0;width:1080px;height:1080px;background:" + NAVY + ";color:" + CREAM + ";"
            "font-family:Montserrat,Arial,sans-serif;padding:90px;display:flex;flex-direction:column}"
            ".eb{font-size:22px;letter-spacing:.3em;color:" + GOLD + ";font-weight:700}"
            ".t{font-family:'Cormorant Garamond',Georgia,serif;font-size:92px;font-weight:600;line-height:1.02;margin:34px 0 10px}"
            ".s{font-size:24px;opacity:.8;margin-bottom:56px}"
            ".r{border-top:2px solid " + GOLD + ";padding:24px 0 18px}"
            ".v{font-family:'Cormorant Garamond',Georgia,serif;font-size:76px;font-weight:700;line-height:1}"
            ".l{font-size:22px;opacity:.85;margin-top:8px}"
            ".f{margin-top:auto;font-size:20px;letter-spacing:.2em;color:" + GOLD + "}"
            "</style></head><body><div class='eb'>MARKET STATS</div>"
            "<div class='t'>" + _e(label) + "</div>"
            "<div class='s'>Data through " + _e(m["data_month"]) + "</div>" + rows +
            "<div class='f'>FORWARD REAL ESTATE</div></body></html>")


async def render_pdf_and_png(report_html: str, square_html: str) -> tuple:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"])
        try:
            page = await browser.new_page(viewport={"width": 816, "height": 1056})
            await page.set_content(report_html, wait_until="networkidle", timeout=45000)
            await page.evaluate("() => document.fonts.ready.then(() => true).catch(() => true)")
            pdf = await page.pdf(print_background=True, prefer_css_page_size=True)
            sq = await browser.new_page(viewport={"width": 1080, "height": 1080})
            await sq.set_content(square_html, wait_until="networkidle", timeout=45000)
            await sq.evaluate("() => document.fonts.ready.then(() => true).catch(() => true)")
            png = await sq.screenshot(type="png", full_page=False)
            return pdf, png
        finally:
            await browser.close()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Drive + publishing
# ─────────────────────────────────────────────────────────────────────────────
def drive_probe(drive, folder_id: str) -> dict:
    """Prove the service account can write to the folder: create then delete a tiny file."""
    from googleapiclient.http import MediaIoBaseUpload
    f = drive.files().create(
        body={"name": "_forward_os_write_probe.txt", "parents": [folder_id]},
        media_body=MediaIoBaseUpload(io.BytesIO(b"probe"), mimetype="text/plain"),
        fields="id", supportsAllDrives=True).execute()
    drive.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
    return {"can_write": True}


def drive_month_folder(drive, parent_id: str, name: str) -> str:
    q = ("name = '" + name.replace("'", "\\'") + "' and '" + parent_id + "' in parents and "
         "mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    r = drive.files().list(q=q, fields="files(id)", supportsAllDrives=True,
                           includeItemsFromAllDrives=True, pageSize=1).execute()
    if r.get("files"):
        return r["files"][0]["id"]
    f = drive.files().create(body={"name": name, "parents": [parent_id],
                                   "mimeType": "application/vnd.google-apps.folder"},
                             fields="id", supportsAllDrives=True).execute()
    return f["id"]


def drive_upload(drive, folder_id: str, name: str, data: bytes, mime: str) -> str:
    from googleapiclient.http import MediaIoBaseUpload
    f = drive.files().create(body={"name": name, "parents": [folder_id]},
                             media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False),
                             fields="id,webViewLink", supportsAllDrives=True).execute()
    return f.get("webViewLink") or ("https://drive.google.com/file/d/" + f["id"] + "/view")


# Shared with the OS front end. These used to live in property_notes under a
# placeholder property id, which that table's foreign key rejects, so no write
# ever succeeded. os_settings holds one jsonb value per key.
SETTINGS_KEYS = {LINKS_SUBFOLDER: "market_report_links", META_SUBFOLDER: "market_reports_meta"}


def notes_read(supabase, subfolder: str) -> Optional[dict]:
    r = (supabase.table("os_settings").select("value")
         .eq("key", SETTINGS_KEYS[subfolder]).limit(1).execute())
    return r.data[0]["value"] if r.data else None


def notes_write(supabase, subfolder: str, obj: dict) -> None:
    """One atomic upsert. A failed write leaves the stored value exactly as it was."""
    supabase.table("os_settings").upsert({
        "key": SETTINGS_KEYS[subfolder], "value": obj,
        "updated_by": "FORWARD OS (market reports)",
        "updated_at": datetime.now(timezone.utc).isoformat()}, on_conflict="key").execute()


# ─────────────────────────────────────────────────────────────────────────────
# 6. The run
# ─────────────────────────────────────────────────────────────────────────────
def _slug(label: str) -> str:
    return re.sub(r"[^A-Za-z]", "", label.replace("'", ""))


def _month_code(statpak_month: str) -> str:
    mon, yr = statpak_month.split()
    return yr + "-" + str(MONTHS.index(mon) + 1).zfill(2)


async def run(*, fetch_text: Callable, http_post: Callable, supabase, drive_factory: Callable,
              folder_id: str, api_key: str, logger, force: bool = False, dry_run: bool = False) -> dict:
    """One monthly cycle. Returns a report dict; never raises for per-region problems."""
    started = datetime.now(timezone.utc).isoformat()
    hub = parse_hub(await fetch_text(HUB_URL))
    month = hub["statpak_month"]
    if not month:
        raise RuntimeError("Could not find the StatPak month on " + HUB_URL + ". The page layout may have changed.")

    prev = None if dry_run else notes_read(supabase, META_SUBFOLDER)
    if prev and prev.get("statpak_month") == month and prev.get("status") == "ok" and not force:
        return {"skipped": True, "reason": "already published " + month, "statpak_month": month}

    results, errors = {}, {}
    for key, label, _h in REGIONS:
        blk = hub["regions"].get(key)
        if not blk:
            errors[key] = "region not found on the hub page"
            continue
        try:
            m = extract_metrics(blk["summary"])
            n = await build_narrative(label, m, blk["summary"], month, api_key, http_post)
            results[key] = {"label": label, "metrics": m, "narrative": n,
                            "summary": blk["summary"], "source_url": blk["source_url"]}
        except Exception as e:
            errors[key] = str(e)

    if dry_run:
        return {"dry_run": True, "statpak_month": month, "regions": results, "errors": errors}

    # Drive setup failures (env var missing, service account not a member of the
    # shared drive) must be RECORDED, not just raised, or the health check would
    # never see them and the OS would keep serving last month's links silently.
    try:
        if not folder_id:
            raise RuntimeError("MARKET_STATS_FOLDER_ID is not set")
        drive = drive_factory()
        month_folder = drive_month_folder(drive, folder_id, "FORWARD_Market_" + _month_code(month))
    except Exception as e:
        meta = {"statpak_month": month, "status": "failed", "generated_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(), "regions": {},
                "errors": dict(errors, _drive="Drive setup failed: " + str(e)), "source": HUB_URL}
        notes_write(supabase, META_SUBFOLDER, meta)
        logger.error("[market-reports] Drive setup failed: %s", e)
        return meta
    links = dict((notes_read(supabase, LINKS_SUBFOLDER) or {}))
    published = {}
    for key, r in results.items():
        try:
            base = "FORWARD_" + "{}" + "_" + _slug(r["label"]) + "_" + _month_code(month)
            pdf, png = await render_pdf_and_png(
                render_report_html(r["label"], r["metrics"], r["narrative"], month),
                render_square_html(r["label"], r["metrics"], month))
            pdf_url = drive_upload(drive, month_folder, base.format("Market") + ".pdf", pdf, "application/pdf")
            png_url = drive_upload(drive, month_folder, base.format("IG_Square") + ".png", png, "image/png")
            cap_url = drive_upload(drive, month_folder, base.format("Caption") + ".txt",
                                   r["narrative"]["caption"].encode("utf-8"), "text/plain")
            published[key] = {"label": r["label"], "pdf": pdf_url, "png": png_url, "caption": cap_url,
                              "data_month": r["metrics"]["data_month"], "narrative_source": r["narrative"]["source"],
                              "source_url": r["source_url"]}
            links[key] = pdf_url
        except Exception as e:
            errors[key] = "render/upload failed: " + str(e)
            logger.exception("[market-reports] %s failed", key)

    status = "ok" if published and not errors else ("partial" if published else "failed")
    meta = {"statpak_month": month, "status": status, "generated_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "regions": published, "errors": errors, "source": HUB_URL}
    if published:
        notes_write(supabase, LINKS_SUBFOLDER, links)   # only regions that succeeded change
    notes_write(supabase, META_SUBFOLDER, meta)
    return meta


def health_from_meta(meta: Optional[dict], now: Optional[datetime] = None) -> tuple:
    """(status, detail) for automation health. Monthly cadence, so 40 days is the limit."""
    now = now or datetime.now(timezone.utc)
    if not meta:
        return "late", "No FORWARD market report has been published yet."
    if meta.get("status") == "failed":
        return "failed", "Last run failed: " + json.dumps(meta.get("errors", {}))[:400]
    try:
        gen = datetime.fromisoformat(meta["generated_at"])
    except Exception:
        return "late", "Last run has no timestamp."
    days = (now - gen).days
    if days > 40:
        return "late", "Last report is " + str(days) + " days old (" + meta.get("statpak_month", "?") + ")."
    if meta.get("status") == "partial":
        return "late", "Some regions failed: " + ", ".join(sorted(meta.get("errors", {}).keys()))
    return "ok", meta.get("statpak_month", "") + " published"
