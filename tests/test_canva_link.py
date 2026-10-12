"""The Canva connection for listing presentations (canva_link.py).

No network and no secrets: Canva and the database are stand-ins that behave
the way Canva's docs and the real tables say. Run:
    python -m pytest tests/test_canva_link.py

The deck texts below follow the order the five real decks were read in on
Oct 12, 2026 (Canva MCP read-design): each deck's Sales Team section opens
with its owner's contact page, Marc's founder page follows, and Marc's own
deck also carries Ashling's, Niki's and Shannon Casey's pages.
"""
import asyncio, base64, hashlib, os, sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import canva_link as cl

# The FORWARD team as THIS app sees it (Canva ids differ per app; Oct 12, 2026).
APP_TEAM = "oBZT6QMQrCaCdayC2NYxSQ"
AGENTS = ["Marc Cashin", "Ashling McGowan", "Niki Lang", "Cesar Rivera", "Charlotte Lee"]
PRINTED = {  # exactly as printed in each deck
    "Marc Cashin": "Marc.Cashin@CorcoranMCE.com",
    "Ashling McGowan": "Ash.McGowan@CorcoranMCE.com",
    "Niki Lang": "Niki.Lang@CorcoranMCE.com",
    "Cesar Rivera": "Cesar.Rivera@CorcoranMCE.com",
    "Charlotte Lee": "charlotte.lee@corcoranmce.com",
    "Shannon Casey": "Shannon.Casey@CorcoranMCE.com",
}


def deck_text(owner):
    order = ([owner, "Marc Cashin"] if owner != "Marc Cashin"
             else ["Marc Cashin", "Ashling McGowan", "Niki Lang", "Shannon Casey"])
    body = ["A Guide to Selling Your Property", "Sales Team", "Work with the most trusted names"]
    for name in order:
        body.append(f"{name}Licensed Real Estate Agent202.000.0000{PRINTED[name]}\nSales Team\nbio text")
    body.append("Operational Support Sofia BurtonTransactions Management")
    return "\n".join(body)


# ── PKCE ──
def test_pkce_challenge_matches_rfc7636_example():
    assert cl.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verifier_is_within_canvas_rules():
    for _ in range(50):
        v = cl.new_code_verifier()
        assert 43 <= len(v) <= 128
        assert all(c.isalnum() or c in "-._~" for c in v)


def test_authorize_url_carries_the_challenge_not_the_verifier():
    v = cl.new_code_verifier()
    q = parse_qs(urlparse(cl.authorize_url("CID", "https://x/cb", "STATE", v)).query)
    assert q["code_challenge"] == [cl.code_challenge(v)]
    assert q["code_challenge_method"] == ["S256"]
    assert q["scope"] == ["design:meta:read design:content:read"]
    assert q["client_id"] == ["CID"] and q["state"] == ["STATE"] and q["redirect_uri"] == ["https://x/cb"]
    assert v not in cl.authorize_url("CID", "https://x/cb", "STATE", v)


def test_basic_auth_header():
    assert cl.basic_auth("a", "b") == "Basic " + base64.b64encode(b"a:b").decode()


# ── the ownership check: every deck against every agent ──
@pytest.mark.parametrize("owner", AGENTS)
@pytest.mark.parametrize("asked", AGENTS)
def test_each_deck_belongs_to_its_owner_and_nobody_else(owner, asked):
    ok, why = cl.check_owner(asked, deck_text(owner))
    assert ok == (owner == asked), (owner, asked, why)


def test_mapping_covers_the_five_agents_and_no_one_else():
    assert sorted(cl.PRESENTATIONS) == sorted(AGENTS)
    assert len(set(cl.PRESENTATIONS.values())) == 5
    for a in AGENTS:
        assert cl.AGENT_EMAILS[a] == PRINTED[a].lower()


def test_no_email_readable_fails():
    assert cl.check_owner("Niki Lang", "A Guide to Selling Your Property") == (False, "no agent contact email could be read in the PDF")


def test_an_agent_deck_carrying_a_third_agent_fails():
    text = deck_text("Niki Lang") + "\n" + PRINTED["Cesar Rivera"]
    ok, why = cl.check_owner("Niki Lang", text)
    assert not ok and "cesar.rivera" in why


def test_unknown_login_gets_nothing():
    for name in ["Operations", "Concierge", "Shannon Casey", ""]:
        assert cl.check_owner(name, deck_text("Marc Cashin"))[0] is False


# ── stand-ins ──
class Store:
    def __init__(self, row=None):
        self.row = row
        self.pending = {}
        self.puts = []

    def get(self):
        return dict(self.row) if self.row else None

    def put(self, row):
        self.row = {**row, "id": 1}
        self.puts.append(row)

    def swap(self, old, row):
        if not self.row or self.row["refresh_token"] != old:
            return False
        self.row.update(row)
        return True

    def add_pending(self, state, v):
        self.pending[state] = v

    def take_pending(self, state):
        return self.pending.pop(state, None)


class FakeCanva:
    """Answers like Canva: designs by id, one-use refresh tokens, export jobs."""

    def __init__(self):
        self.designs = {d: {"id": d, "title": f"{a} - Listing Presentation", "page_count": 33,
                            "owner": {"user_id": "u", "team_id": APP_TEAM}, "updated_at": 1}
                        for a, d in cl.PRESENTATIONS.items()}
        self.live_refresh = {"R0"}
        self.n = 0
        self.calls = []
        self.job_polls = 2
        self.job_result = {"status": "success", "urls": ["https://export/x.pdf"]}
        self.me = (200, {"team_user": {"user_id": "ops-user", "team_id": APP_TEAM}})

    async def http(self, method, url, headers=None, data=None, json=None):
        self.calls.append((method, url, data, json))
        if url == cl.TOKEN_URL:
            assert headers["Authorization"].startswith("Basic ")
            if data["grant_type"] == "authorization_code":
                if data["code"] != "GOOD":
                    return 400, {"error": "invalid_grant"}
            else:
                if data["refresh_token"] not in self.live_refresh:
                    return 400, {"error": "invalid_grant"}
                self.live_refresh.discard(data["refresh_token"])   # one use only
            self.n += 1
            self.live_refresh.add(f"R{self.n}")
            return 200, {"access_token": f"A{self.n}", "refresh_token": f"R{self.n}",
                         "expires_in": 14400, "token_type": "Bearer"}
        if url.endswith("/users/me"):
            return self.me
        if "/designs/" in url:
            d = self.designs.get(url.rsplit("/", 1)[1])
            return (200, {"design": d}) if d else (403, {"code": "permission_denied", "message": "no"})
        if url.endswith("/exports") and method == "POST":
            return 200, {"job": {"id": "J1", "status": "in_progress"}}
        if "/exports/" in url:
            self.job_polls -= 1
            if self.job_polls > 0:
                return 200, {"job": {"id": "J1", "status": "in_progress"}}
            return 200, {"job": {"id": "J1", **self.job_result}}
        return 404, {}


def link(store, canva, pdf=b"%PDF-1.4 x", now=None):
    async def fetch(url, max_bytes):
        return pdf
    return cl.CanvaLink(client_id="CID", client_secret="SEC", redirect_uri="https://x/cb",
                        store=store, http=canva.http, fetch_bytes=fetch,
                        now=now or (lambda: datetime(2026, 10, 12, 19, 0, tzinfo=timezone.utc)))


def run(c):
    return asyncio.run(c)


def connect(store, canva, code="GOOD"):
    lk = link(store, canva)
    state = parse_qs(urlparse(lk.start()).query)["state"][0]
    return lk, run(lk.finish(code, state))


# ── connect ──
def test_connect_saves_only_after_all_five_decks_check_out():
    store, canva = Store(), FakeCanva()
    _, rep = connect(store, canva)
    assert rep["ok"] and rep["saved"] and len(rep["decks"]) == 5
    assert store.row["refresh_token"] == "R1" and store.row["canva_team_id"] == APP_TEAM
    assert store.row["canva_user_id"] == "ops-user"


def test_team_is_compared_within_this_app_never_to_a_copied_id():
    # The id the Canva connector showed for FORWARD must play no part.
    assert not hasattr(cl, "FORWARD_TEAM_ID")
    code_lines = [l for l in open(cl.__file__).read().splitlines() if not l.lstrip().startswith("#")]
    assert not any("oBXdxZtv1SuqMjcj59OGxE" in l or "oBZT6QMQrCaCdayC2NYxSQ" in l for l in code_lines)


def test_an_account_from_another_team_that_can_see_the_decks_saves_nothing():
    # e.g. a deck shared outside the team: readable, but not this account's team's
    store, canva = Store(), FakeCanva()
    canva.me = (200, {"team_user": {"user_id": "x", "team_id": "some-other-team"}})
    _, rep = connect(store, canva)
    assert not rep["ok"] and store.row is None
    assert all("not the signed-in account's team" in d["reason"] for d in rep["decks"])


def test_when_the_account_team_cannot_be_read_nothing_is_saved():
    store, canva = Store(), FakeCanva()
    canva.me = (401, {"message": "bad token"})
    _, rep = connect(store, canva)
    assert not rep["ok"] and store.row is None and len(rep["decks"]) == 5


@pytest.mark.parametrize("breakage", ["other_team", "renamed", "cannot_read"])
def test_connect_with_an_account_that_cannot_see_the_team_decks_saves_nothing(breakage):
    store, canva = Store(row={"refresh_token": "OLD-GOOD"}), FakeCanva()
    did = cl.PRESENTATIONS["Niki Lang"]
    if breakage == "other_team":
        canva.designs[did]["owner"]["team_id"] = "someone-elses-team"
    elif breakage == "renamed":
        canva.designs[did]["title"] = "Cesar Rivera - Listing Presentation"
    else:
        del canva.designs[did]
    _, rep = connect(store, canva)
    assert not rep["ok"] and not rep["saved"]
    assert store.puts == [] and store.row == {"refresh_token": "OLD-GOOD"}, "an existing good connection must survive"
    bad = [d for d in rep["decks"] if not d["ok"]]
    assert [d["agent"] for d in bad] == ["Niki Lang"]


def test_a_connect_link_works_once():
    store, canva = Store(), FakeCanva()
    lk = link(store, canva)
    state = parse_qs(urlparse(lk.start()).query)["state"][0]
    run(lk.finish("GOOD", state))
    with pytest.raises(cl.CanvaError, match="expired or was already used"):
        run(lk.finish("GOOD", state))
    with pytest.raises(cl.CanvaError, match="expired"):
        run(lk.finish("GOOD", "made-up-state"))


def test_refused_code_saves_nothing():
    store, canva = Store(), FakeCanva()
    with pytest.raises(cl.CanvaError, match="refused"):
        connect(store, canva, code="BAD")
    assert store.row is None


def test_not_configured_says_so():
    lk = cl.CanvaLink(client_id="", client_secret="", redirect_uri="x", store=Store(),
                      http=None, fetch_bytes=None)
    with pytest.raises(cl.CanvaError, match="CANVA_CLIENT_ID"):
        lk.start()


# ── tokens ──
def test_a_fresh_access_token_is_reused_without_calling_canva():
    store, canva = Store(), FakeCanva()
    lk, _ = connect(store, canva)
    n = len(canva.calls)
    assert run(lk.access_token()) == "A1"
    assert len(canva.calls) == n


def test_an_expiring_token_is_refreshed_and_the_new_refresh_token_saved():
    store, canva = Store(), FakeCanva()
    connect(store, canva)
    later = lambda: datetime(2026, 10, 12, 22, 58, tzinfo=timezone.utc)  # 3h58 later
    lk = link(store, canva, now=later)
    assert run(lk.access_token()) == "A2"
    assert store.row["refresh_token"] == "R2"
    assert "R1" not in canva.live_refresh, "Canva refresh tokens are single use"


def test_another_copy_refreshed_first_so_we_use_its_token():
    store, canva = Store(), FakeCanva()
    connect(store, canva)
    later = lambda: datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc)
    a, b = link(store, canva, now=later), link(store, canva, now=later)
    # Copy b already used R1 and saved R2; copy a still holds R1 from an old read.
    assert run(b.access_token()) == "A2"
    stale = dict(store.row, refresh_token="R1", access_token="A1",
                 access_expires_at="2026-10-12T23:00:00+00:00")
    reads = iter([stale, store.get()])
    store_get = store.get
    store.get = lambda: next(reads, None) or store_get()
    assert run(a.access_token()) == "A2"


def test_a_lapsed_connection_is_said_plainly():
    store = Store(row={"refresh_token": "DEAD", "access_token": "x",
                       "access_expires_at": "2026-10-01T00:00:00+00:00"})
    with pytest.raises(cl.CanvaError, match="lapsed. Marc needs to connect again"):
        run(link(store, FakeCanva()).access_token())


def test_not_connected_is_said_plainly():
    with pytest.raises(cl.CanvaError, match="not connected"):
        run(link(Store(), FakeCanva()).access_token())


# ── export ──
def test_export_waits_for_the_job_and_returns_the_pdf():
    store, canva = Store(), FakeCanva()
    lk, _ = connect(store, canva)
    lk_fast = cl.CanvaLink(client_id="CID", client_secret="SEC", redirect_uri="x", store=store,
                           http=canva.http, fetch_bytes=lk.fetch_bytes, now=lk.now)
    canva.job_polls = 3
    data = run(lk_fast.export_pdf(cl.PRESENTATIONS["Niki Lang"], poll_s=0))
    assert data.startswith(b"%PDF-")
    post = [c for c in canva.calls if c[0] == "POST" and c[1].endswith("/exports")][0]
    assert post[3] == {"design_id": "DAHEC2uBYwc", "format": {"type": "pdf", "export_quality": "regular"}}


@pytest.mark.parametrize("result,msg", [
    ({"status": "failed", "error": {"code": "license_required", "message": "x"}}, "export failed"),
    ({"status": "success", "urls": ["a", "b"]}, "one PDF"),
    ({"status": "success", "urls": []}, "one PDF"),
])
def test_export_failures_are_errors(result, msg):
    store, canva = Store(), FakeCanva()
    lk, _ = connect(store, canva)
    canva.job_result = result
    with pytest.raises(cl.CanvaError, match=msg):
        run(lk.export_pdf("DAHEC2uBYwc", poll_s=0))


def test_export_that_never_finishes_times_out():
    store, canva = Store(), FakeCanva()
    lk, _ = connect(store, canva)
    canva.job_polls = 10 ** 9
    with pytest.raises(cl.CanvaError, match="still running"):
        run(lk.export_pdf("DAHEC2uBYwc", poll_s=0, timeout_s=0.05))


def test_something_that_is_not_a_pdf_is_refused():
    store, canva = Store(), FakeCanva()
    connect(store, canva)
    lk = link(store, canva, pdf=b"<html>error</html>")
    canva.job_polls = 1
    with pytest.raises(cl.CanvaError, match="not a PDF"):
        run(lk.export_pdf("DAHEC2uBYwc", poll_s=0))


# ── test export end to end, with a real PDF that pypdf reads ──
def tiny_pdf(lines):
    """A minimal one-page PDF whose text layer holds `lines`."""
    ops = "BT /F1 10 Tf 40 760 Td " + " ".join(
        "(" + l.replace("(", "").replace(")", "") + ") Tj 0 -14 Td" for l in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(ops)} >>\nstream\n{ops}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    return out


@pytest.mark.parametrize("owner", AGENTS)
def test_test_export_reports_the_owner_check_on_a_real_pdf(owner):
    store, canva = Store(), FakeCanva()
    connect(store, canva)
    pdf = tiny_pdf(deck_text(owner).split("\n"))
    lk = link(store, canva, pdf=pdf)
    canva.job_polls = 1
    rep = run(cl.test_export(lk, owner))
    assert rep["pages"] == 1 and rep["bytes"] == len(pdf)
    assert rep["owner_check"]["ok"], rep
    assert rep["sha256"] == hashlib.sha256(pdf).hexdigest()
    wrong = next(a for a in AGENTS if a != owner)
    assert cl.check_owner(wrong, cl.pdf_facts(pdf)["text"])[0] is False
