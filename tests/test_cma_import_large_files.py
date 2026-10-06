"""The CMA Builder's sheet reader on files that hold many properties.

Runs without the AI service: claude_post is replaced by a stand-in that answers
from the text it is sent. Run: python -m pytest tests/test_cma_import_large_files.py

Real sheets are not kept in the repo (they carry agent remarks), so the pages
here are built in the shape pypdf gives for a Bright "Agent Full" export, which
was read from two real sheets on Oct 6, 2026: the first page of a property has
one line starting "MLS #:", continuation pages have none, and the address line
is the first line under the "Agent Full" title.
"""
import asyncio, io, json, os, re, sys, types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cma_split


def first_page(n, closed=False):
    head = f"{n} Test St, Baltimore, MD 21204 " + ("Closed | 09/22/26 Residential" if closed else "Active Residential $500,000")
    lines = ["Agent Full", head]
    if closed:
        lines += ["  $2,000,000", "Recent Change: 09/25/2026 : Closed : PND->CLS    "]
    lines += [f"MLS #: MDBC{n:07d}", "Tax ID #: 1", "Beds: 3", "© BRIGHT MLS"] + ["filler line of sheet text"] * 12
    return "\n".join(lines)


def more_page(n):
    return "\n".join(["Remarks", f"Public: remarks for property {n}", "© BRIGHT MLS"] + ["remark filler"] * 10)


def export(count, closed_at=()):
    pages = []
    for n in range(1, count + 1):
        pages += [first_page(n, n in closed_at), more_page(n)]
    return pages


# ── the splitter ──
def test_sixteen_properties_split_into_sixteen():
    out = cma_split.split_listings(export(16, closed_at=(3,)))
    assert len(out) == 16
    assert out[0]["label"].startswith("1 Test St, Baltimore")
    assert out[2]["label"].startswith("3 Test St, Baltimore"), "a closed sale's label is its address line, not 'Recent Change'"
    assert "remarks for property 16" in out[15]["text"] and "remarks for property 15" not in out[15]["text"]


def test_no_text_is_lost_or_moved():
    pages = export(7)
    out = cma_split.split_listings(pages)
    assert "\n".join(l["text"] for l in out) == "\n".join(pages)


def test_pages_before_the_first_property_stay_with_it():
    out = cma_split.split_listings(["Cover page"] + export(2))
    assert len(out) == 2 and out[0]["text"].startswith("Cover page")


def test_cannot_split_means_none():
    assert cma_split.split_listings(["no marker here", "nor here"]) is None
    assert cma_split.split_listings([first_page(1) + "\n" + first_page(2)]) is None
    assert cma_split.split_listings([]) is None


def test_chunks_of_four():
    parts = cma_split.chunk(cma_split.split_listings(export(16)))
    assert [len(p) for p in parts] == [4, 4, 4, 4]
    assert [len(p) for p in cma_split.chunk(list(range(9)))] == [4, 4, 1]


# ── the endpoint, with the AI service replaced ──
def load_main():
    # main.py reads its settings at import. Made-up values: nothing here reaches a real service.
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")).read()
    for k in set(re.findall(r'os\.environ\["([A-Z0-9_]+)"\]', src)):
        os.environ.setdefault(k, "test")
    os.environ["SUPABASE_URL"] = "https://x.supabase.co"
    os.environ["SMOKE_ON_BOOT"] = "off"
    os.environ["SMOKE_GATE"] = "off"
    os.environ.setdefault("ANTHROPIC_API_KEY", "test")
    import main
    return main


class Upload:
    def __init__(self, name, data):
        self.filename, self.content_type, self._d = name, "application/pdf", data
    async def read(self):
        return self._d


def run_endpoint(monkeypatch, pages, fail_when=None, slow=False):
    main = load_main()
    import httpx, pypdf
    calls = []
    run_endpoint.sent = []

    class FakeReader:
        def __init__(self, _):
            self.pages = [types.SimpleNamespace(extract_text=lambda t=t: t) for t in pages]
    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)

    async def fake_post(headers, body, timeout=120, where=""):
        text = body["messages"][0]["content"]
        assert text.endswith(main.CMA_PARSE_PROMPT), "the prompt is sent unchanged"
        nums = re.findall(r"MLS #: MDBC(\d+)", text)
        calls.append(nums)
        run_endpoint.sent.append(text)
        if slow:
            raise httpx.ReadTimeout("slow")
        if fail_when and fail_when(nums):
            raise httpx.ReadTimeout("slow")
        out = {"listings": [{"mlsNumber": "MDBC" + n, "address": f"{int(n)} Test St"} for n in nums]}
        return types.SimpleNamespace(json=lambda: {"content": [{"type": "text", "text": json.dumps(out)}], "stop_reason": "end_turn"})
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "test", raising=False)
    monkeypatch.setattr(main, "claude_post", fake_post)
    res = asyncio.run(main.cma_parse_listings([Upload("Agent_Full3535.pdf", b"%PDF-1.4 stand-in")]))
    return res["results"], calls


def test_sixteen_properties_are_all_read_four_at_a_time(monkeypatch):
    results, calls = run_endpoint(monkeypatch, export(16))
    assert sorted(len(c) for c in calls) == [4, 4, 4, 4], "no request holds more than four properties"
    ok = [r for r in results if r["ok"]]
    assert [r["address"] for r in ok] == [f"{n} Test St" for n in range(1, 17)], "all sixteen, in the file's order"
    assert all(r["file"] == "Agent_Full3535.pdf" and "flags" in r for r in ok)
    assert not [r for r in results if not r["ok"]]


def test_a_small_file_is_one_request_with_the_same_text_as_before(monkeypatch):
    pages = export(4)
    main = load_main()
    results, calls = run_endpoint(monkeypatch, pages)
    assert len(calls) == 1 and len(calls[0]) == 4
    assert len([r for r in results if r["ok"]]) == 4
    joined = "\n".join(pages)
    assert run_endpoint.sent == [f"MLS LISTING SHEET:\n\n{joined}\n\n---\n\n{main.CMA_PARSE_PROMPT}"], "byte for byte what the reader sent before Oct 6, 2026"


def test_one_part_failing_keeps_the_rest_and_names_what_is_missing(monkeypatch):
    results, calls = run_endpoint(monkeypatch, export(16), fail_when=lambda nums: "0000006" in nums)
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    assert len(ok) == 12 and len(bad) == 1
    assert "4 of the 16 properties" in bad[0]["error"]
    for n in (5, 6, 7, 8):
        assert f"{n} Test St, Baltimore" in bad[0]["error"]
    assert "1 Test St" not in bad[0]["error"]


def test_a_single_slow_file_says_it_took_too_long(monkeypatch):
    results, calls = run_endpoint(monkeypatch, export(2), slow=True)
    assert len(results) == 1 and not results[0]["ok"]
    assert "took too long" in results[0]["error"] and "smaller files" in results[0]["error"]


def test_more_than_thirty_is_said_out_loud(monkeypatch):
    results, calls = run_endpoint(monkeypatch, export(33))
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    assert len(ok) == 30 and len(bad) == 1
    assert "first 30 were read and the last 3 were not" in bad[0]["error"]


def test_a_reply_that_leaves_a_property_out_is_reported(monkeypatch):
    main = load_main()
    pages = export(8)
    results, calls = run_endpoint(monkeypatch, pages)
    assert len([r for r in results if r["ok"]]) == 8
    # now the stand-in drops one property from every answer
    import httpx, pypdf
    async def short_post(headers, body, timeout=120, where=""):
        nums = re.findall(r"MLS #: MDBC(\d+)", body["messages"][0]["content"])[:-1]
        out = {"listings": [{"address": f"{int(n)} Test St"} for n in nums]}
        return types.SimpleNamespace(json=lambda: {"content": [{"type": "text", "text": json.dumps(out)}], "stop_reason": "end_turn"})
    monkeypatch.setattr(main, "claude_post", short_post)
    res = asyncio.run(main.cma_parse_listings([Upload("x.pdf", b"%PDF")]))["results"]
    bad = [r for r in res if not r["ok"]]
    assert len(bad) == 1 and "holds 8 properties but only 6 were read" in bad[0]["error"]
