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
        self.qualities = []
        self.pro_refused_for = set()
        self.canva.job_polls = 1
        orig = self.canva.http

        async def http(method, url, **kw):
            if method == "POST" and url.endswith("/exports"):
                self.last_design = kw["json"]["design_id"]
                self.last_quality = kw["json"]["format"]["export_quality"]
                self.exports.append(self.last_design)
                self.qualities.append(self.last_quality)
                bad = self.pro_refused_for and self.last_quality == "pro" and self.last_design in self.pro_refused_for
                self.canva.job_result = ({"status": "failed", "error": {"code": "license_required", "message": "premium"}}
                                         if bad else {"status": "success", "urls": ["https://export/x.pdf"]})
            if "/exports/" in url:
                self.canva.job_polls = 1
            return await orig(method, url, **kw)
        self.canva.http = http

        async def fetch(url, max_bytes):
            return self.pdf_for[self.last_design]
        self.link = cl.CanvaLink(client_id="CID", client_secret="SEC", redirect_uri="x",
                                 store=self.store_obj, http=self.canva.http, fetch_bytes=fetch, now=self.clock)
        real_export = self.link.export_pdf
        self.link.export_pdf = lambda design_id, **kw: real_export(design_id, poll_s=0, **kw)   # no 2 s wait in tests

    def sync(self, agent=None, force=False, **kw):
        if agent:
            return run(lp.sync_one(self.link, self.storage, agent, force=force, now=self.clock, **kw))
        return run(lp.sync_all(self.link, self.storage, force=force, now=self.clock, **kw))

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


# ── image quality (Oct 13, 2026: regular exports capped photos at 800 px) ──
def test_every_export_asks_for_full_resolution():
    w = World()
    w.sync()
    assert w.qualities and set(w.qualities) == {"pro"}
    for a in AGENTS:
        assert json.loads(w.mem.files[lp.FOLDER[a] + "/current.json"])["quality"] == "pro"


def test_pro_refused_falls_back_to_regular_and_says_so():
    w = World()
    w.pro_refused_for = {cl.PRESENTATIONS["Niki Lang"]}
    res = w.sync()
    niki = next(r for r in res["results"] if r["agent"] == "Niki Lang")
    assert niki["result"] == "updated" and niki["quality"] == "regular" and "license_required" in niki["warning"]
    assert not res["ok"], "a lower-resolution deck must not count as a clean run (it emails Marc)"
    others = [r for r in res["results"] if r["agent"] != "Niki Lang"]
    assert all(r["quality"] == "pro" and "warning" not in r for r in others)
    d = w.download("Niki Lang")   # the agent still gets a deck
    assert cl.check_owner("Niki Lang", cl.pdf_facts(w.pdf_behind(d["url"]))["text"])[0]
    st = run(lp.status(w.storage))
    assert st["Niki Lang"]["quality"] == "regular" and st["Niki Lang"]["quality_warning"]


def test_a_copy_made_before_the_quality_fix_is_re_exported_once():
    w = World()
    w.sync()
    for a in AGENTS:   # make the stored records look like Oct 12/13 morning ones
        key = lp.FOLDER[a] + "/current.json"
        rec = json.loads(w.mem.files[key]); rec.pop("quality"); rec.pop("quality_warning")
        w.mem.files[key] = json.dumps(rec).encode()
    w.clock.advance(minutes=30)
    assert {r["result"] for r in w.sync()["results"]} == {"updated"}
    w.clock.advance(minutes=30)
    assert {r["result"] for r in w.sync()["results"]} == {"unchanged"}, "then left alone"


def test_regular_fallback_tries_pro_again_next_hour():
    w = World()
    w.pro_refused_for = {cl.PRESENTATIONS["Cesar Rivera"]}
    w.sync()
    w.pro_refused_for = set()   # Marc removed the premium element
    w.clock.advance(hours=1)
    cesar = next(r for r in w.sync()["results"] if r["agent"] == "Cesar Rivera")
    assert cesar["result"] == "updated" and cesar["quality"] == "pro" and "warning" not in cesar


# ── full resolution, reduced like Marc's own export (pdf_slim, Oct 13, 2026) ──
import io as _io
import pikepdf as _pikepdf
import pdf_slim
import test_pdf_slim as S


def _served(w, agent):
    return w.pdf_behind(w.download(agent)["url"])


def test_a_full_resolution_deck_is_stored_reduced_and_still_the_owners():
    w = World()
    big = S.build(deck_pdf("Cesar Rivera"))
    w.pdf_for[cl.PRESENTATIONS["Cesar Rivera"]] = big
    r = w.sync("Cesar Rivera")
    assert r["result"] == "updated" and r["quality"] == "pro" and r["photo_side_cap"] == 3000
    got = _served(w, "Cesar Rivera")
    assert len(got) < len(big) * 0.6
    assert cl.check_owner("Cesar Rivera", cl.pdf_facts(got)["text"])[0]
    assert cl.pdf_facts(got)["text"] == cl.pdf_facts(big)["text"]
    pdf = _pikepdf.open(_io.BytesIO(got))
    xo = pdf.pages[0].obj.Resources.XObject
    assert max(int(xo["/big"].Width), int(xo["/big"].Height)) == 3000
    rec = json.loads(w.mem.files[lp.FOLDER["Cesar Rivera"] + "/current.json"])
    assert rec["bytes"] == len(got) and rec["bytes_from_canva"] == len(big) and rec["photo_side_cap"] == 3000
    assert rec["sha256"] == __import__("hashlib").sha256(got).hexdigest()
    st = run(lp.status(w.storage))["Cesar Rivera"]
    assert st["megabytes"] == round(len(got) / 1048576, 1) and st["photo_side_cap"] == 3000


def _good_then(w, agent, **kw):
    w.sync(agent)
    before = _served(w, agent)
    w.clock.advance(hours=1)
    r = w.sync(agent, force=True, **kw)
    return r, before


def test_a_reduced_file_whose_text_differs_is_never_stored():
    w = World()
    def bad(data, limit):
        return deck_pdf("Charlotte Lee"), {"error": None, "max_side": 3000, "bytes_in": len(data), "fits": True}
    r, before = _good_then(w, "Niki Lang", slim=bad)
    assert r["result"] == "failed" and "did not match" in r["reason"]
    assert _served(w, "Niki Lang") == before
    assert all(cl.check_owner("Niki Lang", cl.pdf_facts(v)["text"])[0]
               for k, v in w.mem.files.items() if k.startswith(lp.FOLDER["Niki Lang"]) and k.endswith(".pdf"))


def test_a_reduction_that_crashes_or_errors_keeps_the_last_good_copy():
    for slim in (lambda d, l: 1 / 0,
                 lambda d, l: (d, {"error": "could not open the PDF: broken", "max_side": 3000, "bytes_in": len(d)})):
        w = World()
        r, before = _good_then(w, "Ashling McGowan", slim=slim)
        assert r["result"] == "failed" and "could not reduce" in r["reason"]
        assert _served(w, "Ashling McGowan") == before
        assert run(lp.status(w.storage))["Ashling McGowan"]["last_error"]


def test_too_big_even_reduced_is_refused_loudly_and_the_last_copy_served():
    w = World()
    r, before = _good_then(w, "Marc Cashin", max_bytes=200)
    assert r["result"] == "failed" and "MB" in r["reason"] and "last good copy" in r["reason"]
    assert _served(w, "Marc Cashin") == before
    n = len([k for k in w.mem.files if k.startswith(lp.FOLDER["Marc Cashin"]) and k.endswith(".pdf")])
    assert n == 1, "nothing new stored"
    w2 = World()   # never had a copy: the agent gets a clear refusal, not someone else's
    assert w2.sync("Marc Cashin", max_bytes=200)["result"] == "failed"
    with pytest.raises(LookupError):
        w2.download("Marc Cashin")


def test_the_standard_quality_fallback_is_not_reduced_again():
    w = World()
    w.pro_refused_for = {cl.PRESENTATIONS["Niki Lang"]}
    calls = []
    def spy(d, l):
        calls.append(1); return pdf_slim.slim_to_fit(d, l)
    r = w.sync("Niki Lang", slim=spy)
    assert r["quality"] == "regular" and not calls
    assert _served(w, "Niki Lang") == w.pdf_for[cl.PRESENTATIONS["Niki Lang"]]
