# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf", "pillow", "pillow-heif", "pytesseract", "numpy"]
# ///
import pymupdf, pathlib, sys, re, csv, io, json, argparse, shutil
from collections import Counter
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC = True
except Exception:
    HEIC = False

INK_MIN = 5
TEXT_INK_MIN = 25
INK_MAYBE = 1
OCR_DPI = 150
OCR_IF_TEXT_UNDER = 320
JUNK_MAX_CHARS = 24
CONF_LABEL = 0
CONF_CONTENT = 60
DARK_UNVERIFIED = 0.05
TEXT_DARK_UNVERIFIED = 0.03
INVISIBLE = dict.fromkeys(map(ord, "​‌‍﻿\xa0"), None)
IMG_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp"}

FORM_SPECS = {
    "contract": {
        "geom": "right",
        "fingerprints": ["Parent-Student Contract", "NAME OF PARENT", "NAME OF STUDENT",
                         "Signature of PARENT", "Signature of STUDENT", "PROGRAM FEES"],
        "filename_hints": ["parent-student", "parent student", "contract"],
        "signatures": {
            "parent_sig": ["Signature of PARENT", "Signature of Parent"],
            "student_sig": ["Signature of STUDENT", "Signature of Student"],
        },
        "text_fields": {
            "parent_name": ["NAME OF PARENT", "Name of Parent"],
            "student_name": ["NAME OF STUDENT", "Name of Student"],
        },
    },
    "conduct": {
        "geom": "above",
        "fingerprints": ["Trip Code of Conduct", "Student Overnight Trip Expectations",
                         "Parent Printed Name", "Student Printed Name", "Overnight Trip"],
        "filename_hints": ["code of conduct", "conduct"],
        "signatures": {
            "parent_sig": ["Parent Signature"],
            "student_sig": ["Student Signature"],
        },
        "text_fields": {
            "parent_name": ["Parent Printed Name"],
            "student_name": ["Student Printed Name"],
        },
    },
    "formB": {
        "geom": "right",
        "fingerprints": ["CALIFORNIA DECA DELEGATE PERMISSION",
                         "Please remember to fill out sections boxed in red",
                         "Release of Claim for Damages", "MEDICAL INFORMATION",
                         "INSURANCE INFORMATION", "Name of Delegate",
                         "Parent/Guardian Signature", "Emergency Medical Treatment",
                         "Date of last tetanus shot"],
        "filename_hints": ["form_b", "form b", "formb", "medical release"],
        "needs_ocr": True,
        "soft_fields": {"tetanus"},
        "signatures": {
            "student_sig": ["Student Signature"],
            "parent_sig": ["Parent/Guardian Signature", "Parent Guardian Signature"],
        },
        "text_fields": {
            "delegate_name": ["Name of Delegate"],
            "home_address": ["Home Address"],
            "dob": ["Date of Birth"],
            "high_school": ["Name of High School"],
            "allergies": ["Known allergies (drug or natural)", "Known allergies"],
            "medication": ["Special medication being taken"],
            "tetanus": ["Date of last tetanus shot"],
            "heart_condition": ["History of heart condition, diabetes, asthma, epilepsy, or rheumatic fever", "History of heart condition"],
            "physical_restrictions": ["Any physical restrictions"],
            "other_conditions": ["Other conditions"],
            "family_doctor": ["Family doctor"],
            "doctor_phone": ["Family doctor|Phone"],
            "insurance_company": ["Company Name"],
            "policy_number": ["Policy Number"],
        },
    },
}


def clean(s):
    return re.sub(r"[_\s]+", " ", str(s).translate(INVISIBLE)).strip(" _:()*")


def open_doc(path):
    p = pathlib.Path(path)
    if p.suffix.lower() in {".heic", ".heif"}:
        if not HEIC:
            raise RuntimeError("pillow-heif unavailable")
        buf = io.BytesIO()
        Image.open(p).convert("RGB").save(buf, format="PNG")
        return pymupdf.open(stream=buf.getvalue(), filetype="png"), "image"
    if p.suffix.lower() in IMG_EXT:
        return pymupdf.open(p), "image"
    return pymupdf.open(p), "pdf"


STOP_WORDS = {"date", "phone", "policy", "number", "company",
              "birth", "advisor", "advisors"}


SUFFIX_WORDS = {"printed", "printea", "print", "name", "s", ""}


def right_span(rect, page, words, width):
    h = max(rect.height, 4.0)
    ymid = (rect.y0 + rect.y1) / 2
    line = sorted((w for w in words
                   if abs((w[1] + w[3]) / 2 - ymid) <= 0.65 * h and w[2] > rect.x1 - 1),
                  key=lambda w: w[0])
    start, i = rect.x1, 0
    while i < len(line) and line[i][0] < start + 1.1 * h:
        t = str(line[i][4]).strip()
        c = clean(t).lower()
        if not c:
            i += 1
            continue
        if c in SUFFIX_WORDS or (t.endswith(":") and len(c) <= 8):
            start = max(start, line[i][2])
            i += 1
            continue
        break
    stops = [w[0] for w in line[i:]
             if w[0] > start + 0.7 * h
             and (clean(str(w[4])).lower() in STOP_WORDS or str(w[4]).strip().endswith(":"))]
    limit = min(stops) - 0.3 * h if stops else start + width
    return start, min(page.rect.x1, start + width, max(start + 3.2 * h, limit))


DATE_EXTRA_UP = 1.5


def regions(rect, page, width=300, prefer="right", allow_other=True, tight=False,
            words=(), extra_up=0.0, right_up=None, right_down=None):
    h = max(rect.height, 4.0)
    width = width / 11.0 * h
    up = ((2.0 if tight else 3.1) + extra_up) * h
    rx0, rx1 = right_span(rect, page, words, width)
    if right_up is None:
        right_up = 1.1 if tight else 2.4
    if right_down is None:
        right_down = 0.2 if tight else 0.7
    r = {
        "right": pymupdf.Rect(rx0, max(0, rect.y0 - right_up * h), rx1,
                              rect.y1 + right_down * h),
        "above": pymupdf.Rect(max(0, rect.x0 - 0.9 * h), max(0, rect.y0 - up),
                              min(page.rect.x1, rect.x0 + width), rect.y0 - 0.2 * h),
    }
    order = [prefer] + ([g for g in r if g != prefer] if allow_other else [])
    return {g: r[g] for g in order}


def luminance(c):
    if not c:
        return None
    try:
        r, g, b = (float(x) for x in c[:3])
    except (TypeError, ValueError):
        return None
    return 0.299 * r + 0.587 * g + 0.114 * b


VISIBLE_MAX_LUM = 0.75


def is_visible(d):
    lums = [l for l in (luminance(d.get("color")), luminance(d.get("fill")))
            if l is not None]
    if not lums:
        return True
    return min(lums) <= VISIBLE_MAX_LUM


def is_rule(d):
    r = pymupdf.Rect(d["rect"])
    return r.height <= 1.2 and r.width >= 8


def paintable(drawings):
    covers = [(i, pymupdf.Rect(d["rect"])) for i, d in enumerate(drawings)
              if not is_visible(d) and not is_rule(d)]
    out = []
    for i, d in enumerate(drawings):
        if not is_visible(d) or is_rule(d):
            continue
        r = pymupdf.Rect(d["rect"])
        if any(j > i and c.contains(r) for j, c in covers):
            continue
        out.append(d)
    return out


def ink_in(region, drawings):
    n = 0
    for d in paintable(drawings):
        for it in d["items"]:
            if it[0] == "re":
                continue
            if any(p in region for p in (x for x in it[1:] if isinstance(x, pymupdf.Point))):
                n += 1
    return n


def text_in(region, words, drop, min_conf=CONF_CONTENT):
    out = []
    for x0, y0, x1, y1, w, conf, *_ in words:
        if conf < min_conf:
            continue
        if pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2) in region:
            c = clean(w)
            if c and c.lower() not in drop:
                out.append(c)
    return clean(" ".join(out))


DROP = {w.lower() for w in ("signature signatures of parent student name printed date "
                            "delegate delegates home address birth high school phone "
                            "advisor advisors charge guardian in the for and").split()}


INVERT_BELOW = 110


def render(page):
    pix = page.get_pixmap(dpi=OCR_DPI)
    from PIL import Image as PILImage, ImageOps
    img = PILImage.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    hist = img.convert("L").histogram()
    half, acc, median = sum(hist) / 2, 0, 255
    for v, n in enumerate(hist):
        acc += n
        if acc >= half:
            median = v
            break
    if median < INVERT_BELOW:
        img = ImageOps.invert(img)
    return img


NONE_WORD = re.compile(r"(?i)n[\W_il1|]{0,2}a|none|no|nil")
RULE_RUN_PX = 14
RULE_HALF_THICK = 3
CROP_OCR_MIN_DARK = 0.003


def region_crop(img, region, pad=0, words=()):
    import numpy as np
    scale = OCR_DPI / 72.0
    box = (max(0, int(region.x0 * scale) - pad), max(0, int(region.y0 * scale) - pad),
           min(img.width, int(region.x1 * scale) + pad), min(img.height, int(region.y1 * scale) + pad))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return None
    g = np.asarray(img.crop(box).convert("L")).copy()
    for x0, y0, x1, y1, w, conf, *_ in words:
        if conf < CONF_CONTENT or not re.search(r"[A-Za-z]{2}", str(w)):
            continue
        px0, py0 = int(x0 * scale) - box[0] - 1, int(y0 * scale) - box[1] - 1
        px1, py1 = int(x1 * scale) - box[0] + 1, int(y1 * scale) - box[1] + 1
        if px1 <= 0 or py1 <= 0 or px0 >= g.shape[1] or py0 >= g.shape[0]:
            continue
        g[max(0, py0):py1, max(0, px0):px1] = 255
    dark = g < 165
    if dark.shape[1] > RULE_RUN_PX and dark.shape[0] > 2 * RULE_HALF_THICK:
        win = np.lib.stride_tricks.sliding_window_view(dark, RULE_RUN_PX, axis=1).all(axis=2)
        longrun = np.zeros_like(dark)
        for k in range(RULE_RUN_PX):
            longrun[:, k:k + win.shape[1]] |= win
        t = RULE_HALF_THICK
        thick = np.zeros_like(dark)
        thick[t:-t] = dark[t:-t] & dark[:-2 * t] & dark[2 * t:]
        g[longrun & ~thick] = 255
    return g


def dark_frac(img, region, words=()):
    g = region_crop(img, region, words=words)
    if g is None:
        return 0.0
    return float((g < 165).sum()) / g.size


def crop_ocr(img, region, drop, words=()):
    import pytesseract
    from PIL import Image as PILImage
    g = region_crop(img, region, pad=int(3 * OCR_DPI / 72.0), words=words)
    if g is None:
        return ""
    crop = PILImage.fromarray(g)
    crop = crop.resize((crop.width * 2, crop.height * 2))
    for psm in (7, 6):
        try:
            txt = pytesseract.image_to_string(crop, config=f"--psm {psm}")
        except Exception:
            return ""
        out = []
        for tok in txt.split():
            c = clean(tok)
            if c and c.lower() not in drop and re.search(r"[A-Za-z0-9]{2}", c) \
                    and not re.fullmatch(r"(.)\1+", c):
                out.append(c)
        if any(re.search(r"[A-Za-z0-9]{4}|\d.*\d", c) or NONE_WORD.fullmatch(c)
               for c in out):
            return clean(" ".join(out))
    return ""


def ocr_words(page, img=None):
    import pytesseract
    img = img or render(page)
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    scale = 72.0 / OCR_DPI
    out = []
    for i, txt in enumerate(data["text"]):
        conf = int(data["conf"][i])
        if not txt.strip() or conf < CONF_LABEL:
            continue
        x, y = data["left"][i] * scale, data["top"][i] * scale
        w, h = data["width"][i] * scale, data["height"][i] * scale
        out.append((x, y, x + w, y + h, txt, conf))
    return out


def norm(t):
    return re.sub(r"[^a-z0-9]", "", str(t).lower())


def find_label(words, variants):
    for v in variants:
        if "|" in v:
            anchor, target = v.split("|", 1)
            _, arect = find_label(words, [anchor])
            if arect is None:
                continue
            tol = max(0.6 * arect.height, 4.0)
            line = [w for w in words
                    if abs((w[1] + w[3]) / 2 - (arect.y0 + arect.y1) / 2) <= tol
                    and w[0] > arect.x1]
            if line:
                _, trect = find_label(sorted(line, key=lambda w: w[0]), [target])
                if trect is not None:
                    return v, trect
            continue
    n = len(words)
    for v in variants:
        if "|" in v:
            continue
        target = norm(v)
        if not target:
            continue
        exact = len(target) <= 6
        for i in range(n):
            if not norm(words[i][4]):
                continue
            if not target.startswith(norm(words[i][4])[:3]):
                continue
            acc, yref = "", (words[i][1] + words[i][3]) / 2
            tol = max(0.6 * (words[i][3] - words[i][1]), 4.0)
            for j in range(i, min(i + 16, n)):
                w = words[j]
                if abs((w[1] + w[3]) / 2 - yref) > tol:
                    break
                piece = norm(w[4])
                if not piece:
                    continue
                acc += piece
                hit = acc == target if exact else (
                    acc == target or (len(acc) >= len(target) and acc.startswith(target)))
                if hit:
                    grp = [words[k] for k in range(i, j + 1)]
                    return v, pymupdf.Rect(min(g[0] for g in grp), min(g[1] for g in grp),
                                           max(g[2] for g in grp), max(g[3] for g in grp))
                if not target.startswith(acc):
                    break
    return None, None


OVERLAY_ANNOTS = {"Stamp", "Ink", "FreeText", "Square", "Circle", "Line",
                  "Polygon", "PolyLine"}
OVERLAY_PAGE_MAX = 0.5
OVERLAY_IMAGE_MAX = 0.25
OVERLAY_IMAGE_MAX_COUNT = 6
OVERLAY_MIN_COVER = 0.2


OVERLAY_DPI = 100
OVERLAY_MIN_PIXELS = 12


def annot_ink(annot):
    try:
        pix = annot.get_pixmap(dpi=OVERLAY_DPI, alpha=True)
    except Exception:
        return None
    from PIL import Image as PILImage
    img = PILImage.frombytes("RGBA", (pix.width, pix.height), pix.samples)
    alpha = img.getchannel("A").point(lambda a: 255 if a > 40 else 0)
    dark = img.convert("L").point(lambda v: 255 if v < 165 else 0)
    from PIL import ImageChops
    return ImageChops.multiply(alpha, dark)


def page_overlays(page):
    area = abs(page.rect.get_area()) or 1.0
    out = []
    for a in page.annots():
        if a.type[1] not in OVERLAY_ANNOTS:
            continue
        r = pymupdf.Rect(a.rect)
        if abs(r.get_area()) / area >= OVERLAY_PAGE_MAX:
            continue
        text = a.info.get("content", "") if a.type[1] == "FreeText" else ""
        out.append((r, text, annot_ink(a)))
    for w in page.widgets():
        v = str(w.field_value or "").strip()
        if v:
            out.append((pymupdf.Rect(w.rect), v, None))
    small = [pymupdf.Rect(im["bbox"]) for im in page.get_image_info()
             if 0 < abs(pymupdf.Rect(im["bbox"]).get_area()) / area < OVERLAY_IMAGE_MAX]
    if len(small) < OVERLAY_IMAGE_MAX_COUNT:
        out.extend((r, "", None) for r in small)
    return out


def ink_pixels(mask, r, inter):
    if mask is None:
        return None
    sx = mask.width / max(r.width, 1e-6)
    sy = mask.height / max(r.height, 1e-6)
    box = (int((inter.x0 - r.x0) * sx), int((inter.y0 - r.y0) * sy),
           int((inter.x1 - r.x0) * sx) + 1, int((inter.y1 - r.y0) * sy) + 1)
    box = (max(0, box[0]), max(0, box[1]), min(mask.width, box[2]), min(mask.height, box[3]))
    if box[2] <= box[0] or box[3] <= box[1]:
        return 0
    return mask.crop(box).histogram()[255]


OVERLAY_INSIDE = 0.5


def overlays_in(region, overlays, whole=False):
    hits, texts = 0, []
    for r, t, mask in overlays:
        inter = region & r
        if inter.is_empty:
            continue
        if t:
            cover = abs(inter.get_area()) / max(min(abs(r.get_area()), abs(region.get_area())), 1.0)
            if cover < OVERLAY_MIN_COVER:
                continue
        elif mask is not None:
            px = ink_pixels(mask, r, inter)
            if px < OVERLAY_MIN_PIXELS:
                continue
            if whole and px < OVERLAY_INSIDE * max(mask.histogram()[255], 1):
                continue
        else:
            cover = abs(inter.get_area()) / max(min(abs(r.get_area()), abs(region.get_area())), 1.0)
            if cover < OVERLAY_MIN_COVER:
                continue
        hits += 1
        if t:
            texts.append(clean(t))
    return hits, " ".join(texts)


def probe(page, rect, drawings, words, width=300, img=None, prefer="right",
          allow_other=False, fallback_text=True, overlays=(), extra_up=0.0, tight=None,
          whole_ink=False, right_up=None, right_down=None):
    best = None
    if tight is None:
        tight = img is not None
    for geom, reg in regions(rect, page, width, prefer,
                             allow_other=allow_other, tight=tight,
                             words=words, extra_up=extra_up,
                             right_up=right_up, right_down=right_down).items():
        ink = ink_in(reg, drawings)
        txt = text_in(reg, words, DROP)
        dk = dark_frac(img, reg, words) if img is not None else 0.0
        raw = text_in(reg, words, DROP, min_conf=0) if img is not None else txt
        if img is not None and not raw and dk >= CROP_OCR_MIN_DARK:
            raw = crop_ocr(img, reg, DROP, words)
        ov, ovtext = overlays_in(reg, overlays, whole=whole_ink)
        if geom != prefer and not fallback_text:
            txt = raw = ""
        if ovtext:
            txt = clean(f"{txt} {ovtext}")
            raw = clean(f"{raw} {ovtext}")
        cand = {"ink": ink, "text": txt, "geom": geom, "dark": dk, "raw": raw, "overlay": ov}
        if best is None or (ink, ov, len(txt), dk) > (best["ink"], best["overlay"],
                                                     len(best["text"]), best["dark"]):
            best = cand
    return best or {"ink": 0, "text": "", "geom": None, "dark": 0.0, "raw": "", "overlay": 0}


def page_role(page, native):
    if page.get_images(full=True):
        return "content"
    if any(it[0] != "re" for d in paintable(page.get_drawings())
           for it in d["items"]):
        return "content"
    return "junk" if len(native) < JUNK_MAX_CHARS else "content"


SCAN_IMAGE_COVER = 0.5


def scan_backed(page):
    area = abs(page.rect.get_area()) or 1.0
    return any(abs(pymupdf.Rect(im["bbox"]).get_area()) / area >= SCAN_IMAGE_COVER
               for im in page.get_image_info())


def page_bundles(doc, use_ocr, force_ocr=False):
    out = []
    for page in doc:
        native = re.sub(r"\s+", "", page.get_text())
        role = page_role(page, native)
        words = [(w[0], w[1], w[2], w[3], w[4], 100)
                 for w in page.get_text("words")]
        if role == "junk":
            out.append({"page": page, "words": [], "img": None, "ocr": False,
                        "err": None, "role": role, "snippet": page.get_text().strip()[:24]})
            continue
        img, used, err = None, False, None
        if use_ocr and (force_ocr or scan_backed(page)
                        or (page.get_images(full=True) and len(native) < OCR_IF_TEXT_UNDER)):
            try:
                img = render(page)
                words = words + ocr_words(page, img)
                used = True
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        out.append({"page": page, "words": words, "img": img, "ocr": used,
                    "err": err, "role": role, "snippet": "",
                    "tight": img is not None and len(native) < OCR_IF_TEXT_UNDER})
    return out


def detect_form_text(text, path):
    scores = Counter()
    flat = norm(text)
    fname = pathlib.Path(path).name.lower()
    for key, spec in FORM_SPECS.items():
        for fp in spec["fingerprints"]:
            if norm(fp) in flat:
                scores[key] += 2
        for hint in spec["filename_hints"]:
            if hint in fname:
                scores[key] += 1
    return scores.most_common(1)[0][0] if scores else "unknown"


def analyse(path, use_ocr=True, debug=False):
    doc, kind = open_doc(path)
    r = {"file": pathlib.Path(path).name, "pages": doc.page_count,
         "kind": kind, "flags": [], "fields": {}}
    bundles = page_bundles(doc, use_ocr, force_ocr=(kind == "image"))
    for i, b in enumerate(bundles):
        if b["err"]:
            r["flags"].append(f"ocr_failed_p{i}:{b['err'][:40]}")
    if any(b["ocr"] for b in bundles):
        r["flags"].append("ocr_used")
    junk = [i for i, b in enumerate(bundles) if b["role"] == "junk"]
    r["junk_pages"] = junk
    r["content_pages"] = doc.page_count - len(junk)
    if junk:
        labels = ", ".join(f"p{i}" + (f" ({bundles[i]['snippet']!r})"
                                      if bundles[i]["snippet"] else "")
                           for i in junk)
        r["flags"].append(f"skipped blank page(s): {labels}")

    alltext = " ".join(str(w[4]) for b in bundles for w in b["words"])
    form = detect_form_text(alltext, path)
    r["form"] = form
    spec = FORM_SPECS.get(form)
    if not spec:
        r["flags"].append("unknown_form_type")
        doc.close()
        return r

    targets = {**{k: ("sig", v) for k, v in spec["signatures"].items()},
               **{k: ("text", v) for k, v in spec["text_fields"].items()}}

    for pno, b in enumerate(bundles):
        page, words, img = b["page"], b["words"], b["img"]
        if not words:
            continue
        drawings = page.get_drawings()
        overlays = page_overlays(page)
        for key, (ftype, variants) in targets.items():
            if r["fields"].get(key, {}).get("found"):
                continue
            label, rect = find_label(words, variants)
            if rect is None:
                continue
            m = probe(page, rect, drawings, words,
                      width=260 if ftype == "text" else 300, img=img,
                      prefer=spec.get("geom", "right"),
                      allow_other=(ftype == "sig"), fallback_text=False,
                      overlays=overlays, tight=b["tight"], whole_ink=(ftype == "text"),
                      right_down=(0.2 if ftype == "text" else None))
            entry = {"found": True, "page": pno, "label": label, "geom": m["geom"],
                     "ocr": b["ocr"], "ink": m["ink"], "dark": round(m["dark"], 4),
                     "text": m["text"], "overlay": m["overlay"]}
            if ftype == "sig":
                mark = m.get("raw") or m["text"]
                entry["signed"] = m["ink"] >= INK_MIN or bool(mark) or m["overlay"] > 0
                entry["ink_unverified"] = (not entry["signed"]
                                           and img is not None
                                           and m["dark"] >= DARK_UNVERIFIED)
                entry["maybe_ink"] = (not entry["signed"] and img is None
                                      and INK_MAYBE <= m["ink"] < INK_MIN)
                lh = max(rect.height, 4.0)
                near = [w for w in words if abs(w[1] - rect.y0) < 4.2 * lh]
                dlabel, drect = find_label(near, ["Date"])
                entry["date_found"] = drect is not None
                if drect is not None:
                    d = probe(page, drect, drawings, words, width=200, img=img,
                              prefer=spec.get("geom", "right"),
                              allow_other=(spec.get("geom") == "above"),
                              overlays=overlays, extra_up=DATE_EXTRA_UP, tight=b["tight"],
                              whole_ink=True, right_up=(None if b["tight"] else 1.2))
                    entry["date_text"] = d["text"]
                    entry["date_ink"] = d["ink"]
                    entry["date_dark"] = round(d["dark"], 4)
                    entry["date_present"] = bool(d.get("raw") or d["text"]) \
                        or d["ink"] >= TEXT_INK_MIN or d["overlay"] > 0
                else:
                    entry.update(date_text="", date_ink=0, date_dark=0.0,
                                 date_present=False)
            else:
                entry["filled"] = bool(m.get("raw") or m["text"]) or m["ink"] >= TEXT_INK_MIN \
                    or m["overlay"] > 0
                entry["ink_unverified"] = (not entry["filled"] and img is not None
                                           and m["dark"] >= TEXT_DARK_UNVERIFIED)
            r["fields"][key] = entry
            if debug:
                print(f"    DBG {key:14s} p{pno} ink={m['ink']:5d} ov={m['overlay']} "
                      f"dark={m['dark']:.4f} geom={m['geom']} text={m['text'][:24]!r} "
                      f"raw={m.get('raw','')[:24]!r}")

    for key in targets:
        r["fields"].setdefault(key, {"found": False})

    field_pages = sorted({f["page"] for f in r["fields"].values()
                          if f.get("found") and f.get("page") is not None})
    r["field_pages"] = field_pages
    content = [i for i, b in enumerate(bundles) if b["role"] != "junk"]
    r["skipped_pages"] = [i for i in content if i not in field_pages]
    if field_pages:
        r["flags"].append(
            f"fields on page(s) {field_pages}; "
            f"{len(r['skipped_pages'])} page(s) had nothing to check")
    doc.close()
    return r


TEMPLATES = {}


def load_templates(d, use_ocr=True):
    d = pathlib.Path(d).expanduser()
    if not d.is_dir():
        return {}
    profiles = {}
    for f in sorted(d.iterdir()):
        if f.suffix.lower() not in {".pdf"} | IMG_EXT or not f.is_file():
            continue
        try:
            t = analyse(f, use_ocr=use_ocr)
        except Exception as e:
            print(f"  {f.name}: FAILED {type(e).__name__}: {e}")
            continue
        if t["form"] == "unknown":
            print(f"  {f.name}: form not recognised, skipped")
            continue
        spec = FORM_SPECS[t["form"]]
        wanted = {**spec["signatures"], **spec["text_fields"]}
        found = {k for k in wanted if t["fields"].get(k, {}).get("found")}
        pages = t.get("content_pages", t["pages"])
        prof = profiles.setdefault(t["form"], {"page_counts": set(), "refs": [],
                                               "ever_found": set(), "all": set(wanted)})
        prof["page_counts"].add(pages)
        prof["ever_found"] |= found
        prof["refs"].append({"file": f.name, "pages": pages,
                             "missing": sorted(set(wanted) - found)})
        miss = sorted(set(wanted) - found)
        note = f"  [{len(miss)} label(s) not on this one: {', '.join(miss)}]" if miss else ""
        print(f"  {f.name}: {t['form']}, {pages} content pages{note}")

    for form, prof in profiles.items():
        never = sorted(prof["all"] - prof["ever_found"])
        if never:
            print(f"  WARNING {form}: {never} not locatable on ANY reference")
        print(f"  -> {form}: accepts {sorted(prof['page_counts'])} content pages "
              f"({len(prof['refs'])} reference(s))")
    return profiles


def verdict(r):
    if r["form"] == "unknown":
        return "not_sure", ["could not identify which form this is"]
    spec = FORM_SPECS[r["form"]]
    bad, unsure = [], []

    for key in spec["signatures"]:
        f = r["fields"].get(key, {})
        if not f.get("found"):
            unsure.append(f"{key}: signature line not located")
            continue
        if f.get("signed"):
            pass
        elif f.get("ink_unverified"):
            unsure.append(f"{key}: something is written on the line but it could "
                          "not be read, check by eye")
        elif f.get("maybe_ink"):
            unsure.append(f"{key}: only {f.get('ink')} stray marks, unclear if signed")
        else:
            bad.append(f"{key}: not signed (blank)")
        if not f.get("date_present"):
            if not f.get("date_found"):
                unsure.append(f"{key}: date line not found on the page")
            else:
                bad.append(f"{key}: date blank")

    for key in spec["text_fields"]:
        f = r["fields"].get(key, {})
        if not f.get("found"):
            unsure.append(f"{key}: field not located")
        elif not f.get("filled"):
            if f.get("ink_unverified"):
                unsure.append(f"{key}: something is written but it could not be read, "
                              "check by eye")
            elif key in spec.get("soft_fields", ()):
                unsure.append(f"{key}: blank, check whether that is acceptable")
            else:
                bad.append(f"{key}: blank")

    if any(x.startswith("ocr_failed") for x in r["flags"]):
        unsure.append("OCR failed on a scanned page")

    if not r.get("field_pages"):
        unsure.append("no recognisable fields on any page of this document")

    pages_used = {f.get("page") for f in r["fields"].values() if f.get("found")}
    if pages_used:
        r["field_pages"] = sorted(p for p in pages_used if p is not None)

    if bad and not unsure:
        return "incorrect", bad
    if bad and unsure:
        return "incorrect", bad + [f"(also unverified: {'; '.join(unsure)})"]
    if unsure:
        return "not_sure", unsure
    return "correct", ["all required signatures present, dates and fields filled"]


def student_address(rec):
    email = rec.get("student_email", "").strip().lower()
    if email.endswith("@warriorlife.net"):
        return email
    first = re.sub(r"\s+", "", rec["first"].strip().lower())
    last = re.sub(r"\s+", "", rec["last"].strip().lower())
    return f"{first}.{last}@warriorlife.net"


def load_manifest(path):
    path = pathlib.Path(path).expanduser()
    if not path.is_file():
        print(f"no manifest at {path}, files will be sorted under unknown_student/")
        return {}
    by_id = {}
    for rec in csv.DictReader(open(path)):
        if rec["file_id"]:
            by_id.setdefault(rec["file_id"], rec)
    return by_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--out", default=str(pathlib.Path.home() / "deca-form-check/sorted"))
    ap.add_argument("--csv", default=str(pathlib.Path.home() / "deca-form-check/verdicts.csv"))
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--templates",
                    default=str(pathlib.Path.home() / "deca-form-check/templates"))
    ap.add_argument("--manifest",
                    default=str(pathlib.Path.home() / "deca-form-check/manifest.csv"))
    a = ap.parse_args()

    global TEMPLATES
    print(f"loading reference templates from {a.templates}")
    TEMPLATES = load_templates(a.templates, use_ocr=not a.no_ocr)
    print()

    root = pathlib.Path(a.input).expanduser()
    files = [p for p in (sorted(root.rglob("*")) if root.is_dir() else [root])
             if p.is_file() and (p.suffix.lower() == ".pdf" or p.suffix.lower() in IMG_EXT)]

    out = pathlib.Path(a.out)
    for b in ("correct", "incorrect", "not_sure"):
        d = out / b
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    by_id = load_manifest(a.manifest)

    rows, tally, unmatched = [], Counter(), 0
    for p in files:
        try:
            r = analyse(p, use_ocr=not a.no_ocr, debug=a.debug)
            v, why = verdict(r)
        except Exception as e:
            r = {"file": p.name, "form": "?", "pages": 0, "flags": [], "fields": {}}
            v, why = "not_sure", [f"error reading file: {type(e).__name__}: {e}"]
        tally[v] += 1
        file_id = p.resolve().parent.name
        rec = by_id.get(file_id)
        if rec:
            student = student_address(rec)
        else:
            student, file_id = "unknown_student", ""
            unmatched += 1
        folder = out / v / student
        folder.mkdir(exist_ok=True)
        dest = folder / f"{r.get('form', '?')}_{p.name}"
        shutil.copy2(p, dest)
        rows.append({"file": r["file"], "form": r.get("form", "?"),
                     "pages": r.get("pages", 0), "verdict": v,
                     "reasons": "; ".join(why), "flags": "; ".join(r.get("flags", [])),
                     "student": student, "file_id": file_id, "path": str(dest)})
        if not a.quiet:
            print(f"[{v:9s}] {r.get('form','?'):9s} {r['file'][:52]:52s} {'; '.join(why)[:80]}")

    with open(a.csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\n{dict(tally)}  ->  {a.csv}  |  {out}/")
    if unmatched:
        print(f"{unmatched} file(s) not in the manifest, sorted under unknown_student/")


if __name__ == "__main__":
    main()
