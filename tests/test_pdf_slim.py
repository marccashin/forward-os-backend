"""pdf_slim: photos re-saved, everything else left exactly as it was.

PDFs are built here with real pikepdf and read back with real pikepdf, Pillow
and pypdf. Run: python -m pytest tests/test_pdf_slim.py
"""
import io, math, os, random, sys, zlib

import pikepdf
import pytest
from PIL import Image, ImageChops
from pikepdf import Name, PdfImage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdf_slim
import test_canva_link as T
import canva_link as cl


def photo(w, h, seed=1):
    """A photo-like image: smooth gradients plus mild noise (not flat colour,
    which JPEG would flatter)."""
    rnd = random.Random(seed)
    small = Image.new("RGB", (64, 48))
    small.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(64 * 48)])
    img = small.resize((w, h), Image.BICUBIC)
    noise = Image.effect_noise((w, h), 6).convert("RGB")
    return ImageChops.add(img, noise, scale=2.0)


def raw_image(pdf, img, **extra):
    """An uncompressed-style (Flate) image XObject, the way Canva's pro export holds photos."""
    mode_cs = {"RGB": Name.DeviceRGB, "L": Name.DeviceGray}[img.mode]
    s = pikepdf.Stream(pdf, zlib.compress(img.tobytes(), 6))
    s.Type, s.Subtype = Name.XObject, Name.Image
    s.Width, s.Height, s.BitsPerComponent = img.width, img.height, 8
    s.ColorSpace, s.Filter = mode_cs, Name.FlateDecode
    for k, v in extra.items():
        s["/" + k] = v
    return s


def build(pdf_bytes=None, lines=("Hello", "World")):
    """A real PDF with text (owner text when given) and five kinds of image on page 1."""
    pdf = pikepdf.open(io.BytesIO(pdf_bytes or T.tiny_pdf(list(lines))))
    page = pdf.pages[0]
    imgs = {
        "big": raw_image(pdf, photo(4200, 2800, 1)),
        "mid": raw_image(pdf, photo(2000, 1500, 2)),
        "logo": raw_image(pdf, photo(600, 300, 3)),
        "grey": raw_image(pdf, photo(3600, 2400, 4).convert("L")),
    }
    mask = raw_image(pdf, photo(3600, 2400, 5).convert("L"))
    imgs["clear"] = raw_image(pdf, photo(3600, 2400, 6), SMask=mask)
    res = page.obj.get("/Resources") or pikepdf.Dictionary()
    xo = pikepdf.Dictionary({"/" + k: v for k, v in imgs.items()})
    res[Name.XObject] = xo
    page.obj.Resources = res
    draw = b"".join(b"q 100 0 0 60 %d 10 cm /%s Do Q\n" % (10 + 110 * i, k.encode()) for i, k in enumerate(imgs))
    page.contents_add(pikepdf.Stream(pdf, draw))
    out = io.BytesIO(); pdf.save(out)
    return out.getvalue()


def images(data):
    pdf = pikepdf.open(io.BytesIO(data))
    xo = pdf.pages[0].obj.Resources.XObject
    return pdf, {k[1:]: xo[k] for k in xo.keys()}


def psnr(a, b):
    d = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).convert("L")
    h = d.histogram()
    mse = sum(v * v * n for v, n in enumerate(h)) / (a.width * a.height)
    return 99.0 if mse == 0 else 10 * math.log10(255 * 255 / mse)


@pytest.fixture(scope="module")
def before():
    return build()


@pytest.fixture(scope="module")
def after(before):
    return pdf_slim.slim(before)


def test_smaller_and_reported(before, after):
    out, rep = after
    assert rep["error"] is None
    assert rep["bytes_in"] == len(before) and rep["bytes_out"] == len(out)
    assert len(out) < len(before) * 0.6
    assert rep["images_resaved"] == 3          # big, mid, grey
    assert rep["largest_side_out"] == 3000


def test_photos_capped_never_upscaled_and_still_look_the_same(before, after):
    pa, a = images(before)
    pb, b = images(after[0])
    big = b["big"]
    assert (int(big.Width), int(big.Height)) == (3000, 2000)        # 4200x2800, ratio kept
    assert big.Filter == Name.DCTDecode and big.ColorSpace == Name.DeviceRGB
    assert "/DecodeParms" not in big
    assert (int(b["mid"].Width), int(b["mid"].Height)) == (2000, 1500)   # under the cap: same size
    assert b["grey"].ColorSpace == Name.DeviceGray and int(b["grey"].Width) == 3000
    # Look: compare at the new size against an independent resize of the original.
    orig = PdfImage(a["big"]).as_pil_image().resize((3000, 2000), Image.LANCZOS)
    assert psnr(orig, PdfImage(big).as_pil_image()) > 35
    assert psnr(PdfImage(a["mid"]).as_pil_image(), PdfImage(b["mid"]).as_pil_image()) > 35


def test_logos_and_transparent_images_and_masks_are_byte_identical(before, after):
    pa, a = images(before)
    pb, b = images(after[0])
    for k in ("logo", "clear"):
        assert b[k].read_raw_bytes() == a[k].read_raw_bytes(), k
        assert (int(b[k].Width), b[k].Filter) == (int(a[k].Width), a[k].Filter), k
    assert b["clear"].SMask.read_raw_bytes() == a["clear"].SMask.read_raw_bytes()
    assert int(b["clear"].SMask.Width) == 3600


def test_same_pages_and_text(before, after):
    fa, fb = cl.pdf_facts(before), cl.pdf_facts(after[0])
    assert fa["pages"] == fb["pages"] and fa["text"] == fb["text"] and "Hello" in fb["text"]


def test_owner_text_survives_on_a_real_deck():
    owner = "Niki Lang"
    data = build(T.tiny_pdf(T.deck_text(owner).split("\n")))
    out, rep = pdf_slim.slim(data)
    assert rep["images_resaved"] == 3
    assert cl.check_owner(owner, cl.pdf_facts(out)["text"])[0]


def test_a_pdf_it_cannot_read_comes_back_unchanged_with_the_reason():
    junk = b"%PDF-1.7 this is not really a pdf"
    out, rep = pdf_slim.slim(junk)
    assert out == junk and rep["error"] and rep["bytes_out"] == len(junk)


def test_already_compact_jpeg_is_left_alone():
    pdf = pikepdf.open(io.BytesIO(T.tiny_pdf(["x"])))
    buf = io.BytesIO(); photo(2000, 1400, 9).save(buf, "JPEG", quality=60)
    s = pikepdf.Stream(pdf, buf.getvalue())
    s.Type, s.Subtype, s.Width, s.Height = Name.XObject, Name.Image, 2000, 1400
    s.BitsPerComponent, s.ColorSpace, s.Filter = 8, Name.DeviceRGB, Name.DCTDecode
    pdf.pages[0].obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(P=s))
    o = io.BytesIO(); pdf.save(o)
    out, rep = pdf_slim.slim(o.getvalue())
    pb, b = images(out)
    assert rep["images_resaved"] == 0 and rep["skipped"].get("already compact") == 1
    assert b["P"].read_raw_bytes() == buf.getvalue()


def test_slim_to_fit_steps_down_only_when_needed(before):
    out, rep = pdf_slim.slim_to_fit(before, 50 * 1024 * 1024)
    assert rep["fits"] and rep["max_side"] == 3000
    first = len(pdf_slim.slim(before, 3000)[0])
    out, rep = pdf_slim.slim_to_fit(before, first - 1)
    assert rep["max_side"] in (2400, 2000) and len(out) < first
    pb, b = images(out)
    assert max(int(b["big"].Width), int(b["big"].Height)) == rep["max_side"]


def test_slim_to_fit_says_when_nothing_fits(before):
    out, rep = pdf_slim.slim_to_fit(before, 1000)
    assert rep["fits"] is False and len(out) > 1000
