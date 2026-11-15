"""Open To Close poller: record WHEN each Transactions task is first seen ticked.

Why (Oct 17, 2026): the Deal Progress tracker in FORWARD OS shows what Transactions
(Sofia, working in Open To Close) has done on each deal and how long items have been
open. Open To Close's task API gives only id, title, created and completed (true or
null): no completion date, no name, no assignee, no due date (read live with the
probe, Oct 16-17). So the backend reads every open transaction's tasks on a schedule
and keeps its own record in Supabase (otc_properties, otc_tasks; SQL in
migrations/2026-10-17_otc_tasks.sql).

Rules kept here:
- READ ONLY toward Open To Close. Only GETs, through otc_probe.OtcClient, which
  spaces calls MIN_GAP apart (their limit is one per second) and scrubs the token
  from every error text.
- A tick's time is the first poll that saw it, never invented. A box that was
  already ticked the first time a transaction is read is marked
  completed_before_tracking (date unknown), not given today's date.
- An untick clears the time and records reopened_at.
- A task missing from Open To Close is marked removed, but ONLY when the task list
  was read completely. If a list fills a page and paging cannot be confirmed, the
  poll stops for that transaction with an error instead of guessing.
- Closed and cancelled transactions are not read, except once more on the poll
  that first sees them closed, so the last ticks are kept.

The store is injected (see SupabaseOtcStore in main.py's wiring) so the tests run
without a database.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from otc_probe import OtcClient, OtcError, _err

PAGE = 100
CLOSED_RE = re.compile(r"closed|cancel|withdrawn|expired|terminated", re.I)


def is_open(status: Optional[str]) -> bool:
    """Unknown status counts as open, so nothing is skipped by mistake."""
    return not (status and CLOSED_RE.search(status))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def otc_time(created) -> Optional[str]:
    """Open To Close task 'created' is {date: 'YYYY-MM-DD HH:MM:SS.ffffff', timezone: 'UTC'}."""
    if isinstance(created, dict) and created.get("date"):
        tz = (created.get("timezone") or "").upper()
        if tz in ("UTC", "Z", "+00:00"):
            try:
                d = datetime.strptime(created["date"][:19], "%Y-%m-%d %H:%M:%S")
                return d.replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                return None
    return None


async def _get_all(c: OtcClient, path: str, base: Optional[dict] = None) -> list:
    """Every row of a list endpoint. Raises OtcError if completeness cannot be shown."""
    rows: list = []
    seen: set = set()
    offset = 0
    while True:
        params = dict(base or {})
        params["limit"] = PAGE
        if offset:
            params["offset"] = offset
        st, data = await c.get(path, params)
        if st != 200 or not isinstance(data, list):
            raise OtcError(f"{path}: " + _err(st, data, c.token))
        ids = [r.get("id") for r in data if isinstance(r, dict)]
        if offset and (not ids or any(i in seen for i in ids)):
            if any(i in seen for i in ids):
                raise OtcError(f"{path}: more than {PAGE} rows and Open To Close ignored the offset; list not read completely")
            break
        rows.extend(r for r in data if isinstance(r, dict))
        seen.update(ids)
        if len(data) < PAGE:
            break
        offset += PAGE
        if offset > 20 * PAGE:
            raise OtcError(f"{path}: over {20 * PAGE} rows; stopped")
    return rows


def merge_tasks(property_id: int, api_tasks: list, existing: list, now: datetime) -> tuple[list, list]:
    """Rows to upsert for one transaction, and a list of changes for the log.

    existing: rows already stored for this property_id (may be empty).
    """
    now_s = _iso(now)
    first_read = not existing
    old = {int(r["task_id"]): r for r in existing}
    out, changes = [], []
    present = set()
    for pos, t in enumerate(api_tasks):
        tid = t.get("id")
        if tid is None:
            continue
        tid = int(tid)
        present.add(tid)
        done = t.get("completed") is True
        prev = old.get(tid)
        row = {
            "property_id": int(property_id),
            "task_id": tid,
            "title": t.get("title"),
            "position": pos,
            "otc_created": otc_time(t.get("created")),
            "completed": done,
            "last_seen_at": now_s,
            "removed_at": None,
        }
        if prev is None:
            row["first_seen_at"] = now_s
            row["reopened_at"] = None
            if done:
                row["completed_seen_at"] = None if first_read else now_s
                row["completed_before_tracking"] = first_read
                changes.append(("ticked_before_tracking" if first_read else "ticked", tid, row["title"]))
            else:
                row["completed_seen_at"] = None
                row["completed_before_tracking"] = False
                if not first_read:
                    changes.append(("added", tid, row["title"]))
        else:
            row["first_seen_at"] = prev.get("first_seen_at") or now_s
            row["reopened_at"] = prev.get("reopened_at")
            was = bool(prev.get("completed"))
            if done and not was:
                row["completed_seen_at"] = now_s
                row["completed_before_tracking"] = False
                changes.append(("ticked", tid, row["title"]))
            elif done and was:
                row["completed_seen_at"] = prev.get("completed_seen_at")
                row["completed_before_tracking"] = bool(prev.get("completed_before_tracking"))
            elif not done and was:
                row["completed_seen_at"] = None
                row["completed_before_tracking"] = False
                row["reopened_at"] = now_s
                changes.append(("unticked", tid, row["title"]))
            else:
                row["completed_seen_at"] = None
                row["completed_before_tracking"] = False
            if prev.get("removed_at"):
                changes.append(("back", tid, row["title"]))
        out.append(row)
    for tid, prev in old.items():
        if tid in present:
            continue
        if prev.get("removed_at"):
            continue
        row = dict(prev)
        row["removed_at"] = now_s
        out.append(row)
        changes.append(("removed", tid, prev.get("title")))
    return out, changes


async def poll(c: OtcClient, store, now: Optional[datetime] = None) -> dict:
    """One pass over Open To Close. Never raises for one bad transaction."""
    now = now or datetime.now(timezone.utc)
    now_s = _iso(now)
    run = {"ok": True, "ran_at": now_s, "properties": 0, "read": [], "skipped": 0,
           "errors": [], "changes": []}
    try:
        props = await _get_all(c, "/v1.1/properties", {"sort_by": "id", "sort_dir": "desc"})
    except OtcError as e:
        run["ok"] = False
        run["errors"].append({"property_id": None, "error": str(e)})
        return run
    known = store.properties()
    prop_rows = []
    for p in props:
        pid = p.get("id")
        if pid is None:
            continue
        pid = int(pid)
        prev = known.get(pid)
        status = p.get("contract_status")
        row = {
            "id": pid,
            "address": p.get("address"),
            "status": status,
            "client_type": p.get("contract_client_type"),
            "agent_name": p.get("agent_name"),
            "team_user_name": p.get("team_user_name"),
            "otc_created": p.get("created"),
            "last_seen_at": now_s,
            "first_seen_at": (prev or {}).get("first_seen_at") or now_s,
            "tasks_polled_at": (prev or {}).get("tasks_polled_at"),
            "task_count": (prev or {}).get("task_count"),
            "done_count": (prev or {}).get("done_count"),
        }
        # Read open deals; read a closed one once more on the poll that first sees it closed.
        need = is_open(status) or (prev is not None and is_open(prev.get("status")))
        if not need:
            run["skipped"] += 1
            prop_rows.append(row)
            continue
        try:
            tasks = await _get_all(c, f"/v1/properties/{pid}/tasks")
            rows, changes = merge_tasks(pid, tasks, store.tasks(pid), now)
            store.upsert_tasks(rows)
            live = [r for r in rows if not r.get("removed_at")]
            row["tasks_polled_at"] = now_s
            row["task_count"] = len(live)
            row["done_count"] = sum(1 for r in live if r.get("completed"))
            run["read"].append(pid)
            run["changes"].extend({"property_id": pid, "change": k, "task_id": t, "title": ti}
                                  for k, t, ti in changes)
        except OtcError as e:
            run["ok"] = False
            run["errors"].append({"property_id": pid, "error": str(e)})
        except Exception as e:   # a store failure must not hide the others
            run["ok"] = False
            run["errors"].append({"property_id": pid, "error": f"{type(e).__name__}: {e}"})
        prop_rows.append(row)
    try:
        store.upsert_properties(prop_rows)
    except Exception as e:
        run["ok"] = False
        run["errors"].append({"property_id": None, "error": f"saving transactions: {type(e).__name__}: {e}"})
    run["properties"] = len(prop_rows)
    return run
