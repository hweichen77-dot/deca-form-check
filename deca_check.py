# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf", "pillow", "pillow-heif", "pytesseract", "numpy"]
# ///
import pymupdf, pathlib, sys, re, csv, io, json, argparse, shutil
from collections import Counter
import difflib
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
TEXT_WIDTH = 260
TEXT_WIDE = 400
INK_MAYBE = 1
OCR_DPI = 150
OCR_MIN_LONG_EDGE = 1650
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
            "parent_sig": ["Parent Signature", "Parent Printed Name>Signature",
                           "Parent Printed Name>Parent"],
            "student_sig": ["Student Signature", "Student Printed Name>Signature",
                            "Student Printed Name>Student"],
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
        "optional_fields": {"allergies", "medication", "heart_condition",
                            "physical_restrictions", "other_conditions"},
        "reach_down": {"home_address": 1.6},
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


def sniff(p):
    head = open(p, "rb").read(12)
    if head[4:8] == b"ftyp":
        return "heic"
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image"
    return None


def open_doc(path):
    p = pathlib.Path(path)
    kind = sniff(p) or ("heic" if p.suffix.lower() in {".heic", ".heif"}
                        else "image" if p.suffix.lower() in IMG_EXT else "pdf")
    if kind == "heic":
        if not HEIC:
            raise RuntimeError("pillow-heif unavailable")
        buf = io.BytesIO()
        Image.open(p).convert("RGB").save(buf, format="PNG")
        return pymupdf.open(stream=buf.getvalue(), filetype="png"), "image"
    if kind == "image":
        return pymupdf.open(p), "image"
    return pymupdf.open(p), "pdf"


STOP_WORDS = {"date", "phone", "policy", "number", "company",
              "birth", "advisor", "advisors"}
LABEL_FUZZ = 0.7
LABEL_FUZZ_MIN_LEN = 8
BELOW_LABEL_REACH = 9.0
LABEL_FUZZ_RATIO = 0.9


def looks_like(word, vocab, fuzz=LABEL_FUZZ):
    import difflib
    w = norm(word)
    if len(w) < 3:
        return w in vocab
    if w in vocab:
        return True
    return any(len(v) >= 4 and v[0] == w[0]
               and difflib.SequenceMatcher(None, w, v).ratio() >= fuzz
               for v in vocab)


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
             if w[0] > start + 0.7 * h and w[5] >= CONF_CONTENT
             and (clean(str(w[4])).lower() in STOP_WORDS or str(w[4]).strip().endswith(":"))]
    limit = min(stops) - 0.3 * h if stops else start + width
    return start, min(page.rect.x1, start + width, max(start + 3.2 * h, limit))


DATE_EXTRA_UP = 1.5
ABOVE_UP = 4.5
ABOVE_LEFT_REACH = 30
SCAN_DOWN = 0.8
SCAN_UP = 1.25
NEXT_ROW_GAP = 0.3


def regions(rect, page, width=300, prefer="right", allow_other=True, tight=False,
            words=(), extra_up=0.0, right_up=None, right_down=None, left_to_prev=False):
    h = max(rect.height, 4.0)
    width = width / 11.0 * h
    up = (ABOVE_UP + extra_up) * h
    left = rect.x0 - 0.9 * h
    if left_to_prev:
        ymid = (rect.y0 + rect.y1) / 2
        prev = [w[2] for w in words
                if abs((w[1] + w[3]) / 2 - ymid) <= 0.65 * h and w[2] < rect.x0 - 0.5 * h
                and rect.x0 - w[2] <= ABOVE_LEFT_REACH * h and re.search(r"[A-Za-z]{3}", str(w[4]))]
        if prev:
            left = min(left, (max(prev) + rect.x0) / 2)
    rx0, rx1 = right_span(rect, page, words, width)
    if right_up is None:
        right_up = SCAN_UP if tight else 2.4
    if right_down is None:
        right_down = 0.2 if tight else 0.7
    bottom = rect.y1 + right_down * h
    top = max(0, rect.y0 - right_up * h)

    def label_row(w, x0, x1):
        return (w[0] < x1 and w[2] > x0 and w[5] >= CONF_CONTENT
                and (str(w[4]).strip().endswith(":") or clean(str(w[4])).lower() in LABEL_VOCAB
                     or clean(str(w[4])).lower() in STOP_WORDS))
    if tight:
        below = [w[1] for w in words if w[1] > rect.y1 + 0.2 * h and label_row(w, rect.x0, rx1)]
        if below:
            bottom = max(rect.y1 + 0.2 * h, min(bottom, min(below) - NEXT_ROW_GAP * h))
        above = [w[3] for w in words if w[3] < rect.y0 - 0.2 * h and label_row(w, rect.x0, rx1)]
        if above:
            top = max(top, (max(above) + rect.y0) / 2)
    above_top = max(0, rect.y0 - up)
    prev_rows = [w[3] for w in words if w[3] < rect.y0 - 0.2 * h
                 and label_row(w, left, rect.x0 + width)]
    if prev_rows:
        above_top = max(above_top, (max(prev_rows) + rect.y0) / 2)
    above_right = min(page.rect.x1, rect.x0 + width)
    ymid = (rect.y0 + rect.y1) / 2
    same_row = [w[0] for w in words if w[0] > rect.x1 + 0.5 * h
                and abs((w[1] + w[3]) / 2 - ymid) <= 0.65 * h and label_row(w, rect.x1, above_right)]
    if same_row:
        above_right = max(rect.x1 + 2 * h, min(above_right, min(same_row) - 0.5 * h))
    r = {
        "right": pymupdf.Rect(rx0, top, rx1, bottom),
        "above": pymupdf.Rect(max(0, left), above_top, above_right, rect.y0 - 0.2 * h),
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


RECT_INK_MIN_HEIGHT = 1.5
RECT_INK_MAX_FRAC = 0.5
CLIP_GLYPH_MIN_ITEMS = 12


def page_drawings(page):
    out = page.get_drawings()
    for d in page.get_drawings(extended=True):
        if d.get("type") != "clip" or not d.get("scissor"):
            continue
        items = [it for it in d.get("items", []) if it[0] != "re"]
        if len(items) < CLIP_GLYPH_MIN_ITEMS:
            continue
        out.append({"type": "s", "rect": pymupdf.Rect(d["scissor"]), "items": items,
                    "color": (0.0, 0.0, 0.0), "fill": None})
    return out


INK_BAND = 0.8
SPECK = 0.6
TEXT_X_SLACK = 1.0


def ink_in(region, drawings, line_y=None, h=None):
    n = 0
    cap = RECT_INK_MAX_FRAC * region.height
    zone = region
    if line_y is not None and h:
        zone = pymupdf.Rect(region.x0, max(region.y0, line_y - INK_BAND * h),
                            region.x1, min(region.y1, line_y + INK_BAND * h))
    for d in paintable(drawings):
        if h:
            dr = pymupdf.Rect(d["rect"])
            if dr.width < SPECK * h and dr.height < SPECK * h:
                continue
        for it in d["items"]:
            if it[0] == "re":
                r = pymupdf.Rect(it[1])
                if r.height > RECT_INK_MIN_HEIGHT and max(r.width, r.height) <= cap:
                    n += sum(1 for p in (r.tl, r.tr, r.bl, r.br) if p in zone)
                continue
            if any(p in zone for p in (x for x in it[1:] if isinstance(x, pymupdf.Point))):
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
LABEL_VOCAB = {w for w in DROP | STOP_WORDS if len(w) >= 4}
CROP_LABEL_FUZZ = 0.6


INVERT_BELOW = 110


def page_dpi(page):
    return max(OCR_DPI, 72.0 * OCR_MIN_LONG_EDGE / max(page.rect.width, page.rect.height, 1.0))


def render(page):
    dpi = page_dpi(page)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72.0, dpi / 72.0))
    from PIL import Image as PILImage, ImageOps
    img = PILImage.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    hist = img.convert("L").histogram()
    half, acc, median = sum(hist) / 2, 0, 255
    for v, n in enumerate(hist):
        acc += n
        if acc >= half:
            median = v
            break
    inverted = median < INVERT_BELOW
    if inverted:
        img = ImageOps.invert(img)
    img.info["inverted"] = inverted
    img.info["dpi"] = dpi
    return img


DATE_LABELS = ["Date", "pate", "Dote", "Dete"]
DATE_RE = re.compile(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}")
NONE_WORD = re.compile(r"(?i)n[\W_il1|]{0,2}a|none|no|nil")
RULE_RUN_PX = 40
RULE_HALF_THICK = 3
CROP_OCR_MIN_DARK = 0.003
CROP_MIN_CONF = 0


RULE_DILATE_PX = 2
RULE_DETECT_LUM = 200
RULE_DETECT_BELOW_PAPER = 20
RULE_SKEW_PX = 1
RULE_BAND_FRAC = 0.9


def erase_rules(g, run_px, half_thick):
    g = erase_runs(g, run_px, half_thick)
    return erase_runs(g.T.copy(), run_px, half_thick).T.copy()


def erase_runs(g, run_px, half_thick):
    import numpy as np
    dark = g < 165
    if dark.shape[1] <= run_px or dark.shape[0] <= 2 * half_thick:
        return g
    wide = g < min(RULE_DETECT_LUM, int(np.median(g)) - RULE_DETECT_BELOW_PAPER)
    for k in range(1, RULE_DILATE_PX + 1):
        wide[:, k:] |= wide[:, :-k].copy()
        wide[:, :-k] |= wide[:, k:].copy()
    band = wide.mean(axis=1) >= RULE_BAND_FRAC
    for k in range(1, RULE_SKEW_PX + 1):
        wide[k:] |= wide[:-k].copy()
        wide[:-k] |= wide[k:].copy()
    win = np.lib.stride_tricks.sliding_window_view(wide, run_px, axis=1).all(axis=2)
    longrun = np.zeros_like(dark)
    for k in range(run_px):
        longrun[:, k:k + win.shape[1]] |= win
    t = half_thick
    thick = np.zeros_like(dark)
    thick[t:-t] = dark[t:-t] & dark[:-2 * t] & dark[2 * t:]
    g[longrun & ~thick] = 255
    g[band, :] = 255
    return g


def erase_words(g, words, scale, origin, region=None):
    ox, oy = origin
    for x0, y0, x1, y1, w, conf, *_ in words:
        outside = region is not None and conf >= CONF_CONTENT \
            and (y0 >= region.y1 - 0.25 * (y1 - y0) or y1 <= region.y0 + 0.25 * (y1 - y0))
        if not outside and not re.search(r"[A-Za-z]{2}", str(w)):
            continue
        if not outside and conf < CONF_CONTENT and not looks_like(str(w), LABEL_VOCAB):
            continue
        px0, py0 = int((x0 - ox) * scale) - 1, int((y0 - oy) * scale) - 1
        px1, py1 = int((x1 - ox) * scale) + 1, int((y1 - oy) * scale) + 1
        if px1 <= 0 or py1 <= 0 or px0 >= g.shape[1] or py0 >= g.shape[0]:
            continue
        g[max(0, py0):py1, max(0, px0):px1] = 255
    return g


RED_MARGIN = 60


def grey_no_red(img):
    import numpy as np
    a = np.asarray(img.convert("RGB")).astype(int)
    g = np.asarray(img.convert("L")).copy()
    g[(a[..., 0] - np.maximum(a[..., 1], a[..., 2])) > RED_MARGIN] = 255
    return g


def region_crop(img, region, pad=0, words=()):
    import numpy as np
    scale = img.info["dpi"] / 72.0
    box = (max(0, int(region.x0 * scale) - pad), max(0, int(region.y0 * scale) - pad),
           min(img.width, int(region.x1 * scale) + pad), min(img.height, int(region.y1 * scale) + pad))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return None
    g = grey_no_red(img.crop(box))
    g = erase_words(g, words, scale, (box[0] / scale, box[1] / scale), region)
    return erase_rules(g, RULE_RUN_PX, RULE_HALF_THICK)


FAINT_ZOOM = 2
FAINT_MIN_AREA = 0.25
FAINT_WEAK_MIN_AREA = 0.05
FAINT_FLOOR_CAP = 0.5
BELOW_REACH = 6.0
HIGH_REACH = 10.0
BELOW_WIDTH = 20.0
BELOW_MIN_AREA = 0.3


def faint_area(page, region, words, h, inverted=False):
    import numpy as np
    from PIL import Image as PILImage, ImageFilter, ImageOps
    scale = FAINT_ZOOM * page_dpi(page) / 72.0
    try:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=region)
    except Exception:
        return 0.0
    img = PILImage.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    if img.width < 8 or img.height < 8:
        return 0.0
    if inverted:
        img = ImageOps.invert(img)
    g = grey_no_red(img)
    g = erase_words(g, words, scale, (region.x0, region.y0), region)
    g = erase_rules(g, RULE_RUN_PX * 2, RULE_HALF_THICK * 2)
    binary = PILImage.fromarray(((g < 165) * 255).astype("uint8"))
    opened = binary.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    px = opened.histogram()[255]
    hpx = max(h, 4.0) * scale
    return px / (hpx * hpx)


def dark_frac(img, region, words=()):
    g = region_crop(img, region, words=words)
    if g is None:
        return 0.0
    return float((g < 165).sum()) / g.size


def strong_token(text):
    return any(re.search(r"[A-Za-z0-9]{4}|\d.*\d", c) or NONE_WORD.fullmatch(c)
               for c in str(text).split())


def crop_ocr(img, region, drop, words=(), weak=False):
    import pytesseract
    from PIL import Image as PILImage
    g = region_crop(img, region, pad=int(3 * img.info["dpi"] / 72.0), words=words)
    if g is None:
        return ""
    crop = PILImage.fromarray(g)
    crop = crop.resize((crop.width * 2, crop.height * 2))
    fallback = ""
    for psm in (7, 6):
        try:
            data = pytesseract.image_to_data(crop, config=f"--psm {psm}",
                                             output_type=pytesseract.Output.DICT)
        except Exception:
            return ""
        toks = [t for t, cf in zip(data["text"], data["conf"])
                if t.strip() and int(cf) >= CROP_MIN_CONF]
        out = []
        for tok in toks:
            c = clean(tok)
            if c and c.lower() not in drop and re.search(r"[A-Za-z0-9]{2}", c) \
                    and not re.fullmatch(r"(.)\1+", c) \
                    and not looks_like(c, LABEL_VOCAB, CROP_LABEL_FUZZ):
                out.append(c)
        if strong_token(" ".join(out)):
            return clean(" ".join(out))
        if weak and not fallback and any(re.search(r"[A-Za-z0-9]{3}", c) for c in out):
            fallback = clean(" ".join(out))
    return fallback


OCR_PSMS = (3, 11)


def ocr_words(page, img=None):
    import pytesseract
    img = img or render(page)
    scale = 72.0 / img.info["dpi"]
    out = []
    for psm in OCR_PSMS:
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT,
                                         config=f"--psm {psm}")
        seen, fresh = list(out), []
        for i, txt in enumerate(data["text"]):
            conf = int(data["conf"][i])
            if not txt.strip() or conf < CONF_LABEL:
                continue
            x, y = data["left"][i] * scale, data["top"][i] * scale
            w, h = data["width"][i] * scale, data["height"][i] * scale
            box = pymupdf.Rect(x, y, x + w, y + h)
            if any(abs((box & pymupdf.Rect(o[:4])).get_area()) > 0.5 * abs(box.get_area())
                   for o in seen):
                continue
            fresh.append((x, y, x + w, y + h, txt, conf))
        out.extend(fresh)
    return out


def norm(t):
    return re.sub(r"[^a-z0-9]", "", str(t).lower())


def find_label(words, variants):
    n = len(words)
    for v in variants:
        if "|" in v or ">" in v:
            continue
        target = norm(v)
        if not target:
            continue
        exact = len(target) <= 6
        hits = []
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
                    acc == target or (len(acc) >= len(target) and acc.startswith(target))
                    or (len(target) >= LABEL_FUZZ_MIN_LEN and abs(len(acc) - len(target)) <= 2
                        and difflib.SequenceMatcher(None, acc, target).ratio() >= LABEL_FUZZ_RATIO))
                if hit:
                    grp = [words[k] for k in range(i, j + 1)]
                    hits.append(pymupdf.Rect(min(g[0] for g in grp), min(g[1] for g in grp),
                                             max(g[2] for g in grp), max(g[3] for g in grp)))
                    break
                if not target.startswith(acc):
                    break
        if hits:
            first = hits[0]
            row = [r for r in hits if abs((r.y0 + r.y1) / 2 - (first.y0 + first.y1) / 2)
                   <= max(0.6 * first.height, 4.0)]
            return v, min(row, key=lambda r: r.x0)
    for v in variants:
        if ">" in v:
            anchor, target = v.split(">", 1)
            _, arect = find_label(words, [anchor])
            if arect is None:
                continue
            h = max(arect.height, 4.0)
            below = sorted((w for w in words if arect.y1 - 0.2 * h < w[1] < arect.y1 + BELOW_LABEL_REACH * h),
                           key=lambda w: (w[1], w[0]))
            _, trect = find_label(below, [target])
            if trect is not None:
                return v, trect
            continue
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
    return None, None


OVERLAY_ANNOTS = {"Stamp", "Ink", "FreeText", "Square", "Circle", "Line",
                  "Polygon", "PolyLine"}
OVERLAY_PAGE_MAX = 0.5
OVERLAY_IMAGE_MAX = 0.3
OVERLAY_IMAGE_MAX_COUNT = 6
OVERLAY_MIN_COVER = 0.2


OVERLAY_DPI = 100
IMAGE_INK_MAX_LUM = 220
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
        out.extend((r, "", image_ink(page, r)) for r in small)
    return out


def image_ink(page, r):
    import numpy as np
    from PIL import Image as PILImage
    try:
        pix = page.get_pixmap(dpi=OVERLAY_DPI, clip=r)
    except Exception:
        return None
    g = np.asarray(PILImage.open(io.BytesIO(pix.tobytes("png"))).convert("L")).copy()
    if g.shape[0] < 4 or g.shape[1] < 4:
        return None
    g = erase_rules(g, RULE_RUN_PX * 2, 2)
    return PILImage.fromarray(((g < IMAGE_INK_MAX_LUM) * 255).astype("uint8"))


def ink_slice(mask, r, inter):
    sx = mask.width / max(r.width, 1e-6)
    sy = mask.height / max(r.height, 1e-6)
    box = (int((inter.x0 - r.x0) * sx), int((inter.y0 - r.y0) * sy),
           int((inter.x1 - r.x0) * sx) + 1, int((inter.y1 - r.y0) * sy) + 1)
    box = (max(0, box[0]), max(0, box[1]), min(mask.width, box[2]), min(mask.height, box[3]))
    if box[2] <= box[0] or box[3] <= box[1]:
        return 0, 0
    sl = mask.crop(box)
    px = sl.histogram()[255]
    if not px:
        return 0, None
    import numpy as np
    rows = np.asarray(sl).sum(axis=1)
    centroid = float((rows * np.arange(rows.size)).sum() / rows.sum())
    return px, inter.y0 + (centroid + 0.5) / sy


OVERLAY_TEXT_MIN_AREA = 0.9
OVERLAY_TEXT_MAX_OFFSET = 1.0


def overlays_in(region, overlays, whole=False, h=12.0, line_y=None):
    hits, texts = 0, []
    hpx = h * OVERLAY_DPI / 72.0
    for r, t, mask in overlays:
        inter = region & r
        if inter.is_empty:
            continue
        if t:
            cover = abs(inter.get_area()) / max(min(abs(r.get_area()), abs(region.get_area())), 1.0)
            if cover < OVERLAY_MIN_COVER:
                continue
        elif mask is not None:
            px, cy = ink_slice(mask, r, inter)
            if px < OVERLAY_MIN_PIXELS:
                continue
            if whole:
                if line_y is not None:
                    band = inter & pymupdf.Rect(inter.x0, line_y - OVERLAY_TEXT_MAX_OFFSET * h,
                                                inter.x1, line_y + OVERLAY_TEXT_MAX_OFFSET * h)
                    px = ink_slice(mask, r, band)[0] if not band.is_empty else 0
                if px < OVERLAY_TEXT_MIN_AREA * hpx * hpx:
                    continue
        else:
            cover = abs(inter.get_area()) / max(min(abs(r.get_area()), abs(region.get_area())), 1.0)
            if cover < OVERLAY_MIN_COVER:
                continue
        hits += 1
        if t:
            texts.append(clean(t))
    return hits, " ".join(texts)


def label_tail(words, rect, variants):
    last = max((w for w in words if abs(w[2] - rect.x1) < 0.5 and w[1] < rect.y1 and w[3] > rect.y0),
               key=lambda w: w[2] - w[0], default=None)
    if last is None:
        return ""
    text = norm(last[4])
    for v in variants:
        tokens = norm(v.split("|")[-1])
        for k in range(len(tokens), 2, -1):
            end = tokens[-k:]
            if end in text:
                tail = text[text.index(end) + len(end):]
                if re.search(r"[a-z0-9]{2}", tail) and not looks_like(tail, LABEL_VOCAB, CROP_LABEL_FUZZ):
                    return tail
                return ""
    return ""


def plausible_token(c):
    return bool(re.search(r"[A-Za-z0-9]{3}|\d", c) or NONE_WORD.fullmatch(c))


def probe(page, rect, drawings, words, width=300, img=None, prefer="right",
          allow_other=False, fallback_text=True, overlays=(), extra_up=0.0, tight=None,
          whole_ink=False, right_up=None, right_down=None, left_to_prev=False,
          sig_field=False):
    best = None
    if tight is None:
        tight = img is not None
    for geom, reg in regions(rect, page, width, prefer,
                             allow_other=allow_other, tight=tight,
                             words=words, extra_up=extra_up,
                             right_up=right_up, right_down=right_down,
                             left_to_prev=left_to_prev).items():
        h = max(rect.height, 4.0)
        ink = ink_in(reg, drawings, (rect.y0 + rect.y1) / 2 if whole_ink and geom == "right" else None, h)
        treg = pymupdf.Rect(reg.x0 - TEXT_X_SLACK * h, reg.y0, reg.x1, reg.y1) if geom == "right" else reg
        wide = treg
        if geom == "right" and not sig_field:
            wide = pymupdf.Rect(treg.x0, treg.y0,
                                max(treg.x1, right_span(rect, page, words, TEXT_WIDE / 11.0 * h)[1]),
                                treg.y1)
        txt = text_in(wide, words, DROP)
        dk = dark_frac(img, reg, words) if img is not None else 0.0
        raw = text_in(treg, words, DROP, min_conf=0) if img is not None else txt
        weak = raw
        if raw and not txt and not sig_field:
            raw = clean(" ".join(c for c in raw.split() if plausible_token(c)))
        if img is not None and not raw and dk >= CROP_OCR_MIN_DARK:
            raw = crop_ocr(img, reg, DROP, words)
        ov, ovtext = overlays_in(reg, overlays, whole=whole_ink, h=h,
                                 line_y=(rect.y0 + rect.y1) / 2 if geom == "right" else None)
        if geom != prefer and not fallback_text:
            txt = raw = ""
        if ovtext:
            txt = clean(f"{txt} {ovtext}")
            raw = clean(f"{raw} {ovtext}")
        cand = {"ink": ink, "text": txt, "geom": geom, "dark": dk, "raw": raw, "overlay": ov,
                "weak": weak, "reg": reg, "pref_reg": reg if geom == prefer else None}
        if best is None or (ink, ov, len(raw), len(txt), dk) > (
                best["ink"], best["overlay"], len(best["raw"]), len(best["text"]), best["dark"]):
            best = cand
    if best is not None and best["pref_reg"] is None:
        best["pref_reg"] = regions(rect, page, width, prefer, allow_other=False, tight=tight,
                                   words=words, extra_up=extra_up, right_up=right_up,
                                   right_down=right_down, left_to_prev=left_to_prev)[prefer]
    return best or {"ink": 0, "text": "", "geom": None, "dark": 0.0, "raw": "", "overlay": 0,
                    "reg": None, "pref_reg": None}


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


OSD_MIN_WORDS = 8


def form_words():
    out = set()
    for spec in FORM_SPECS.values():
        for text in spec["fingerprints"] + [v for vs in list(spec["signatures"].values())
                                             + list(spec["text_fields"].values()) for v in vs]:
            out |= {norm(t) for t in re.split(r"[^A-Za-z]+", text) if len(t) >= 4}
    return out


FORM_WORDS = form_words()


def confident(words):
    return sum(1 for w in words if w[5] >= CONF_CONTENT and norm(w[4]) in FORM_WORDS)


def orientation(img):
    import pytesseract
    try:
        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
    except Exception:
        return 0
    return int(osd.get("rotate", 0)) % 360


def best_rotation(page, img, ocr):
    base = page.rotation
    tried = [(confident(ocr), 0, img, ocr)]
    osd = orientation(img)
    order = [r for r in ([osd] if osd else []) + [90, 180, 270] if r]
    for rot in dict.fromkeys(order):
        page.set_rotation((base + rot) % 360)
        img2 = render(page)
        ocr2 = ocr_words(page, img2)
        tried.append((confident(ocr2), rot, img2, ocr2))
        if tried[-1][0] >= OSD_MIN_WORDS or (rot == osd and tried[-1][0] > 2 * tried[0][0]):
            break
    n, rot, img, ocr = max(tried, key=lambda t: t[0])
    if n < OSD_MIN_WORDS or n < 2 * tried[0][0]:
        n, rot, img, ocr = tried[0]
    page.set_rotation((base + rot) % 360)
    return img, ocr


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
                ocr = ocr_words(page, img)
                if len(native) < OCR_IF_TEXT_UNDER and confident(ocr) < OSD_MIN_WORDS:
                    img, ocr = best_rotation(page, img, ocr)
                words = words + ocr
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


STACK_ROW_GAP = 0.7
SYNTH_ROWS = {"family_doctor": ("other_conditions", "physical_restrictions")}
PHONE_X_FRAC = (0.53, 0.60)
SYNTH_MAX_LABEL_H = 12.0


def synth_label(key, fields, pno, page):
    def rect_of(k):
        f = fields.get(k, {})
        return pymupdf.Rect(f["rect"]) if f.get("found") and f.get("page") == pno else None
    if key in SYNTH_ROWS:
        below, above = (rect_of(k) for k in SYNTH_ROWS[key])
        if below is None:
            return None, None
        h = max(below.height, 4.0)
        pitch = (below.y0 - above.y0) if above is not None and 1.2 * h < below.y0 - above.y0 < 3 * h \
            else 2.1 * h
        return "synth:" + key, pymupdf.Rect(below.x0, below.y0 + pitch, below.x0 + 7 * h, below.y1 + pitch)
    if key == "doctor_phone":
        fd = rect_of("family_doctor")
        if fd is None:
            return None, None
        x0 = fd.x0 + PHONE_X_FRAC[0] * (page.rect.x1 - fd.x0)
        x1 = fd.x0 + PHONE_X_FRAC[1] * (page.rect.x1 - fd.x0)
        hcap = SYNTH_MAX_LABEL_H * page.rect.x1 / 612.0
        y1 = min(fd.y1, fd.y0 + hcap)
        return "synth:doctor_phone", pymupdf.Rect(x0, fd.y0, x1, y1)
    return None, None


def stacked_above(fields, pno, rect, words):
    h = max(rect.height, 4.0)
    prev = [f for f in fields.values()
            if f.get("found") and f.get("page") == pno and f.get("reg") and f.get("filled")
            and 0 < rect.y0 - f["rect"][3] <= 2.5 * h]
    if not prev:
        return False
    f = max(prev, key=lambda f: f["rect"][3])
    reg = pymupdf.Rect(f["reg"])
    rows = sorted((w[1] + w[3]) / 2 for w in words
                  if w[5] >= CONF_CONTENT and (w[0] + w[2]) / 2 >= f["rect"][2] - 2 * h
                  and (pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2) in reg
                       or (f["rect"][1] - 0.3 * h <= (w[1] + w[3]) / 2 <= rect.y0 + 0.2 * h
                           and (w[0] + w[2]) / 2 < reg.x1))
                  and (w[1] + w[3]) / 2 >= f["rect"][1] - 0.3 * h
                  and re.search(r"[A-Za-z0-9]", str(w[4])))
    return bool(rows) and rows[-1] - rows[0] > STACK_ROW_GAP * h


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
    claimed_dates = []
    claimed_date_labels = []
    claimed_date_labels_x = []

    for pno, b in enumerate(bundles):
        page, words, img = b["page"], b["words"], b["img"]
        if not words:
            continue
        drawings = page_drawings(page)
        overlays = page_overlays(page)
        for key, (ftype, variants) in targets.items():
            if r["fields"].get(key, {}).get("found"):
                continue
            label, rect = find_label(words, variants)
            if rect is None:
                label, rect = synth_label(key, r["fields"], pno, page)
            if rect is None:
                continue
            m = probe(page, rect, drawings, words,
                      width=TEXT_WIDTH if ftype == "text" else 300, img=img,
                      prefer=spec.get("geom", "right"),
                      allow_other=(ftype == "sig"), fallback_text=False,
                      overlays=overlays, tight=b["tight"], whole_ink=(ftype == "text"),
                      sig_field=(ftype == "sig"),
                      right_down=spec.get("reach_down", {}).get(
                          key, SCAN_DOWN if b["tight"] else (0.2 if ftype == "text" else None)))
            tail = label_tail(words, rect, variants)
            if tail:
                m["text"] = clean(f"{tail} {m['text']}")
                m["raw"] = clean(f"{tail} {m['raw']}")
            entry = {"found": True, "page": pno, "label": label, "geom": m["geom"],
                     "ocr": b["ocr"], "ink": m["ink"], "dark": round(m["dark"], 4),
                     "text": m["text"], "overlay": m["overlay"],
                     "rect": tuple(rect), "reg": tuple(m["reg"]) if m["reg"] else None}
            if ftype == "sig":
                mark = m.get("raw") or m["text"]
                entry["signed"] = m["ink"] >= INK_MIN or bool(mark) or m["overlay"] > 0
                entry["ink_unverified"] = (not entry["signed"]
                                           and img is not None
                                           and m["dark"] >= DARK_UNVERIFIED)
                entry["faint"] = False
                if not entry["signed"] and not entry["ink_unverified"] and img is not None \
                        and m["pref_reg"] is not None:
                    area = faint_area(page, m["pref_reg"], words, rect.height,
                                      img.info.get("inverted", False))
                    weak = crop_ocr(img, m["pref_reg"], DROP, words, weak=True)
                    floor = TEMPLATES.get(form, {}).get("faint_floor", {}).get(key, 0.0)
                    entry["faint"] = (area - floor >= FAINT_MIN_AREA
                                      or (bool(weak) and area >= FAINT_WEAK_MIN_AREA))
                    entry["faint_area"] = round(area, 3)
                entry["maybe_ink"] = (not entry["signed"] and img is None
                                      and INK_MAYBE <= m["ink"] < INK_MIN)
                lh = max(rect.height, 4.0)
                entry["below"] = False
                if not any(entry[k] for k in ("signed", "ink_unverified", "faint", "maybe_ink")):
                    if spec.get("geom") == "right":
                        nxt = [w[1] for w in words if w[5] >= CONF_CONTENT and w[1] > rect.y1 + 0.5 * lh
                               and w[0] < rect.x0 + BELOW_WIDTH * lh and w[2] > rect.x0]
                        breg = pymupdf.Rect(rect.x0, rect.y1 + 0.2 * lh,
                                            min(page.rect.x1, rect.x0 + BELOW_WIDTH * lh),
                                            min(rect.y1 + BELOW_REACH * lh,
                                                min(nxt) - 0.2 * lh if nxt else page.rect.y1))
                    else:
                        prv = [w[3] for w in words if w[5] >= CONF_CONTENT and w[3] < rect.y0 - 0.5 * lh
                               and w[0] < rect.x0 + BELOW_WIDTH * lh and w[2] > rect.x0
                               and re.search(r"[A-Za-z]{3}", str(w[4]))]
                        breg = pymupdf.Rect(rect.x0, max(rect.y0 - HIGH_REACH * lh,
                                                         max(prv) + 0.2 * lh if prv else 0),
                                            min(page.rect.x1, rect.x0 + BELOW_WIDTH * lh),
                                            rect.y0 - 0.2 * lh)
                    if breg.height >= lh:
                        area = faint_area(page, breg, words, rect.height,
                                          img.info.get("inverted", False) if img is not None else False)
                        entry["below"] = area >= BELOW_MIN_AREA
                        entry["below_area"] = round(area, 3)
                mid = (rect.y0 + rect.y1) / 2
                near = sorted((w for w in words if abs(w[1] - rect.y0) < 4.2 * lh),
                              key=lambda w: (round(abs((w[1] + w[3]) / 2 - mid) / lh), w[0]))
                dlabel, drect = None, None
                found = []
                for v in DATE_LABELS:
                    pool = list(near)
                    while pool:
                        _, cand = find_label(pool, [v])
                        if cand is None:
                            break
                        cy = (cand.y0 + cand.y1) / 2
                        if not any(abs(cy - y) < 0.5 * lh for y in claimed_date_labels):
                            found.append((abs(cy - mid), cand))
                        pool = [w for w in pool if abs((w[1] + w[3]) / 2 - cy) >= 0.5 * lh]
                if found:
                    dlabel, drect = "Date", min(found, key=lambda f: f[0])[1]
                if drect is None:
                    taken = [(x, y) for x, y in claimed_date_labels_x if abs(y - mid) < 4.2 * lh]
                    if taken:
                        drect = pymupdf.Rect(taken[0][0], rect.y0, taken[0][0] + 2.5 * lh, rect.y1)
                        dlabel = "Date"
                if drect is not None:
                    claimed_date_labels.append((drect.y0 + drect.y1) / 2)
                    claimed_date_labels_x.append((drect.x0, (drect.y0 + drect.y1) / 2))
                entry["date_found"] = drect is not None
                if drect is not None:
                    d = probe(page, drect, drawings, words, width=200, img=img,
                              prefer=spec.get("geom", "right"),
                              allow_other=(spec.get("geom") == "above"),
                              overlays=overlays, extra_up=DATE_EXTRA_UP, tight=b["tight"],
                              whole_ink=True, right_up=(None if b["tight"] else 1.2),
                              right_down=(SCAN_DOWN if b["tight"] else None),
                              left_to_prev=(spec.get("geom") == "above"))
                    dtail = label_tail(words, drect, ["Date"])
                    if dtail:
                        d["text"] = clean(f"{dtail} {d['text']}")
                        d["raw"] = clean(f"{dtail} {d['raw']}")
                    entry["date_text"] = d["text"]
                    entry["date_ink"] = d["ink"]
                    entry["date_dark"] = round(d["dark"], 4)
                    entry["date_present"] = bool(d.get("raw") or d["text"]) \
                        or d["ink"] >= TEXT_INK_MIN or d["overlay"] > 0
                    if not entry["date_present"] and d["reg"] is not None:
                        zone = pymupdf.Rect(d["reg"].x0, min(rect.y0, drect.y0) - 0.5 * lh,
                                            d["reg"].x1, max(rect.y1, drect.y1) + 0.5 * lh)
                        stray = [w for w in words if w[5] >= CONF_CONTENT
                                 and pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2) in zone
                                 and DATE_RE.search(str(w[4]))]
                        if stray:
                            entry["date_present"] = True
                            entry["date_text"] = clean(" ".join(str(w[4]) for w in stray))
                    entry["date_unverified"] = False
                    entry["date_shared"] = False
                    if entry["date_present"] and d["ink"] < TEXT_INK_MIN and d["overlay"] == 0 \
                            and d["reg"] is not None:
                        zone = pymupdf.Rect(d["reg"].x0 - TEXT_X_SLACK * lh,
                                            min(d["reg"].y0, rect.y0 - 0.5 * lh),
                                            d["reg"].x1,
                                            max(d["reg"].y1, rect.y1 + 0.5 * lh, drect.y1 + 0.5 * lh))
                        dmid = (drect.y0 + drect.y1) / 2
                        rows = [pymupdf.Rect(f["rect"]) for f in r["fields"].values()
                                if f.get("found") and f.get("page") == pno and "signed" in f] + [rect]
                        zone.y0 = max(zone.y0, min(x.y0 for x in rows) - 0.5 * lh)
                        zone.y1 = min(zone.y1, max(x.y1 for x in rows) + 0.5 * lh)
                        mine = sorted((((w[0] + w[2]) / 2, (w[1] + w[3]) / 2, norm(w[4]))
                                       for w in words if w[5] >= CONF_CONTENT
                                       and pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2) in zone
                                       and re.search(r"\d", str(w[4]))),
                                      key=lambda m: abs(m[1] - dmid))

                        def taken(m):
                            return any(abs(m[0] - x) < lh and abs(m[1] - y) < 0.5 * lh and m[2] == n
                                       for x, y, n in claimed_dates)
                        free = [m for m in mine if not taken(m)]
                        if mine and not free:
                            entry["date_present"] = False
                            entry["date_shared"] = True
                        if free:
                            claimed_dates.extend(m for m in free if abs(m[1] - free[0][1]) < 0.5 * lh)
                        if debug:
                            print(f"    DBG {key} date label={[round(x) for x in drect]} zone={[round(x) for x in zone]} mine={[(round(x), round(y), t) for x, y, t in mine]} free={len(free)}")
                    if not entry["date_present"] and img is not None and d["pref_reg"] is not None:
                        area = faint_area(page, d["pref_reg"], words, drect.height,
                                          img.info.get("inverted", False))
                        floor = TEMPLATES.get(form, {}).get("faint_floor", {}).get(key + ":date", 0.0)
                        entry["date_unverified"] = (d["dark"] >= TEXT_DARK_UNVERIFIED
                                                    or area - floor >= FAINT_MIN_AREA)
                        entry["date_area"] = round(area, 3)
                else:
                    entry.update(date_text="", date_ink=0, date_dark=0.0,
                                 date_present=False, date_unverified=False)
            else:
                entry["filled"] = bool(m.get("raw") or m["text"]) or m["ink"] >= TEXT_INK_MIN \
                    or m["overlay"] > 0
                entry["ink_unverified"] = (not entry["filled"] and img is not None
                                           and m["dark"] >= TEXT_DARK_UNVERIFIED)
                if not entry["filled"] and not entry["ink_unverified"] and spec.get("geom") == "right":
                    entry["ink_unverified"] = stacked_above(r["fields"], pno, rect, words)
                if not entry["filled"] and not entry["ink_unverified"] and img is not None \
                        and m["pref_reg"] is not None and m["dark"] >= CROP_OCR_MIN_DARK:
                    area = faint_area(page, m["pref_reg"], words, rect.height,
                                      img.info.get("inverted", False))
                    floor = TEMPLATES.get(form, {}).get("faint_floor", {}).get(key, 0.0)
                    entry["ink_unverified"] = area - floor >= FAINT_MIN_AREA
                    entry["faint_area"] = round(area, 3)
            r["fields"][key] = entry
            if debug:
                print(f"    DBG {key:14s} p{pno} ink={m['ink']:5d} ov={m['overlay']} "
                      f"dark={m['dark']:.4f} geom={m['geom']} text={m['text'][:24]!r} "
                      f"raw={m.get('raw','')[:24]!r} faint={entry.get('faint_area', '-')} "
                      f"date={entry.get('date_text', '-')!r}/{entry.get('date_area', '-')}")

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
                                               "ever_found": set(), "all": set(wanted),
                                               "faint_floor": {}})
        prof["page_counts"].add(pages)
        prof["ever_found"] |= found
        for k, fld in t["fields"].items():
            for src, dst in (("faint_area", k), ("date_area", k + ":date")):
                if src in fld:
                    prof["faint_floor"][dst] = min(prof["faint_floor"].get(dst, 9e9),
                                                       fld[src], FAINT_FLOOR_CAP)
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
        elif f.get("faint"):
            unsure.append(f"{key}: faint or very small signature, check by eye")
        elif f.get("maybe_ink"):
            unsure.append(f"{key}: only {f.get('ink')} stray marks, unclear if signed")
        elif f.get("below"):
            unsure.append(f"{key}: something sits near the signature line but off it, check by eye")
        else:
            bad.append(f"{key}: not signed (blank)")
        if not f.get("date_present"):
            if not f.get("date_found"):
                unsure.append(f"{key}: date line not found on the page")
            elif f.get("date_shared"):
                unsure.append(f"{key}: the only date nearby already serves the other "
                              "signature line, check by eye")
            elif f.get("date_unverified"):
                unsure.append(f"{key}: date is written but could not be read, check by eye")
            else:
                bad.append(f"{key}: date blank")

    for key in spec["text_fields"]:
        if key in spec.get("optional_fields", ()):
            continue
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
    if bad and len(unsure) > len(bad):
        return "not_sure", unsure + [f"(also blank: {'; '.join(bad)})"]
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


def check_one(args):
    p, use_ocr, debug = args
    try:
        r = analyse(p, use_ocr=use_ocr, debug=debug)
        v, why = verdict(r)
    except Exception as e:
        r = {"file": p.name, "form": "?", "pages": 0, "flags": [], "fields": {}}
        v, why = "not_sure", [f"error reading file: {type(e).__name__}: {e}"]
    return r, v, why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--out", default=str(pathlib.Path.home() / "deca-form-check/sorted"))
    ap.add_argument("--csv", default=str(pathlib.Path.home() / "deca-form-check/verdicts.csv"))
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
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
    job = (not a.no_ocr, a.debug)
    if a.workers > 1 and len(files) > 1:
        import multiprocessing
        pool = multiprocessing.get_context("fork").Pool(a.workers)
        results = pool.imap(check_one, [(p, *job) for p in files])
    else:
        results = (check_one((p, *job)) for p in files)
    for p, (r, v, why) in zip(files, results):
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

    if not rows:
        sys.exit(f"no PDF or image files found under {root}")
    with open(a.csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\n{dict(tally)}  ->  {a.csv}  |  {out}/")
    if unmatched:
        print(f"{unmatched} file(s) not in the manifest, sorted under unknown_student/")


if __name__ == "__main__":
    main()
