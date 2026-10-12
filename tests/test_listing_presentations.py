"""Listing presentations, step 2 (listing_presentations.py).

Stand-ins: Canva (from test_canva_link) and an in-memory Storage that answers
the same REST calls Supabase Storage does. Real PDFs read by real pypdf.
Run: python -m pytest tests/test_listing_presentations.py
"""
import asyncio, json, os, sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs, unquote

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import canva_link as cl
import listing_presentations as lp
import test_canva_link as T

AGENTS = T.AGENTS
OTHER = {a: next(b for b in AGENTS if b != a) for a in AGENTS}


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 12, 22, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class MemStorage:
    """Behaves like the Supabase Storage REST endpoints SupabaseStorage calls."""

    def __init__(self):
        self.files = {}
        self.fail_put = set()     # path suffixes whose upload is refused
        self.calls = []

    async def http(self, method, url, headers=None, content=None, json=None):
        self.calls.append((method, url))
        assert headers["Authorization"].startswith("Bearer ")
        base = "https://db.example/storage/v1"
        path = url[len(base):]
        if method == "POST" and path.startswith(f"/object/list/{lp.BUCKET}"):
            pre = json["prefix"]
            names = [k[len(pre):] for k in self.files if k.startswith(pre)]
            return 200, __import__("json").dumps([{"name": n} for n in names]).encode()
        if method == "POST" and path.startswith(f"/object/sign/{lp.BUCKET}/"):
            p = unquote(path[len(f"/object/sign/{lp.BUCKET}/"):])
            if p not in self.files:
                return 400, b'{"error":"not found"}'
            return 200, __import__("json").dumps({"signedURL": f"/object/sign/{lp.BUCKET}/{p}?token=T"}).encode()
        if method == "POST" and path.startswith(f"/object/{lp.BUCKET}/"):
            p = unquote(path[len(f"/object/{lp.BUCKET}/"):])
            if any(p.endswith(s) for s in self.fail_put):
                return 500, b"refused"
            if p in self.files and headers.get("x-upsert") != "true":
                return 409, b"exists"
            self.files[p] = content
            return 200, b"{}"
        if method == "GET" and path.startswith(f"/object/authenticated/{lp.BUCKET}/"):
            p = unquote(path[len(f"/object/authenticated/{lp.BUCKET}/"):])
            return (200, self.files[p]) if p in self.files else (400, b"not found")
        if method == "DELETE" and path == f"/object/{lp.BUCKET}":
            for p in json["prefixes"]:
                self.files.pop(p, None)
            return 200, b"[]"
        return 404, b""

    def store(self):
        return lp.SupabaseStorage("https://db.example", "SERVICE", self.http)


def deck_pdf(owner):
    return T.tiny_pdf(T.deck_text(owner).split("\n"))


class World:
    """Connected Canva + storage, with per-design PDFs Canva will export."""

    def __init__(self):
        self.canva = T.FakeCanva()
        self.store_obj = T.Store()
        self.clock = Clock()
        T.connect(self.store_obj, self.canva)
        self.mem = MemStorage()
        self.storage = self.mem.store()
        self.pdf_for = {cl.PRESENTATIONS[a]: deck_pdf(a) for a in AGENTS}
        self.exports = []
        self.canva.job_polls = 1
        orig = self.canva.http

        async def http(method, url, **kw):
            if method == "POST" and url.endswith("/exports"):
                self.last_design = kw["json"]["design_id"]
                self.exports.append(self.last_design)
            if "/exports/" in url:
                self.canva.job_polls = 1
            return await orig(method, url, **kw)
        self.canva.http = http

        async def fetch(url, max_bytes):
            return self.pdf_for[self.last_design]
        self.link = cl.CanvaLink(client_id="CID", client_secret="SEC", redirect_uri="x",
                                 store=self.store_obj, http=self.canva.http, fetch_bytes=fetch, now=self.clock)
        real_export = self.link.export_pdf
        self.link.export_pdf = lambda design_id: real_export(design_id, poll_s=0)   # no 2 s wait in tests

    def sync(self, agent=None, force=False):
        if agent:
            return run(lp.sync_one(self.link, self.storage, agent, force=force, now=self.clock))
        return run(lp.sync_all(self.link, self.storage, force=force, now=self.clock))

    def download(self, agent):
        return run(lp.download_link(self.storage, agent))

    def pdf_behind(self, url):
        p = urlparse(url).path.split(f"/object/sign/{lp.BUCKET}/")[1]
        return self.mem.files[unquote(p)]


# ── the main promise ──
def test_every_agent_downloads_only_their_own_deck():
    w = World()
    res = w.sync()
    assert res["ok"], res
    for asked in AGENTS:
        d = w.download(asked)
        pdf = w.pdf_behind(d["url"])
        text = cl.pdf_facts(pdf)["text"]
        for other in AGENTS:
            assert cl.check_owner(other, text)[0] == (other == asked), (asked, other)
        assert d["file_name"] == lp.FOLDER[asked] + "_Listing_Presentation.pdf"
        assert f"/{lp.FOLDER[asked]}/" in unquote(d["url"])
        assert parse_qs(urlparse(d["url"]).query)["download"] == [d["file_name"]]


def test_operations_concierge_and_strangers_get_nothing():
    w = World()
    w.sync()
    for name in ["Operations", "Concierge", "Shannon Casey", "", "../Niki_Lang"]:
        with pytest.raises(LookupError):
            w.download(name)


def test_nothing_exported_yet_is_said():
    w = World()
    with pytest.raises(LookupError, match="no presentation has been exported yet"):
        w.download("Niki Lang")


# ── a wrong file is never stored or served ──
def test_a_deck_that_exports_someone_elses_pdf_is_not_stored():
    w = World()
    w.sync()
    good = w.download("Niki Lang")
    w.pdf_for[cl.PRESENTATIONS["Niki Lang"]] = deck_pdf("Cesar Rivera")   # Canva mix-up
    r = w.sync("Niki Lang", force=True)
    assert r["result"] == "failed" and "ownership check" in r["reason"]
    again = w.download("Niki Lang")
    assert w.pdf_behind(again["url"]) == w.pdf_behind(good["url"]), "still the last good Niki deck"
    assert "ownership check" in again["last_error"]
    for k, v in w.mem.files.items():
        if k.startswith("Niki_Lang/") and k.endswith(".pdf"):
            assert cl.check_owner("Niki Lang", cl.pdf_facts(v)["text"])[0], "a non-Niki PDF was stored in Niki's folder"


@pytest.mark.parametrize("tamper", ["agent", "design", "folder", "dotdot", "nocheck"])
def test_a_tampered_record_is_never_served(tamper):
    w = World()
    w.sync()
    key = "Niki_Lang/current.json"
    rec = json.loads(w.mem.files[key])
    cesar = json.loads(w.mem.files["Cesar_Rivera/current.json"])
    if tamper == "agent":
        rec["agent"] = "Cesar Rivera"
    elif tamper == "design":
        rec["design_id"] = cl.PRESENTATIONS["Cesar Rivera"]
    elif tamper == "folder":
        rec["path"] = cesar["path"]
    elif tamper == "dotdot":
        rec["path"] = "Niki_Lang/../" + cesar["path"]
    else:
        rec["owner_check"] = "first agent contact is cesar.rivera@corcoranmce.com"
    w.mem.files[key] = json.dumps(rec).encode()
    with pytest.raises(LookupError):
        w.download("Niki Lang")


def test_renamed_deck_is_not_exported():
    w = World()
    w.canva.designs[cl.PRESENTATIONS["Cesar Rivera"]]["title"] = "Copy of something"
    r = w.sync("Cesar Rivera")
    assert r["result"] == "failed" and "title" in r["reason"]
    assert not w.exports


# ── failures keep the last good copy ──
def test_lapsed_connection_keeps_serving_the_last_good_copy():
    w = World()
    w.sync()
    before = w.download("Charlotte Lee")
    w.store_obj.row.update(access_token=None, refresh_token="DEAD")
    r = w.sync("Charlotte Lee", force=True)
    assert r["result"] == "failed" and "lapsed" in r["reason"]
    after = w.download("Charlotte Lee")
    assert w.pdf_behind(after["url"]) == w.pdf_behind(before["url"])
    assert "lapsed" in after["last_error"]


def test_pointer_write_failure_keeps_the_old_pdf_and_pointer():
    w = World()
    w.sync()
    before = w.download("Marc Cashin")
    w.clock.advance(hours=1)
    w.mem.fail_put.add("Marc_Cashin/current.json")
    r = w.sync("Marc Cashin", force=True)
    assert r["result"] == "failed"
    w.mem.fail_put.clear()
    after = w.download("Marc Cashin")
    assert after["url"] == before["url"] or w.pdf_behind(after["url"]) == w.pdf_behind(before["url"])


def test_one_agent_failing_does_not_stop_the_others():
    w = World()
    del w.canva.designs[cl.PRESENTATIONS["Ashling McGowan"]]
    res = w.sync()
    by = {r["agent"]: r["result"] for r in res["results"]}
    assert by.pop("Ashling McGowan") == "failed" and set(by.values()) == {"updated"}
    assert not res["ok"]


# ── when it exports ──
def test_unchanged_deck_is_not_exported_again():
    w = World()
    w.sync()
    n = len(w.exports)
    w.clock.advance(hours=1)
    res = w.sync()
    assert {r["result"] for r in res["results"]} == {"unchanged"} and len(w.exports) == n


def test_an_edit_in_canva_is_picked_up():
    w = World()
    w.sync()
    w.clock.advance(hours=1)
    w.canva.designs[cl.PRESENTATIONS["Niki Lang"]]["updated_at"] = 999
    res = w.sync()
    by = {r["agent"]: r["result"] for r in res["results"]}
    assert by["Niki Lang"] == "updated" and list(by.values()).count("unchanged") == 4


def test_a_day_old_copy_is_refreshed_and_refresh_forces():
    w = World()
    w.sync()
    w.clock.advance(hours=25)
    assert {r["result"] for r in w.sync()["results"]} == {"updated"}
    w.clock.advance(minutes=5)
    assert {r["result"] for r in w.sync(force=True)["results"]} == {"updated"}


def test_old_pdfs_are_tidied_keeping_the_current_and_previous():
    w = World()
    for _ in range(4):
        w.sync("Niki Lang", force=True)
        w.clock.advance(minutes=10)
    pdfs = [k for k in w.mem.files if k.startswith("Niki_Lang/") and k.endswith(".pdf")]
    assert len(pdfs) == 2
    assert json.loads(w.mem.files["Niki_Lang/current.json"])["path"] in pdfs


def test_files_never_land_outside_the_agents_folder():
    w = World()
    w.sync()
    for k in w.mem.files:
        assert k.split("/")[0] in lp.FOLDER.values() and k.count("/") == 1


# ── who may ask ──
@pytest.mark.parametrize("origin,ok", [
    ("https://forward-os.netlify.app", True),
    ("https://forward-os-staging.netlify.app", True),
    ("https://deploy-preview-12--forward-os.netlify.app", True),
    ("https://abc123--forward-os-staging.netlify.app", True),
    ("https://forward-os.netlify.app.evil.com", False),
    ("https://evil-forward-os.netlify.app", False),
    ("http://forward-os.netlify.app", False),
    ("https://example.com", False),
    (None, False), ("", False), ("null", False),
])
def test_only_os_pages_may_ask(origin, ok):
    assert lp.origin_allowed(origin) is ok
