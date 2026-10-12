"""The backend's read-only connection to FORWARD's Canva team, for the agents'
listing presentations.

Why this exists (Marc, Oct 12, 2026): the OS sent agents to each deck's Canva
edit link. Canva refuses anyone not signed in to the team account, and only
Marc, Niki and one other person are team members, so most agents got "not
allowed". The fix: the backend exports each agent's deck to PDF itself and the
OS hands the agent that file. Canva stays the master copy; Marc keeps editing
there.

This file is step 1: connect once, prove the connection reads all five decks,
and run a test export that reports size, pages, time and the ownership check.
Nothing here stores or serves a PDF yet.

Canva facts this relies on, read from canva.dev on Oct 12, 2026:
  - OAuth 2.0 authorization code with PKCE (S256). Authorize at
    https://www.canva.com/api/oauth/authorize, token at
    https://api.canva.com/rest/v1/oauth/token (Basic auth, form encoded,
    backend only).
  - Access token lasts 4 hours (expires_in 14400 in the example).
  - A refresh token can be used ONCE; each refresh returns a new one.
  - GET /v1/designs/{id} needs design:meta:read (100 per minute per user).
  - POST /v1/exports needs design:content:read (20 per minute per user,
    75 per 5 minutes per document). Download URLs last 24 hours.
  - Teams that are not on Enterprise can only make a "public" app. We never
    submit it for review; whether Canva lets its creator use it unreviewed is
    exactly what this step finds out.

Everything that talks to the network or the database is passed in, so the
tests run with stand-ins and no secrets.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlencode

AUTHORIZE_URL = "https://www.canva.com/api/oauth/authorize"
API_BASE = "https://api.canva.com/rest/v1"
TOKEN_URL = API_BASE + "/oauth/token"
SCOPES = "design:meta:read design:content:read"

# The team that owns all five decks (read from Canva on Oct 12, 2026: every
# deck's owner.team_id). A connection that cannot see these decks as this
# team's is refused and nothing is saved.
FORWARD_TEAM_ID = "oBXdxZtv1SuqMjcj59OGxE"

# Login name -> Canva design id. The ONE list. The browser never chooses a
# design: it only says who is signed in. Ids are the ones the OS linked to
# until now (index.html LISTING_PRESENTATIONS); titles were checked against
# Canva on Oct 12, 2026.
PRESENTATIONS: dict[str, str] = {
    "Marc Cashin":     "DAHECgtXhJc",
    "Ashling McGowan": "DAHEC0x1ja8",
    "Niki Lang":       "DAHEC2uBYwc",
    "Cesar Rivera":    "DAHECx4jFMs",
    "Charlotte Lee":   "DAHECzMRm6M",
}

# The contact email printed on each agent's own Sales Team page, as it appears
# in the decks (Charlotte's is lower case there; matching ignores case).
# Shannon Casey left in July 2026 but Marc's deck still carries her page, so
# she is listed: her email in a deck is recognised as an agent's email.
AGENT_EMAILS: dict[str, str] = {
    "Marc Cashin":     "marc.cashin@corcoranmce.com",
    "Ashling McGowan": "ash.mcgowan@corcoranmce.com",
    "Niki Lang":       "niki.lang@corcoranmce.com",
    "Cesar Rivera":    "cesar.rivera@corcoranmce.com",
    "Charlotte Lee":   "charlotte.lee@corcoranmce.com",
    "Shannon Casey":   "shannon.casey@corcoranmce.com",
}
FOUNDER = "Marc Cashin"

def expected_title(agent: str) -> str:
    return f"{agent} - Listing Presentation"


# ── PKCE (RFC 7636) ──────────────────────────────────────────────────────────
def new_code_verifier() -> str:
    # 64 url-safe characters: inside Canva's 43 to 128, only A-Z a-z 0-9 - _
    return secrets.token_urlsafe(48)[:64]


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(client_id: str, redirect_uri: str, state: str, verifier: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
        "scope": SCOPES,
        "response_type": "code",
        "client_id": client_id,
        "state": state,
        "redirect_uri": redirect_uri,
    })


def basic_auth(client_id: str, client_secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()


# ── The ownership check ──────────────────────────────────────────────────────
def agent_emails_in_order(text: str) -> list[str]:
    """Known agent emails in the order they appear, lower case, repeats kept.

    Each known address is searched for as exact text (ignoring case), not with
    a general email pattern: the decks print the phone number straight against
    the email ("202.900.3126Marc.Cashin@CorcoranMCE.com"), and a pattern takes
    the digits as part of the address and then matches nobody."""
    low = (text or "").lower()
    hits = []
    for email in set(AGENT_EMAILS.values()):
        start = low.find(email)
        while start != -1:
            hits.append((start, email))
            start = low.find(email, start + 1)
    return [e for _, e in sorted(hits)]


def check_owner(agent: str, text: str) -> tuple[bool, str]:
    """Is this deck's text the given agent's deck?

    Rules, from reading all five decks on Oct 12, 2026:
      1. The FIRST agent email in the deck is the owner's (each deck opens its
         Sales Team section with its owner's page).
      2. For the four agent decks, no agent email appears except the owner's
         and Marc's (every deck carries Marc's founder page). Marc's own deck
         also carries other agents' pages, so rule 2 does not apply to it.
    A deck with no agent email at all fails: we cannot tell whose it is.
    """
    own = AGENT_EMAILS.get(agent)
    if not own:
        return False, f"no contact email on file for {agent}"
    found = agent_emails_in_order(text)
    if not found:
        return False, "no agent contact email could be read in the PDF"
    if found[0] != own:
        return False, f"the first agent contact in the PDF is {found[0]}, not {own}"
    if agent != FOUNDER:
        allowed = {own, AGENT_EMAILS[FOUNDER]}
        others = sorted(set(found) - allowed)
        if others:
            return False, "the PDF also carries " + ", ".join(others)
    return True, "first agent contact is " + own


# ── Token store ──────────────────────────────────────────────────────────────
class SupabaseStore:
    """The two tables in migrations/2026-10-12_canva_connection.sql.
    Service role only; the app's public key cannot read them."""

    def __init__(self, supabase):
        self.sb = supabase

    def get(self) -> Optional[dict]:
        rows = self.sb.table("canva_connection").select("*").eq("id", 1).execute().data
        return rows[0] if rows else None

    def put(self, row: dict) -> None:
        self.sb.table("canva_connection").upsert({**row, "id": 1}, on_conflict="id").execute()

    def swap(self, old_refresh: str, row: dict) -> bool:
        """Save new tokens only if nobody else rotated first."""
        res = (self.sb.table("canva_connection").update(row)
               .eq("id", 1).eq("refresh_token", old_refresh).execute())
        return bool(res.data)

    def add_pending(self, state: str, verifier: str) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        self.sb.table("canva_oauth_pending").delete().lt("created_at", cutoff).execute()
        self.sb.table("canva_oauth_pending").insert({"state": state, "code_verifier": verifier}).execute()

    def take_pending(self, state: str) -> Optional[str]:
        rows = (self.sb.table("canva_oauth_pending").select("code_verifier,created_at")
                .eq("state", state).execute().data)
        self.sb.table("canva_oauth_pending").delete().eq("state", state).execute()
        if not rows:
            return None
        made = datetime.fromisoformat(str(rows[0]["created_at"]).replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - made > timedelta(minutes=15):
            return None
        return rows[0]["code_verifier"]


# ── HTTP ─────────────────────────────────────────────────────────────────────
# http(method, url, headers=..., data=..., json=...) -> (status, parsed JSON or {})
Http = Callable[..., Awaitable[tuple[int, Any]]]
# fetch_bytes(url, max_bytes) -> bytes
FetchBytes = Callable[[str, int], Awaitable[bytes]]


class CanvaError(Exception):
    pass


def _err(status: int, body: Any) -> str:
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error_description") or body.get("error") or body.get("code")
        if msg:
            return f"{status}: {msg}"
    return f"HTTP {status}"


class CanvaLink:
    def __init__(self, *, client_id: str, client_secret: str, redirect_uri: str,
                 store, http: Http, fetch_bytes: FetchBytes, logger=None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.store = store
        self.http = http
        self.fetch_bytes = fetch_bytes
        self.log = logger
        self.now = now
        self._lock = asyncio.Lock()

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    # ── connect ──
    def start(self) -> str:
        if not self.configured():
            raise CanvaError("CANVA_CLIENT_ID and CANVA_CLIENT_SECRET are not set in Railway.")
        state = secrets.token_urlsafe(24)
        verifier = new_code_verifier()
        self.store.add_pending(state, verifier)
        return authorize_url(self.client_id, self.redirect_uri, state, verifier)

    async def _token_request(self, form: dict) -> dict:
        status, body = await self.http(
            "POST", TOKEN_URL,
            headers={"Authorization": basic_auth(self.client_id, self.client_secret),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data=form)
        if status != 200 or not isinstance(body, dict) or not body.get("refresh_token"):
            raise CanvaError("Canva refused the token request (" + _err(status, body) + ")")
        return body

    def _row(self, tok: dict) -> dict:
        exp = self.now() + timedelta(seconds=int(tok.get("expires_in") or 0))
        return {"refresh_token": tok["refresh_token"], "access_token": tok["access_token"],
                "access_expires_at": exp.isoformat(), "scope": tok.get("scope"),
                "updated_at": self.now().isoformat()}

    async def finish(self, code: str, state: str) -> dict:
        """Callback. Saves the connection ONLY if it reads all five decks as the
        FORWARD team's. Returns the per-deck report either way."""
        verifier = self.store.take_pending(state or "")
        if not verifier:
            raise CanvaError("This connect link has expired or was already used. Start again.")
        tok = await self._token_request({
            "grant_type": "authorization_code", "code": code,
            "code_verifier": verifier, "redirect_uri": self.redirect_uri})
        report = await self.check_decks(tok["access_token"])
        if not report["ok"]:
            # Never keep a connection that cannot see the team's decks: it
            # would only replace a good one with a useless one.
            return report
        row = self._row(tok)
        row.update({"canva_team_id": FORWARD_TEAM_ID, "connected_at": self.now().isoformat()})
        self.store.put(row)
        report["saved"] = True
        return report

    async def check_decks(self, access_token: str) -> dict:
        decks = []
        for agent, design_id in PRESENTATIONS.items():
            status, body = await self.http(
                "GET", f"{API_BASE}/designs/{design_id}",
                headers={"Authorization": f"Bearer {access_token}"})
            d = body.get("design") if isinstance(body, dict) else None
            if status != 200 or not d:
                decks.append({"agent": agent, "ok": False, "reason": "cannot read the deck (" + _err(status, body) + ")"})
                continue
            problems = []
            if d.get("title") != expected_title(agent):
                problems.append(f"title is '{d.get('title')}'")
            team = (d.get("owner") or {}).get("team_id")
            if team != FORWARD_TEAM_ID:
                problems.append(f"owned by team {team}, not FORWARD's")
            decks.append({"agent": agent, "ok": not problems, "pages": d.get("page_count"),
                          "updated_at": d.get("updated_at"),
                          "reason": "; ".join(problems) or "ok"})
        return {"ok": all(x["ok"] for x in decks), "decks": decks, "saved": False}

    # ── tokens ──
    async def access_token(self) -> str:
        async with self._lock:
            row = self.store.get()
            if not row:
                raise CanvaError("Canva is not connected yet.")
            exp = row.get("access_expires_at")
            if row.get("access_token") and exp:
                when = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                if when - self.now() > timedelta(minutes=5):
                    return row["access_token"]
            old = row["refresh_token"]
            try:
                tok = await self._token_request({"grant_type": "refresh_token", "refresh_token": old})
            except CanvaError:
                # Another process may have used this refresh token a moment ago
                # (two copies run side by side during a deploy). If the stored
                # token moved, theirs is good; otherwise the connection is gone.
                again = self.store.get()
                if again and again.get("refresh_token") != old and again.get("access_token"):
                    return again["access_token"]
                raise CanvaError("The Canva connection has lapsed. Marc needs to connect again.")
            new_row = self._row(tok)
            if not self.store.swap(old, new_row):
                if self.log:
                    self.log.error("[canva] refreshed, but the stored token had already moved")
            return tok["access_token"]

    # ── export ──
    async def export_pdf(self, design_id: str, *, timeout_s: float = 180, poll_s: float = 2.0,
                         max_bytes: int = 200 * 1024 * 1024) -> bytes:
        token = await self.access_token()
        auth = {"Authorization": f"Bearer {token}"}
        status, body = await self.http(
            "POST", f"{API_BASE}/exports", headers={**auth, "Content-Type": "application/json"},
            json={"design_id": design_id, "format": {"type": "pdf", "export_quality": "regular"}})
        job = body.get("job") if isinstance(body, dict) else None
        if status != 200 or not job:
            raise CanvaError("Canva would not start the export (" + _err(status, body) + ")")
        start = time.monotonic()
        while job.get("status") == "in_progress":
            if time.monotonic() - start > timeout_s:
                raise CanvaError(f"Canva's export was still running after {int(timeout_s)} seconds")
            await asyncio.sleep(poll_s)
            status, body = await self.http("GET", f"{API_BASE}/exports/{job['id']}", headers=auth)
            job = body.get("job") if isinstance(body, dict) else None
            if status != 200 or not job:
                raise CanvaError("Lost track of the export (" + _err(status, body) + ")")
        if job.get("status") != "success":
            e = job.get("error") or {}
            raise CanvaError("Canva's export failed (" + str(e.get("code") or "") + " " + str(e.get("message") or "") + ")")
        urls = job.get("urls") or []
        if len(urls) != 1:
            raise CanvaError(f"Expected one PDF from Canva, got {len(urls)} files")
        data = await self.fetch_bytes(urls[0], max_bytes)
        if not data.startswith(b"%PDF-"):
            raise CanvaError("What Canva returned is not a PDF")
        return data


def pdf_facts(data: bytes) -> dict:
    """Pages and text of a PDF (pypdf is already a backend dependency)."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    texts = []
    for p in reader.pages:
        try:
            texts.append(p.extract_text() or "")
        except Exception:
            texts.append("")
    return {"pages": len(reader.pages), "text": "\n".join(texts),
            "chars": sum(len(t) for t in texts)}


async def test_export(link: CanvaLink, agent: str) -> dict:
    """Export one deck and report what came back. Stores nothing."""
    design_id = PRESENTATIONS[agent]
    t0 = time.monotonic()
    data = await link.export_pdf(design_id)
    secs = round(time.monotonic() - t0, 1)
    facts = await asyncio.to_thread(pdf_facts, data)
    ok, why = check_owner(agent, facts["text"])
    return {"agent": agent, "design_id": design_id, "bytes": len(data),
            "megabytes": round(len(data) / 1024 / 1024, 1), "pages": facts["pages"],
            "text_chars": facts["chars"], "seconds": secs,
            "sha256": hashlib.sha256(data).hexdigest(),
            "emails_found_in_order": agent_emails_in_order(facts["text"])[:6],
            "owner_check": {"ok": ok, "detail": why}}
