"""Daily hot sheet emails for Concierge and Transactions (Oct 17, 2026).

Step 4 of the Deal Progress tracker. Every weekday morning a Zap asks this backend
for each team's sheet (GET /api/hot-sheet/{team}) and sends it through Gmail to
concierge@ or transactions@fwrdrealestate.com. This module only decides WHAT goes
on the sheet and writes the email; it sends nothing and writes nothing.

What goes on it (Marc, Oct 16 and 17, 2026):
- Items open 3 or more BUSINESS days ("check these"). Open To Close has no due
  dates, so age is the only signal. Business days are Monday to Friday in
  Eastern time; public holidays are not skipped (none are kept anywhere yet).
  An item opened Monday is 3 business days old on Thursday.
- Concierge: unticked Command Center rows owned by Concierge, on ACTIVE listings
  (listing_checklist_items, owner not 'agent') AND on open buyer deals
  (deal_tasks, owner 'concierge'). Open since = the row's created_at, the moment
  the stage opened it, as Deal Progress counts it.
- Left out: rows that wait on an event (worded as the event, not overdue while
  they wait; same titles as WAITING_TITLES in the Command Center's
  src/lib/conciergeTasks.js and DP_WAITING in FORWARD OS: change all three
  together) and Keep in mind reminders (never stored as rows, so never read).
- Buyer deals: pipeline 'buyer', no closed_at, a stage, and the stage is not a
  finished one (Closed..., Total..., Terminated). Some deals still carry
  "Total Closed This Year" with no closed_at (seen live Oct 17): those are done.
- Transactions: for each open Open To Close transaction, the NEXT 4 unticked tasks
  in Open To Close's own order (what Deal Progress shows), flagged only when open
  3+ business days. Open To Close creates every task on day one, so listing every
  unticked task would put closing-day tasks on the sheet weeks early (Marc chose
  next 4, Oct 17). Open since = Open To Close's own created time.
  A task with no created time is still listed, marked "open since an unknown date",
  rather than dropped: silent omissions are worse than an odd line.
- Every email reminds the team to tick items as they finish them, because the
  sheet and the agents' Deal Progress are only as true as the ticks.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MIN_BUSINESS_DAYS = 3
OTC_NEXT = 4

# Word for word the waiting: true rows of forward-command-center src/lib/conciergeTasks.js.
WAITING_TITLES = frozenset({
    "Clear to close received from Transactions",
    "All contingencies cleared",
    "Post-closing plan started in Command Center (if not, mark closed manually)",
    "Closing gift ready in office",
})

FINISHED_BUYER_STAGE = re.compile(r"closed|^total\b|terminat", re.I)
OTC_CLOSED = re.compile(r"closed|cancel|withdrawn|expired|terminated", re.I)

TEAMS = ("concierge", "transactions")


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    if isinstance(ts, datetime):
        d = ts
    else:
        s = str(ts).strip().replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def business_days_open(ts, now: datetime) -> Optional[int]:
    """Weekdays (Eastern) after the day it opened, up to and including today.

    Opened Monday: Tuesday 1, Wednesday 2, Thursday 3. Opened Friday: Monday 1.
    None when the time is missing or unreadable."""
    d = _parse(ts)
    if d is None:
        return None
    start = d.astimezone(ET).date()
    end = now.astimezone(ET).date()
    n = 0
    day = start
    while day < end:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n += 1
    return n


def is_waiting(title) -> bool:
    return str(title or "").strip() in WAITING_TITLES


def _old_enough(days: Optional[int]) -> bool:
    return days is None or days >= MIN_BUSINESS_DAYS


# ── Concierge ────────────────────────────────────────────────────────────────

def concierge_sheet(listings: Iterable[dict], listing_items: Iterable[dict],
                    deals: Iterable[dict], deal_tasks: Iterable[dict], now: datetime) -> dict:
    groups = []

    active = {x["id"]: x for x in listings if str(x.get("status") or "").lower() == "active"}
    by_listing: dict = {}
    for it in listing_items:
        if it.get("completed") or it.get("listing_id") not in active:
            continue
        if str(it.get("owner") or "").lower() == "agent" or is_waiting(it.get("title")):
            continue
        days = business_days_open(it.get("created_at"), now)
        if it.get("created_at") and not _old_enough(days):
            continue
        if not it.get("created_at"):
            days = None
        by_listing.setdefault(it["listing_id"], []).append(
            {"title": str(it.get("title") or "").strip(), "days": days, "stage": it.get("stage")})
    for lid, items in by_listing.items():
        l = active[lid]
        groups.append({"kind": "Listing", "name": l.get("address") or "Listing with no address",
                       "agent": l.get("agent") or "", "stage": l.get("stage") or "", "items": items})

    open_deals = {}
    for d in deals:
        if str(d.get("pipeline") or "").lower() != "buyer" or d.get("closed_at"):
            continue
        st = str(d.get("stage") or "").strip()
        if not st or FINISHED_BUYER_STAGE.search(st):
            continue
        open_deals[d["id"]] = d
    by_deal: dict = {}
    for t in deal_tasks:
        if t.get("completed") or t.get("deal_id") not in open_deals:
            continue
        if str(t.get("owner") or "").lower() != "concierge" or is_waiting(t.get("title")):
            continue
        days = business_days_open(t.get("created_at"), now)
        if t.get("created_at") and not _old_enough(days):
            continue
        if not t.get("created_at"):
            days = None
        by_deal.setdefault(t["deal_id"], []).append(
            {"title": str(t.get("title") or "").strip(), "days": days, "stage": t.get("stage_triggered")})
    for did, items in by_deal.items():
        d = open_deals[did]
        groups.append({"kind": "Buyer", "name": d.get("client_name") or "Buyer with no name",
                       "agent": d.get("agent_name") or "", "stage": d.get("stage") or "", "items": items})

    return _finish("concierge", groups)


# ── Transactions ─────────────────────────────────────────────────────────────

def transactions_sheet(otc_properties: Iterable[dict], otc_tasks: Iterable[dict], now: datetime) -> dict:
    props = {x["id"]: x for x in otc_properties if not OTC_CLOSED.search(str(x.get("status") or ""))}
    by_prop: dict = {}
    for t in otc_tasks:
        if t.get("property_id") in props and not t.get("completed") and not t.get("removed_at"):
            by_prop.setdefault(t["property_id"], []).append(t)
    groups = []
    for pid, tasks in by_prop.items():
        tasks.sort(key=lambda t: (t.get("position") is None, t.get("position") or 0, t.get("task_id") or 0))
        items = []
        for t in tasks[:OTC_NEXT]:
            days = business_days_open(t.get("otc_created"), now)
            if not _old_enough(days):
                continue
            items.append({"title": str(t.get("title") or "").strip(), "days": days, "stage": None})
        if items:
            p = props[pid]
            groups.append({"kind": "Transaction", "name": p.get("address") or "Transaction with no address",
                           "agent": "", "stage": p.get("status") or "", "items": items})
    return _finish("transactions", groups)


def _finish(team: str, groups: list) -> dict:
    for g in groups:
        g["items"].sort(key=lambda i: -(i["days"] if i["days"] is not None else 10_000))
    groups.sort(key=lambda g: (-max((i["days"] if i["days"] is not None else 10_000) for i in g["items"]),
                               g["kind"], g["name"]))
    return {"team": team, "groups": groups, "count": sum(len(g["items"]) for g in groups)}


# ── The email ────────────────────────────────────────────────────────────────

TICK_LINE = {
    "concierge": "Please tick each item in the Command Center the moment it is done. "
                 "This list and the agents' Deal Progress view are only as accurate as the ticks.",
    "transactions": "Please tick each task in Open To Close the moment it is done. "
                    "This list and the agents' Deal Progress view are only as accurate as the ticks.",
}
TEAM_NAME = {"concierge": "Concierge", "transactions": "Transactions"}


def _days_label(days: Optional[int]) -> str:
    if days is None:
        return "open since an unknown date"
    return "open {} business day{}".format(days, "" if days == 1 else "s")


def render(sheet: dict, now: datetime) -> dict:
    team = sheet["team"]
    name = TEAM_NAME[team]
    day = now.astimezone(ET).strftime("%a %b %-d")
    n = sheet["count"]
    if n:
        subject = "{} hot sheet, {}: {} item{} open 3+ business days".format(name, day, n, "" if n == 1 else "s")
    else:
        subject = "{} hot sheet, {}: nothing open 3+ business days".format(name, day)

    e = html.escape
    parts = [
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#1b2a3a;max-width:640px">',
        '<p style="margin:0 0 6px;font-size:18px;font-weight:bold">{} hot sheet, {}</p>'.format(e(name), e(day)),
        '<p style="margin:0 0 16px;font-size:14px">{}</p>'.format(e(TICK_LINE[team])),
    ]
    text = ["{} hot sheet, {}".format(name, day), "", TICK_LINE[team], ""]
    if not n:
        parts.append('<p style="font-size:14px">Nothing has been open 3 or more business days. Nice work.</p>')
        text.append("Nothing has been open 3 or more business days. Nice work.")
    else:
        parts.append('<p style="margin:0 0 16px;font-size:14px">Check these: open 3 or more business days.</p>')
        text.append("Check these: open 3 or more business days.")
        for g in sheet["groups"]:
            meta = " · ".join(x for x in (g["kind"], g["stage"], g["agent"]) if x)
            parts.append('<p style="margin:16px 0 4px;font-size:15px;font-weight:bold">{}</p>'.format(e(g["name"])))
            parts.append('<p style="margin:0 0 6px;font-size:12px;color:#5b6b7b">{}</p>'.format(e(meta)))
            parts.append('<ul style="margin:0;padding-left:20px;font-size:14px">')
            text += ["", g["name"], meta]
            for i in g["items"]:
                parts.append('<li style="margin:0 0 4px">{} <span style="color:#a0522d">({})</span></li>'
                             .format(e(i["title"]), e(_days_label(i["days"]))))
                text.append("- {} ({})".format(i["title"], _days_label(i["days"])))
            parts.append("</ul>")
    parts.append('<p style="margin:24px 0 0;font-size:12px;color:#5b6b7b">Sent by FORWARD OS every weekday '
                 'morning. Items waiting on an event and Keep in mind reminders are left out.</p></div>')
    text += ["", "Sent by FORWARD OS every weekday morning. Items waiting on an event and Keep in mind reminders are left out."]
    return {"team": team, "subject": subject, "html": "".join(parts), "text": "\n".join(text), "count": n}
