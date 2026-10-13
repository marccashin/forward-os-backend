"""Agents' listing presentations, exported from Canva and handed out by the OS.

Step 2 of 2 (Oct 12, 2026). Step 1 (canva_link.py) connects the backend to
Canva as operations@fwrdrealestate.com and checks a deck is the agent's.

How it works:
  - Every hour, and when Marc presses Refresh, each agent's deck is checked
    (exact title, owned by the connected account's team), exported to PDF,
    and the PDF's text is checked: the first agent contact must be that
    agent's, and an agent's deck may carry no other agent but Marc
    (canva_link.check_owner). Only then is it stored.
  - Storage: private bucket `listing-presentations` (no access rules for the
    app's public key; only this backend's service key can read or write).
    Per agent a folder, e.g. `Niki_Lang/`, holding the PDFs and one pointer
    file `current.json` naming the PDF to hand out.
  - Order of writes: new PDF first, then the pointer, then old PDFs are
    removed (the previous one is kept). A failure at any step leaves the
    last good PDF in place and being handed out.
  - A deck is exported again only when Canva says it changed since the last
    export, or the last export is more than 24 hours old, or Marc forces it.
  - Download: the OS sends only who is signed in. The backend reads that
    agent's pointer, checks it names that agent, that design and a file in
    that agent's own folder, and returns a link to that one file that works
    for 5 minutes. The browser never names a file or a design.

The 5 measured exports (Oct 12, 2026): 10.0 to 11.7 MB, 33 or 37 pages,
8.5 to 11.7 seconds each.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote

import canva_link as cl
import pdf_slim

BUCKET = "listing-presentations"
LINK_SECONDS = 300
REEXPORT_AFTER = timedelta(hours=24)
# The bucket refuses files over 50 MB; leave room so a deck never sits at the edge.
MAX_STORE_BYTES = 45 * 1024 * 1024

# Folder per agent. Fixed text, never built from a request.
FOLDER: dict[str, str] = {a: a.replace(" ", "_") for a in cl.PRESENTATIONS}


def file_name_for(agent: str) -> str:
    return f"{FOLDER[agent]}_Listing_Presentation.pdf"


# ── Storage (Supabase Storage REST, service key) ─────────────────────────────
class StorageError(Exception):
    pass


class SupabaseStorage:
    """The few Storage calls this needs, made directly so behaviour does not
    depend on the installed client library version."""

    def __init__(self, base_url: str, service_key: str, http):
        self.base = base_url.rstrip("/") + "/storage/v1"
        self.key = service_key
        self.http = http   # http(method, url, headers=, content=, json=) -> (status, bytes)

    def _h(self, extra: Optional[dict] = None) -> dict:
        h = {"Authorization": f"Bearer {self.key}", "apikey": self.key}
        h.update(extra or {})
        return h

    async def put(self, path: str, data: bytes, content_type: str, upsert: bool = False) -> None:
        status, body = await self.http(
            "POST", f"{self.base}/object/{BUCKET}/{quote(path)}",
            headers=self._h({"Content-Type": content_type, "x-upsert": "true" if upsert else "false"}),
            content=data)
        if status not in (200, 201):
            raise StorageError(f"store refused {path} ({status}: {body[:200]!r})")

    async def get(self, path: str) -> Optional[bytes]:
        status, body = await self.http(
            "GET", f"{self.base}/object/authenticated/{BUCKET}/{quote(path)}", headers=self._h())
        if status in (400, 404):
            return None
        if status != 200:
            raise StorageError(f"could not read {path} ({status})")
        return body

    async def list(self, folder: str) -> list[str]:
        status, body = await self.http(
            "POST", f"{self.base}/object/list/{BUCKET}",
            headers=self._h({"Content-Type": "application/json"}),
            json={"prefix": folder + "/", "limit": 100, "offset": 0})
        if status != 200:
            raise StorageError(f"could not list {folder} ({status})")
        return [f"{folder}/{x['name']}" for x in json.loads(body or b"[]") if x.get("name")]

    async def remove(self, paths: list[str]) -> None:
        if not paths:
            return
        status, body = await self.http(
            "DELETE", f"{self.base}/object/{BUCKET}",
            headers=self._h({"Content-Type": "application/json"}), json={"prefixes": paths})
        if status != 200:
            raise StorageError(f"could not remove old files ({status})")

    async def sign(self, path: str, seconds: int, download_name: str) -> str:
        status, body = await self.http(
            "POST", f"{self.base}/object/sign/{BUCKET}/{quote(path)}",
            headers=self._h({"Content-Type": "application/json"}), json={"expiresIn": seconds})
        if status != 200:
            raise StorageError(f"could not make a download link ({status})")
        signed = json.loads(body).get("signedURL") or json.loads(body).get("signedUrl")
        if not signed:
            raise StorageError("the store returned no download link")
        sep = "&" if "?" in signed else "?"
        return self.base + "/" + signed.lstrip("/") + sep + "download=" + quote(download_name)


# ── Pointer file ─────────────────────────────────────────────────────────────
async def read_pointer(store, agent: str) -> Optional[dict]:
    raw = await store.get(f"{FOLDER[agent]}/current.json")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def write_pointer(store, agent: str, rec: dict) -> None:
    await store.put(f"{FOLDER[agent]}/current.json",
                    json.dumps(rec, sort_keys=True).encode(), "application/json", upsert=True)


def pointer_is_sound(agent: str, rec: Optional[dict]) -> tuple[bool, str]:
    """The pointer must name this agent, this agent's design and a PDF inside
    this agent's own folder. Checked on every download."""
    if not rec or not rec.get("path"):
        return False, "no presentation has been exported yet"
    if rec.get("agent") != agent:
        return False, "the stored record names another agent"
    if rec.get("design_id") != cl.PRESENTATIONS[agent]:
        return False, "the stored record names another design"
    path = rec["path"]
    if not (path.startswith(FOLDER[agent] + "/") and path.endswith(".pdf")) or "/" in path[len(FOLDER[agent]) + 1:] or ".." in path:
        return False, "the stored file is not in this agent's folder"
    if not (rec.get("owner_check") or "").startswith("first agent contact is " + cl.AGENT_EMAILS[agent]):
        return False, "the stored file did not pass the ownership check"
    return True, "ok"


# ── Export one agent ─────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


async def sync_one(link: cl.CanvaLink, store, agent: str, *, force: bool = False,
                   now: Callable[[], datetime] = _now, pdf_facts=cl.pdf_facts,
                   slim=pdf_slim.slim_to_fit, max_bytes: int = MAX_STORE_BYTES) -> dict:
    """Bring one agent's stored PDF up to date. Never removes the last good
    PDF unless a new one passed every check and is stored."""
    folder = FOLDER[agent]
    cur = await read_pointer(store, agent)
    attempt = now().isoformat()

    async def record_failure(reason: str) -> dict:
        rec = dict(cur or {"agent": agent, "design_id": cl.PRESENTATIONS[agent], "path": None})
        rec.update({"last_error": reason, "last_attempt_at": attempt})
        try:
            await write_pointer(store, agent, rec)
        except Exception:
            pass
        return {"agent": agent, "result": "failed", "reason": reason,
                "serving": (cur or {}).get("exported_at")}

    try:
        d = await link.deck_check(agent)
    except cl.CanvaError as e:
        return await record_failure(str(e))

    # A copy made before Oct 13, 2026 (no "quality") or at regular quality is
    # re-exported: regular caps every image at 800 px. If Canva refuses pro
    # again, the regular copy is kept and the run says so (see below).
    if not force and cur and cur.get("path") and not cur.get("last_error") and cur.get("quality") == "pro":
        fresh = now() - datetime.fromisoformat(cur["exported_at"]) < REEXPORT_AFTER
        if fresh and str(cur.get("design_updated_at")) == str(d.get("updated_at")):
            return {"agent": agent, "result": "unchanged", "serving": cur.get("exported_at")}

    quality, warning = "pro", None
    try:
        data = await link.export_pdf(cl.PRESENTATIONS[agent], quality="pro")
    except cl.CanvaError as e:
        if "license_required" not in str(e):
            return await record_failure(str(e))
        # Canva refused full resolution (premium elements not licensed). A
        # lower-resolution deck is better than none, but it is never silent.
        quality = "regular"
        warning = ("Canva refused a full-resolution export (license_required), so this deck is "
                   "stored at standard quality and its photos are lower resolution. Remove or "
                   "license the premium elements in Canva, then press Refresh from Canva.")
        try:
            data = await link.export_pdf(cl.PRESENTATIONS[agent], quality="regular")
        except cl.CanvaError as e2:
            return await record_failure(str(e2))
    # Full resolution is 96 to 113 MB a deck (Oct 13, 2026): too big for the
    # bucket and for a phone. Re-save the photos the way Marc's own Canva
    # download is (pdf_slim). The slimmed file must have the same pages and the
    # same text as what Canva sent, or nothing is stored.
    slim_report = None
    if quality == "pro":
        original = await asyncio.to_thread(pdf_facts, data)
        try:
            slimmed, slim_report = await asyncio.to_thread(slim, data, max_bytes)
        except Exception as e:
            return await record_failure(f"could not reduce the full-resolution PDF: {type(e).__name__}: {e}")
        if slim_report.get("error"):
            return await record_failure("could not reduce the full-resolution PDF: " + slim_report["error"])
        facts = await asyncio.to_thread(pdf_facts, slimmed)
        if facts["pages"] != original["pages"] or facts["text"] != original["text"]:
            return await record_failure("the reduced PDF did not match Canva's export (pages or text differ)")
        data = slimmed
    else:
        facts = await asyncio.to_thread(pdf_facts, data)
    if len(data) > max_bytes:
        return await record_failure(
            f"the PDF is {len(data) / 1048576:.1f} MB, over the {max_bytes / 1048576:.0f} MB the store "
            "takes, even with photos reduced; the last good copy is still served")
    ok, why = cl.check_owner(agent, facts["text"])
    if not ok:
        return await record_failure("the exported PDF failed the ownership check: " + why)

    sha = hashlib.sha256(data).hexdigest()
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    path = f"{folder}/{stamp}_{sha[:12]}.pdf"
    try:
        await store.put(path, data, "application/pdf")
        # Canva's "updated" time read AFTER the export, so an export that
        # itself bumps it does not look like an edit next hour.
        try:
            after = await link.deck_check(agent)
        except cl.CanvaError:
            after = d
        rec = {"agent": agent, "design_id": cl.PRESENTATIONS[agent], "path": path,
               "sha256": sha, "bytes": len(data), "pages": facts["pages"],
               "exported_at": now().isoformat(), "design_updated_at": after.get("updated_at"),
               "owner_check": why, "last_error": None, "last_attempt_at": attempt,
               "quality": quality, "quality_warning": warning,
               "photo_side_cap": (slim_report or {}).get("max_side"),
               "bytes_from_canva": (slim_report or {}).get("bytes_in")}
        await write_pointer(store, agent, rec)
    except cl.CanvaError as e:
        return await record_failure(str(e))
    except Exception as e:
        return await record_failure(f"could not store the new PDF: {e}")

    # Tidy: keep the new PDF and the one it replaced.
    keep = {path, (cur or {}).get("path")}
    try:
        old = [p for p in await store.list(folder) if p.endswith(".pdf") and p not in keep]
        await store.remove(old)
    except Exception:
        pass
    out = {"agent": agent, "result": "updated", "pages": facts["pages"], "quality": quality,
           "megabytes": round(len(data) / 1048576, 1), "serving": rec["exported_at"]}
    if slim_report:
        out["megabytes_from_canva"] = round(slim_report["bytes_in"] / 1048576, 1)
        out["photo_side_cap"] = slim_report["max_side"]
    if warning:
        out["warning"] = warning
    return out


async def sync_all(link, store, *, force: bool = False, **kw) -> dict:
    results = []
    for agent in cl.PRESENTATIONS:
        try:
            results.append(await sync_one(link, store, agent, force=force, **kw))
        except Exception as e:   # one agent never stops the others
            results.append({"agent": agent, "result": "failed", "reason": f"{type(e).__name__}: {e}"})
    failed = [r for r in results if r["result"] == "failed" or r.get("warning")]
    return {"ok": not failed, "at": _now().isoformat(), "results": results}


# ── Download ────────────────────────────────────────────────────────────────
async def download_link(store, agent: str) -> dict:
    """The one way a PDF leaves the store. `agent` is the canonical login name."""
    if agent not in cl.PRESENTATIONS:
        raise LookupError("There is no listing presentation for this login.")
    rec = await read_pointer(store, agent)
    ok, why = pointer_is_sound(agent, rec)
    if not ok:
        raise LookupError(why)
    url = await store.sign(rec["path"], LINK_SECONDS, file_name_for(agent))
    return {"agent": agent, "pages": rec.get("pages"), "exported_at": rec.get("exported_at"),
            "file_name": file_name_for(agent), "url": url, "expires_in": LINK_SECONDS,
            "last_error": rec.get("last_error")}


async def status(store) -> dict:
    out = {}
    for agent in cl.PRESENTATIONS:
        try:
            rec = await read_pointer(store, agent) or {}
        except Exception as e:
            rec = {"last_error": f"could not read: {e}"}
        out[agent] = {"pages": rec.get("pages"), "exported_at": rec.get("exported_at"),
                      "last_attempt_at": rec.get("last_attempt_at"), "last_error": rec.get("last_error"),
                      "has_file": bool(rec.get("path")), "quality": rec.get("quality"),
                      "megabytes": round(rec["bytes"] / 1048576, 1) if rec.get("bytes") else None,
                      "photo_side_cap": rec.get("photo_side_cap"),
                      "quality_warning": rec.get("quality_warning")}
    return out


# ── Who may ask (casual protection; the OS has no real login) ───────────────
import re as _re
_ALLOWED_ORIGIN = _re.compile(
    r"^https://((forward-os|forward-os-staging)\.netlify\.app|[a-z0-9-]+--(forward-os|forward-os-staging)\.netlify\.app)$")


def origin_allowed(origin: Optional[str]) -> bool:
    """Only pages of the OS itself. A browser always sends Origin on these
    requests and a page cannot fake it; a script outside a browser can, so
    this keeps casual visitors out and is not a login."""
    return bool(origin and _ALLOWED_ORIGIN.match(origin))
