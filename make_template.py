# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf", "pillow", "pillow-heif", "pytesseract"]
# ///
import sys, pathlib, argparse, pymupdf
import deca_check as D


def field_regions(path, verbose=False):
    r = D.analyse(path)
    if r["form"] == "unknown":
        sys.exit(f"form not recognised: {pathlib.Path(path).name}")
    spec = D.FORM_SPECS[r["form"]]
    geom = spec.get("geom", "right")
    sig_labels = list(spec["signatures"].values())
    txt_labels = list(spec["text_fields"].values())

    doc = pymupdf.open(path)
    out = []
    for pno, page in enumerate(doc):
        words = [(w[0], w[1], w[2], w[3], w[4], 100) for w in page.get_text("words")]
        if len(words) < 5 and page.get_images(full=True):
            try:
                words = words + D.ocr_words(page)
            except Exception:
                pass
        if not words:
            continue

        def clear(rect, width, tag, up=3.6, down=1.6):
            h = max(rect.height, 4.0)
            reg = D.regions(rect, page, width, geom, allow_other=False,
                            words=words)[geom]
            reg = pymupdf.Rect(reg.x0, max(0, rect.y0 - up * h),
                               min(page.rect.x1, reg.x1 + 0.5 * h),
                               min(page.rect.y1, rect.y1 + down * h))
            out.append((pno, reg))
            if verbose:
                print(f"    p{pno} {tag:22s} -> [{reg.x0:.0f},{reg.y0:.0f},"
                      f"{reg.x1:.0f},{reg.y1:.0f}]")
            return reg

        for variants in sig_labels:
            _, rect = D.find_label(words, variants)
            if rect is None:
                continue
            clear(rect, 300, variants[0])
            h = max(rect.height, 4.0)
            below = [w for w in words if 0 < (w[1] + w[3]) / 2 - rect.y1 < 4.0 * h]
            _, drect = D.find_label(sorted(below, key=lambda w: w[1]), ["Date"])
            if drect is not None:
                clear(drect, 200, f"Date under {variants[0][:14]}", up=0.35, down=0.9)

        for variants in txt_labels:
            _, rect = D.find_label(words, variants)
            if rect is not None:
                clear(rect, 260, variants[0], up=2.4, down=1.0)


    doc.close()
    return r["form"], out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dest")
    ap.add_argument("--pages", help="keep only these pages, e.g. 5 or 4-5")
    a = ap.parse_args()

    form, regions = field_regions(a.src, verbose=True)
    doc = pymupdf.open(a.src)

    by_page = {}
    for pno, reg in regions:
        by_page.setdefault(pno, []).append(reg)

    for pno, regs in by_page.items():
        page = doc[pno]
        for reg in regs:
            page.add_redact_annot(reg, fill=(1, 1, 1))
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)

    if a.pages:
        lo, _, hi = a.pages.partition("-")
        keep = list(range(int(lo), int(hi or lo) + 1))
        doc.select(keep)

    pathlib.Path(a.dest).parent.mkdir(parents=True, exist_ok=True)
    doc.save(a.dest, garbage=4, deflate=True)
    doc.close()
    print(f"{form}: cleared {len(regions)} field region(s) on "
          f"page(s) {sorted(by_page)} -> {a.dest}")


if __name__ == "__main__":
    main()
