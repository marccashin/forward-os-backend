"""
FORWARD Command Center — FastAPI Backend
Deploy on Railway using existing FORWARD OS infrastructure.

Environment variables required:
  SUPABASE_URL               — Supabase project URL
  SUPABASE_SERVICE_ROLE_KEY  — Supabase service role key (backend only, never exposed to browser)
  FUB_API_KEY                — Follow Up Boss API key
  GOOGLE_SERVICE_ACCOUNT_JSON — Service account credentials JSON string
  MARKET_STATS_FOLDER_ID      — Google Drive folder ID for market stats
  CFO_REPORTS_FOLDER_ID       — Google Drive folder ID for CFO reports
  ALLOWED_ORIGINS             — Comma-separated CORS origins (e.g. https://forward-cc.netlify.app)
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import zipfile
import os
import logging
from uuid import uuid4
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Depends, Request, Header, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from supabase import create_client, Client
from jose import jwt, JWTError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("forward-cc")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUPABASE_URL              = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
FUB_API_KEY               = os.environ["FUB_API_KEY"]
FUB_BASE_URL              = "https://api.followupboss.com/v1"
GOOGLE_SA_JSON            = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
MARKET_STATS_FOLDER_ID    = os.environ.get("MARKET_STATS_FOLDER_ID", "")
CFO_REPORTS_FOLDER_ID     = os.environ.get("CFO_REPORTS_FOLDER_ID", "")
ALLOWED_ORIGINS           = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")]
ANTHROPIC_API_KEY         = os.environ.get("ANTHROPIC_API_KEY", "")
NETLIFY_ACCESS_TOKEN      = os.environ.get("NETLIFY_ACCESS_TOKEN", "")
BMR_MODEL                 = "claude-sonnet-4-5"

# ---------------------------------------------------------------------------
# Agent name mapping: FUB display name → FORWARD OS canonical agent name
# ---------------------------------------------------------------------------
AGENT_NAME_MAP: dict[str, str] = {
    "Ash McGowan": "Ashling McGowan",
    # All other FUB agent names are expected to match FORWARD OS names exactly
}

# FUB agent name → OS agent email (mirrors AGENT_EMAILS in index.html)
AGENT_EMAIL_MAP: dict[str, str] = {
    "Marc Cashin":     "marc@marccashin.com",
    "Ashling McGowan": "ashling@fwrdrealestate.com",
    "Ash McGowan":     "ashling@fwrdrealestate.com",
    "Charlotte Lee":   "charlotte@fwrdrealestate.com",
    "Niki Lang":       "niki@fwrdrealestate.com",
    "Cesar Rivera":    "cesar@fwrdrealestate.com",
}

# Active deal stages for task feed (excludes "Closed This Quarter")
TASK_SKIP_PATTERNS: list[str] = [
    "slide fub deal card",
    "move deal card",
    "move fub deal card",
    "update fub stage",
]


# ---------------------------------------------------------------------------
# Supabase admin client
# ---------------------------------------------------------------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ---------------------------------------------------------------------------
# Google Drive helper
# ---------------------------------------------------------------------------
def get_drive_service():
    if not GOOGLE_SA_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")
    info = json.loads(GOOGLE_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_drive_service_write():
    """Drive service with full write access (for uploads/deletes)."""
    if not GOOGLE_SA_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")
    info = json.loads(GOOGLE_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_folder_last_modified(folder_id: str) -> Optional[datetime]:
    """Return the most recent modifiedTime of any file in the given Drive folder."""
    try:
        service = get_drive_service()
        result = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=1
        ).execute()
        files = result.get("files", [])
        if not files:
            return None
        return datetime.fromisoformat(files[0]["modifiedTime"].replace("Z", "+00:00"))
    except Exception as e:
        logger.error("Drive error: %s", e)
        return None

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _safe_str(val) -> str:
    """Coerce a FUB field value (str, dict, None) to a plain string."""
    if not val:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return val.get("name") or val.get("street") or val.get("label") or ""
    return str(val)


# ---------------------------------------------------------------------------
# FUB helpers
# ---------------------------------------------------------------------------
def fub_auth() -> tuple[str, str]:
    return (FUB_API_KEY, "")  # HTTP Basic: key as username, blank password


async def fub_get(path: str, params: dict | None = None) -> Any:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{FUB_BASE_URL}{path}",
            auth=fub_auth(),
            params=params or {}
        )
        logger.info("FUB %s status=%s", path, r.status_code)
        if r.status_code != 200:
            logger.error("FUB error body: %s", r.text[:500])
            r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError(f"FUB returned unexpected type {type(data).__name__}: {str(data)[:200]}")
        return data

# ---------------------------------------------------------------------------
# Auth middleware — verify Supabase JWT
# ---------------------------------------------------------------------------
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")  # optional: from project settings

async def get_current_user(authorization: Optional[str] = Header(None)):
    """Extract and validate the Supabase JWT from the Authorization header."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    try:
        # Decode without full verification if secret not set (development fallback)
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET or "fallback",
            algorithms=["HS256"],
            audience="authenticated",
            options={
                "verify_signature": bool(SUPABASE_JWT_SECRET),
                "verify_aud": bool(SUPABASE_JWT_SECRET)
            }
        )
        return payload
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="FORWARD Command Center API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------
scheduler = AsyncIOScheduler(timezone="America/New_York")
_buyer_create_lock = asyncio.Lock()  # Prevents race-condition duplicate buyers on concurrent FUB webhooks

# ---------------------------------------------------------------------------
# Audit logging helper
# ---------------------------------------------------------------------------

def log_audit(
    action: str,
    agent_name: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    entity_name: str | None = None,
    detail: dict | None = None,
    source: str = "backend",
):
    """Fire-and-forget insert into audit_log. Never raises."""
    try:
        supabase.table("audit_log").insert({
            "action":      action,
            "agent_name":  agent_name,
            "entity_type": entity_type,
            "entity_id":   str(entity_id) if entity_id else None,
            "entity_name": entity_name,
            "detail":      detail or {},
            "source":      source,
        }).execute()
    except Exception as _e:
        logger.warning("[log_audit] insert failed for action=%s: %s", action, _e)





async def job_check_drive_automations():
    """Daily 6am ET: check Drive folders and update automation_health status."""
    logger.info("Running Drive automation check...")
    try:
        await _check_drive_automations()
        logger.info("Drive automation check complete.")
    except Exception as e:
        logger.error("Drive check failed: %s", e)


async def job_sync_agent_task_cache():
    """Daily 6:02am ET: pull FUB tasks per agent and cache in Supabase."""
    logger.info("Running agent task cache sync...")
    try:
        await _sync_agent_task_cache()
        logger.info("Agent task cache sync complete.")
    except Exception as e:
        logger.error("Agent task cache sync failed: %s", e)


async def _sync_agent_task_cache(agent_filter: str | None = None):
    """
    Pull active FUB deals, fetch each deal's next incomplete task, group by
    agent, and upsert one row per agent into agent_task_cache.

    If agent_filter is provided (FORWARD OS canonical name), only that agent's
    row is updated — useful for the manual per-agent refresh endpoint.
    """
    now = datetime.now(timezone.utc)

    # ── Fetch all deals (paginated) ──────────────────────────────────────────
    all_deals: list = []
    offset, limit = 0, 100
    while True:
        data  = await fub_get("/deals", {"limit": limit, "offset": offset})
        batch = data.get("deals", [])
        if not batch:
            break
        all_deals.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    logger.info("Agent task sync: %d total deals fetched", len(all_deals))

    # ── Group tasks by agent ─────────────────────────────────────────────────
    # agent_tasks: { forward_os_agent_name: [task_dict, ...] }
    agent_tasks: dict[str, list] = {}

    for deal in all_deals:
        if not isinstance(deal, dict):
            continue

        pipeline   = _safe_str(deal.get("pipelineName") or "").lower()
        stage      = _safe_str(deal.get("stageName") or "")
        stage_lower = stage.lower()

        if "seller" in pipeline:
            ptype = "seller"
            if stage_lower not in {"listing agreement signed", "coming soon", "listed", "back on the market", "pending"}:
                continue
        elif "buyer" in pipeline:
            ptype = "buyer"
            if stage_lower not in {"ba signed", "actively showing", "pending"}:
                continue
        else:
            continue  # skip Rentals, Referrals, etc.

        # ── Agent name ───────────────────────────────────────────────────────
        users    = deal.get("users") or []
        agent_fub = ""
        if isinstance(users, list) and users:
            for u in users:
                if isinstance(u, dict):
                    role = u.get("role", "").lower()
                    if "agent" in role or role == "":
                        agent_fub = u.get("name", "")
                        break
            if not agent_fub and isinstance(users[0], dict):
                agent_fub = users[0].get("name", "")

        if not agent_fub:
            continue

        # Normalize to FORWARD OS canonical name
        agent_name = AGENT_NAME_MAP.get(agent_fub, agent_fub)

        # If filtering to a single agent, skip all others
        if agent_filter and agent_name != agent_filter:
            continue

        # ── Client name ──────────────────────────────────────────────────────
        people_list = deal.get("people") or []
        if ptype == "buyer" and people_list and isinstance(people_list[0], dict):
            client_name = _safe_str(people_list[0].get("name") or deal.get("name") or "")
        else:
            client_name = _safe_str(deal.get("name") or "")

        person_id = str(people_list[0]["id"]) if people_list and isinstance(people_list[0], dict) else ""
        if not person_id:
            continue

        # ── Next incomplete task ─────────────────────────────────────────────
        task_name, task_due = await _get_next_task(person_id)
        if not task_name:
            continue  # no pending task for this deal — omit from feed

        if agent_name not in agent_tasks:
            agent_tasks[agent_name] = []

        agent_tasks[agent_name].append({
            "client_name": client_name,
            "stage":       stage,
            "pipeline":    ptype,
            "task_name":   task_name,
            "due_date":    task_due,
            "fub_deal_id": str(deal.get("id", ""))
        })

    logger.info("Agent task sync: %d agents with tasks", len(agent_tasks))

    # ── Sort each agent's tasks by due date, then upsert ────────────────────
    for aname, tasks in agent_tasks.items():
        tasks.sort(key=lambda t: t["due_date"] or "9999-99-99")
        supabase.table("agent_task_cache").upsert(
            {"agent_name": aname, "tasks": tasks, "synced_at": now.isoformat()},
            on_conflict="agent_name"
        ).execute()
        logger.info("  Upserted %d tasks for %s", len(tasks), aname)


@app.on_event("startup")
async def startup():
    # Daily 6am ET Drive check (runs alongside FUB sync)
    scheduler.add_job(
        job_check_drive_automations,
        CronTrigger(hour=6, minute=5, timezone="America/New_York"),
        id="drive_check",
        replace_existing=True
    )
    # Daily 6:02am ET — agent task feed cache
    scheduler.add_job(
        job_sync_agent_task_cache,
        CronTrigger(hour=6, minute=2, timezone="America/New_York"),
        id="agent_task_sync",
        replace_existing=True
    )
    scheduler.start()
    logger.info("Scheduler started.")


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


class AgentTaskSyncRequest(BaseModel):
    agent_name: Optional[str] = None


@app.post("/sync-agent-tasks")
async def trigger_agent_task_sync(req: AgentTaskSyncRequest = None):
    """
    Manual trigger for agent task cache sync.
    No auth required — called by FORWARD OS agents from their browser.

    Optionally pass {"agent_name": "..."} to sync only one agent (faster —
    task API calls are only made for that agent's deals).
    """
    agent_filter = req.agent_name if req else None
    try:
        await _sync_agent_task_cache(agent_filter=agent_filter)
        return {"status": "synced", "ts": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        logger.error("Manual agent task sync failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))



async def _get_next_task(person_id: str) -> tuple[str, Optional[str]]:
    """
    Return (task_name, due_date_str) for the next incomplete, non-internal task.
    Fetches up to 10 tasks, skips any matching TASK_SKIP_PATTERNS.
    """
    try:
        data = await fub_get("/tasks", {"personId": person_id, "isCompleted": "false", "sort": "dueDate", "limit": 10})
        tasks = data.get("tasks", [])
        for t in tasks:
            name = t.get("name") or t.get("description") or t.get("subject") or ""
            if any(pat in name.lower().strip() for pat in TASK_SKIP_PATTERNS):
                logger.info("Skipping internal FUB task for person %s: %r", person_id, name)
                continue
            due = t.get("dueDate") or t.get("due")
            return str(name), str(due) if due else None
        return "", None
    except Exception as e:
        logger.warning("Task fetch for person %s: %s", person_id, e)
        return "", None


# ---------------------------------------------------------------------------
# Automation Health — Drive check
# ---------------------------------------------------------------------------
async def _check_drive_automations():
    now = datetime.now(timezone.utc)

    # Check Market Stats (fires Wednesdays — flag if >8 days without update)
    if MARKET_STATS_FOLDER_ID:
        last_mod = get_folder_last_modified(MARKET_STATS_FOLDER_ID)
        if last_mod:
            days_old = (now - last_mod).days
            status   = "ok" if days_old <= 8 else "late"
        else:
            status = "late"

        supabase.table("automation_health").update({
            "last_run": last_mod.isoformat() if last_mod else None,
            "status":   status
        }).eq("automation_name", "Weekly Market Stats Post").execute()

    # Check CFO Report (fires 1st of month — look for file modified this month)
    if CFO_REPORTS_FOLDER_ID:
        last_mod = get_folder_last_modified(CFO_REPORTS_FOLDER_ID)
        if last_mod:
            same_month = last_mod.year == now.year and last_mod.month == now.month
            status     = "ok" if same_month else "late"
        else:
            status = "late"

        supabase.table("automation_health").update({
            "last_run": last_mod.isoformat() if last_mod else None,
            "status":   status
        }).eq("automation_name", "Monthly CFO Report").execute()

# ---------------------------------------------------------------------------
# Webhook receiver — Zapier posts here after each automation runs
# ---------------------------------------------------------------------------
class AutomationPing(BaseModel):
    automation_name: str
    status:          str   # 'ok' | 'failed' | 'late'
    timestamp:       Optional[str] = None
    notes:           Optional[str] = None



@app.post("/webhooks/fub-deal-engaged")
async def fub_deal_engaged(request: Request):
    """
    FUB fires this webhook when a deal moves to "Buyer Engaged" or "Seller Engaged".
    Creates the corresponding OS buyer or property card in Supabase, filed under
    the assigned agent.

    Configure in FUB: Admin > Settings > API > Webhooks (or via FUB automation)
    URL: https://forward-os-backend-production.up.railway.app/webhooks/fub-deal-engaged
    Events: Deals — stage changed to "Buyer Engaged" or "Seller Engaged"

    Expected payload fields (FUB sends these on deal events):
      data.stageName       — "Buyer Engaged" | "Seller Engaged"
      data.pipelineName    — "Buyers" | "Sellers"
      data.name            — deal name (for sellers: property address)
      data.users           — [{"name": "Charlotte Lee", ...}]
      data.people          — [{"name": "John Smith", "email": "...", "phones": [...]}]
    """
    try:
        body = await request.json()
    except Exception:
        return {"received": True, "error": "invalid JSON"}

    logger.info("FUB deal-engaged webhook: %s", str(body)[:500])

    # FUB wraps the deal in body["data"] for webhook events
    data = body.get("data") or body  # handle both wrapped and flat payloads

    stage = _safe_str(data.get("stageName") or "").strip().lower()
    if stage not in ("buyer engaged", "seller engaged"):
        return {"received": True, "skipped": f"stage '{stage}' not handled"}

    # Resolve assigned agent
    users = data.get("users") or []
    fub_agent_name = ""
    if isinstance(users, list) and users:
        for u in users:
            if isinstance(u, dict):
                role = (u.get("role") or "").lower()
                if "agent" in role or role == "":
                    fub_agent_name = u.get("name", "")
                    break
        if not fub_agent_name and isinstance(users[0], dict):
            fub_agent_name = users[0].get("name", "")

    # Map FUB name → OS canonical name
    os_agent = AGENT_NAME_MAP.get(fub_agent_name, fub_agent_name).strip()
    if not os_agent or os_agent not in KNOWN_AGENTS:
        logger.warning("fub-deal-engaged: unrecognised agent '%s' — rejecting webhook to prevent misassignment", fub_agent_name)
        return {"received": True, "skipped": f"unrecognised agent '{fub_agent_name}' — no record created"}
    agent_email = AGENT_EMAIL_MAP.get(os_agent, "")

    # Extract contact info from people array
    people = data.get("people") or []
    person = people[0] if (isinstance(people, list) and people and isinstance(people[0], dict)) else {}
    contact_name = _safe_str(person.get("name") or data.get("name") or "")
    contact_email = _safe_str(person.get("email") or "")
    # FUB phones: [{"value": "...", "type": "mobile"}, ...]
    phones = person.get("phones") or person.get("phone") or []
    contact_phone = ""
    if isinstance(phones, list) and phones:
        contact_phone = _safe_str(phones[0].get("value") if isinstance(phones[0], dict) else phones[0])
    elif isinstance(phones, str):
        contact_phone = phones

    try:
        if stage == "buyer engaged":
            if not contact_name:
                return {"received": True, "error": "no contact name for buyer"}
            # Duplicate guard + insert: held under a lock so concurrent FUB bulk-webhooks
            # can't race past the dup check and create duplicate records.
            async with _buyer_create_lock:
                _dup_q = supabase.table("buyers").select("id,buyer_name").eq("agent_name", os_agent).eq("buyer_name", contact_name).execute().data
                if not _dup_q and contact_email:
                    _dup_q = supabase.table("buyers").select("id,buyer_name").eq("agent_name", os_agent).eq("email", contact_email).neq("email", "").execute().data
                if _dup_q:
                    logger.info("Webhook duplicate skipped: '%s' already exists for %s (id=%s)", contact_name, os_agent, _dup_q[0]["id"])
                    log_audit("buyer.dup_skipped", agent_name=os_agent,
                              entity_type="buyer", entity_id=_dup_q[0]["id"],
                              entity_name=contact_name, detail={"source": "fub_webhook"})
                    return {"received": True, "skipped": "duplicate", "existing_id": _dup_q[0]["id"]}
                result = supabase.table("buyers").insert({
                    "buyer_name":  contact_name,
                    "agent_name":  os_agent,
                    "agent_email": agent_email,
                    "email":       contact_email,
                    "phone":       contact_phone,
                    "status":      "active",
                }).execute()
            logger.info("Created OS buyer: %s → %s", contact_name, os_agent)
            log_audit("buyer.create", agent_name=os_agent,
                      entity_type="buyer", entity_id=result.data[0].get("id") if result.data else None,
                      entity_name=contact_name, detail={"source": "fub_webhook", "email": contact_email})
            return {"received": True, "created": "buyer", "name": contact_name, "agent": os_agent}

        else:  # seller engaged
            # For sellers, deal name is typically the property address
            address = _safe_str(data.get("name") or contact_name or "")
            if not address:
                return {"received": True, "error": "no address for seller"}
            # Duplicate guard: check by address + agent
            _dup_prop = supabase.table("properties").select("id,address").eq("agent_name", os_agent).eq("address", address).execute().data
            if _dup_prop:
                logger.info("Webhook duplicate skipped: property '%s' already exists for %s (id=%s)", address, os_agent, _dup_prop[0]["id"])
                return {"received": True, "skipped": "duplicate", "existing_id": _dup_prop[0]["id"]}
            result = supabase.table("properties").insert({
                "address":     address,
                "agent_name":  os_agent,
                "agent_email": agent_email,
                "seller_name": contact_name,
                "market":      "DC",
                "status":      "draft",
            }).execute()
            logger.info("Created OS property: %s → %s", address, os_agent)
            return {"received": True, "created": "property", "address": address, "agent": os_agent}

    except Exception as e:
        logger.error("fub-deal-engaged DB insert failed: %s", e)
        return {"received": True, "error": str(e)}

@app.post("/webhooks/fub-task-update")
async def fub_task_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    FUB fires this webhook whenever a task is created, updated, or completed.
    We return 200 immediately and re-sync the agent task cache in the background
    so FORWARD OS reflects the change within seconds.

    Configure in FUB: Admin > Settings > API > Webhooks
    URL: https://forward-command-center-production.up.railway.app/webhooks/fub-task-update
    Events: Tasks (all)
    """
    try:
        payload = await request.json()
        logger.info("FUB task webhook received: %s", str(payload)[:300])
    except Exception:
        pass  # payload logging is best-effort
    background_tasks.add_task(job_sync_agent_task_cache)
    return {"received": True}


@app.post("/webhooks/automation-ping")
async def automation_ping(payload: AutomationPing, request: Request):
    """
    Zapier automations POST here on completion.
    Webhook URL (after deploy): https://your-api.railway.app/webhooks/automation-ping

    Example payload:
    {
      "automation_name": "Weekly FUB Pipeline Report",
      "status": "ok",
      "timestamp": "2026-05-03T14:00:00Z",
      "notes": "Sent to all team members"
    }
    """
    ts = payload.timestamp or datetime.now(timezone.utc).isoformat()

    update = {
        "last_run": ts,
        "status":   payload.status if payload.status in ("ok", "late", "failed") else "ok"
    }
    if payload.notes:
        update["notes"] = payload.notes

    result = supabase.table("automation_health").update(update).eq(
        "automation_name", payload.automation_name
    ).execute()

    if not result.data:
        logger.warning("Automation ping: no row found for '%s'", payload.automation_name)

    return {"received": True, "automation": payload.automation_name, "status": payload.status}


# ===========================================================================
# BUYER MARKET REPORT  — /api/buyer-report/*
# ===========================================================================

class BuyerReportComp(BaseModel):
    address: str; status: str; beds: str; baths: str; sqft: str
    list_price: str; sale_price: str; dom: str; ls_ratio: str; notes: str

class BuyerReportSubject(BaseModel):
    address: str; beds: str; baths: str; sqft: str; list_price: str

class BuyerReportAnalysis(BaseModel):
    subject: BuyerReportSubject
    comps: list[BuyerReportComp]
    narrative: str
    offer_guidance: str = ""          # full UAD adjustment detail (collapsible)
    offer_summary: str = ""           # 2-3 sentence buyer-facing plain-English conclusion
    supported_value: str = ""         # e.g. "$659,449" — average adjusted value
    offer_range: str = ""             # e.g. "$639,666–$679,232"
    suggested_offer_price: str = ""   # e.g. "$625,000" — pre-filled in agent UI, editable

class BuyerReportDeployRequest(BaseModel):
    analysis: BuyerReportAnalysis
    offer_price: str; offer_terms: list[str]; client_name: str
    agent_name: str; agent_email: str; agent_phone: str; report_title: str
    market_conditions: str = "balanced"  # low | balanced | competitive | high | war
    subject_dom: int = 0                 # days subject has been on market

class BuyerReportCandidate(BaseModel):
    address: str = ""; beds: str = ""; baths: str = ""; sqft: str = ""
    list_price: str = ""; sale_price: str = ""; dom: str = ""; status: str = ""
    ls_ratio: str = ""; notes: str = ""; suggested_offer: str = ""

class BuyerReportComparisonRequest(BaseModel):
    comparison_mode: bool = True
    candidates: list[BuyerReportCandidate]
    client_name: str; agent_name: str; agent_email: str = ""; agent_phone: str = ""
    report_title: str = "Property Comparison Report"
    market_conditions: str = "balanced"
    offer_terms: list[str] = []


async def _bmr_claude(pdf_bytes: bytes) -> BuyerReportAnalysis:
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode()
    prompt = """Analyze this MLS Agent Full report PDF and return ONLY a JSON object:
{
  "subject": {"address":"","beds":"","baths":"","sqft":"","list_price":""},
  "comps": [{"address":"","status":"Active|Pending|Closed","beds":"","baths":"","sqft":"","list_price":"","sale_price":"","dom":"","ls_ratio":"","notes":""}],
  "narrative": "2-3 paragraphs using ONLY numbers from the PDF. Cite specific comp addresses. Cover: (1) closed sale price range with addresses, (2) DOM range and what it signals about demand, (3) how the subject compares to the comp pool. Never reference data not in the PDF.",
  "offer_summary": "",
  "offer_guidance": "",
  "supported_value": "",
  "offer_range": "",
  "suggested_offer_price": ""
}
Rules: include ALL comps shown (up to 12); sale_price empty if not sold; ls_ratio = sale_price/list_price as "98.5%" (empty if not sold); sqft and beds/baths as plain numbers (e.g. "1248", "4", "2.0"); prices with $ and commas. Return ONLY the JSON — the offer math is calculated separately."""
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": BMR_MODEL, "max_tokens": 4096, "temperature": 0, "messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
        {"type": "text", "text": prompt}
    ]}]}
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
        resp.raise_for_status()
    text = resp.json()["content"][0]["text"].strip()
    text = re.sub(r"^```(?:json)?\s*", "", text); text = re.sub(r"\s*```$", "", text)
    try:
        analysis = BuyerReportAnalysis(**json.loads(text))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Claude returned invalid JSON: {e}")
    # Fill in offer fields with deterministic Python math (not Claude)
    uad = _uad_calc(analysis)
    if uad:
        analysis.supported_value      = uad["supported_value_str"]
        analysis.offer_range          = uad["offer_range_str"]
        analysis.suggested_offer_price = uad["suggested_str"]
        analysis.offer_guidance        = uad["detail_text"]
    return analysis


# ---------------------------------------------------------------------------
# UAD Adjustment Calculator — deterministic Python math, never Claude
# Defaults match the CMA Builder (cma-tool.html) exactly
# ---------------------------------------------------------------------------
UAD = {
    "gla_per_sqft":  50,
    "bedroom":       15_000,
    "full_bath":     12_000,
    "half_bath":      5_000,
    "garage":        20_000,
    "condition": {"A+": 15_000, "A": 10_000, "B": 5_000, "C": 0, "D": -10_000},
}

MARKET_COND_ADJ = {
    "low":         -0.03,   # Slow / Buyer Favored      → -3%
    "balanced":     0.00,   # Balanced                  → ±0%
    "competitive":  0.03,   # Competitive               → +3%
    "high":         0.06,   # Very Competitive          → +6%
    "war":          0.10,   # Bidding War               → +10%
}
MARKET_COND_LABEL = {
    "low":         "Slow / Buyer Favored",
    "balanced":    "Balanced Market",
    "competitive": "Competitive",
    "high":        "Very Competitive",
    "war":         "Bidding War / Multiple Offers",
}
# In competitive markets, the list price is effectively the market floor.
# We don't recommend offering more than this % below list no matter what the comps say.
# In a bidding war, we anchor ABOVE list price.
MARKET_LIST_FLOOR = {
    "low":         None,    # No floor — comps drive everything
    "balanced":    0.94,    # Max 6% below list
    "competitive": 0.97,    # Max 3% below list
    "high":        0.99,    # Max 1% below list
    "war":         1.01,    # At minimum 1% above list (expect competition)
}

def _dom_adj(dom: int) -> float:
    """Adjustment to supported value based on subject's days on market."""
    if dom <= 0:   return 0.0
    if dom <= 7:   return 0.02    # Fresh listing — high demand signal
    if dom <= 21:  return 0.01
    if dom <= 45:  return 0.0
    if dom <= 90:  return -0.02   # Cooling
    return -0.04                   # Stale

def _parse_num(s: str) -> float:
    try: return float(re.sub(r"[^\d.]", "", str(s or "")))
    except: return 0.0

def _parse_baths(baths_str: str) -> tuple[float, float]:
    """Parse '2.1' → (2 full, 1 half). Also handles '2', '2.0', '2.5'."""
    try:
        f = float(baths_str or 0)
        full = int(f)
        half = round((f - full) * 10)  # '2.1' → 1 half bath
        return float(full), float(half)
    except: return 0.0, 0.0

MAX_NET_ADJ_PCT = 0.15  # Exclude comp if |net_adj| > 15% of its sale price (too dissimilar)
MAX_GLA_PCT     = 0.15  # Flag (but don't exclude) if GLA adj alone > 15% of sale price

def _uad_calc(analysis: "BuyerReportAnalysis") -> dict:
    """
    Run UAD adjustments on all CLOSED comps against the subject.

    Quality gates:
    - Comps where |net_adj| > MAX_NET_ADJ are excluded from the average (too dissimilar).
    - Comps where GLA adjustment alone > 10% of sale price are flagged with a warning.
    - If exclusions leave fewer than 2 usable comps, all comps are re-included (with flags).

    Returns dict with comp_rows, excluded_rows, quality_warnings, avg_adjusted,
    supported_value_str, offer_range_str, detail_text, and related fields.
    """
    s = analysis.subject
    subj_sqft  = _parse_num(s.sqft)
    subj_beds  = _parse_num(s.beds)
    subj_full, subj_half = _parse_baths(s.baths)

    def _fmt(v: float) -> str:
        return (f"+${v:,.0f}" if v >= 0 else f"-${abs(v):,.0f}")

    all_rows = []
    for c in analysis.comps:
        if c.status.lower() != "closed" or not c.sale_price:
            continue
        sale = _parse_num(c.sale_price)
        if sale == 0:
            continue
        comp_sqft = _parse_num(c.sqft)
        comp_beds = _parse_num(c.beds)
        comp_full, comp_half = _parse_baths(c.baths)

        gla_adj  = (subj_sqft - comp_sqft) * UAD["gla_per_sqft"] if comp_sqft and subj_sqft else 0
        bed_adj  = (subj_beds - comp_beds) * UAD["bedroom"]
        full_adj = (subj_full - comp_full) * UAD["full_bath"]
        half_adj = (subj_half - comp_half) * UAD["half_bath"]
        net_adj  = gla_adj + bed_adj + full_adj + half_adj
        adjusted = sale + net_adj

        # Quality flags for this comp
        flags = []
        gla_pct = abs(gla_adj) / sale if sale else 0
        net_pct = abs(net_adj) / sale if sale else 0
        if gla_pct > MAX_GLA_PCT:
            flags.append(
                f"GLA adjustment ({_fmt(gla_adj)}) is {gla_pct:.0%} of sale price "
                f"— sqft difference ({abs(subj_sqft - comp_sqft):.0f} sqft) is large"
            )
        if net_pct > MAX_NET_ADJ_PCT:
            flags.append(
                f"Net adjustment {_fmt(net_adj)} is {net_pct:.0%} of sale price — comp may be too dissimilar"
            )

        detail = (
            f"{c.address}: Sale ${sale:,.0f}"
            + (f" | GLA adj: {_fmt(gla_adj)} ({comp_sqft:.0f}→{subj_sqft:.0f} sqft)" if comp_sqft and subj_sqft else "")
            + (f" | Bed adj: {_fmt(bed_adj)}" if bed_adj != 0 else "")
            + (f" | Bath adj: {_fmt(full_adj + half_adj)}" if (full_adj + half_adj) != 0 else "")
            + f" | Net adj: {_fmt(net_adj)} | Adjusted: ${adjusted:,.0f}"
            + (" ⚠ " + "; ".join(flags) if flags else "")
        )

        all_rows.append({
            "address":   c.address,
            "sale":      sale,
            "net_adj":   net_adj,
            "adjusted":  adjusted,
            "detail":    detail,
            "flags":     flags,
            "excluded":  net_pct > MAX_NET_ADJ_PCT,
        })

    if not all_rows:
        return {}

    # Split into usable vs excluded
    usable   = [r for r in all_rows if not r["excluded"]]
    excluded = [r for r in all_rows if r["excluded"]]

    # If fewer than 2 usable comps remain, fall back to using everything (flag it)
    fallback = False
    if len(usable) < 2:
        usable   = all_rows
        excluded = []
        fallback = True

    avg  = sum(r["adjusted"] for r in usable) / len(usable)
    low  = avg * 0.97
    high = avg * 1.03

    list_price  = _parse_num(s.list_price)
    pct_diff    = ((list_price - avg) / avg * 100) if avg else 0
    above_below = "above" if pct_diff >= 0 else "below"

    # Build detail text
    used_lines = "\n".join(r["detail"] for r in usable)
    excl_lines = (
        "\n\nEXCLUDED FROM AVERAGE (adjustment too large — verify these comps):\n"
        + "\n".join(r["detail"] for r in excluded)
    ) if excluded else ""
    fallback_note = (
        "\n\n⚠ NOTE: All comps have large adjustments. Verify subject sqft, beds, and baths above."
    ) if fallback else ""

    detail_text = (
        used_lines
        + excl_lines
        + fallback_note
        + f"\n\nAverage adjusted value (from {len(usable)} comp{'s' if len(usable)!=1 else ''}): ${avg:,.0f}"
        + f" | List price ${list_price:,.0f} is {abs(pct_diff):.1f}% {above_below} supported value"
        + f"\nOffer range: ${low:,.0f}–${high:,.0f} (±3%)"
    )

    # Surface just a count — the detail is visible in the comp cards below
    quality_warnings = []
    if fallback:
        quality_warnings.append("All comps have large size differences — verify subject sqft, beds, and baths")
    if excluded:
        quality_warnings.append(
            f"{len(excluded)} comp{'s' if len(excluded)!=1 else ''} excluded from average (net adjustment >15% of sale price)"
        )

    return {
        "comp_rows":           usable,
        "excluded_rows":       excluded,
        "quality_warnings":    quality_warnings,
        "avg_adjusted":        avg,
        "low":                 low,
        "high":                high,
        "supported_value_str": f"${avg:,.0f}",
        "offer_range_str":     f"${low:,.0f}–${high:,.0f}",
        "suggested_str":       f"${avg:,.0f}",
        "detail_text":         detail_text,
        "pct_diff":            pct_diff,
        "above_below":         above_below,
        "list_price":          list_price,
        "n_comps":             len(usable),
        "n_excluded":          len(excluded),
    }


def _ls_class(ls: str) -> str:
    try:
        p = float(ls.replace("%","").strip())
        return "r-over" if p >= 100 else "r-at" if p >= 95 else "r-under"
    except: return ""

def _bar_pct(price: str, mx: float) -> int:
    try: return min(100, round(float(re.sub(r"[^\d.]","",price)) / mx * 100)) if mx else 0
    except: return 0

def _bmr_build_html(req: BuyerReportDeployRequest) -> str:
    a = req.analysis; s = a.subject
    prices = []
    for c in a.comps:
        for p in [c.list_price, c.sale_price]:
            try: prices.append(float(re.sub(r"[^\d.]","",p)))
            except: pass
    try: prices.append(float(re.sub(r"[^\d.]","",s.list_price)))
    except: pass
    mx = max(prices) if prices else 1.0

    def _ppsf(price_str, sqft_str):
        try:
            p = float(re.sub(r"[^\d.]","",price_str))
            q = float(re.sub(r"[^\d.]","",sqft_str))
            return f"${p/q:,.0f}" if p>0 and q>0 else "—"
        except: return "—"

    comp_rows = ""
    for c in a.comps:
        pc = _ls_class(c.ls_ratio)
        pill = f'<span class="rpill {pc}">{c.ls_ratio}</span>' if c.ls_ratio else "—"
        sc = {"active":"t-active","pending":"t-pending","closed":"t-closed"}.get(c.status.lower(),"t-active")
        ppsf = _ppsf(c.sale_price or c.list_price, c.sqft)
        comp_rows += f"<tr><td><strong>{c.address}</strong></td><td><span class='stag {sc}'>{c.status}</span></td><td>{c.beds}</td><td>{c.baths}</td><td>{c.sqft}</td><td>{c.list_price}</td><td>{c.sale_price or '—'}</td><td>{ppsf}</td><td>{c.dom}</td><td>{pill}</td></tr>"

    bars = f'<div class="bar-row"><div class="bar-label">Subject: {s.address}</div><div class="bar-track"><div class="bar-fill bsubj" style="width:{_bar_pct(s.list_price,mx)}%"></div></div><div class="bar-val">{s.list_price}</div></div>'
    for c in a.comps:
        price = c.sale_price if c.sale_price else c.list_price
        fc = {"r-over":"bgreen","r-at":"bamber","r-under":"bred"}.get(_ls_class(c.ls_ratio),"bn")
        bars += f'<div class="bar-row"><div class="bar-label">{c.address}</div><div class="bar-track"><div class="bar-fill {fc}" style="width:{_bar_pct(price,mx)}%"></div></div><div class="bar-val">{price}</div></div>'

    terms_html = "".join(f'<div class="iblock">{t}</div>' for t in req.offer_terms)
    phone_line = f"<div>{req.agent_phone}</div>" if req.agent_phone.strip() else ""
    gen_date = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # Run deterministic UAD math in Python — never trust Claude's arithmetic
    uad = _uad_calc(a)
    mc_key   = getattr(req, "market_conditions", "balanced") or "balanced"
    dom_days = getattr(req, "subject_dom", 0) or 0
    mc_label = MARKET_COND_LABEL.get(mc_key, "Balanced Market")

    if uad:
        avg       = uad["avg_adjusted"]
        list_p    = uad["list_price"]
        mc_pct    = MARKET_COND_ADJ.get(mc_key, 0.0)
        d_pct     = _dom_adj(dom_days)
        total_pct = mc_pct + d_pct

        # UAD-adjusted offer
        uad_win   = avg * (1 + total_pct)

        # List-price floor: in competitive markets we never recommend more than
        # a small discount from list (the market IS the market).
        floor_pct = MARKET_LIST_FLOOR.get(mc_key)
        if floor_pct is not None and list_p > 0:
            list_floor = list_p * floor_pct
            win_offer  = max(uad_win, list_floor)
            floored    = win_offer > uad_win  # flag: floor was binding
        else:
            win_offer  = uad_win
            floored    = False

        win_low   = win_offer * 0.97
        win_high  = win_offer * 1.03

        py_supported   = uad["supported_value_str"]
        py_detail      = uad["detail_text"]
        py_n           = uad["n_comps"]
        py_pct         = f"{abs(uad['pct_diff']):.1f}%"
        py_above_below = uad["above_below"]

        # Display strings
        mc_adj_str    = (f"+{mc_pct*100:.0f}%" if mc_pct >= 0 else f"{mc_pct*100:.0f}%")
        d_adj_str     = (f"+{d_pct*100:.0f}%" if d_pct >= 0 else f"{d_pct*100:.0f}%") if d_pct != 0 else ""
        total_str     = (f"+{total_pct*100:.0f}%" if total_pct >= 0 else f"{total_pct*100:.0f}%")
        mc_dollar     = win_offer - avg
        mc_dollar_str = (f"+${mc_dollar:,.0f}" if mc_dollar >= 0 else f"-${abs(mc_dollar):,.0f}")
        py_win_offer  = f"${win_offer:,.0f}"
        py_win_range  = f"${win_low:,.0f}–${win_high:,.0f}"

        dom_line   = f" | DOM adjustment ({dom_days} days): {d_adj_str}" if d_pct != 0 else ""
        floor_note = (
            f'<div style="font-size:10px;color:#92400e;margin-top:6px;background:#fef9c3;padding:4px 8px;border-radius:4px">'
            f'Floor applied: comps-only result was ${uad_win:,.0f} — market conditions anchor raised to {py_win_offer}'
            f'</div>'
        ) if floored else ""

        mc_color   = "#166534" if mc_dollar >= 0 else "#991b1b"
        py_market_box = (
            '<div style="background:#fff;border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:16px">'
            '<div style="font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:10px">Market Conditions Adjustment</div>'
            '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px">'
            f'<div><div style="font-size:11px;color:var(--muted);margin-bottom:2px">UAD Supported Value</div><div style="font-size:16px;font-weight:700;color:var(--navy)">{py_supported}</div></div>'
            f'<div><div style="font-size:11px;color:var(--muted);margin-bottom:2px">Market Conditions</div><div style="font-size:14px;font-weight:600;color:var(--navy)">{mc_label}</div><div style="font-size:11px;color:var(--muted)">{mc_adj_str}{dom_line}</div></div>'
            f'<div><div style="font-size:11px;color:var(--muted);margin-bottom:2px">Market Adjustment</div><div style="font-size:16px;font-weight:700;color:{mc_color}">{mc_dollar_str} ({total_str})</div></div>'
            f'</div>{floor_note}</div>'
        )

        if a.offer_summary:
            py_summary = a.offer_summary
        else:
            if floored:
                py_summary = (
                    f"Comps adjusted for size, bedrooms, and bathrooms support a value of {py_supported}. "
                    f"However, in current {mc_label.lower()} conditions with {dom_days} days on market, "
                    f"offering that far below list is not a realistic strategy. "
                    f"The estimated winning offer is {py_win_offer} — anchored to current market dynamics. "
                    f"We recommend offering between {py_win_range.split(chr(8211))[0]} and {py_win_range.split(chr(8211))[1]}."
                )
            else:
                py_summary = (
                    f"Based on {py_n} comparable {'sale' if py_n==1 else 'sales'} adjusted for size, bedrooms, and bathrooms, "
                    f"the market supports a value of {py_supported}. "
                    f"In current {mc_label.lower()} conditions, the estimated winning offer is {py_win_offer} "
                    f"({mc_dollar_str} above supported value). "
                    f"We recommend offering between {py_win_range.split(chr(8211))[0]} and {py_win_range.split(chr(8211))[1]}."
                )
        py_range = py_win_range
    else:
        py_supported   = a.supported_value or a.suggested_offer_price or "—"
        py_win_offer   = a.suggested_offer_price or "—"
        py_range       = a.offer_range or "—"
        py_detail      = a.offer_guidance or "No closed comp data available for adjustment analysis."
        py_summary     = a.offer_summary or "Insufficient closed comp data to calculate a supported value."
        py_market_box  = ""

    # Pre-compute narrative snippet vars (f-strings can't contain backslashes in expressions)
    if len(a.narrative) > 350:
        narr_short_html = a.narrative[:350] + "..."
        narr_toggle_html = (
            '<button class="narrative-toggle" onclick="'
            "document.getElementById('narr-short').style.display='none';"
            "document.getElementById('narr-full').style.display='block';"
            "this.style.display='none'"
            '">Read full narrative ↓</button>'
        )
    else:
        narr_short_html = a.narrative
        narr_toggle_html = ""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{req.report_title}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600;700&family=Jost:wght@300;400;500;600&display=swap">
<style>
:root{{--navy:#0A2342;--navy2:#1b3461;--gold:#C8A96E;--offwhite:#F7F4EF;--white:#ffffff;--text:#1a1a2e;--muted:#6b7280;--border:#e5e0d8}}
*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:'Jost',sans-serif;background:var(--offwhite);color:var(--text);font-size:15px;line-height:1.6}}
.header{{background:var(--navy);padding:20px 40px;display:flex;align-items:center;justify-content:space-between}}
.header-logo{{font-family:'Cormorant Garamond',serif;font-size:22px;color:#fff;letter-spacing:2px;text-transform:uppercase}}
.header-sub{{font-size:11px;color:var(--gold);letter-spacing:3px;text-transform:uppercase;margin-top:2px}}
.nav-tabs{{background:var(--navy2);display:flex;border-bottom:3px solid var(--gold)}}
.nav-tabs button{{background:none;border:none;color:rgba(255,255,255,.65);font-family:'Jost',sans-serif;font-size:13px;letter-spacing:1px;text-transform:uppercase;padding:14px 28px;cursor:pointer;transition:all .2s}}
.nav-tabs button.active,.nav-tabs button:hover{{color:#fff;background:rgba(255,255,255,.07)}}
.nav-tabs button.active{{border-bottom:3px solid var(--gold);margin-bottom:-3px}}
.section{{display:none;padding:40px;max-width:1100px;margin:0 auto}}.section.active{{display:block}}
.hero{{background:var(--navy);color:#fff;border-radius:12px;padding:36px 40px;margin-bottom:32px}}
.hero h1{{font-family:'Cormorant Garamond',serif;font-size:32px;font-weight:700;margin-bottom:6px}}
.hero-sub{{color:var(--gold);font-size:13px;letter-spacing:2px;text-transform:uppercase;margin-bottom:24px}}
.hs{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:16px;margin-top:20px}}
.hs-item label{{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:rgba(255,255,255,.5);display:block;margin-bottom:4px}}
.hs-item span{{font-size:20px;font-weight:600}}
.sc-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin-bottom:32px}}
.sc{{background:#fff;border-radius:10px;padding:20px;border:1px solid var(--border);box-shadow:0 2px 8px rgba(0,0,0,.04)}}
.sc label{{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);display:block;margin-bottom:4px}}
.sc span{{font-size:18px;font-weight:600;color:var(--navy)}}
.verdict{{background:#fff;border-left:4px solid var(--gold);border-radius:0 10px 10px 0;padding:24px 28px;margin-bottom:32px;box-shadow:0 2px 8px rgba(0,0,0,.04)}}
.verdict h3{{font-family:'Cormorant Garamond',serif;font-size:20px;color:var(--navy);margin-bottom:10px}}
.verdict p{{line-height:1.75}}
.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:32px}}
.iblock{{background:#fff;border:1px solid var(--border);border-radius:8px;padding:12px 16px;font-size:13px;color:var(--navy);font-weight:500}}
.comps-table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.04);margin-bottom:32px}}
.comps-table th{{background:var(--navy);color:#fff;font-size:10px;letter-spacing:1.5px;text-transform:uppercase;padding:12px 14px;text-align:left;font-weight:500}}
.comps-table td{{padding:12px 14px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:middle}}
.comps-table tr:last-child td{{border-bottom:none}}.comps-table tr:hover td{{background:var(--offwhite)}}
.stag{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px}}
.t-active{{background:#dbeafe;color:#1e40af}}.t-pending{{background:#fef9c3;color:#92400e}}.t-closed{{background:#dcfce7;color:#166534}}.t-subject{{background:var(--gold);color:var(--navy)}}
.rpill{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}}
.r-over{{background:#dcfce7;color:#166534}}.r-at{{background:#fef9c3;color:#92400e}}.r-under{{background:#fee2e2;color:#991b1b}}
.bar-row{{display:grid;grid-template-columns:200px 1fr 80px;align-items:center;gap:12px;margin-bottom:10px}}
.bar-label{{font-size:12px;color:var(--navy);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.bar-track{{background:#e5e0d8;border-radius:4px;height:18px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:4px;transition:width .6s ease}}
.bn{{background:var(--navy)}}.bg{{background:var(--gold)}}.bgreen{{background:#22c55e}}.bamber{{background:#f59e0b}}.bred{{background:#ef4444}}
.bsubj{{background:linear-gradient(90deg,var(--navy),var(--gold));border:2px solid var(--gold)}}
.bar-val{{font-size:12px;font-weight:600;color:var(--navy);text-align:right}}
.rec-box{{background:var(--navy);color:#fff;border-radius:12px;padding:28px 32px;margin-bottom:24px}}
.rec-box h3{{font-family:'Cormorant Garamond',serif;font-size:22px;color:var(--gold);margin-bottom:12px}}
.rec-box p{{line-height:1.75;color:rgba(255,255,255,.9)}}
.rec-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}}
.offer-grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.offer-price-box{{background:#fff;border:2px solid var(--gold);border-radius:12px;padding:24px;text-align:center}}
.offer-price-box label{{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);display:block;margin-bottom:6px}}
.offer-price-box .price{{font-family:'Cormorant Garamond',serif;font-size:36px;font-weight:700;color:var(--navy)}}
.footer{{background:var(--navy);color:rgba(255,255,255,.7);padding:32px 40px;margin-top:40px;display:grid;grid-template-columns:1fr auto;gap:40px;align-items:start}}
.footer-brand{{font-family:'Cormorant Garamond',serif;font-size:18px;color:#fff;letter-spacing:1px;margin-bottom:6px}}
.footer-sub{{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--gold)}}
.footer-agent{{text-align:right;font-size:13px;line-height:1.8}}
.footer-agent strong{{color:#fff;font-size:15px}}
.disclaimer{{background:var(--offwhite);border:1px solid var(--border);border-radius:8px;padding:16px 20px;margin-top:24px;font-size:11px;color:var(--muted);line-height:1.6}}
.narrative-full{{display:none}}.narrative-toggle{{background:none;border:none;color:var(--gold);font-family:'Jost',sans-serif;font-size:13px;font-weight:500;cursor:pointer;padding:8px 0;text-decoration:underline}}
@media(max-width:768px){{
  .header{{padding:14px 16px}}.header-logo{{font-size:17px}}
  .nav-tabs{{overflow-x:auto;-webkit-overflow-scrolling:touch;flex-wrap:nowrap}}
  .nav-tabs button{{padding:11px 16px;font-size:11px;white-space:nowrap;flex-shrink:0}}
  .section{{padding:20px 14px}}
  .hero{{padding:22px 18px;border-radius:8px}}.hero h1{{font-size:20px;line-height:1.3}}
  .hs{{grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}}.hs-item span{{font-size:16px}}
  .sc-grid{{grid-template-columns:1fr 1fr;gap:10px}}
  .sc{{padding:14px}}.sc span{{font-size:15px}}
  .verdict{{padding:18px 16px}}.verdict h3{{font-size:17px}}
  .verdict p{{font-size:14px}}
  .comps-table{{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch;font-size:12px}}
  .comps-table th,.comps-table td{{padding:9px 10px;white-space:nowrap}}
  .bar-row{{grid-template-columns:100px 1fr 55px;gap:6px;margin-bottom:8px}}
  .bar-label{{font-size:11px}}.bar-val{{font-size:11px}}
  .offer-grid{{grid-template-columns:1fr;gap:12px}}
  .offer-price-box{{padding:18px}}.offer-price-box .price{{font-size:28px}}
  .rec-box{{padding:20px 18px}}.rec-box p{{font-size:14px}}
  .two-col{{grid-template-columns:1fr}}
  .footer{{grid-template-columns:1fr;gap:16px;padding:24px 16px}}.footer-agent{{text-align:left}}
  h2{{font-size:20px!important}}
}}
</style></head><body>
<div class="header"><div><div class="header-logo">FORWARD</div><div class="header-sub">Real Estate Group</div></div>
<div style="color:rgba(255,255,255,.6);font-size:12px;text-align:right">{req.report_title}<br><span style="color:var(--gold)">{gen_date}</span></div></div>
<div class="nav-tabs">
<button class="active" onclick="show('overview',this)">Overview</button>
<button onclick="show('comps',this)">Comparable Sales</button>
<button onclick="show('charts',this)">Market Charts</button>
<button onclick="show('offer',this)">Offer Guidance</button>
</div>
<div id="overview" class="section active">
<div class="hero"><div class="hero-sub">Subject Property</div><h1>{s.address}</h1>
<div class="hs"><div class="hs-item"><label>Beds</label><span>{s.beds}</span></div><div class="hs-item"><label>Baths</label><span>{s.baths}</span></div><div class="hs-item"><label>Sq Ft</label><span>{s.sqft}</span></div><div class="hs-item"><label>List Price</label><span>{s.list_price}</span></div></div></div>
<div class="verdict"><h3>Market Narrative</h3><p id="narr-short">{narr_short_html}</p><p id="narr-full" class="narrative-full">{a.narrative}</p>{narr_toggle_html}</div>
<div class="sc-grid"><div class="sc"><label>Comps Analyzed</label><span>{len(a.comps)}</span></div><div class="sc"><label>Offer Price</label><span>{req.offer_price}</span></div><div class="sc"><label>Prepared For</label><span>{req.client_name}</span></div><div class="sc"><label>Prepared By</label><span>{req.agent_name}</span></div></div>
<div class="disclaimer"><strong>Disclaimer:</strong> This report is prepared for informational purposes only. All data sourced from MLS records and public information. Market conditions change frequently; this analysis represents a snapshot in time and should not be relied upon as the sole basis for any purchase decision.</div>
</div>
<div id="comps" class="section">
<h2 style="font-family:'Cormorant Garamond',serif;font-size:26px;color:var(--navy);margin-bottom:24px">Comparable Sales</h2>
<table class="comps-table"><thead><tr><th>Address</th><th>Status</th><th>Beds</th><th>Baths</th><th>Sq Ft</th><th>List Price</th><th>Sale Price</th><th>$/Sqft</th><th>DOM</th><th>L/S Ratio</th></tr></thead>
<tbody><tr style="background:#f0f4ff"><td><strong>{s.address}</strong> <span class="stag t-subject">Subject</span></td><td>—</td><td>{s.beds}</td><td>{s.baths}</td><td>{s.sqft}</td><td>{s.list_price}</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>
{comp_rows}</tbody></table></div>
<div id="charts" class="section">
<h2 style="font-family:'Cormorant Garamond',serif;font-size:26px;color:var(--navy);margin-bottom:24px">Price Comparison</h2>
<div style="background:#fff;border-radius:12px;padding:28px;box-shadow:0 2px 8px rgba(0,0,0,.04)">{bars}</div></div>
<div id="offer" class="section">
<div class="offer-grid" style="margin-bottom:24px">
<div class="offer-price-box"><label>Your Offer Price</label><div class="price">{req.offer_price}</div></div>
<div class="offer-price-box" style="border-color:var(--navy)"><label>Estimated Winning Offer</label><div class="price" style="font-size:28px;color:var(--navy)">{py_win_offer}</div><div style="font-size:12px;color:var(--muted);margin-top:4px">Range: {py_range}</div></div>
</div>
{py_market_box}
<div class="verdict" style="margin-bottom:24px"><h3>What This Means for You</h3><p style="line-height:1.8">{py_summary}</p></div>
<div style="background:#fff;border-radius:12px;border:1px solid var(--border);margin-bottom:24px">
<button onclick="var d=document.getElementById('uad-detail');var open=d.style.display!=='none';d.style.display=open?'none':'block';this.querySelector('span').textContent=open?'▸ Show':'▾ Hide'" style="width:100%;background:none;border:none;padding:16px 20px;text-align:left;cursor:pointer;font-family:'Jost',sans-serif;font-size:13px;font-weight:600;color:var(--navy);letter-spacing:.5px;display:flex;justify-content:space-between;align-items:center">HOW WE CALCULATED THIS <span style="color:var(--gold);font-size:12px">▸ Show</span></button>
<div id="uad-detail" style="display:none;padding:0 20px 20px;font-size:12px;line-height:1.9;color:var(--text);white-space:pre-wrap;border-top:1px solid var(--border)">{py_detail}</div>
</div>
<div style="background:#fff;border-radius:12px;padding:24px;border:1px solid var(--border)">
<div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:12px">Offer Terms</div>
<div class="rec-grid">{terms_html}</div></div></div>
<div class="footer"><div><div class="footer-brand">FORWARD Real Estate Group</div><div class="footer-sub">Washington DC Metro Area</div></div>
<div class="footer-agent"><strong>{req.agent_name}</strong><div>{req.agent_email}</div>{phone_line}</div></div>
<script>function show(id,el){{document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));document.querySelectorAll('.nav-tabs button').forEach(b=>b.classList.remove('active'));document.getElementById(id).classList.add('active');el.classList.add('active');}}</script>
</body></html>"""


async def _bmr_comparison_claude(req: "BuyerReportComparisonRequest") -> list[dict]:
    """Call Claude to generate per-property offer analysis for comparison mode."""
    if not ANTHROPIC_API_KEY:
        return [{} for _ in req.candidates]
    mc_label = MARKET_COND_LABEL.get(req.market_conditions, "Balanced Market")
    props_text = ""
    for i, c in enumerate(req.candidates):
        props_text += f"""
Property {i+1}: {c.address}
  Beds: {c.beds} | Baths: {c.baths} | SqFt: {c.sqft}
  List Price: {c.list_price} | Sale Price: {c.sale_price or "Active/Not yet sold"}
  DOM: {c.dom or "N/A"} | L/S Ratio: {c.ls_ratio or "N/A"}
  Status: {c.status or "Active"}
  Notes: {c.notes or "None"}
  Agent-suggested offer: {c.suggested_offer or "Not specified"}
"""
    prompt = f"""You are a senior real estate appraiser and buyer's agent in the Washington DC metro area.

The buyer is evaluating {len(req.candidates)} properties with no single subject property selected yet.
Market conditions: {mc_label}
Client: {req.client_name}

{props_text}

IMPORTANT: DOM (Days on Market) may be formatted as "current/total" (e.g. "84/285" means 84 days in this current listing period, 285 total cumulative days on market including prior listings). High total DOM signals a stale listing and stronger negotiating position. Use the total DOM figure when assessing seller motivation.

For EACH property, provide:
1. suggested_offer: A specific recommended offer price (e.g. "$2,350,000") based on list price, DOM, market conditions, and any notes. If the agent provided a suggested offer, use that as your anchor.
2. offer_range: A low-to-high range (e.g. "$2,250,000–$2,450,000")
3. rationale: 2–3 concise sentences explaining the offer strategy for this specific property. Consider DOM, price per sqft, market conditions, and any notes. If the property has been relisted (high cumulative DOM), note the leverage this gives the buyer.
4. strength: One word — "Strong", "Moderate", or "Cautious" — reflecting how aggressively to pursue this property.

Respond ONLY with a JSON array with one object per property, in order. Example:
[
  {{"suggested_offer": "$2,350,000", "offer_range": "$2,250,000–$2,450,000", "rationale": "...", "strength": "Strong"}},
  {{"suggested_offer": "$4,100,000", "offer_range": "$3,950,000–$4,200,000", "rationale": "...", "strength": "Moderate"}}
]"""

    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": "claude-haiku-4-5-20251001", "max_tokens": 1024, "messages": [{"role": "user", "content": prompt}]}
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
            resp.raise_for_status()
            raw = resp.json()["content"][0]["text"].strip()
            logger.info("comparison claude raw: %s", raw[:300])
            # Extract JSON array
            m = re.search(r'\[.*\]', raw, re.DOTALL)
            if m:
                return json.loads(m.group())
            logger.warning("no JSON array found in comparison claude response")
    except Exception as e:
        logger.warning("comparison claude call failed: %s", e)
    return [{} for _ in req.candidates]


def _bmr_build_comparison_html(req: BuyerReportComparisonRequest, analyses: list = None) -> str:
    mc_label = MARKET_COND_LABEL.get(req.market_conditions, "Balanced Market")
    gen_date = datetime.now(timezone.utc).strftime("%B %d, %Y")
    phone_line = f"<div style='opacity:.75'>{req.agent_phone}</div>" if req.agent_phone.strip() else ""
    if analyses is None:
        analyses = [{} for _ in req.candidates]

    STRENGTH_COLOR = {"Strong": "#166534", "Moderate": "#92400e", "Cautious": "#991b1b"}
    STRENGTH_BG    = {"Strong": "#dcfce7", "Moderate": "#fef9c3", "Cautious": "#fee2e2"}

    # Property cards
    cards_html = ""
    for i, c in enumerate(req.candidates):
        ai = analyses[i] if i < len(analyses) else {}
        specs = " &nbsp;·&nbsp; ".join(filter(None, [
            f"{c.beds} bd" if c.beds else "",
            f"{c.baths} ba" if c.baths else "",
            f"{c.sqft} sqft" if c.sqft else "",
        ]))
        suggested = ai.get("suggested_offer") or c.suggested_offer or ""
        offer_range = ai.get("offer_range", "")
        rationale = ai.get("rationale", "")
        strength = ai.get("strength", "")
        dom_note = f'<div style="font-size:12px;color:var(--muted);margin-top:4px">DOM: {c.dom}</div>' if c.dom else ""
        notes_note = f'<div style="font-size:12px;color:var(--muted);margin-top:4px;font-style:italic">{c.notes}</div>' if c.notes else ""
        strength_badge = (
            f'<span style="font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;padding:3px 10px;border-radius:20px;background:{STRENGTH_BG.get(strength,"#f3f4f6")};color:{STRENGTH_COLOR.get(strength,"#374151")}">{strength} Interest</span>'
        ) if strength else ""
        offer_block = ""
        if suggested:
            range_line = f'<div style="font-size:12px;color:var(--muted);margin-top:2px">Range: {offer_range}</div>' if offer_range else ""
            offer_block = f'''
            <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border);display:grid;grid-template-columns:1fr auto;align-items:start;gap:12px">
              <div>
                <div style="font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:4px">Suggested Offer</div>
                <div style="font-size:22px;font-weight:700;color:var(--navy)">{suggested}</div>
                {range_line}
              </div>
              <div>{strength_badge}</div>
            </div>'''
        rationale_block = (
            f'<div style="margin-top:14px;padding:12px 14px;background:#f8fafc;border-left:3px solid var(--gold);border-radius:0 6px 6px 0;font-size:12px;color:#374151;line-height:1.6">{rationale}</div>'
        ) if rationale else ""
        cards_html += f"""
        <div style="background:#fff;border:1px solid var(--border);border-radius:12px;padding:24px;break-inside:avoid;margin-bottom:20px">
          <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px">
            <div style="flex:1;min-width:0">
              <div style="font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--gold);margin-bottom:4px;font-weight:600">Option {i+1}</div>
              <div style="font-size:17px;font-weight:700;color:var(--navy);line-height:1.3">{c.address}</div>
            </div>
            <div style="text-align:right;flex-shrink:0">
              <div style="font-size:10px;color:var(--muted);margin-bottom:2px">List Price</div>
              <div style="font-size:20px;font-weight:700;color:var(--navy)">{c.list_price or "—"}</div>
            </div>
          </div>
          <div style="font-size:13px;color:#555;margin-bottom:6px">{specs}</div>
          {dom_note}{notes_note}{offer_block}{rationale_block}
        </div>"""

    # Comparison table
    table_rows = ""
    fields = [("List Price","list_price"),("Beds","beds"),("Baths","baths"),("Sq Ft","sqft"),("DOM","dom"),("Suggested Offer","suggested_offer")]
    for label, key in fields:
        cells = "".join(f"<td style='padding:8px 12px;border-bottom:1px solid var(--border);text-align:center;font-size:13px'>{getattr(c,key) or '—'}</td>" for c in req.candidates)
        table_rows += f"<tr><td style='padding:8px 12px;border-bottom:1px solid var(--border);font-size:11px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);white-space:nowrap'>{label}</td>{cells}</tr>"
    
    col_headers = "".join(f"<th style='padding:10px 12px;text-align:center;font-size:12px;font-weight:700;color:var(--navy);border-bottom:2px solid var(--border)'>Option {i+1}</th>" for i, _ in enumerate(req.candidates))

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{req.report_title}</title>
<style>
  :root{{--navy:#0a2342;--gold:#c8a96e;--border:#e5e7eb;--bg:#f9fafb;--muted:#6b7280}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:#1f2937;padding:0}}
  .wrap{{max-width:860px;margin:0 auto;padding:32px 20px}}
  @media(max-width:600px){{.wrap{{padding:16px 12px}}}}
</style></head><body>
<div style="background:var(--navy);padding:20px 32px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
  <div><div style="font-size:10px;letter-spacing:3px;text-transform:uppercase;color:var(--gold);margin-bottom:4px">FORWARD | Corcoran McEnearney</div>
  <div style="font-size:20px;font-weight:700;color:#fff">{req.report_title}</div></div>
  <div style="text-align:right;color:#fff;font-size:13px">
    <div style="font-weight:600">{req.agent_name}</div>
    {phone_line}<div style="opacity:.65;font-size:11px">{gen_date}</div>
  </div>
</div>
<div class="wrap">
  <div style="background:#fff;border:1px solid var(--border);border-radius:12px;padding:20px 24px;margin-bottom:28px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
    <div><div style="font-size:11px;color:var(--muted);margin-bottom:2px">Prepared for</div>
    <div style="font-size:18px;font-weight:700;color:var(--navy)">{req.client_name}</div></div>
    <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px 16px;text-align:center">
      <div style="font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:2px">Market Conditions</div>
      <div style="font-size:14px;font-weight:700;color:var(--navy)">{mc_label}</div>
    </div>
  </div>
  <div style="font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:16px;font-weight:600">{len(req.candidates)} Properties · Independent Analysis</div>
  {cards_html}
  <div style="background:#fff;border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:28px">
    <div style="padding:16px 20px;border-bottom:1px solid var(--border)"><div style="font-size:13px;font-weight:700;color:var(--navy)">Side-by-Side Comparison</div></div>
    <div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse">
      <thead><tr><th style="padding:10px 12px;text-align:left;font-size:12px;font-weight:700;color:var(--muted);border-bottom:2px solid var(--border)">Metric</th>{col_headers}</tr></thead>
      <tbody>{table_rows}</tbody>
    </table></div>
  </div>
  <div style="font-size:11px;color:var(--muted);line-height:1.6;padding:16px;background:#fffbeb;border:1px solid #fde68a;border-radius:8px">
    <strong>Disclaimer:</strong> This report is prepared for informational purposes only. All data has been reviewed by {req.agent_name} and is subject to market changes. This does not constitute legal or financial advice.
  </div>
</div></body></html>"""

async def _bmr_netlify_deploy(html: str, site_name: str) -> str:
    if not NETLIFY_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="NETLIFY_ACCESS_TOKEN not configured")
    headers = {"Authorization": f"Bearer {NETLIFY_ACCESS_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        cr = await client.post("https://api.netlify.com/api/v1/sites", headers=headers, json={"name": site_name})
        if cr.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"Netlify site creation failed: {cr.status_code} {cr.text[:200]}")
        site_id = cr.json()["id"]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("index.html", html)
            # Tell Netlify CDN to serve HTML with correct Content-Type
            zf.writestr("_headers", "/index.html\n  Content-Type: text/html; charset=UTF-8\n")
        buf.seek(0)
        dr = await client.post(f"https://api.netlify.com/api/v1/sites/{site_id}/deploys",
            headers={"Authorization": f"Bearer {NETLIFY_ACCESS_TOKEN}", "Content-Type": "application/zip"},
            content=buf.read())
        if dr.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"Netlify deploy failed: {dr.status_code} {dr.text[:200]}")
        deploy_id = dr.json().get("id")

    # Poll until deploy is ready (avoids serving raw HTML before processing finishes)
    if deploy_id:
        poll_headers = {"Authorization": f"Bearer {NETLIFY_ACCESS_TOKEN}"}
        async with httpx.AsyncClient(timeout=60) as poll_client:
            for _ in range(20):  # up to ~60 seconds
                await asyncio.sleep(3)
                pr = await poll_client.get(
                    f"https://api.netlify.com/api/v1/deploys/{deploy_id}",
                    headers=poll_headers
                )
                if pr.status_code == 200 and pr.json().get("state") == "ready":
                    break
                logger.info("BMR deploy state: %s", pr.json().get("state") if pr.status_code == 200 else pr.status_code)

    return f"https://{site_name}.netlify.app"



@app.post("/api/buyer-report/regenerate")
async def buyer_report_regenerate(request: Request):
    """
    Re-generate narrative from the current (agent-edited) comp list.
    UAD math is calculated deterministically in Python — Claude only writes the narrative.
    """
    payload = await request.json()
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    subject_data     = payload.get("subject", {})
    comps_data       = payload.get("comps", [])
    market_conditions = payload.get("market_conditions", "balanced")
    subject_dom       = int(payload.get("subject_dom", 0) or 0)
    if not comps_data:
        raise HTTPException(status_code=400, detail="No comps provided")

    # Build Pydantic objects so we can reuse _uad_calc
    subject = BuyerReportSubject(
        address=subject_data.get("address",""),
        beds=subject_data.get("beds",""),
        baths=subject_data.get("baths",""),
        sqft=subject_data.get("sqft",""),
        list_price=subject_data.get("list_price",""),
    )
    comps = [BuyerReportComp(
        address=c.get("address",""), status=c.get("status",""),
        beds=c.get("beds",""), baths=c.get("baths",""), sqft=c.get("sqft",""),
        list_price=c.get("list_price",""), sale_price=c.get("sale_price",""),
        dom=c.get("dom",""), ls_ratio=c.get("ls_ratio",""), notes=c.get("notes",""),
    ) for c in comps_data]

    temp_analysis = BuyerReportAnalysis(subject=subject, comps=comps, narrative="")
    uad = _uad_calc(temp_analysis)

    # Build comp table for Claude (narrative only — no math)
    comp_lines = "\n".join(
        f"  {c.address} | {c.status} | Beds:{c.beds} Baths:{c.baths} SqFt:{c.sqft} "
        f"| List:{c.list_price} Sale:{c.sale_price or '—'} DOM:{c.dom}"
        for c in comps
    )
    mc_pct    = MARKET_COND_ADJ.get(market_conditions, 0.0)
    d_pct     = _dom_adj(subject_dom)
    mc_label  = MARKET_COND_LABEL.get(market_conditions, "Balanced Market")
    if uad:
        avg       = uad["avg_adjusted"]
        list_p    = uad["list_price"]
        uad_win   = avg * (1 + mc_pct + d_pct)
        floor_pct = MARKET_LIST_FLOOR.get(market_conditions)
        floored   = False
        if floor_pct is not None and list_p > 0:
            win_offer = max(uad_win, list_p * floor_pct)
            floored   = win_offer > uad_win
        else:
            win_offer = uad_win
        win_low   = win_offer * 0.97
        win_high  = win_offer * 1.03
        win_str   = f"${win_offer:,.0f}"
        win_range = f"${win_low:,.0f}–${win_high:,.0f}"
        mc_sign   = "+" if mc_pct >= 0 else ""
        d_sign    = "+" if d_pct >= 0 else ""
        floor_note = f"\n⚠ Floor applied: raw comp result was ${uad_win:,.0f} — raised to {win_str} based on {mc_label} conditions." if floored else ""
        # Offer guidance shown in Step 2 — includes full market-adjusted result
        offer_guidance_text = (
            uad["detail_text"]
            + f"\n\n── Market Conditions Adjustment ──"
            + f"\nConditions: {mc_label} ({mc_sign}{mc_pct*100:.0f}%)"
            + (f"\nDOM adjustment ({subject_dom} days): {d_sign}{d_pct*100:.0f}%" if d_pct != 0 else "")
            + f"\nUAD-adjusted estimate: ${uad_win:,.0f}"
            + floor_note
            + f"\n\n✓ ESTIMATED WINNING OFFER: {win_str}"
            + f"\n  Range: {win_range}"
        )
    else:
        win_str = win_range = ""
        offer_guidance_text = "No closed comps available for adjustment analysis."
    uad_summary = (
        f"Supported value: {uad['supported_value_str']} (avg of {uad['n_comps']} adjusted comps). "
        f"List price ${uad['list_price']:,.0f} is {abs(uad['pct_diff']):.1f}% {uad['above_below']}. "
        f"Market conditions: {mc_label} ({'+' if mc_pct>=0 else ''}{mc_pct*100:.0f}%). "
        f"Estimated winning offer: {win_str}. Recommended range: {win_range}."
    ) if uad else "No closed comps available for adjustment analysis."

    prompt = f"""You are a real estate analyst. Write a market narrative for a buyer based ONLY on the data below. Do not calculate or mention $/sqft. Do not invent any numbers.

SUBJECT: {subject.address} | Beds:{subject.beds} Baths:{subject.baths} SqFt:{subject.sqft} | List:{subject.list_price}

COMPARABLES:
{comp_lines}

UAD ADJUSTMENT RESULT (pre-calculated): {uad_summary}

Return ONLY this JSON (no markdown):
{{
  "narrative": "2-3 paragraphs using ONLY the numbers above. Cover: (1) closed sale price range citing specific addresses, (2) what the UAD-adjusted supported value means relative to list price, (3) DOM range and what it signals about demand.",
  "offer_summary": "2-3 plain-English sentences for the buyer. Reference the supported value and offer range from the UAD result above. No math formulas, no jargon."
}}"""

    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": BMR_MODEL, "max_tokens": 1024, "temperature": 0,
            "messages": [{"role": "user", "content": prompt}]}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
        resp.raise_for_status()

    text = resp.json()["content"][0]["text"].strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Claude returned invalid JSON: {e}")

    return {
        "narrative":              data.get("narrative", ""),
        "offer_summary":          data.get("offer_summary", ""),
        "offer_guidance":         offer_guidance_text,
        "supported_value":        uad.get("supported_value_str", "") if uad else "",
        "offer_range":            win_range,
        "suggested_offer_price":  win_str,
        "quality_warnings":       uad.get("quality_warnings", []) if uad else [],
        "n_excluded":             uad.get("n_excluded", 0) if uad else 0,
    }

@app.post("/api/buyer-report/analyze")
async def buyer_report_analyze(file: UploadFile = File(...)):
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")
    pdf_bytes = await file.read()
    if len(pdf_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF exceeds 20 MB limit")
    analysis = await _bmr_claude(pdf_bytes)
    result = analysis.model_dump()
    # Attach comp quality warnings so the frontend can flag issues in Step 2
    uad = _uad_calc(analysis)
    result["quality_warnings"] = uad.get("quality_warnings", []) if uad else []
    result["n_excluded"]       = uad.get("n_excluded", 0) if uad else 0
    return result


@app.post("/api/buyer-report/deploy")
async def buyer_report_deploy(request: Request):
    body = await request.json()
    report_id = str(uuid4())
    expires_at = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()

    if body.get("comparison_mode"):
        # ── Comparison mode: analyze each property independently ──
        req = BuyerReportComparisonRequest(**body)
        site_name = f"forward-compare-{str(uuid4())[:6]}"
        analyses = await _bmr_comparison_claude(req)
        html = _bmr_build_comparison_html(req, analyses)
        url = await _bmr_netlify_deploy(html, site_name)
        try:
            supabase.table("buyer_reports").insert({
                "id": report_id, "agent_email": req.agent_email, "agent_name": req.agent_name,
                "client_name": req.client_name, "subject_address": f"{len(req.candidates)} properties compared",
                "report_title": req.report_title, "offer_price": "",
                "netlify_site_name": site_name, "url": url, "expires_at": expires_at,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            logger.warning("Supabase buyer_reports insert failed: %s", e)
        return {"url": url, "report_id": report_id, "expires_at": expires_at}

    else:
        # ── Standard mode: single subject + comps ──
        payload = BuyerReportDeployRequest(**body)
        _addr_parts = payload.analysis.subject.address.lower().split(",")[0].strip().split()
        if len(_addr_parts) >= 2:
            _slug_core = re.sub(r"[^a-z0-9]", "", _addr_parts[0]) + re.sub(r"[^a-z0-9]", "", _addr_parts[1])
        else:
            _slug_core = re.sub(r"[^a-z0-9]+", "-", payload.analysis.subject.address.lower())[:20].strip("-")
        site_name = f"forward-{_slug_core}-{str(uuid4())[:4]}"
        html = _bmr_build_html(payload)
        url = await _bmr_netlify_deploy(html, site_name)
        try:
            supabase.table("buyer_reports").insert({
                "id": report_id, "agent_email": payload.agent_email, "agent_name": payload.agent_name,
                "client_name": payload.client_name, "subject_address": payload.analysis.subject.address,
                "report_title": payload.report_title, "offer_price": payload.offer_price,
                "netlify_site_name": site_name, "url": url, "expires_at": expires_at,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            logger.warning("Supabase buyer_reports insert failed: %s", e)
        return {"url": url, "report_id": report_id, "expires_at": expires_at}


@app.post("/api/parse-offer")
async def parse_offer(file: UploadFile = File(...)):
    """Parse a real estate offer PDF and extract structured field data."""
    import io, gc
    from pypdf import PdfReader

    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")
    pdf_bytes = await file.read()
    if len(pdf_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF exceeds 20 MB limit")

    # Extract text from PDF — send plain text to Claude instead of base64-encoded binary.
    # This cuts payload size by ~80% and eliminates the Railway OOM that caused sequential
    # offer uploads to fail (base64 of a 5MB PDF = 7MB in memory for the full Anthropic call).
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        pdf_text = ""
    del pdf_bytes
    gc.collect()

    # Fallback: if text extraction fails (scanned/image PDF), use base64 document API
    use_vision = len(pdf_text.strip()) < 200

    prompt = """You are parsing a real estate purchase offer contract. Extract ALL of the following fields. Return ONLY a valid JSON object with these exact keys. If a field is not present or not applicable, use an empty string "".

{
  "buyer_name": "Full name(s) of buyer(s)",
  "date_submitted": "Date the offer was submitted or contract date",
  "selling_agent_name": "Buyer's agent / selling agent full name",
  "agent_phone": "Selling agent phone number",
  "agent_email": "Selling agent email address",
  "closing_coordinator": "Closing coordinator or assistant name and contact info",
  "cash_portion": "Paragraph 3A - cash portion dollar amount",
  "loan_amount": "Paragraph 3B - loan amount",
  "sales_price": "Paragraph 3C - total sales price",
  "earnest_money": "Paragraph 5A - earnest money amount",
  "option_fee": "Paragraph 5A - option fee amount",
  "additional_earnest_money": "Paragraph 5A(1) - additional earnest money",
  "option_period": "Paragraph 5B - option period in days",
  "title_policy_paid_by": "Paragraph 6A - who pays title policy premium (Buyer or Seller)",
  "title_company": "Paragraph 6A - title company name",
  "shortages_expense_of": "Paragraph 6A(8) - shortages in area at expense of",
  "survey_paid_by": "Paragraph 6C - who pays for new survey",
  "objections": "Paragraph 6D - any objections noted",
  "objection_period": "Paragraph 6D - objection period in days",
  "repairs_requested": "Paragraph 7D(2) - repairs and treatments requested",
  "residential_service_contract": "Paragraph 7H - residential service contract amount",
  "closing_date": "Paragraph 9A - closing date",
  "possession": "Paragraph 10A - possession terms",
  "buyer_contingencies": "Any buyer contingencies (financing, inspection, sale of current home, etc.)",
  "seller_contingencies": "Any seller contingencies",
  "non_realty_items": "Non-realty items requested by buyer",
  "non_realty_sum": "Dollar sum offered for non-realty items",
  "proof_of_funds": "Whether proof of funds was received or mentioned (Yes / No / Not mentioned)",
  "pre_approval_letter": "Whether pre-approval letter was received or mentioned (Yes / No / Not mentioned)",
  "loan_type": "Type of financing (Conventional, FHA, VA, Cash, etc.)",
  "lender_name": "Name of lender or mortgage company",
  "hoa_resale_paid_by": "Who pays HOA resale package (Buyer or Seller)",
  "hoa_resale_fees": "Buyer HOA resale fees not to exceed amount",
  "appraisal_waiver": "Whether buyer is waiving appraisal contingency (Yes / No / Partial)",
  "appraisal_price_delta": "Appraisal gap coverage — amount buyer will cover above appraised value",
  "proof_of_funds_appraisal": "Proof of funds for appraisal waiver provided (Yes / No / Not mentioned)",
  "seller_temp_lease_offered": "Whether seller is requesting a leaseback after closing (Yes / No)",
  "seller_temp_lease_days": "Number of days for seller leaseback",
  "seller_temp_lease_price": "Seller leaseback price per day",
  "buyer_temp_lease_requested": "Whether buyer requested temporary lease before closing (Yes / No)",
  "buyer_temp_lease_days": "Number of days for buyer temporary lease",
  "buyer_temp_lease_price": "Buyer temporary lease price per day",
  "net_to_seller": "Net to seller if calculable — leave blank if not",
  "buyer_agent_compensation": "Buyer agent compensation requested from seller — dollar amount or percentage",
  "additional_notes": "Any other notable terms, conditions, escalation clauses, or special provisions not captured above"
}

Return ONLY the JSON object. No explanation, no markdown, no code fences."""

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    if use_vision:
        raise HTTPException(status_code=422, detail="This PDF appears to be a scanned image and cannot be read as text. Please use a digital PDF or enter the offer details manually.")

    # Send extracted text — no base64, no binary, tiny payload
    payload = {
        "model": BMR_MODEL,
        "max_tokens": 2500,
        "messages": [{"role": "user", "content": f"CONTRACT TEXT:\n\n{pdf_text}\n\n---\n\n{prompt}"}]
    }
    del pdf_text
    gc.collect()

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
            resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()
    finally:
        del payload
        gc.collect()

    raw = re.sub(r"^```(?:json)?\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


class AnalyzeOffersRequest(BaseModel):
    offers: list[dict]
    property_address: str = ""
    list_price: str = ""


@app.post("/api/analyze-offers")
async def analyze_offers(payload: AnalyzeOffersRequest):
    """Generate agent-facing AI analysis of multiple offers."""
    if not payload.offers:
        raise HTTPException(status_code=400, detail="No offers provided")

    def _fmt(offers: list[dict]) -> str:
        lines = []
        for o in offers:
            n = o.get("offer_number", "?")
            d = o.get("offer_data", {})
            lines.append(f"""OFFER {n}:
  Buyer: {d.get('buyer_name') or '—'}
  Sales Price: {d.get('sales_price') or '—'}
  Financing: {d.get('loan_type') or '—'} | Loan: {d.get('loan_amount') or '—'} | Cash: {d.get('cash_portion') or '—'}
  Closing Date: {d.get('closing_date') or '—'} | Possession: {d.get('possession') or '—'}
  Earnest Money: {d.get('earnest_money') or '—'} | Option Fee: {d.get('option_fee') or '—'} | Option Period: {d.get('option_period') or '—'} days
  Appraisal Waiver: {d.get('appraisal_waiver') or '—'} | Appraisal Gap: {d.get('appraisal_price_delta') or '—'}
  Title Paid By: {d.get('title_policy_paid_by') or '—'} | Survey: {d.get('survey_paid_by') or '—'}
  Repairs Requested: {d.get('repairs_requested') or '—'}
  Buyer Contingencies: {d.get('buyer_contingencies') or '—'}
  Seller Contingencies: {d.get('seller_contingencies') or '—'}
  HOA Resale: {d.get('hoa_resale_paid_by') or '—'} | HOA Fees Cap: {d.get('hoa_resale_fees') or '—'}
  Buyer Agent Compensation: {d.get('buyer_agent_compensation') or '—'}
  Pre-Approval: {d.get('pre_approval_letter') or '—'} | Proof of Funds: {d.get('proof_of_funds') or '—'}
  Seller Leaseback: {d.get('seller_temp_lease_offered') or '—'} ({d.get('seller_temp_lease_days') or ''} days @ {d.get('seller_temp_lease_price') or ''}/day)
  Non-Realty Items: {d.get('non_realty_items') or '—'} (sum: {d.get('non_realty_sum') or '—'})
  Additional Notes: {d.get('additional_notes') or '—'}""")
        return "\n\n".join(lines)

    prompt = f"""You are an expert real estate listing agent advising a colleague who has received {len(payload.offers)} offers on their listing.

Property: {payload.property_address or "Not specified"}
List Price: {payload.list_price or "Not specified"}

OFFERS RECEIVED:
{_fmt(payload.offers)}

Output your analysis using EXACTLY the section markers and bullet format shown below. Do not add any text before the first marker.

---STRONGEST OFFER---
• [Why this offer is strongest — price, net to seller, or terms advantage]
• [Financing strength — down payment %, pre-approval headroom, cash reserves]
• [Risk comparison vs. other offers — appraisal, inspection, contingencies]
• [Any additional differentiator — closing timeline, leaseback, earnest money]

---LIKELIHOOD TO CLOSE---
OFFER 1:
• [1-sentence close likelihood rating]
• [Primary risk factor with specific dollar or % figure]
OFFER 2:
• [1-sentence close likelihood rating]
• [Primary risk factor with specific dollar or % figure]
[Repeat for each offer received]

---COUNTER STRATEGY---
OFFER 1:
• [Specific counter price recommendation with dollar figure]
• [Key term to change — repairs cap, earnest money, appraisal gap, timeline]
• [What to insist on and why — use dollar amounts]
OFFER 2:
• [Specific counter price recommendation with dollar figure]
• [Key term to change]
• [What to insist on and why]
[Repeat for each offer received]

---TALKING POINTS FOR SELLER CONVERSATION---
• [How to explain price vs. terms tradeoff to the seller]
• [How to explain financing risk in plain language — avoid jargon]
• [Appraisal gap or waiver implications for this seller specifically]
• [Closing timeline fit — does it match seller's needs?]
• [How to frame the decision without steering — seller chooses]
• [Anything else specific to these offers the seller must understand]

Rules:
- Use OFFER 1:, OFFER 2: etc. (with colon) as the only sub-headers inside sections
- Every point must start with • and use actual numbers from the offers
- Do not repeat section header names inside the section body
- This is for the agent's eyes only — be direct and specific"""

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": BMR_MODEL,
        "max_tokens": 2500,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}]
    }

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
        resp.raise_for_status()

    raw = resp.json()["content"][0]["text"].strip()

    # Parse into structured sections so frontend renders reliably
    def _parse_sections(txt):
        markers = [
            ("strongest",  "---STRONGEST OFFER---"),
            ("likelihood", "---LIKELIHOOD TO CLOSE---"),
            ("counter",    "---COUNTER STRATEGY---"),
            ("talking",    "---TALKING POINTS FOR SELLER CONVERSATION---"),
        ]
        positions = []
        for key, marker in markers:
            idx = txt.find(marker)
            positions.append((idx, key, marker))
        positions.sort()

        extracted = {}
        for i, (idx, key, marker) in enumerate(positions):
            if idx == -1:
                extracted[key] = ""
                continue
            start = idx + len(marker)
            next_idx = positions[i + 1][0] if i + 1 < len(positions) else -1
            chunk = txt[start:next_idx].strip() if next_idx != -1 else txt[start:].strip()
            extracted[key] = chunk

        def split_bullets(text):
            if not text:
                return []
            import re
            return [p.strip() for p in re.split(r'[\n•·]+\s*', text) if p.strip()]

        def split_offer_blocks(text):
            if not text:
                return []
            import re
            parts = re.split(r'(?=(?:^|\n)OFFER \d+:)', text, flags=re.IGNORECASE)
            if len(parts) <= 1:
                parts = re.split(r'(?=(?:^|\n)Offer \d+:)', text, flags=re.IGNORECASE)
            if len(parts) <= 1:
                return [{"label": "", "bullets": split_bullets(text)}]
            blocks = []
            for p in parts:
                p = p.strip()
                if not p:
                    continue
                m = re.match(r'^(OFFER \d+|Offer \d+):\s*([\s\S]*)$', p, re.IGNORECASE)
                if m:
                    num = re.sub(r'\D', '', m.group(1))
                    blocks.append({"label": f"Offer {num}", "bullets": split_bullets(m.group(2).strip())})
                else:
                    blocks.append({"label": "", "bullets": split_bullets(p)})
            return blocks

        return {
            "strongest":  split_bullets(extracted.get("strongest", "")),
            "likelihood": split_offer_blocks(extracted.get("likelihood", "")),
            "counter":    split_offer_blocks(extracted.get("counter", "")),
            "talking":    split_bullets(extracted.get("talking", "")),
        }

    try:
        sections = _parse_sections(raw)
    except Exception as e:
        logger.error("_parse_sections failed: %s", e)
        sections = None
    return {"analysis": raw, "sections": sections}


# ---------------------------------------------------------------------------
# Offer Chat — follow-up Q&A after initial analysis
# ---------------------------------------------------------------------------
class ChatOffersRequest(BaseModel):
    offers: list[dict]
    analysis: str = ""
    history: list[dict] = []   # [{role:"user",content:"..."},{role:"assistant",content:"..."}]
    question: str
    property_address: str = ""
    list_price: str = ""

@app.post("/api/chat-offers")
async def chat_offers(payload: ChatOffersRequest):
    """Follow-up conversational Q&A about offers, retaining full context."""
    if not payload.question.strip():
        raise HTTPException(status_code=400, detail="Question required")

    def _fmt_chat(offers):
        lines = []
        for o in offers:
            n = o.get("offer_number", "?")
            d = o.get("offer_data", {})
            lines.append(f"Offer {n}: Price {d.get('sales_price') or '—'} | {d.get('loan_type') or '—'} | "
                         f"Close {d.get('closing_date') or '—'} | Option {d.get('option_period') or '—'}d | "
                         f"Appraisal waiver: {d.get('appraisal_waiver') or '—'} | "
                         f"Earnest {d.get('earnest_money') or '—'} | Contingencies: {d.get('buyer_contingencies') or '—'} | "
                         f"Seller credit: {d.get('seller_contingencies') or '—'} | "
                         f"Net to seller: {d.get('net_to_seller') or '—'}")
        return "\n".join(lines)

    system = f"""You are an expert real estate listing agent advising a colleague on multiple offers for their listing.

Property: {payload.property_address or "Not specified"} | List Price: {payload.list_price or "Not specified"}

CURRENT OFFER TERMS:
{_fmt_chat(payload.offers)}

INITIAL ANALYSIS ALREADY PROVIDED:
{payload.analysis or "Not yet generated."}

Answer the agent's follow-up questions directly and specifically. Use actual numbers from the offers. Be concise — 2–4 sentences unless complexity warrants more. This is agent-only, confidential."""

    messages = [{"role": h["role"], "content": h["content"]} for h in payload.history]
    messages.append({"role": "user", "content": payload.question})

    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": BMR_MODEL, "max_tokens": 1024, "temperature": 0, "system": system, "messages": messages}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
        resp.raise_for_status()

    return {"answer": resp.json()["content"][0]["text"].strip()}


# ── Market Report Links ──────────────────────────────────────────────────────

class MarketReportLinksRequest(BaseModel):
    dc: str = ""
    nova: str = ""
    montgomery: str = ""
    loudoun: str = ""
    countryside: str = ""

@app.post("/market-reports/update")
async def update_market_report_links(payload: MarketReportLinksRequest):
    """Called by the scheduled task after uploading PDFs to Drive.
    Writes the new Drive links into property_notes so FORWARD OS picks them up."""
    links = {
        "dc":          payload.dc,
        "nova":        payload.nova,
        "montgomery":  payload.montgomery,
        "loudoun":     payload.loudoun,
        "countryside": payload.countryside,
    }
    content = json.dumps(links)

    # Delete existing record
    supabase.table("property_notes").delete().eq("property_id", "__market_reports__").eq("subfolder", "links").execute()

    # Insert fresh record
    supabase.table("property_notes").insert({
        "property_id": "__market_reports__",
        "subfolder":   "links",
        "content":     content,
        "updated_by":  "scheduled-task"
    }).execute()

    return {"ok": True, "links": links}


@app.get("/market-reports")
async def get_market_report_links():
    """Returns the current market report Drive links."""
    rows = supabase.table("property_notes").select("content").eq("property_id", "__market_reports__").eq("subfolder", "links").order("updated_at", desc=True).limit(1).execute()
    if rows.data:
        import json as _json
        try:
            return {"ok": True, "links": _json.loads(rows.data[0]["content"])}
        except Exception:
            pass
    return {"ok": True, "links": {}}


# ── Property Management ──────────────────────────────────────────────────────

class CreatePropertyRequest(BaseModel):
    address: str
    agent_name: str = ""
    agent_email: str = ""
    seller_name: str = ""
    market: str = "DC"
    status: str = "draft"
    # co_seller stored in subfolder_drive_ids JSONB — no dedicated column
    co_seller: str = ""

@app.post("/create-property")
async def create_property(payload: CreatePropertyRequest):
    """Create a new property record. co_seller stored in subfolder_drive_ids JSONB."""
    # Normalize agent name
    agent = AGENT_NAME_MAP.get((payload.agent_name or "").strip(), (payload.agent_name or "").strip()).strip()
    # Duplicate guard: check by address + agent
    try:
        _dup = supabase.table("properties").select("id,address").eq("agent_name", agent).eq("address", (payload.address or "").strip()).execute().data
        if _dup:
            raise HTTPException(status_code=409, detail=f"A property at '{_dup[0]['address']}' already exists in your list.")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Property duplicate check failed (proceeding): %s", e)
    insert_data = {
        "address":     payload.address,
        "agent_name":  payload.agent_name,
        "agent_email": payload.agent_email,
        "seller_name": payload.seller_name,
        "market":      payload.market,
        "status":      payload.status,
    }
    if payload.co_seller:
        insert_data["subfolder_drive_ids"] = {"_co_seller": payload.co_seller}

    try:
        result = supabase.table("properties").insert(insert_data).execute()
        if not result.data:
            raise HTTPException(status_code=500, detail="Insert returned no data")
        log_audit("property.create", agent_name=payload.agent_name,
                  entity_type="property", entity_id=result.data[0].get("id"),
                  entity_name=payload.address, detail={"source": "manual"})
        return result.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database insert failed: {e}")


class SavePropertyNoteRequest(BaseModel):
    property_id: str
    subfolder: str
    content: str

@app.post("/save-property-note")
async def save_property_note(payload: SavePropertyNoteRequest):
    """Upsert a property note via service role key — bypasses RLS."""
    try:
        supabase.table("property_notes") \
            .delete() \
            .eq("property_id", payload.property_id) \
            .eq("subfolder", payload.subfolder) \
            .execute()
        result = supabase.table("property_notes").insert({
            "property_id": payload.property_id,
            "subfolder":   payload.subfolder,
            "content":     payload.content,
        }).execute()
        return result.data[0] if result.data else {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Save note failed: {e}")


# ---------------------------------------------------------------------------
# File upload / delete endpoints
# ---------------------------------------------------------------------------

@app.post("/upload-file")
async def upload_property_file(
    drive_file_id: str = Form(""),
    drive_link: str = Form(""),
    file_name: str = Form(""),
    subfolder: str = Form(""),
    property_id: str = Form(""),
    uploaded_by: str = Form(""),
):
    """Record a Drive file in property_assets (browser uploads directly to Drive)."""
    try:
        asset = None
        if property_id:
            result = supabase.table("property_assets").insert({
                "property_id": property_id,
                "subfolder": subfolder,
                "file_name": file_name,
                "drive_link": drive_link,
                "uploaded_by": uploaded_by,
            }).execute()
            asset = result.data[0] if result.data else {}

        return {
            "ok": True,
            "file_id": drive_file_id,
            "drive_link": drive_link,
            "asset_id": asset.get("id") if asset else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload record failed: {e}")


@app.post("/upload-buyer-file")
async def upload_buyer_file(
    drive_file_id: str = Form(""),
    drive_link: str = Form(""),
    file_name: str = Form(""),
    subfolder: str = Form(""),
    buyer_id: str = Form(""),
    uploaded_by: str = Form(""),
):
    """Record a Drive file in buyer_assets (browser uploads directly to Drive)."""
    try:
        asset = None
        if buyer_id:
            result = supabase.table("buyer_assets").insert({
                "buyer_id": buyer_id,
                "subfolder": subfolder,
                "file_name": file_name,
                "drive_link": drive_link,
                "uploaded_by": uploaded_by,
            }).execute()
            asset = result.data[0] if result.data else {}

        return {
            "ok": True,
            "file_id": drive_file_id,
            "drive_link": drive_link,
            "asset_id": asset.get("id") if asset else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload record failed: {e}")


class DeleteFileRequest(BaseModel):
    file_id: str = ""
    asset_id: str = ""
    asset_type: str = "property"  # "property" or "buyer"


@app.delete("/delete-file")
async def delete_file(payload: DeleteFileRequest):
    """Remove an asset record from Supabase (browser handles Drive file deletion)."""
    errors = []
    if payload.asset_id:
        try:
            table = "property_assets" if payload.asset_type != "buyer" else "buyer_assets"
            supabase.table(table).delete().eq("id", payload.asset_id).execute()
        except Exception as e:
            errors.append(f"Supabase delete failed: {e}")
    if errors:
        raise HTTPException(status_code=500, detail="; ".join(errors))
    return {"ok": True}


class UpdatePropertyRequest(BaseModel):
    fields: dict

@app.patch("/properties/{property_id}")
async def update_property(property_id: str, payload: UpdatePropertyRequest):
    """Update specific fields on a property record."""
    allowed = {"seller_name", "address", "status", "market", "agent_name", "archived"}
    safe_fields = {k: v for k, v in payload.fields.items() if k in allowed}
    if not safe_fields:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    try:
        result = supabase.table("properties").update(safe_fields).eq("id", property_id).execute()
        return result.data[0] if result.data else {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Update failed: {e}")

@app.delete("/properties/{property_id}")
async def delete_property(property_id: str, agent_name: str = None):
    """Delete a property and all related records (notes, assets, offers)."""
    try:
        row = supabase.table("properties").select("address,agent_name").eq("id", property_id).execute().data
        address_val = row[0]["address"] if row else None
        owner_agent = row[0]["agent_name"] if row else None
        # Delete related records first (FK constraints)
        supabase.table("property_notes").delete().eq("property_id", property_id).execute()
        supabase.table("property_assets").delete().eq("property_id", property_id).execute()
        supabase.table("property_offers").delete().eq("property_id", property_id).execute()
        # Delete the property itself
        supabase.table("properties").delete().eq("id", property_id).execute()
        log_audit("property.delete", agent_name=agent_name or owner_agent,
                  entity_type="property", entity_id=property_id,
                  entity_name=address_val, detail={"deleted_by": agent_name})
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")

# ── Buyer CRUD ────────────────────────────────────────────────────────────────

class CreateBuyerRequest(BaseModel):
    buyer_name: str
    agent_name: str
    agent_email: str = ""
    email: str = ""
    phone: str = ""
    status: str = "active"

# Canonical agent names — must match exactly what FORWARD OS stores in the buyers table.
# Add new agents here when they join the team.
KNOWN_AGENTS: set[str] = {
    "Marc Cashin",
    "Ashling McGowan",
    "Niki Lang",
    "Cesar Rivera",
    "Charlotte Lee",
    "Operations",
    "Concierge",
}

@app.post("/create-buyer")
async def create_buyer(payload: CreateBuyerRequest):
    """Create a new buyer record via service role key — bypasses RLS."""
    # Reject empty, whitespace-only, or unrecognised agent names so no buyer
    # ever lands in the "Unknown" bucket on the frontend.
    agent = AGENT_NAME_MAP.get((payload.agent_name or "").strip(), (payload.agent_name or "").strip()).strip()
    if not agent:
        raise HTTPException(
            status_code=400,
            detail="agent_name is required. The creating agent's session may not be fully loaded — please refresh and try again."
        )
    if agent not in KNOWN_AGENTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unrecognised agent '{agent}'. Ensure you are logged in with a valid FORWARD agent account."
        )
    buyer_name = (payload.buyer_name or "").strip()
    if not buyer_name:
        raise HTTPException(status_code=400, detail="buyer_name is required.")

    # Duplicate guard: check name + agent, email + agent, or phone + agent
    try:
        _dup = supabase.table("buyers").select("id,buyer_name").eq("agent_name", agent).eq("buyer_name", buyer_name).execute().data
        if not _dup and payload.email and payload.email.strip():
            _dup = supabase.table("buyers").select("id,buyer_name").eq("agent_name", agent).eq("email", payload.email.strip()).execute().data
        if not _dup and payload.phone and payload.phone.strip():
            _dup = supabase.table("buyers").select("id,buyer_name").eq("agent_name", agent).eq("phone", payload.phone.strip()).execute().data
        if _dup:
            raise HTTPException(status_code=409, detail=f"A buyer named '{_dup[0]['buyer_name']}' already exists in your list.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Duplicate check query failed — rejecting insert to prevent accidental duplicate: %s", e)
        raise HTTPException(status_code=503, detail="Duplicate check temporarily unavailable — please retry in a moment.")

    try:
        result = supabase.table("buyers").insert({
            "buyer_name":  buyer_name,
            "agent_name":  agent,
            "agent_email": payload.agent_email,
            "email":       payload.email,
            "phone":       payload.phone,
            "status":      payload.status,
        }).execute()
        if not result.data:
            raise HTTPException(status_code=500, detail="Insert returned no data")
        log_audit("buyer.create", agent_name=agent,
                  entity_type="buyer", entity_id=result.data[0].get("id"),
                  entity_name=buyer_name, detail={"source": "manual", "email": payload.email})
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database insert failed: {e}")

@app.get("/buyers")
async def get_buyers(agent_name: str = None):
    """Return buyers. Admins pass no agent_name and get all; agents pass their name."""
    try:
        if agent_name:
            # Primary agent OR co-agent stored in subfolder_drive_ids._co_agents
            primary = supabase.table("buyers").select("*").eq("agent_name", agent_name).order("created_at", desc=True).execute().data or []
            all_buyers = supabase.table("buyers").select("*").order("created_at", desc=True).execute().data or []
            co_agent_buyers = [b for b in all_buyers if agent_name in ((b.get("subfolder_drive_ids") or {}).get("_co_agents", []))]
            seen = {b["id"] for b in primary}
            combined = primary + [b for b in co_agent_buyers if b["id"] not in seen]
            return combined
        else:
            result = supabase.table("buyers").select("*").order("created_at", desc=True).execute()
            return result.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fetch buyers failed: {e}")

class UpdateBuyerRequest(BaseModel):
    fields: dict

@app.patch("/buyers/{buyer_id}")
async def update_buyer(buyer_id: str, payload: UpdateBuyerRequest):
    """Update specific fields on a buyer record."""
    allowed = {
        "buyer_name", "first_name", "last_name", "email", "phone", "status", "agent_name",
        "market_area", "target_areas", "areas_note",
        "budget_min", "budget_max", "down_payment", "loan_type",
        "pre_approved", "pre_approval_amount", "pre_approval_lender", "approval_status",
        "bedrooms_min", "bedrooms_note", "bathrooms", "home_type", "home_type_note",
        "outdoor_space", "outdoor_note", "parking", "parking_note",
        "hoa_acceptable", "school_district",
        "must_haves", "deal_breakers",
        "timeline", "move_in_target", "urgency", "urgency_note",
        "current_status", "current_note", "motivation", "agent_notes",
        "drive_folder_id", "subfolder_drive_ids",
        "archived",
    }
    safe_fields = {k: v for k, v in payload.fields.items() if k in allowed}
    if not safe_fields:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    try:
        result = supabase.table("buyers").update(safe_fields).eq("id", buyer_id).execute()
        if _is_archive:
            row = result.data[0] if result.data else {}
            log_audit("buyer.archive", agent_name=row.get("agent_name"),
                      entity_type="buyer", entity_id=buyer_id,
                      entity_name=row.get("buyer_name"), detail={})
        return result.data[0] if result.data else {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Update failed: {e}")

@app.delete("/buyers/{buyer_id}")
async def delete_buyer(buyer_id: str, agent_name: str = None):
    """Delete a buyer and all related records (assets)."""
    try:
        row = supabase.table("buyers").select("buyer_name,agent_name").eq("id", buyer_id).execute().data
        buyer_name_val = row[0]["buyer_name"] if row else None
        owner_agent    = row[0]["agent_name"] if row else None
        supabase.table("buyer_assets").delete().eq("buyer_id", buyer_id).execute()
        supabase.table("buyers").delete().eq("id", buyer_id).execute()
        log_audit("buyer.delete", agent_name=agent_name or owner_agent,
                  entity_type="buyer", entity_id=buyer_id,
                  entity_name=buyer_name_val, detail={"deleted_by": agent_name})
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")


# ── Meeting Prep Web Research ──────────────────────────────────────────────
class MeetingPrepResearchRequest(BaseModel):
    name: str
    company: str = ""
    title: str = ""
    email: str = ""
    agent_context: str = ""

@app.post("/api/meeting-prep-research")
async def meeting_prep_research(payload: MeetingPrepResearchRequest):
    """Research a person online using Claude web search to inform Meeting Prep DISC brief."""
    if not ANTHROPIC_API_KEY:
        return {"found": False, "error": "API key not configured"}

    system = (
        "You are a research assistant helping a real estate agent prepare for a client meeting. "
        "Search the web to find professional information about the person named. "
        "Focus on: LinkedIn bio, company website, news mentions, published writing, speaking engagements, or social media presence. "
        "Return ONLY valid JSON in this exact format:\n"
        "{\n"
        '  "found": true,\n'
        '  "background": "2-3 sentence professional summary",\n'
        '  "title": "current role/title or empty string",\n'
        '  "company": "current company or empty string",\n'
        '  "personalitySignals": "observed communication style, tone, writing patterns, interests",\n'
        '  "discHints": "specific behavioral signals suggesting D/I/S/C type — be concrete",\n'
        '  "sources": ["brief label of each source found e.g. LinkedIn, company bio, news article"]\n'
        "}\n"
        'If you cannot find meaningful information, return: {"found": false, "sources": []}'
    )

    user_msg = (
        "Research this person for a real estate agent meeting brief. "
        "Find professional background, personality signals, and communication style.\n\n"
        f"Name: {payload.name}"
    )
    if payload.title:         user_msg += f"\nTitle: {payload.title}"
    if payload.company:       user_msg += f"\nCompany: {payload.company}"
    if payload.email:         user_msg += f"\nEmail: {payload.email}"
    if payload.agent_context: user_msg += f"\nAgent context: {payload.agent_context}"

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "web-search-2025-03-05",
        "content-type": "application/json"
    }
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        "system": system,
        "messages": [{"role": "user", "content": user_msg}]
    }

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers, json=body
            )
            resp.raise_for_status()
            data = resp.json()

        result_text = ""
        for block in data.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                result_text += block["text"]

        json_match = re.search(r'\{[\s\S]*\}', result_text)
        if json_match:
            return json.loads(json_match.group())
        return {"found": False, "background": result_text[:400] if result_text else "", "sources": []}
    except Exception as e:
        logging.warning(f"Meeting prep research failed: {e}")
        return {"found": False, "error": str(e), "sources": []}


# ── Deal Partners ─────────────────────────────────────────────────────────────

def _get_co_agents(record):
    return (record.get("subfolder_drive_ids") or {}).get("_co_agents", [])

def _set_co_agents(sdi, co_agents):
    d = dict(sdi or {})
    d["_co_agents"] = co_agents
    return d

class PartnerRequest(BaseModel):
    agent_name: str

@app.post("/properties/{property_id}/add-partner")
async def add_property_partner(property_id: str, payload: PartnerRequest):
    try:
        rows = supabase.table("properties").select("subfolder_drive_ids").eq("id", property_id).execute()
        if not rows.data:
            raise HTTPException(status_code=404, detail="Property not found")
        record = rows.data[0]
        co_agents = _get_co_agents(record)
        if payload.agent_name not in co_agents:
            co_agents.append(payload.agent_name)
        new_sdi = _set_co_agents(record.get("subfolder_drive_ids"), co_agents)
        supabase.table("properties").update({"subfolder_drive_ids": new_sdi}).eq("id", property_id).execute()
        return {"co_agents": co_agents}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/properties/{property_id}/remove-partner")
async def remove_property_partner(property_id: str, payload: PartnerRequest):
    try:
        rows = supabase.table("properties").select("subfolder_drive_ids").eq("id", property_id).execute()
        if not rows.data:
            raise HTTPException(status_code=404, detail="Property not found")
        record = rows.data[0]
        co_agents = [a for a in _get_co_agents(record) if a != payload.agent_name]
        new_sdi = _set_co_agents(record.get("subfolder_drive_ids"), co_agents)
        supabase.table("properties").update({"subfolder_drive_ids": new_sdi}).eq("id", property_id).execute()
        return {"co_agents": co_agents}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/buyers/{buyer_id}/add-partner")
async def add_buyer_partner(buyer_id: str, payload: PartnerRequest):
    try:
        rows = supabase.table("buyers").select("subfolder_drive_ids").eq("id", buyer_id).execute()
        if not rows.data:
            raise HTTPException(status_code=404, detail="Buyer not found")
        record = rows.data[0]
        co_agents = _get_co_agents(record)
        if payload.agent_name not in co_agents:
            co_agents.append(payload.agent_name)
        new_sdi = _set_co_agents(record.get("subfolder_drive_ids"), co_agents)
        supabase.table("buyers").update({"subfolder_drive_ids": new_sdi}).eq("id", buyer_id).execute()
        return {"co_agents": co_agents}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/buyers/{buyer_id}/remove-partner")
async def remove_buyer_partner(buyer_id: str, payload: PartnerRequest):
    try:
        rows = supabase.table("buyers").select("subfolder_drive_ids").eq("id", buyer_id).execute()
        if not rows.data:
            raise HTTPException(status_code=404, detail="Buyer not found")
        record = rows.data[0]
        co_agents = [a for a in _get_co_agents(record) if a != payload.agent_name]
        new_sdi = _set_co_agents(record.get("subfolder_drive_ids"), co_agents)
        supabase.table("buyers").update({"subfolder_drive_ids": new_sdi}).eq("id", buyer_id).execute()
        return {"co_agents": co_agents}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Knowledge Docs (Training Library sync) ────────────────────────────────────

class KnowledgeDocEntry(BaseModel):
    file_name: str
    content: str
    category: str = ""

class KnowledgeDocsSyncRequest(BaseModel):
    entries: list[KnowledgeDocEntry]

@app.get("/admin/audit-log")
async def get_audit_log(agent_name: str = None, action: str = None, limit: int = 200, offset: int = 0):
    """Audit log — Marc / Operations only (enforced client-side)."""
    try:
        q = supabase.table("audit_log").select("*").order("created_at", desc=True).limit(limit).offset(offset)
        if agent_name:
            q = q.eq("agent_name", agent_name)
        if action:
            q = q.eq("action", action)
        return q.execute().data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Audit log fetch failed: {e}")

@app.post("/knowledge-docs/sync")
async def sync_knowledge_docs(payload: KnowledgeDocsSyncRequest):
    """
    Upsert all Training Library entries into knowledge_docs.
    Uses file_name as the unique key — uploading the same title again
    replaces the old version automatically.
    """
    try:
        for entry in payload.entries:
            # Delete any existing row with this file_name (handles versioning)
            supabase.table("knowledge_docs").delete().eq("file_name", entry.file_name).execute()
            # Insert fresh
            supabase.table("knowledge_docs").insert({
                "file_name": entry.file_name,
                "content":   entry.content,
                "category":  entry.category,
            }).execute()
        return {"ok": True, "synced": len(payload.entries)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/knowledge-docs")
async def delete_knowledge_doc(file_name: str):
    """Remove a single document from knowledge_docs by file_name (title)."""
    try:
        supabase.table("knowledge_docs").delete().eq("file_name", file_name).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



# ---------------------------------------------------------------------------
# FUB Pipeline cache sync
# ---------------------------------------------------------------------------

BUYER_STAGES = {"BA Signed", "Actively Showing", "Pending"}
SELLER_STAGES = {"Listing Agreement Signed", "Coming Soon", "Listed", "Back on the Market", "Pending"}


async def _sync_fub_pipeline() -> dict:
    """Fetch all active buyer/seller deals from FUB and write to pipeline_cache."""
    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    offset = 0
    limit = 100

    while True:
        data = await fub_get("/deals", {"limit": limit, "offset": offset})
        batch = data.get("deals", [])

        for deal in batch:
            if not isinstance(deal, dict):
                continue

            stage    = _safe_str(deal.get("stageName") or "")
            pipeline = _safe_str(deal.get("pipelineName") or "").lower()

            if "buyer" in pipeline:
                if stage not in BUYER_STAGES:
                    continue
                ptype = "buyer"
            elif "seller" in pipeline:
                if stage not in SELLER_STAGES:
                    continue
                ptype = "seller"
            else:
                continue

            # Agent name
            users     = deal.get("users") or []
            agent_fub = ""
            if isinstance(users, list) and users:
                for u in users:
                    if isinstance(u, dict):
                        role = u.get("role", "").lower()
                        if "agent" in role or role == "":
                            agent_fub = u.get("name", "")
                            break
                if not agent_fub and isinstance(users[0], dict):
                    agent_fub = users[0].get("name", "")
            agent = AGENT_NAME_MAP.get(agent_fub, agent_fub)

            # Client name
            people_list = deal.get("people") or []
            if people_list and isinstance(people_list[0], dict):
                client_name = _safe_str(people_list[0].get("name") or deal.get("name") or "")
            else:
                client_name = _safe_str(deal.get("name") or "")

            # Last activity / days since contact
            last_activity_str = _safe_str(deal.get("lastActivityDate") or deal.get("updated") or "")
            last_activity = None
            days_since    = None
            if last_activity_str:
                try:
                    la = datetime.fromisoformat(last_activity_str.replace("Z", "+00:00"))
                    if la.tzinfo is None:
                        la = la.replace(tzinfo=timezone.utc)
                    last_activity = la.isoformat()
                    days_since    = (now - la).days
                except Exception:
                    pass

            rows.append({
                "id":                str(uuid4()),
                "type":              ptype,
                "fub_id":            str(deal.get("id") or ""),
                "client_name":       client_name,
                "agent":             agent,
                "lead_stage":        stage,
                "last_activity":     last_activity,
                "days_since_contact": days_since,
                "property_address":  _safe_str(deal.get("address") or ""),
                "synced_at":         now.isoformat(),
            })

        if len(batch) < limit:
            break
        offset += limit

    # Atomically replace pipeline_cache
    supabase.table("pipeline_cache").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
    if rows:
        supabase.table("pipeline_cache").insert(rows).execute()

    logger.info("[pipeline-sync] wrote %d rows to pipeline_cache", len(rows))
    return {"synced": len(rows)}


async def job_sync_fub_pipeline():
    """APScheduler job: daily 6am ET FUB pipeline cache sync."""
    logger.info("[pipeline-sync] Starting scheduled sync...")
    try:
        result = await _sync_fub_pipeline()
        logger.info("[pipeline-sync] Done: %s", result)
    except Exception as e:
        logger.error("[pipeline-sync] Failed: %s", e)


@app.on_event("startup")
async def register_pipeline_sync_job():
    """Register daily FUB pipeline cache sync at 6am ET."""
    scheduler.add_job(
        job_sync_fub_pipeline,
        CronTrigger(hour=6, minute=0, timezone="America/New_York"),
        id="fub_pipeline_sync",
        replace_existing=True,
    )
    logger.info("[pipeline-sync] Scheduled daily 6am ET sync registered")


# ---------------------------------------------------------------------------
# Pipeline sync trigger
# ---------------------------------------------------------------------------

@app.post("/pipeline/sync")
async def trigger_pipeline_sync(user=Depends(get_current_user)):
    """Manual FUB pipeline cache sync — callable from the CC app."""
    try:
        result = await _sync_fub_pipeline()
        return {"status": "synced", **result, "ts": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        logger.error("Manual pipeline sync failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

# ---------------------------------------------------------------------------
# Nightly CC audit job (APScheduler + Resend email)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Forward OS — Nightly Audit
# ---------------------------------------------------------------------------

async def _run_os_audit() -> dict:
    """
    Run all Forward OS health checks.
    Returns {"checks": [...], "fails": [...], "warns": [...], "passes": [...], "ran_at": str}
    Each check: {"name": str, "status": "PASS"|"FAIL"|"WARN", "detail": str}
    """
    checks = []
    now_utc = datetime.now(timezone.utc)
    # 1. Railway backend self-check
    checks.append({
        "name": "Railway backend",
        "status": "PASS",
        "detail": f"Executing at {now_utc.strftime('%Y-%m-%d %H:%M UTC')}",
    })
    # 2. FUB API — key still live?
    try:
        result = await fub_get("/users", {"limit": 1})
        if result and not result.get("error"):
            checks.append({"name": "FUB API", "status": "PASS",
                            "detail": "Reachable and authenticated"})
        else:
            checks.append({"name": "FUB API", "status": "FAIL",
                            "detail": f"Unexpected response: {str(result)[:120]}"})
    except Exception as e:
        checks.append({"name": "FUB API", "status": "FAIL", "detail": str(e)})
    # 3. pipeline_cache — freshness (did the 6:00am FUB sync run?)
    try:
        r = supabase.table("pipeline_cache").select("synced_at").order("synced_at", desc=True).limit(1).execute()
        if r.data:
            ts = datetime.fromisoformat(r.data[0]["synced_at"].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_h = (now_utc - ts).total_seconds() / 3600
            if age_h < 25:
                checks.append({"name": "pipeline_cache freshness", "status": "PASS",
                                "detail": f"Last sync {ts.strftime('%Y-%m-%d %H:%M UTC')} ({age_h:.1f}h ago)"})
            else:
                checks.append({"name": "pipeline_cache freshness", "status": "FAIL",
                                "detail": f"Stale — last sync {ts.strftime('%Y-%m-%d %H:%M UTC')} ({age_h:.1f}h ago)"})
        else:
            checks.append({"name": "pipeline_cache freshness", "status": "FAIL",
                            "detail": "Table empty — FUB pipeline sync has never run"})
    except Exception as e:
        checks.append({"name": "pipeline_cache freshness", "status": "FAIL", "detail": str(e)})
    # 4. pipeline_cache — buyer deal count
    try:
        r = supabase.table("pipeline_cache").select("id", count="exact").eq("type", "buyer").execute()
        count = r.count if r.count is not None else len(r.data)
        checks.append({"name": "pipeline_cache buyers", "status": "PASS" if count > 0 else "WARN",
                        "detail": f"{count} active buyer deal(s) cached"})
    except Exception as e:
        checks.append({"name": "pipeline_cache buyers", "status": "FAIL", "detail": str(e)})
    # 5. pipeline_cache — seller deal count
    try:
        r = supabase.table("pipeline_cache").select("id", count="exact").eq("type", "seller").execute()
        count = r.count if r.count is not None else len(r.data)
        checks.append({"name": "pipeline_cache sellers", "status": "PASS" if count > 0 else "WARN",
                        "detail": f"{count} active seller deal(s) cached"})
    except Exception as e:
        checks.append({"name": "pipeline_cache sellers", "status": "FAIL", "detail": str(e)})
    # 6. agent_task_cache — freshness (did the 6:02am task sync run?)
    try:
        r = supabase.table("agent_task_cache").select("synced_at").order("synced_at", desc=True).limit(1).execute()
        if r.data:
            ts = datetime.fromisoformat(r.data[0]["synced_at"].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_h = (now_utc - ts).total_seconds() / 3600
            if age_h < 25:
                checks.append({"name": "agent_task_cache freshness", "status": "PASS",
                                "detail": f"Last sync {ts.strftime('%Y-%m-%d %H:%M UTC')} ({age_h:.1f}h ago)"})
            else:
                checks.append({"name": "agent_task_cache freshness", "status": "FAIL",
                                "detail": f"Stale — last sync {ts.strftime('%Y-%m-%d %H:%M UTC')} ({age_h:.1f}h ago)"})
        else:
            checks.append({"name": "agent_task_cache freshness", "status": "FAIL",
                            "detail": "Table empty — agent task sync has never run"})
    except Exception as e:
        checks.append({"name": "agent_task_cache freshness", "status": "FAIL", "detail": str(e)})
    # 7. agent_task_cache — row count
    try:
        r = supabase.table("agent_task_cache").select("id", count="exact").execute()
        count = r.count if r.count is not None else len(r.data)
        checks.append({"name": "agent_task_cache rows", "status": "PASS" if count > 0 else "WARN",
                        "detail": f"{count} agent task row(s) cached"})
    except Exception as e:
        checks.append({"name": "agent_task_cache rows", "status": "FAIL", "detail": str(e)})
    # 8. buyers table — accessible + count
    try:
        r = supabase.table("buyers").select("id", count="exact").execute()
        total = r.count if r.count is not None else len(r.data)
        checks.append({"name": "buyers table", "status": "PASS" if total > 0 else "WARN",
                        "detail": f"Accessible — {total} total buyer record(s)"})
    except Exception as e:
        checks.append({"name": "buyers table", "status": "FAIL", "detail": str(e)})
    # 9. properties table — accessible + count
    try:
        r = supabase.table("properties").select("id", count="exact").execute()
        total = r.count if r.count is not None else len(r.data)
        checks.append({"name": "properties table", "status": "PASS" if total > 0 else "WARN",
                        "detail": f"Accessible — {total} total property record(s)"})
    except Exception as e:
        checks.append({"name": "properties table", "status": "FAIL", "detail": str(e)})
    # 10. webhook_errors — pending unresolved FUB events (with agent breakdown)
    try:
        r = supabase.table("webhook_errors").select("id,fub_agent", count="exact").eq("status", "pending").execute()
        count = r.count if r.count is not None else len(r.data)
        if count == 0:
            checks.append({"name": "webhook_errors pending", "status": "PASS",
                            "detail": "No pending unmatched webhook events"})
        else:
            from collections import Counter
            agent_counts = Counter(row.get("fub_agent", "unknown") for row in (r.data or []))
            breakdown = ", ".join(f"{v} for {k}" for k, v in agent_counts.most_common())
            checks.append({"name": "webhook_errors pending", "status": "WARN",
                            "detail": f"{count} pending unmatched event(s): {breakdown}"})
    except Exception as e:
        checks.append({"name": "webhook_errors pending", "status": "FAIL", "detail": str(e)})
    # 11. audit_log — schema accessible
    try:
        supabase.table("audit_log").select("id").limit(1).execute()
        checks.append({"name": "audit_log table", "status": "PASS", "detail": "Accessible"})
    except Exception as e:
        checks.append({"name": "audit_log table", "status": "FAIL", "detail": str(e)})
    # 12. knowledge_docs — Forward Voice training content
    try:
        r = supabase.table("knowledge_docs").select("id", count="exact").execute()
        count = r.count if r.count is not None else len(r.data)
        if count > 0:
            checks.append({"name": "knowledge_docs (Voice)", "status": "PASS",
                            "detail": f"{count} training doc(s) loaded"})
        else:
            checks.append({"name": "knowledge_docs (Voice)", "status": "WARN",
                            "detail": "Table empty — Voice Q&A will return no training context"})
    except Exception as e:
        checks.append({"name": "knowledge_docs (Voice)", "status": "FAIL", "detail": str(e)})
    # 13. Anthropic API — powers BMR, offer analysis, meeting prep, Voice Q&A
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
            )
        if resp.status_code == 200:
            checks.append({"name": "Anthropic API", "status": "PASS",
                            "detail": "Key valid and reachable"})
        elif resp.status_code == 401:
            checks.append({"name": "Anthropic API", "status": "FAIL",
                            "detail": "401 Unauthorized — ANTHROPIC_API_KEY invalid or expired"})
        else:
            checks.append({"name": "Anthropic API", "status": "FAIL",
                            "detail": f"Unexpected HTTP {resp.status_code}"})
    except Exception as e:
        checks.append({"name": "Anthropic API", "status": "FAIL", "detail": str(e)})
    # 14. Netlify API — powers Buyer Report deploys
    if not NETLIFY_ACCESS_TOKEN:
        checks.append({"name": "Netlify API", "status": "WARN",
                        "detail": "NETLIFY_ACCESS_TOKEN not set — Buyer Report deploys will fail"})
    else:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.netlify.com/api/v1/sites",
                    headers={"Authorization": f"Bearer {NETLIFY_ACCESS_TOKEN}"},
                    params={"per_page": 1},
                )
            if resp.status_code == 200:
                checks.append({"name": "Netlify API", "status": "PASS",
                                "detail": "Key valid and reachable"})
            elif resp.status_code == 401:
                checks.append({"name": "Netlify API", "status": "FAIL",
                                "detail": "401 Unauthorized — NETLIFY_ACCESS_TOKEN invalid or expired"})
            else:
                checks.append({"name": "Netlify API", "status": "FAIL",
                                "detail": f"Unexpected HTTP {resp.status_code}"})
        except Exception as e:
            checks.append({"name": "Netlify API", "status": "FAIL", "detail": str(e)})
    # 15. Google Drive service account
    if not GOOGLE_SA_JSON:
        checks.append({"name": "Google Drive SA", "status": "WARN",
                        "detail": "GOOGLE_SERVICE_ACCOUNT_JSON not set — Drive automation checks disabled"})
    else:
        try:
            import asyncio as _asyncio
            await _asyncio.to_thread(get_drive_service)
            checks.append({"name": "Google Drive SA", "status": "PASS",
                            "detail": "Service account credentials valid"})
        except Exception as e:
            checks.append({"name": "Google Drive SA", "status": "FAIL",
                            "detail": f"Drive SA init failed: {str(e)[:150]}"})
    # 16. automation_health — freshness (did the 6:05am Drive check run?)
    try:
        r = supabase.table("automation_health").select("automation_name,status,last_run").order("last_run", desc=True).execute()
        last_run_str = (r.data[0].get("last_run") if r.data else None)
        if last_run_str:
            ts = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_h = (now_utc - ts).total_seconds() / 3600
            if age_h < 25:
                checks.append({"name": "automation_health freshness", "status": "PASS",
                                "detail": f"Last Drive check {ts.strftime('%Y-%m-%d %H:%M UTC')} ({age_h:.1f}h ago)"})
            else:
                checks.append({"name": "automation_health freshness", "status": "WARN",
                                "detail": f"Stale — last Drive check {ts.strftime('%Y-%m-%d %H:%M UTC')} ({age_h:.1f}h ago)"})
        else:
            checks.append({"name": "automation_health freshness", "status": "WARN",
                            "detail": "No Drive automation runs recorded yet"})
    except Exception as e:
        checks.append({"name": "automation_health freshness", "status": "FAIL", "detail": str(e)})
    # 17. automation_health — any automations in error state?
    try:
        r = supabase.table("automation_health").select("automation_name,status").execute()
        errored = [row for row in (r.data or []) if row.get("status") == "error"]
        if not errored:
            checks.append({"name": "automation_health errors", "status": "PASS",
                            "detail": f"All {len(r.data or [])} automation(s) healthy"})
        else:
            names = ", ".join(row.get("automation_name", "?") for row in errored)
            checks.append({"name": "automation_health errors", "status": "WARN",
                            "detail": f"{len(errored)} automation(s) in error state: {names}"})
    except Exception as e:
        checks.append({"name": "automation_health errors", "status": "FAIL", "detail": str(e)})
    fails  = [c for c in checks if c["status"] == "FAIL"]
    warns  = [c for c in checks if c["status"] == "WARN"]
    passes = [c for c in checks if c["status"] == "PASS"]
    return {
        "checks": checks,
        "fails":  fails,
        "warns":  warns,
        "passes": passes,
        "ran_at": now_utc.isoformat(),
    }


def _build_os_audit_email_html(audit: dict) -> str:
    ran_at = audit["ran_at"]
    checks = audit["checks"]
    fails  = audit["fails"]
    warns  = audit["warns"]
    overall = "✅ ALL SYSTEMS GO" if not fails else f"🚨 {len(fails)} FAILURE(S) DETECTED"
    color   = "#16a34a" if not fails else "#dc2626"
    pass_count = len(audit["passes"])
    total      = len(checks)
    rows_html = ""
    for c in checks:
        icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️"}.get(c["status"], "?")
        bg   = {"PASS": "#f0fdf4", "FAIL": "#fef2f2", "WARN": "#fffbeb"}.get(c["status"], "#fff")
        rows_html += (
            f'<tr style="background:{bg}">'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{icon} {c["name"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-weight:600">{c["status"]}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:#374151">{c["detail"]}</td>'
            f'</tr>'
        )
    fix_prompts_html = ""
    if fails:
        fix_prompts_html = '<h2 style="color:#dc2626;margin-top:32px">Claude Fix Prompts</h2>'
        for c in fails:
            prompt = _get_os_fix_prompt(c["name"], c["detail"])
            fix_prompts_html += (
                f'<div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;'
                f'padding:16px;margin-bottom:16px">'
                f'<strong style="color:#dc2626">❌ {c["name"]}</strong><br>'
                f'<pre style="background:#fff;border:1px solid #e5e7eb;padding:12px;border-radius:4px;'
                f'white-space:pre-wrap;word-break:break-word;margin-top:8px;font-size:13px">{prompt}</pre>'
                f'</div>'
            )
    warn_html = ""
    if warns:
        warn_html = '<h2 style="color:#d97706;margin-top:32px">Warnings</h2>'
        for w in warns:
            warn_html += (
                f'<div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;'
                f'padding:12px;margin-bottom:12px">'
                f'<strong>⚠️ {w["name"]}</strong>: {w["detail"]}</div>'
            )
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>OS Nightly Audit</title></head>
<body style="font-family:system-ui,sans-serif;max-width:720px;margin:0 auto;padding:24px;color:#111">
  <h1 style="color:{color};margin-bottom:4px">Forward OS — Nightly Audit</h1>
  <p style="color:#6b7280;margin-top:0">{ran_at}</p>
  <div style="background:{color};color:#fff;padding:16px 20px;border-radius:8px;font-size:18px;font-weight:bold;margin-bottom:24px">
    {overall} &nbsp;·&nbsp; {pass_count}/{total} checks passed
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <thead>
      <tr style="background:#f3f4f6">
        <th style="padding:8px 12px;text-align:left">Check</th>
        <th style="padding:8px 12px;text-align:left">Status</th>
        <th style="padding:8px 12px;text-align:left">Detail</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
  {fix_prompts_html}
  {warn_html}
  <p style="color:#9ca3af;font-size:12px;margin-top:32px">
    Forward OS nightly audit · Railway APScheduler · 11:01 PM ET daily<br>
    Manual trigger: POST /audit/run?system=os
  </p>
</body>
</html>"""


def _get_os_fix_prompt(check_name: str, detail: str) -> str:
    prompts = {
        "Railway backend": (
            f"The Forward OS Railway backend failed its self-check. Detail: {detail}\n\n"
            "Check Railway dashboard for the forward-os-backend service. Review deployment logs."
        ),
        "FUB API": (
            f"The Forward OS audit cannot reach Follow Up Boss. Detail: {detail}\n\n"
            "1. Verify FUB_API_KEY is set in Railway env vars for forward-os-backend.\n"
            "2. Check the key is active: app.followupboss.com → Admin → API.\n"
            "3. Test: curl -u '<FUB_API_KEY>:' https://api.followupboss.com/v1/users?limit=1"
        ),
        "pipeline_cache freshness": (
            f"Forward OS pipeline_cache is stale. Detail: {detail}\n\n"
            "job_sync_fub_pipeline runs at 6:00am ET via APScheduler in main.py.\n"
            "1. Check Railway logs for forward-os-backend around 6am ET today.\n"
            "2. Manually trigger: POST /pipeline/sync (requires auth token).\n"
            "3. If errored, diagnose _sync_fub_pipeline() in main.py and submit a PR."
        ),
        "pipeline_cache buyers": (
            f"Forward OS pipeline_cache has no buyer rows. Detail: {detail}\n\n"
            "Check BUYER_STAGES in main.py matches current FUB pipeline stage names.\n"
            "Trigger manual sync: POST /pipeline/sync."
        ),
        "pipeline_cache sellers": (
            f"Forward OS pipeline_cache has no seller rows. Detail: {detail}\n\n"
            "Check SELLER_STAGES in main.py matches current FUB pipeline stage names.\n"
            "Trigger manual sync: POST /pipeline/sync."
        ),
        "agent_task_cache freshness": (
            f"Forward OS agent_task_cache is stale. Detail: {detail}\n\n"
            "job_sync_agent_task_cache runs at 6:02am ET.\n"
            "1. Check Railway logs around 6:02am ET today.\n"
            "2. Manually trigger: POST /sync-agent-tasks.\n"
            "3. If errored, diagnose _sync_agent_task_cache() in main.py."
        ),
        "agent_task_cache rows": (
            f"Forward OS agent_task_cache is empty. Detail: {detail}\n\n"
            "Agents have no next tasks in the dashboard.\n"
            "Manually trigger: POST /sync-agent-tasks and verify FUB has active deal tasks."
        ),
        "buyers table": (
            f"Forward OS buyers Supabase table is inaccessible. Detail: {detail}\n\n"
            "Check RLS policies on the buyers table. Verify SUPABASE_SERVICE_ROLE_KEY\n"
            "is correctly set in Railway env vars."
        ),
        "properties table": (
            f"Forward OS properties Supabase table is inaccessible. Detail: {detail}\n\n"
            "Check RLS on properties. Verify SUPABASE_SERVICE_ROLE_KEY is valid."
        ),
        "webhook_errors pending": (
            f"Forward OS has pending unmatched FUB webhook events. Detail: {detail}\n\n"
            "Go to OS admin dashboard → Training Admin → 'Unmatched Webhook Events'.\n"
            "For each row, select the correct agent and click 'Create Record'.\n"
            "If the agent is new, add their alias to AGENT_NAME_MAP in main.py and submit a PR."
        ),
        "audit_log table": (
            f"Forward OS audit_log table is inaccessible. Detail: {detail}\n\n"
            "The table may have been dropped. Re-create it:\n"
            "CREATE TABLE audit_log (\n"
            "  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),\n"
            "  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n"
            "  agent_name TEXT, action TEXT NOT NULL,\n"
            "  entity_type TEXT, entity_id TEXT, entity_name TEXT,\n"
            "  detail JSONB NOT NULL DEFAULT '{}',\n"
            "  source TEXT NOT NULL DEFAULT 'backend'\n"
            ");\n"
            "ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;"
        ),
        "knowledge_docs (Voice)": (
            f"Forward OS knowledge_docs table is empty. Detail: {detail}\n\n"
            "Voice Q&A will return no training context.\n"
            "Go to Training Admin → Training Library → 'Sync to Knowledge Base'."
        ),
        "Anthropic API": (
            f"Forward OS Anthropic API key is invalid or unreachable. Detail: {detail}\n\n"
            "This breaks: Buyer Report analysis, offer parsing, offer analysis,\n"
            "meeting prep research, and Voice Q&A streaming.\n"
            "1. Check ANTHROPIC_API_KEY in Railway env vars.\n"
            "2. Verify the key is active at console.anthropic.com → API Keys.\n"
            "3. Replace the key in Railway and redeploy."
        ),
        "Netlify API": (
            f"Forward OS Netlify API key is invalid. Detail: {detail}\n\n"
            "Buyer Report deployments will fail — agents cannot generate shareable report links.\n"
            "1. Check NETLIFY_ACCESS_TOKEN in Railway env vars.\n"
            "2. Verify at app.netlify.com → User Settings → Personal access tokens.\n"
            "3. Regenerate if expired and update Railway env var."
        ),
        "Google Drive SA": (
            f"Google Drive service account cannot be initialized. Detail: {detail}\n\n"
            "Drive automation health checks are disabled.\n"
            "1. Check GOOGLE_SERVICE_ACCOUNT_JSON in Railway env vars.\n"
            "2. Verify the service account is active in Google Cloud Console.\n"
            "3. Ensure the SA has been shared on the relevant Drive folders."
        ),
        "automation_health freshness": (
            f"Forward OS automation_health table is stale. Detail: {detail}\n\n"
            "job_check_drive_automations runs at 6:05am ET.\n"
            "1. Check Railway logs around 6:05am ET today.\n"
            "2. Verify Google Drive SA credentials are valid.\n"
            "3. If errored, diagnose _check_drive_automations() in main.py."
        ),
        "automation_health errors": (
            f"One or more Drive automations are in error state. Detail: {detail}\n\n"
            "Check the automation_health table in Supabase for rows with status='error'.\n"
            "Investigate the named automation in Drive or Zapier."
        ),
    }
    return prompts.get(
        check_name,
        f"Forward OS audit failed check '{check_name}'. Detail: {detail}\n\n"
        "Review marccashin/forward-os-backend main.py and Railway logs."
    )


async def job_nightly_os_audit():
    """APScheduler job: nightly 11:01pm ET OS health audit, sends report via Resend."""
    import resend as resend_sdk
    logger.info("[os-nightly-audit] Starting...")
    try:
        audit = await _run_os_audit()
        fails = audit["fails"]
        warns = audit["warns"]
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        subject = (
            f"✅ OS Audit PASSED — {date_str}"
            if not fails
            else f"🚨 OS Audit FAILED ({len(fails)} issue(s)) — {date_str}"
        )
        html_body = _build_os_audit_email_html(audit)
        resend_api_key = os.environ.get("RESEND_API_KEY", "")
        if not resend_api_key:
            logger.error("[os-nightly-audit] RESEND_API_KEY not set — cannot send email")
            return
        resend_sdk.api_key = resend_api_key
        resend_sdk.Emails.send({
            "from":    "Forward OS Audit <audit@marccashin.com>",
            "to":      ["marc.cashin@corcoranmce.com"],
            "subject": subject,
            "html":    html_body,
        })
        logger.info("[os-nightly-audit] Email sent. Fails: %d, Warns: %d", len(fails), len(warns))
    except Exception as e:
        logger.error("[os-nightly-audit] Uncaught error: %s", e)


@app.on_event("startup")
async def register_nightly_os_audit_job():
    """Register Forward OS nightly audit at 11:01pm ET."""
    scheduler.add_job(
        job_nightly_os_audit,
        CronTrigger(hour=23, minute=1, timezone="America/New_York"),
        id="nightly_os_audit",
        replace_existing=True,
    )
    logger.info("[os-nightly-audit] Scheduled nightly 11:01pm ET OS audit registered")


async def _run_cc_audit() -> dict:
    """
    Run all Forward CC health checks.
    Each check: {"name": str, "status": "PASS"|"FAIL"|"WARN", "detail": str}
    """
    checks = []
    now_utc = datetime.now(timezone.utc)
    cutoff_24h = (now_utc - timedelta(hours=24)).isoformat()

    # -----------------------------------------------------------------------
    # 1. Netlify site — CC frontend must be live
    # -----------------------------------------------------------------------
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get("https://forward-command-center.netlify.app")
        if resp.status_code == 200:
            checks.append({"name": "Netlify site", "status": "PASS",
                            "detail": f"HTTP {resp.status_code} — site is up"})
        else:
            checks.append({"name": "Netlify site", "status": "FAIL",
                            "detail": f"HTTP {resp.status_code} — site may be down"})
    except Exception as e:
        checks.append({"name": "Netlify site", "status": "FAIL", "detail": str(e)})

    # -----------------------------------------------------------------------
    # 2–11. CC Supabase tables — accessibility + row count
    # -----------------------------------------------------------------------
    cc_tables = [
        "listings",
        "listing_checklist_items",
        "deal_tasks",
        "seller_post_closing_items",
        "inventory_items",
        "agreement_buyers",
        "agreement_sellers",
        "agreement_rentals",
        "virtual_staging_jobs",
        "audit_log",
    ]
    for table in cc_tables:
        try:
            r = supabase.table(table).select("id", count="exact").limit(1).execute()
            total = r.count if r.count is not None else len(r.data)
            checks.append({"name": f"{table} table", "status": "PASS",
                            "detail": f"Accessible ({total} rows)"})
        except Exception as e:
            checks.append({"name": f"{table} table", "status": "FAIL", "detail": str(e)})

    # -----------------------------------------------------------------------
    # 12. FUB API — reachable and authenticated
    # -----------------------------------------------------------------------
    try:
        result = await fub_get("/users", {"limit": 1})
        if result and not result.get("error"):
            checks.append({"name": "FUB API", "status": "PASS",
                            "detail": "Reachable and authenticated"})
        else:
            checks.append({"name": "FUB API", "status": "FAIL",
                            "detail": f"Unexpected response: {str(result)[:100]}"})
    except Exception as e:
        checks.append({"name": "FUB API", "status": "FAIL", "detail": str(e)})

    # -----------------------------------------------------------------------
    # 13. Anthropic API key — required for Agent Intel (claude-sonnet)
    # -----------------------------------------------------------------------
    if not ANTHROPIC_API_KEY:
        checks.append({"name": "Anthropic API", "status": "FAIL",
                        "detail": "ANTHROPIC_API_KEY not set — Agent Intel will fail"})
    else:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                )
            if resp.status_code == 200:
                checks.append({"name": "Anthropic API", "status": "PASS",
                                "detail": "Key valid and reachable"})
            elif resp.status_code == 401:
                checks.append({"name": "Anthropic API", "status": "FAIL",
                                "detail": "401 Unauthorized — ANTHROPIC_API_KEY is invalid or expired"})
            else:
                checks.append({"name": "Anthropic API", "status": "FAIL",
                                "detail": f"HTTP {resp.status_code}"})
        except Exception as e:
            checks.append({"name": "Anthropic API", "status": "FAIL", "detail": str(e)})

    # -----------------------------------------------------------------------
    # 14. InfiniteCreator API — WARN only (known account access issue)
    # -----------------------------------------------------------------------
    ic_key = os.environ.get("INFINITE_CREATOR_API_KEY", "") or os.environ.get("IC_API_KEY", "")
    if not ic_key:
        checks.append({"name": "InfiniteCreator API", "status": "WARN",
                        "detail": "API key not found in env — known account issue, monitor manually"})
    else:
        checks.append({"name": "InfiniteCreator API", "status": "WARN",
                        "detail": "Key set but live validation skipped — known account access issue"})

    # -----------------------------------------------------------------------
    # 15. Resend API key — required for CC audit email alerts
    # -----------------------------------------------------------------------
    resend_key = os.environ.get("RESEND_API_KEY", "")
    if resend_key:
        checks.append({"name": "Resend API key", "status": "PASS",
                        "detail": "RESEND_API_KEY is set"})
    else:
        checks.append({"name": "Resend API key", "status": "FAIL",
                        "detail": "RESEND_API_KEY not set — audit email alerts will not fire"})

    # -----------------------------------------------------------------------
    # 16. audit_log unresolved errors — last 24h
    # -----------------------------------------------------------------------
    try:
        r = (supabase.table("audit_log")
             .select("id", count="exact")
             .gte("created_at", cutoff_24h)
             .is_("resolved_at", "null")
             .execute())
        count = r.count if r.count is not None else len(r.data)
        status = "WARN" if count > 0 else "PASS"
        checks.append({"name": "audit_log unresolved errors", "status": status,
                        "detail": f"{count} unresolved error(s) logged in last 24h"})
    except Exception:
        try:
            r = (supabase.table("audit_log")
                 .select("id", count="exact")
                 .gte("created_at", cutoff_24h)
                 .execute())
            count = r.count if r.count is not None else len(r.data)
            checks.append({"name": "audit_log unresolved errors", "status": "PASS",
                            "detail": f"audit_log accessible; {count} row(s) in last 24h"})
        except Exception as e2:
            checks.append({"name": "audit_log unresolved errors", "status": "FAIL",
                            "detail": str(e2)})

    # -----------------------------------------------------------------------
    # 17. FUB webhook failures in audit_log — last 24h
    # -----------------------------------------------------------------------
    try:
        r = (supabase.table("audit_log")
             .select("id", count="exact")
             .gte("created_at", cutoff_24h)
             .eq("action", "fub.sync.failed")
             .execute())
        count = r.count if r.count is not None else len(r.data)
        status = "WARN" if count > 0 else "PASS"
        checks.append({"name": "FUB webhook failures", "status": status,
                        "detail": f"{count} fub.sync.failed event(s) in last 24h"})
    except Exception as e:
        checks.append({"name": "FUB webhook failures", "status": "WARN",
                        "detail": f"Could not query audit_log for webhook failures: {str(e)[:80]}"})

    fails  = [c for c in checks if c["status"] == "FAIL"]
    warns  = [c for c in checks if c["status"] == "WARN"]
    passes = [c for c in checks if c["status"] == "PASS"]
    return {
        "checks": checks,
        "fails":  fails,
        "warns":  warns,
        "passes": passes,
        "ran_at": now_utc.isoformat(),
    }


def _build_audit_email_html(audit: dict) -> str:
    """Build HTML email body for the Forward CC nightly audit report."""
    ran_at = audit["ran_at"]
    checks = audit["checks"]
    fails  = audit["fails"]
    warns  = audit["warns"]

    pass_count = len(audit["passes"])
    total      = len(checks)
    overall = "✅ ALL SYSTEMS GO" if not fails else f"🚨 {len(fails)} FAILURE(S) DETECTED"
    color   = "#16a34a" if not fails else "#dc2626"

    rows_html = ""
    for c in checks:
        icon  = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️"}.get(c["status"], "?")
        bg    = {"PASS": "#f0fdf4", "FAIL": "#fef2f2", "WARN": "#fffbeb"}.get(c["status"], "#fff")
        rows_html += (
            f'<tr style="background:{bg}">' +
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{icon} {c["name"]}</td>' +
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-weight:600">{c["status"]}</td>' +
            f'<td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:#374151">{c["detail"]}</td>' +
            '</tr>'
        )

    fix_prompts_html = ""
    if fails:
        fix_prompts_html = '<h2 style="color:#dc2626;margin-top:32px">Claude Fix Prompts</h2>'
        for c in fails:
            prompt = _get_fix_prompt(c["name"], c["detail"])
            fix_prompts_html += (
                f'<div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;'
                f'padding:16px;margin-bottom:16px">'
                f'<strong style="color:#dc2626">❌ {c["name"]}</strong><br>'
                f'<pre style="background:#fff;border:1px solid #e5e7eb;padding:12px;border-radius:4px;'
                f'white-space:pre-wrap;word-break:break-word;margin-top:8px;font-size:13px">{prompt}</pre>'
                '</div>'
            )

    warn_html = ""
    if warns:
        warn_html = '<h2 style="color:#d97706;margin-top:32px">Warnings</h2>'
        for w in warns:
            warn_html += (
                f'<div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;'
                f'padding:12px;margin-bottom:12px">'
                f'<strong>⚠️ {w["name"]}</strong>: {w["detail"]}</div>'
            )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>CC Nightly Audit</title></head>
<body style="font-family:system-ui,sans-serif;max-width:720px;margin:0 auto;padding:24px;color:#111">
  <h1 style="color:{color};margin-bottom:4px">Forward CC — Nightly Audit</h1>
  <p style="color:#6b7280;margin-top:0">{ran_at}</p>

  <div style="background:{color};color:#fff;padding:16px 20px;border-radius:8px;font-size:18px;font-weight:bold;margin-bottom:24px">
    {overall} &nbsp;·&nbsp; {pass_count}/{total} checks passed
  </div>

  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <thead>
      <tr style="background:#f3f4f6">
        <th style="padding:8px 12px;text-align:left">Check</th>
        <th style="padding:8px 12px;text-align:left">Status</th>
        <th style="padding:8px 12px;text-align:left">Detail</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>

  {fix_prompts_html}
  {warn_html}

  <p style="color:#9ca3af;font-size:12px;margin-top:32px">
    Sent by Forward CC nightly audit job · Railway APScheduler · 11:00 PM ET daily<br>
    Manual trigger: POST /audit/run?system=cc
  </p>
</body>
</html>"""


def _get_fix_prompt(check_name: str, detail: str) -> str:
    """Return a Claude-ready fix prompt for a failed Forward CC health check."""
    prompts = {
        "Netlify site": (
            f"The Forward CC Netlify site is down. Detail: {detail}\n\n"
            "1. Check Netlify dashboard at app.netlify.com → forward-command-center.\n"
            "2. Look for a failed deploy in the Deploys tab.\n"
            "3. Trigger a manual redeploy or roll back to the last successful deploy.\n"
            "4. Check the build logs for the error. Repo: marccashin/forward-command-center (staging → main)."
        ),
        "listings table": (
            f"The CC listings Supabase table is inaccessible. Detail: {detail}\n\n"
            "Check Supabase RLS on the listings table. Verify SUPABASE_SERVICE_ROLE_KEY "
            "is correctly set in Railway env vars for forward-os-backend."
        ),
        "listing_checklist_items table": (
            f"The CC listing_checklist_items table is inaccessible. Detail: {detail}\n\n"
            "Check Supabase RLS policies. Verify SUPABASE_SERVICE_ROLE_KEY is valid."
        ),
        "deal_tasks table": (
            f"The CC deal_tasks Supabase table is inaccessible. Detail: {detail}\n\n"
            "Check Supabase RLS on deal_tasks. Verify SUPABASE_SERVICE_ROLE_KEY is valid."
        ),
        "seller_post_closing_items table": (
            f"The CC seller_post_closing_items table is inaccessible. Detail: {detail}\n\n"
            "Check Supabase RLS policies and that the table exists."
        ),
        "inventory_items table": (
            f"The CC inventory_items table is inaccessible. Detail: {detail}\n\n"
            "Check Supabase RLS policies and that the table exists."
        ),
        "agreement_buyers table": (
            f"The CC agreement_buyers table is inaccessible. Detail: {detail}\n\n"
            "Check Supabase RLS policies. This table powers buyer agreement tracking in CC."
        ),
        "agreement_sellers table": (
            f"The CC agreement_sellers table is inaccessible. Detail: {detail}\n\n"
            "Check Supabase RLS policies. This table powers seller agreement tracking in CC."
        ),
        "agreement_rentals table": (
            f"The CC agreement_rentals table is inaccessible. Detail: {detail}\n\n"
            "Check Supabase RLS policies. This table powers rental agreement tracking in CC."
        ),
        "virtual_staging_jobs table": (
            f"The CC virtual_staging_jobs table is inaccessible. Detail: {detail}\n\n"
            "Check Supabase RLS policies. This table tracks virtual staging job status in CC."
        ),
        "audit_log table": (
            f"The CC audit_log Supabase table is inaccessible. Detail: {detail}\n\n"
            "Check Supabase RLS on audit_log. Verify SUPABASE_SERVICE_ROLE_KEY is valid."
        ),
        "FUB API": (
            f"The CC nightly audit cannot reach the Follow Up Boss API. Detail: {detail}\n\n"
            "1. Verify FUB_API_KEY is set in Railway env vars for forward-os-backend.\n"
            "2. Check the key is active: app.followupboss.com → Admin → API.\n"
            "3. Test: curl -u '<FUB_API_KEY>:' https://api.followupboss.com/v1/users?limit=1"
        ),
        "Anthropic API": (
            f"The CC Anthropic API key is missing or invalid. Detail: {detail}\n\n"
            "Agent Intel (claude-sonnet) will fail without this key.\n"
            "1. Check ANTHROPIC_API_KEY in Railway env vars for forward-os-backend.\n"
            "2. Verify the key is active at console.anthropic.com → API Keys.\n"
            "3. Replace the key in Railway and redeploy."
        ),
        "Resend API key": (
            f"RESEND_API_KEY is not set. Detail: {detail}\n\n"
            "CC audit email alerts will not fire.\n"
            "1. Get the API key from resend.com → API Keys.\n"
            "2. Add RESEND_API_KEY to Railway env vars for forward-os-backend.\n"
            "3. Redeploy."
        ),
        "audit_log unresolved errors": (
            f"The CC audit_log has unresolved errors in the last 24h. Detail: {detail}\n\n"
            "Check the audit_log table in Supabase for rows where resolved_at IS NULL "
            "and created_at > NOW() - INTERVAL '24 hours'.\n"
            "Review each error entry and resolve or escalate as appropriate."
        ),
        "FUB webhook failures": (
            f"The CC audit_log has fub.sync.failed events in the last 24h. Detail: {detail}\n\n"
            "1. Query: SELECT * FROM audit_log WHERE action = 'fub.sync.failed' "
            "AND created_at > NOW() - INTERVAL '24 hours'.\n"
            "2. Review the detail JSONB for each failure.\n"
            "3. Check Railway logs around the time of each failure for the root cause."
        ),
    }
    return prompts.get(
        check_name,
        f"The Forward CC nightly audit failed check '{check_name}'. Detail: {detail}\n\n"
        "Please investigate marccashin/forward-os-backend main.py and Railway logs."
    )


async def job_nightly_audit():
    """APScheduler job: nightly 11pm ET CC health audit, sends report via Resend."""
    import resend as resend_sdk

    logger.info("[nightly-audit] Starting...")
    try:
        audit = await _run_cc_audit()
        fails = audit["fails"]
        warns = audit["warns"]

        subject = (
            f"✅ CC Audit PASSED — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
            if not fails
            else f"🚨 CC Audit FAILED ({len(fails)} issue(s)) — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        )
        html_body = _build_audit_email_html(audit)

        resend_api_key = os.environ.get("RESEND_API_KEY", "")
        if not resend_api_key:
            logger.error("[nightly-audit] RESEND_API_KEY not set — cannot send email")
            return

        resend_sdk.api_key = resend_api_key
        params = {
            "from": "Forward CC Audit <audit@marccashin.com>",
            "to": ["marc.cashin@corcoranmce.com"],
            "subject": subject,
            "html": html_body,
        }
        resend_sdk.Emails.send(params)
        logger.info("[nightly-audit] Email sent. Fails: %d, Warns: %d", len(fails), len(warns))
    except Exception as e:
        logger.error("[nightly-audit] Uncaught error: %s", e)


@app.on_event("startup")
async def register_nightly_audit_job():
    """Register nightly CC audit at 11pm ET."""
    scheduler.add_job(
        job_nightly_audit,
        CronTrigger(hour=23, minute=0, timezone="America/New_York"),
        id="nightly_audit",
        replace_existing=True,
    )
    logger.info("[nightly-audit] Scheduled nightly 11pm ET audit registered")


# ---------------------------------------------------------------------------
# Audit manual trigger
# ---------------------------------------------------------------------------

@app.post("/audit/run")
async def trigger_audit(system: str = "os", background_tasks: BackgroundTasks = None, user=Depends(get_current_user)):
    """
    Manual trigger for nightly audits.
    ?system=os  → Forward OS audit (default)
    ?system=cc  → Forward CC audit
    Returns immediately; audit runs in a thread pool so the event loop is never blocked.
    Email arrives within ~60 seconds.
    """
    import asyncio as _asyncio

    def _run_in_thread():
        """Run the full audit job in a dedicated thread with its own event loop."""
        async def _async():
            if system == "cc":
                await job_nightly_audit()
            else:
                await job_nightly_os_audit()
        _asyncio.run(_async())

    background_tasks.add_task(_run_in_thread)
    return {
        "status": "started",
        "system": system,
        "message": f"{system.upper()} audit running — email will arrive in about 60 seconds",
    }

