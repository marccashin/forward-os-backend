"""Open To Close: a READ-ONLY probe of what the API sends for tasks.

Why this exists (Oct 15, 2026): Marc wants agents to see in FORWARD OS what the
TC (Sofia, in Open To Close) has done on their deals, and when. Open To Close's
API reference (the Postman collection behind docs.opentoclose.com) lists
GET /v1/properties/:id/tasks and GET /v1/properties/:id/tasks/:task_id, but its
example task shows only id, created, title, private, pinned, critical,
completed (always null in the example) and property. We do not know whether
"completed" is a date or a yes/no, or whether a "completed by" or "assigned to"
field exists that the example leaves out. Never build on a guessed shape: this
probe asks the real API and reports the shape, then the timeline is designed
from what it actually sends.

Rules kept here:
- GET only. Nothing in Open To Close can be changed through this module.
- The token is sent as the api_token query parameter (that is how Open To
  Close authenticates). It must never appear in a response or a log line, so
  every error text is passed through scrub() first: httpx puts the full URL,
  query string included, into its exception messages.
- Open To Close allows one request per second. Every call waits for the
  previous one plus a margin (MIN_GAP seconds).
- /v1/properties (the list with addresses) was announced as retired after
  Aug 14, 2026; the probe lists through /v1.1/properties and reads each
  property's title and status from /v1.1/properties/:id.

The HTTP call is injected (http(method, url) -> (status, bytes)) so the tests
run without the network.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Awaitable, Callable, Optional

BASE = "https://api.opentoclose.com"
MIN_GAP = 1.15          # seconds between calls (their limit is 1 per second)
LIST_LIMIT = 10         # newest transactions shown by the list probe
TASK_ROWS = 60          # tasks returned in full by the task probe

Http = Callable[[str, str], Awaitable[tuple]]


class OtcError(Exception):
    pass


def scrub(text: str, token: str) -> str:
    """Remove the token from any text, and any api_token=... value."""
    s = str(text)
    if token:
        s = s.replace(token, "***")
    return re.sub(r"(api_token=)[^&\s'\"]+", r"\1***", s)


class OtcClient:
    def __init__(self, token: str, http: Http, sleep=asyncio.sleep, clock=time.monotonic):
        self.token = token or ""
        self.http = http
        self.sleep = sleep
        self.clock = clock
        self._last = None
        self._lock = asyncio.Lock()
        self.calls = []   # (path, status) for the report; never the token

    def configured(self) -> bool:
        return bool(self.token)

    async def get(self, path: str, params: Optional[dict] = None):
        """GET BASE+path. Returns (status, parsed JSON or None)."""
        if not self.token:
            raise OtcError("OTC_API_TOKEN is not set on the backend.")
        q = dict(params or {})
        q["api_token"] = self.token
        qs = "&".join(f"{k}={_enc(v)}" for k, v in q.items())
        url = f"{BASE}{path}?{qs}"
        async with self._lock:
            if self._last is not None:
                wait = MIN_GAP - (self.clock() - self._last)
                if wait > 0:
                    await self.sleep(wait)
            try:
                status, body = await self.http("GET", url)
            except Exception as e:
                self._last = self.clock()
                self.calls.append((path, "error"))
                raise OtcError(scrub(f"{type(e).__name__}: {e}", self.token))
            self._last = self.clock()
        self.calls.append((path, status))
        try:
            data = json.loads(body) if body else None
        except Exception:
            data = None
        return status, data


def _enc(v) -> str:
    from urllib.parse import quote
    return quote(str(v), safe="")


def _err(status, data, token: str) -> str:
    msg = data.get("error") if isinstance(data, dict) else None
    return scrub(f"HTTP {status}" + (f": {msg}" if msg else ""), token)


def _field(prop: dict, key: str):
    for fv in (prop.get("field_values") or []):
        if isinstance(fv, dict) and fv.get("key") == key:
            return fv.get("value")
    return None


def _one(data):
    """Single-item endpoints answer a list of one in the published examples."""
    if isinstance(data, list):
        return data[0] if data else None
    return data


def shape(rows: list) -> dict:
    """Every key seen across rows, with the value types seen for it."""
    out: dict = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        for k, v in r.items():
            t = "null" if v is None else ("bool" if isinstance(v, bool) else type(v).__name__)
            if isinstance(v, dict):
                t = "object{" + ",".join(sorted(v.keys())) + "}"
            out.setdefault(k, set()).add(t)
    return {k: sorted(v) for k, v in sorted(out.items())}


async def list_recent(c: OtcClient, limit: int = LIST_LIMIT) -> dict:
    """The newest transactions: id, created, agent, title (address) and status."""
    params = {"limit": limit, "sort_by": "id", "sort_dir": "desc"}
    st, data = await c.get("/v1.1/properties", params)
    sorted_ok = st == 200
    if st != 200:
        # Sorting fields vary by endpoint; try once without it.
        st, data = await c.get("/v1.1/properties", {"limit": limit})
    if st != 200 or not isinstance(data, list):
        raise OtcError("Listing transactions failed: " + _err(st, data, c.token))
    out = []
    for p in data[:limit]:
        if not isinstance(p, dict):
            continue
        row = {"id": p.get("id"), "created": p.get("created"),
               "agent_name": p.get("agent_name"), "team_user_name": p.get("team_user_name")}
        dst, d = await c.get(f"/v1.1/properties/{p.get('id')}")
        d = _one(d)
        if dst == 200 and isinstance(d, dict):
            row["title"] = _field(d, "contract_title")
            row["status"] = _field(d, "contract_status")
            row["client_type"] = _field(d, "contract_client_type")
        else:
            row["detail_error"] = _err(dst, d, c.token)
        out.append(row)
    return {"sorted_newest_first": sorted_ok, "list_keys": shape(data), "properties": out}


async def probe_tasks(c: OtcClient, property_id: int) -> dict:
    """What Open To Close actually sends for one transaction's tasks."""
    attempts = []
    tasks = None
    used = None
    for ver in ("/v1", "/v1.1"):
        path = f"{ver}/properties/{property_id}/tasks"
        st, data = await c.get(path, {"limit": 100})
        attempts.append({"path": path, "status": st,
                         "error": None if st == 200 else _err(st, data, c.token)})
        if st == 200 and isinstance(data, list):
            tasks, used = data, ver
            break
    if tasks is None:
        return {"ok": False, "property_id": property_id, "attempts": attempts}
    done = [t for t in tasks if isinstance(t, dict) and t.get("completed") not in (None, False, "", 0)]
    open_ = [t for t in tasks if isinstance(t, dict) and t not in done]
    # The single-task endpoint may carry fields the list leaves out: read one
    # finished and one open task in full.
    detail = {}
    for label, pool in (("completed_example", done), ("open_example", open_)):
        if pool and pool[0].get("id") is not None:
            st, d = await c.get(f"{used}/properties/{property_id}/tasks/{pool[0]['id']}")
            detail[label] = _one(d) if st == 200 else {"error": _err(st, d, c.token)}
    return {
        "ok": True,
        "property_id": property_id,
        "endpoint": f"{used}/properties/:id/tasks",
        "attempts": attempts,
        "count": len(tasks),
        "completed_count": len(done),
        "keys": shape(tasks),
        "completed_values_seen": sorted({json.dumps(t.get("completed")) for t in tasks if isinstance(t, dict)})[:10],
        "detail_keys": shape([v for v in detail.values() if isinstance(v, dict)]),
        "detail": detail,
        "tasks": tasks[:TASK_ROWS],
    }
