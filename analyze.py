# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf", "pillow", "pillow-heif", "numpy"]
# ///
import sys, pathlib, io, fitz, numpy as np
from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()
Image.MAX_IMAGE_PIXELS = None

SIG_WORDS = ["signature", "sign", "signed", "date", "parent", "guardian",
             "student", "print name", "printed name", "witness", "initial"]
IMG_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"}


def ink_profile(pil):
    im = pil.convert("RGB")
    im.thumbnail((900, 900))
    a = np.asarray(im).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(2), a.min(2)
    sat = mx - mn
    dark = mx < 160
    colored = (sat > 45) & (mx > 60)
    blue = colored & (b > r + 25) & (b > g + 15)
    total = a.shape[0] * a.shape[1]
    return {
        "dark_frac": round(float(dark.sum()) / total, 4),
        "colored_frac": round(float(colored.sum()) / total, 4),
        "blue_ink_frac": round(float(blue.sum()) / total, 5),
        "px": f"{pil.width}x{pil.height}",
    }


def page_report(page, pno):
    text = page.get_text().strip()
    widgets = list(page.widgets())
    annots = list(page.annots())
    ink_annots = [a for a in annots if a.type[0] == fitz.PDF_ANNOT_INK]
    free_text = [a for a in annots if a.type[0] == fitz.PDF_ANNOT_FREE_TEXT]
    filled = [w for w in widgets if str(w.field_value or "").strip()]

    img_area = sum(abs(fitz.Rect(i["bbox"]).get_area()) for i in page.get_image_info())
    cover = img_area / max(abs(page.rect.get_area()), 1)

    segs = squiggle = long_flat = 0
    for d in page.get_drawings():
        if d["type"] not in ("s", "fs"):
            continue
        items = d["items"]
        segs += len(items)
        r = fitz.Rect(d["rect"])
        if r.height < 2 and r.width > 40:
            long_flat += 1
        elif len(items) >= 6 and r.height > 5:
            squiggle += 1

    kind = "PHOTO_PAGE" if (len(text) < 50 and cover > 0.6) else "TEXT_LAYER"
    print(f"  page {pno}: {kind} {page.rect.width:.0f}x{page.rect.height:.0f} "
          f"text={len(text)}ch img_cover={cover:.2f}")
    print(f"    widgets={len(widgets)} (filled={len(filled)})  ink_annots={len(ink_annots)} "
          f"freetext={len(free_text)}  paths: segs={segs} squiggle={squiggle} rules={long_flat}")

    for w in filled:
        print(f"    FILLED  {str(w.field_name)[:34]!r} = {str(w.field_value)[:44]!r}")
    for a in ink_annots:
        v = a.vertices or []
        pts = sum(len(s) for s in v) if v and isinstance(v[0], (list, tuple)) else len(v)
        print(f"    INK     rect={[round(x) for x in a.rect]} strokes={len(v)} pts={pts}")
    for a in free_text:
        print(f"    FREETXT rect={[round(x) for x in a.rect]} {a.info.get('content','')[:50]!r}")

    if kind == "TEXT_LAYER":
        hits = []
        for w in SIG_WORDS:
            for r in page.search_for(w):
                hits.append((w, [round(c) for c in r]))
        if hits:
            print(f"    anchors: {hits[:14]}")
    else:
        pix = page.get_pixmap(dpi=110)
        prof = ink_profile(Image.open(io.BytesIO(pix.tobytes("png"))))
        print(f"    render: {prof}")
    return kind


def do_pdf(p):
    doc = fitz.open(p)
    print(f"\n{'='*74}\n{p.name}  PDF pages={doc.page_count} acroform={doc.is_form_pdf}")
    for i, page in enumerate(doc):
        page_report(page, i)
    doc.close()


def do_image(p):
    im = Image.open(p)
    print(f"\n{'='*74}\n{p.name}  IMAGE {im.format} {im.width}x{im.height}")
    print(f"    {ink_profile(im)}")
    print("    no text layer -> needs OCR or vision to locate signature region")


def main(args):
    files = []
    for a in args:
        p = pathlib.Path(a).expanduser()
        files.extend(sorted(p.iterdir()) if p.is_dir() else [p])
    for p in files:
        if not p.is_file():
            continue
        try:
            if p.suffix.lower() == ".pdf":
                do_pdf(p)
            elif p.suffix.lower() in IMG_EXT:
                do_image(p)
        except Exception as e:
            print(f"\n!! {p.name}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main(sys.argv[1:] or [str(pathlib.Path.home() / "Downloads")])
