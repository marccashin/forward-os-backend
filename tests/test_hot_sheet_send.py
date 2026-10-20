"""Hot sheet emails sent by the backend (Oct 19, 2026): off until HOT_SHEET_SEND=on,
the right sheet to the right inbox, one failure never stops the other, preview to Marc only."""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient


def load_main():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")).read()
    for k in set(re.findall(r'os\.environ\["([A-Z0-9_]+)"\]', src)):
        os.environ.setdefault(k, "test")
    os.environ["SUPABASE_URL"] = "https://x.supabase.co"
    os.environ["SMOKE_ON_BOOT"] = "off"
    os.environ["SMOKE_GATE"] = "off"
    os.environ.setdefault("ANTHROPIC_API_KEY", "test")
    import main
    return main


def setup(monkeypatch, fail_team=None):
    main = load_main()
    sent = []

    def build(team, now):
        if team == fail_team:
            raise RuntimeError("database is down")
        n = 22 if team == "concierge" else 8
        return {"team": team, "subject": f"{team} hot sheet: {n} items", "html": f"<p>{team} HTML</p>",
                "text": f"{team} text", "count": n}

    def resend(payload):
        sent.append(payload)
        return "email-" + str(len(sent))
    monkeypatch.setattr(main, "_hs_build", build)
    monkeypatch.setattr(main, "_hs_resend", resend)
    main._HS_SEND.update({"last": None, "last_preview_at": 0.0})
    return main, sent


def test_off_by_default_sends_nothing(monkeypatch):
    main, sent = setup(monkeypatch)
    monkeypatch.delenv("HOT_SHEET_SEND", raising=False)
    run = asyncio.run(main.job_hot_sheet_send())
    assert sent == [] and run["skipped"], run
    for v in ("", "off", "yes", "true", "ON "):
        monkeypatch.setenv("HOT_SHEET_SEND", v)
        if v.strip().lower() == "on":
            continue
        asyncio.run(main.job_hot_sheet_send())
    assert sent == [], "anything but on (any case, spaces trimmed) leaves it off"
    monkeypatch.setenv("HOT_SHEET_SEND", "off")
    asyncio.run(main.job_hot_sheet_send())
    s = TestClient(main.app).get("/api/hot-sheet/send/status").json()
    assert s["switch"] == "off" and s["last"]["skipped"] and s["schedule"] == "weekdays 8:00 ET", s


def test_on_sends_each_sheet_to_its_team(monkeypatch):
    main, sent = setup(monkeypatch)
    monkeypatch.setenv("HOT_SHEET_SEND", "on")
    run = asyncio.run(main.job_hot_sheet_send())
    assert run["ok"] and len(sent) == 2, run
    by = {p["to"][0]: p for p in sent}
    assert set(by) == {"concierge@fwrdrealestate.com", "transactions@fwrdrealestate.com"}
    assert by["concierge@fwrdrealestate.com"]["html"] == "<p>concierge HTML</p>" and by["concierge@fwrdrealestate.com"]["subject"] == "concierge hot sheet: 22 items"
    assert by["transactions@fwrdrealestate.com"]["html"] == "<p>transactions HTML</p>" and by["transactions@fwrdrealestate.com"]["text"] == "transactions text"
    assert all(len(p["to"]) == 1 and "PREVIEW" not in p["subject"] and p["from"] == main.HOT_SHEET_FROM for p in sent)
    s = TestClient(main.app).get("/api/hot-sheet/send/status").json()
    assert s["switch"] == "on" and [x["count"] for x in s["last"]["sent"]] == [22, 8]
    assert "HTML" not in str(s) and "text" not in str([x for x in s["last"]["sent"]]).replace("'to'", ""), "status never carries a sheet's content"


def test_one_team_failing_still_sends_the_other_and_alerts_marc(monkeypatch):
    main, sent = setup(monkeypatch, fail_team="concierge")
    monkeypatch.setenv("HOT_SHEET_SEND", "on")
    run = asyncio.run(main.job_hot_sheet_send())
    assert not run["ok"] and [e["team"] for e in run["errors"]] == ["concierge"] and "database is down" in run["errors"][0]["error"]
    tos = [p["to"] for p in sent]
    assert ["transactions@fwrdrealestate.com"] in tos, "the other team still got theirs"
    alert = [p for p in sent if p["to"] == [main.HOT_SHEET_MARC]]
    assert len(alert) == 1 and "NOT sent" in alert[0]["subject"] and "concierge" in alert[0]["html"]
    assert ["concierge@fwrdrealestate.com"] not in tos


def test_preview_goes_to_marc_only_and_is_guarded(monkeypatch):
    main, sent = setup(monkeypatch)
    monkeypatch.delenv("HOT_SHEET_SEND", raising=False)   # works with the switch off
    c = TestClient(main.app)
    assert c.post("/api/hot-sheet/preview").status_code == 403 and sent == []
    r = c.post("/api/hot-sheet/preview", headers={"Origin": "https://forward-os.netlify.app"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert len(sent) == 2 and all(p["to"] == ["marc.cashin@corcoranmce.com"] for p in sent)
    assert all(p["subject"].startswith("PREVIEW (only to Marc): ") for p in sent)
    assert {p["html"] for p in sent} == {"<p>concierge HTML</p>", "<p>transactions HTML</p>"}
    r = c.post("/api/hot-sheet/preview", headers={"Origin": "https://forward-os.netlify.app"})
    assert r.status_code == 429 and len(sent) == 2, "once every 2 minutes"
    assert c.get("/api/hot-sheet/send/status").json()["last"] is None, "a preview is not recorded as the morning send"


def test_job_is_weekdays_at_8_eastern(monkeypatch):
    main = load_main()
    added = {}
    monkeypatch.setattr(main.scheduler, "add_job", lambda fn, trig, **k: added.update(fn=fn, trig=str(trig), tz=str(trig.timezone), **k))
    asyncio.run(main.register_hot_sheet_send_job())
    assert added["fn"] is main.job_hot_sheet_send and added["id"] == "hot_sheet_send"
    t = added["trig"]
    assert "day_of_week='mon-fri'" in t and "hour='8'" in t and "minute='0'" in t and added["tz"] == "America/New_York", (t, added["tz"])
