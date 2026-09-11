# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf", "pillow", "pillow-heif", "pytesseract"]
# ///
import pymupdf, pathlib, sys, re, csv, io, os, json, argparse, shutil
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
INK_MAYBE = 1
OCR_DPI = 150
OCR_IF_TEXT_UNDER = 320
JUNK_MAX_CHARS = 24
CONF_LABEL = 0
CONF_CONTENT = 60
DARK_MIN = 0.055
DARK_MAYBE = 0.030
DARK_UNVERIFIED = 0.12
INVISIBLE = dict.fromkeys(map(ord, "​‌‍﻿\xa0"), None)
IMG_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp"}

FORM_SPECS = {
    "contract": {
        "geom": "right",
        "fingerprints": ["Parent-Student Contract", "NAME OF PARENT", "NAME OF STUDENT"],
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
        "fingerprints": ["CODE OF CONDUCT", "Student Overnight Trip Expectations",
                         "Parent Printed Name"],
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
                         "Release of Claim for Damages"],
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


def detect_form(doc, path):
    text = " ".join(p.get_text() for p in doc)
    scores = Counter()
    for key, spec in FORM_SPECS.items():
        for fp in spec["fingerprints"]:
            if fp.lower() in text.lower():
                scores[key] += 2
        for hint in spec["filename_hints"]:
            if hint in pathlib.Path(path).name.lower():
                scores[key] += 1
    return scores.most_common(1)[0][0] if scores else "unknown"


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


def regions(rect, page, width=300, prefer="right", allow_other=True, tight=False,
            words=()):
    h = max(rect.height, 4.0)
    width = width / 11.0 * h
    up = (2.0 if tight else 3.1) * h
    rx0, rx1 = right_span(rect, page, words, width)
    r = {
        "right": pymupdf.Rect(rx0, max(0, rect.y0 - (0.55 if tight else 2.4) * h), rx1,
                              rect.y1 + (0.2 if tight else 0.7) * h),
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


def render(page):
    pix = page.get_pixmap(dpi=OCR_DPI)
    from PIL import Image as PILImage
    return PILImage.open(io.BytesIO(pix.tobytes("png")))


def dark_frac(img, region):
    scale = OCR_DPI / 72.0
    box = (max(0, int(region.x0 * scale)), max(0, int(region.y0 * scale)),
           min(img.width, int(region.x1 * scale)), min(img.height, int(region.y1 * scale)))
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        return 0.0
    g = img.crop(box).convert("L")
    h = g.histogram()
    return sum(h[:165]) / max(sum(h), 1)


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


def probe(page, rect, drawings, words, width=300, img=None, prefer="right",
          allow_other=False, fallback_text=True):
    best = None
    for geom, reg in regions(rect, page, width, prefer,
                             allow_other=allow_other, tight=img is not None,
                             words=words).items():
        ink = ink_in(reg, drawings)
        txt = text_in(reg, words, DROP)
        dk = dark_frac(img, reg) if img is not None else 0.0
        raw = text_in(reg, words, DROP, min_conf=0) if img is not None else txt
        if geom != prefer and not fallback_text:
            txt = raw = ""
        cand = {"ink": ink, "text": txt, "geom": geom, "dark": dk, "raw": raw}
        if best is None or (ink, len(txt), dk) > (best["ink"], len(best["text"]), best["dark"]):
            best = cand
    return best or {"ink": 0, "text": "", "geom": None, "dark": 0.0, "raw": ""}


def marked(entry):
    return entry["ink"] >= INK_MIN or entry["dark"] >= DARK_MIN


def maybe_marked(entry):
    return (INK_MAYBE <= entry["ink"] < INK_MIN) or (DARK_MAYBE <= entry["dark"] < DARK_MIN)


def page_role(page, native):
    if page.get_images(full=True):
        return "content"
    if any(it[0] != "re" for d in paintable(page.get_drawings())
           for it in d["items"]):
        return "content"
    return "junk" if len(native) < JUNK_MAX_CHARS else "content"


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
        if use_ocr and (force_ocr or page.get_images(full=True)) \
                and len(native) < OCR_IF_TEXT_UNDER:
            try:
                img = render(page)
                words = words + ocr_words(page, img)
                used = True
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        out.append({"page": page, "words": words, "img": img, "ocr": used,
                    "err": err, "role": role, "snippet": ""})
    return out


def detect_form_text(text, path):
    scores = Counter()
    low = text.lower()
    fname = pathlib.Path(path).name.lower()
    for key, spec in FORM_SPECS.items():
        for fp in spec["fingerprints"]:
            if fp.lower() in low:
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
        for key, (ftype, variants) in targets.items():
            if r["fields"].get(key, {}).get("found"):
                continue
            label, rect = find_label(words, variants)
            if rect is None:
                continue
            m = probe(page, rect, drawings, words,
                      width=260 if ftype == "text" else 300, img=img,
                      prefer=spec.get("geom", "right"),
                      allow_other=(ftype == "sig"), fallback_text=False)
            entry = {"found": True, "page": pno, "label": label, "geom": m["geom"],
                     "ocr": b["ocr"], "ink": m["ink"], "dark": round(m["dark"], 4),
                     "text": m["text"]}
            if ftype == "sig":
                mark = m.get("raw") or m["text"]
                entry["signed"] = m["ink"] >= INK_MIN or bool(mark)
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
                              prefer=spec.get("geom", "right"), allow_other=True)
                    entry["date_text"] = d["text"]
                    entry["date_ink"] = d["ink"]
                    entry["date_dark"] = round(d["dark"], 4)
                    entry["date_present"] = bool(d.get("raw") or d["text"]) \
                        or d["ink"] >= INK_MIN
                else:
                    entry.update(date_text="", date_ink=0, date_dark=0.0,
                                 date_present=False)
            else:
                entry["filled"] = bool(m.get("raw") or m["text"]) or m["ink"] >= INK_MIN
            r["fields"][key] = entry
            if debug:
                print(f"    DBG {key:14s} p{pno} ink={m['ink']:5d} dark={m['dark']:.4f} "
                      f"geom={m['geom']} text={m['text'][:24]!r} raw={m.get('raw','')[:24]!r}")

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
            if key in spec.get("soft_fields", ()):
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

    rows, tally = [], Counter()
    for p in files:
        try:
            r = analyse(p, use_ocr=not a.no_ocr, debug=a.debug)
            v, why = verdict(r)
        except Exception as e:
            r = {"file": p.name, "form": "?", "pages": 0, "flags": [], "fields": {}}
            v, why = "not_sure", [f"error reading file: {type(e).__name__}: {e}"]
        tally[v] += 1
        link = out / v / p.name
        try:
            os.symlink(p.resolve(), link)
        except FileExistsError:
            pass
        rows.append({"file": r["file"], "form": r.get("form", "?"),
                     "pages": r.get("pages", 0), "verdict": v,
                     "reasons": "; ".join(why), "flags": "; ".join(r.get("flags", [])),
                     "path": str(link)})
        if not a.quiet:
            print(f"[{v:9s}] {r.get('form','?'):9s} {r['file'][:52]:52s} {'; '.join(why)[:80]}")

    with open(a.csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\n{dict(tally)}  ->  {a.csv}  |  {out}/")


if __name__ == "__main__":
    main()
