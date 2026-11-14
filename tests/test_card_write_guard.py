"""Changing or deleting a listing or buyer card works only from FORWARD OS pages (Oct 17, 2026)."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OS = {"origin": "https://forward-os.netlify.app"}
STAGING = {"origin": "https://forward-os-staging.netlify.app"}
PREVIEW = {"origin": "https://deploy-preview-214--forward-os-staging.netlify.app"}
EVIL = {"origin": "https://forward-os.netlify.app.evil.example"}


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


class Q:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def __getattr__(self, op):
        def f(*a, **k):
            self.log.append((self.name, op))
            return self
        return f

    def execute(self):
        self.log.append((self.name, "execute"))
        return type("R", (), {"data": [{"id": "x", "address": "1 Test St", "agent_name": "Marc Cashin", "subfolder_drive_ids": {}}]})


class SB:
    def __init__(self):
        self.log = []

    def table(self, name):
        return Q(self.log, name)


ID = "11111111-1111-4111-8111-111111111111"
CALLS = [
    ("patch", f"/properties/{ID}", {"fields": {"address": "1 Test St"}}),
    ("delete", f"/properties/{ID}", None),
    ("post", f"/properties/{ID}/add-partner", {"agent_name": "Niki Lang"}),
    ("post", f"/properties/{ID}/remove-partner", {"agent_name": "Niki Lang"}),
    ("patch", f"/buyers/{ID}", {"fields": {"buyer_name": "A Buyer"}}),
    ("delete", f"/buyers/{ID}", None),
    ("post", f"/buyers/{ID}/add-partner", {"agent_name": "Niki Lang"}),
    ("post", f"/buyers/{ID}/remove-partner", {"agent_name": "Niki Lang"}),
]


def call(c, method, path, body, headers):
    if body is None:
        return getattr(c, method)(path, headers=headers)
    return getattr(c, method)(path, json=body, headers=headers)


def test_card_writes_need_forward_os(monkeypatch):
    from fastapi.testclient import TestClient
    main = load_main()
    sb = SB()
    monkeypatch.setattr(main, "supabase", sb)
    c = TestClient(main.app)
    for method, path, body in CALLS:
        for h in ({}, EVIL):
            sb.log.clear()
            r = call(c, method, path, body, h)
            assert r.status_code == 403, (method, path, h, r.status_code, r.text)
            assert "only from FORWARD OS" in r.json()["detail"]
            assert sb.log == [], ("nothing read or written when refused", method, path, sb.log)
        for h in (OS, STAGING, PREVIEW):
            sb.log.clear()
            r = call(c, method, path, body, h)
            assert r.status_code != 403, (method, path, h, r.status_code, r.text)
            assert sb.log, ("a FORWARD OS page still reaches the database", method, path)
