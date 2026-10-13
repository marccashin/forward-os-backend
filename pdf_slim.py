"""Make a full-resolution Canva PDF the size of Marc's own exports.

Why (Marc, Oct 13, 2026): Canva's API offers two PDF qualities. "regular" caps
every image at 800 px on its long side and the decks looked pixelated.
"pro" keeps every image as uploaded: 96 to 113 MB per deck, too big to store
or to download on a phone. Marc's own Canva download (Digital, Compress PDF
off) of Cesar's deck is 36 MB with photos up to 3,107 px on the long side
(about 130 to 315 ppi on the page).

So: take the pro PDF and re-save ONLY the photographs:
  - long side capped at MAX_SIDE px (never upscaled),
  - JPEG at JPEG_QUALITY,
  - kept only when it is actually smaller than what was there.
Never touched: transparency masks, any image that HAS a mask or colour key
(/SMask, /Mask), images with a decode array, anything that is not 8 bits per channel RGB or grey,
small images (logos, icons), text and vector art. Same page count, same text.

slim() returns the new bytes and a report; a PDF it cannot read comes back
unchanged with the reason, so a slimming problem never loses a deck.
"""
from __future__ import annotations

import io

MAX_SIDE = 3000          # Marc's export: largest photo 3,107 px
JPEG_QUALITY = 88
MIN_SIDE_TO_TOUCH = 1200  # below this an image is left alone
MIN_SAVING = 0.10         # re-encode only if it saves at least 10%


def _is_mask(stream) -> bool:
    return bool(stream.get("/ImageMask", False))


def slim(data: bytes, max_side: int = MAX_SIDE) -> tuple[bytes, dict]:
    import pikepdf
    from PIL import Image
    from pikepdf import Name, PdfImage

    report = {"bytes_in": len(data), "max_side": max_side, "images_seen": 0, "images_resaved": 0,
              "largest_side_out": 0, "skipped": {}, "error": None}

    def skip(why):
        report["skipped"][why] = report["skipped"].get(why, 0) + 1

    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except Exception as e:
        report["error"] = f"could not open the PDF: {e}"
        report["bytes_out"] = len(data)
        return data, report

    # Every object used as someone's SMask or Mask: never re-encode those.
    masks = set()
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == Name.Image:
            for key in ("/SMask", "/Mask"):
                m = obj.get(key)
                if isinstance(m, pikepdf.Stream):
                    masks.add(m.objgen)

    for obj in list(pdf.objects):
        if not (isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == Name.Image):
            continue
        report["images_seen"] += 1
        if obj.objgen in masks or _is_mask(obj):
            skip("mask"); continue
        if "/SMask" in obj or "/Mask" in obj:
            # Has transparency. Newer pikepdf returns these as RGBA, older as
            # plain RGB without the mask; never re-encode either way.
            skip("has transparency"); continue
        if "/Decode" in obj:
            skip("decode array"); continue
        if int(obj.get("/BitsPerComponent", 8)) != 8:
            skip("not 8 bit"); continue
        w, h = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
        if max(w, h) < MIN_SIDE_TO_TOUCH:
            report["largest_side_out"] = max(report["largest_side_out"], max(w, h))
            skip("small"); continue
        try:
            pil = PdfImage(obj).as_pil_image()
        except Exception:
            skip("unreadable"); continue
        if pil.mode not in ("RGB", "L"):
            skip("colour mode " + pil.mode); continue
        before = len(obj.read_raw_bytes())
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            pil = pil.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True, subsampling=0)
        new = buf.getvalue()
        if scale == 1.0 and len(new) > before * (1 - MIN_SAVING):
            report["largest_side_out"] = max(report["largest_side_out"], max(w, h))
            skip("already compact"); continue
        cs = obj.get("/ColorSpace")
        obj.write(new, filter=Name.DCTDecode)
        obj.Width, obj.Height = pil.width, pil.height
        obj.BitsPerComponent = 8
        # ICC-based colour spaces are kept (3 or 1 components match JPEG);
        # anything else becomes the plain device space of the JPEG.
        if not (isinstance(cs, pikepdf.Array) and len(cs) and cs[0] == Name.ICCBased):
            obj.ColorSpace = Name.DeviceRGB if pil.mode == "RGB" else Name.DeviceGray
        for k in ("/DecodeParms",):
            if k in obj:
                del obj[k]
        report["images_resaved"] += 1
        report["largest_side_out"] = max(report["largest_side_out"], pil.width, pil.height)

    out = io.BytesIO()
    pdf.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    result = out.getvalue()
    report["bytes_out"] = len(result)
    return result, report


STEP_DOWN = (MAX_SIDE, 2400, 2000)   # tried in turn only if a deck is too big


def slim_to_fit(data: bytes, limit: int) -> tuple[bytes, dict]:
    """slim() at MAX_SIDE; if the result is still over `limit` bytes, again
    from the ORIGINAL at each smaller cap. Returns the first that fits, or
    the smallest with report["fits"] False (the caller refuses to store it)."""
    best = None
    for side in STEP_DOWN:
        out, rep = slim(data, side)
        if rep["error"]:
            rep["fits"] = False
            return out, rep
        rep["fits"] = len(out) <= limit
        if rep["fits"]:
            return out, rep
        if best is None or len(out) < len(best[0]):
            best = (out, rep)
    return best
