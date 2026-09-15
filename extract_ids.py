# /// script
# requires-python = ">=3.10"
# dependencies = ["openpyxl"]
# ///
import openpyxl, pathlib, sys, re, csv

FORM_HEADERS = {"medical release": "formB", "code of conduct": "conduct",
                "parent-student contract": "contract"}
COL_HEADERS = dict(first="legal first name", last="legal last name", grade="grade (",
                   student_email="email address", dad_email="father/guardian email",
                   mom_email="mother/guardian 2  email", phone="cell phone")

ID_PATTERNS = [r"[?&]id=([\w-]+)", r"/file/d/([\w-]+)", r"/document/d/([\w-]+)"]

def file_id(cell):
    s = str(cell or "").strip()
    for pat in ID_PATTERNS:
        m = re.search(pat, s)
        if m:
            return m.group(1), ("upload" if "id=" in pat else "pasted_link")
    return (None, "unparsed") if s else (None, "empty")

def cell(row, i):
    return row[i] if i < len(row) else None


def locate(header, needle):
    hits = [i for i, h in enumerate(header) if needle in str(h or "").lower()]
    if not hits:
        sys.exit(f"no column header containing {needle!r}")
    return hits[0]


def main(xlsx, out):
    wb = openpyxl.load_workbook(pathlib.Path(xlsx).expanduser(), read_only=True, data_only=True)
    sheet = list(wb.worksheets[0].iter_rows(values_only=True))
    header = sheet[0]
    FORMS = {locate(header, k): v for k, v in FORM_HEADERS.items()}
    COLS = {k: locate(header, v) for k, v in COL_HEADERS.items()}
    print("columns:", {v: k for k, v in FORMS.items()}, COLS)
    rows = [r for r in sheet[1:] if any(x is not None and str(x).strip() for x in r)]
    recs, stats = [], {"upload": 0, "pasted_link": 0, "unparsed": 0, "empty": 0}
    for i, r in enumerate(rows, start=2):
        base = {k: str(cell(r, c) or "").strip() for k, c in COLS.items()}
        for col, form in FORMS.items():
            fid, kind = file_id(cell(r, col))
            stats[kind] += 1
            recs.append({**base, "sheet_row": i, "form": form,
                         "file_id": fid or "", "link_kind": kind,
                         "raw_url": str(cell(r, col) or "").strip()})
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(recs[0]))
        w.writeheader()
        w.writerows(recs)
    print(f"students={len(rows)} slots={len(recs)} -> {out}")
    print(f"link kinds: {stats}")
    for k, c in COLS.items():
        missing = sum(1 for r in rows if not str(cell(r, c) or "").strip())
        print(f"  {k:15s} missing={missing}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
