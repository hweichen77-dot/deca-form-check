# /// script
# requires-python = ">=3.10"
# ///
import csv, subprocess, sys, pathlib, argparse, shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

MAGIC = {
    b"%PDF": "pdf", b"\xff\xd8\xff": "jpeg", b"\x89PNG": "png",
    b"GIF8": "gif", b"II*\x00": "tiff", b"MM\x00*": "tiff", b"%!PS": "ps",
}


def sniff(path):
    head = path.open("rb").read(32)
    for sig, kind in MAGIC.items():
        if head.startswith(sig):
            return kind
    if head[4:12] in (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftypmif1"):
        return "heic"
    if head[:2] == b"PK":
        return "zip/office"
    if head.lstrip()[:1] in (b"<", b"{"):
        return "NOT_A_FILE_login_page"
    return "unknown"


def have(fid, raw):
    d = raw / fid
    return d.is_dir() and any(d.iterdir())


def fetch_batch(args):
    remote, raw, ids = args
    pairs = []
    for fid in ids:
        (raw / fid).mkdir(parents=True, exist_ok=True)
        pairs += [fid, f"{raw / fid}/"]
    p = subprocess.run(["rclone", "backend", "copyid", remote, *pairs],
                       capture_output=True, text=True, timeout=900)
    return ids, p.returncode, (p.stderr or "").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("inventory")
    ap.add_argument("--remote", default="decadrive:")
    ap.add_argument("--raw", default=str(pathlib.Path.home() / "deca-form-check/raw"))
    ap.add_argument("--batch", type=int, default=25)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    if not shutil.which("rclone"):
        sys.exit("rclone is not installed. brew install rclone")
    remotes = subprocess.run(["rclone", "listremotes"], capture_output=True,
                             text=True).stdout.split()
    if a.remote not in remotes:
        sys.exit(f"remote {a.remote!r} not configured. Found: {remotes or 'none'}\n"
                 f"Set one up with:\n"
                 f"  rclone config create {a.remote.rstrip(':')} drive scope=drive.readonly")

    raw = pathlib.Path(a.raw)
    raw.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(open(a.manifest)))
    by_id = {}
    for r in rows:
        if r["file_id"]:
            by_id.setdefault(r["file_id"], r)

    todo = [f for f in by_id if not have(f, raw)]
    cached = len(by_id) - len(todo)
    print(f"{len(by_id)} unique files, {cached} already downloaded, {len(todo)} to fetch")

    if todo:
        batches = [(a.remote, raw, todo[i:i + a.batch])
                   for i in range(0, len(todo), a.batch)]
        done = 0
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            for ids, rc, err in ex.map(fetch_batch, batches):
                done += len(ids)
                got = sum(1 for f in ids if have(f, raw))
                print(f"  {done}/{len(todo)}  ({got}/{len(ids)} in this batch)"
                      + (f"  {err[:90]}" if rc != 0 and err else ""))

    out, tally, bad = [], Counter(), []
    for fid, rec in by_id.items():
        d = raw / fid
        files = [f for f in d.iterdir() if f.is_file()] if d.is_dir() else []
        if not files:
            tally["MISSING"] += 1
            bad.append((fid, rec, "not downloaded"))
            out.append([fid, "", "", 0, "MISSING", rec["first"], rec["last"],
                        rec["form"], rec["link_kind"]])
            continue
        f = files[0]
        kind = sniff(f)
        tally[kind] += 1
        if kind.startswith("NOT_A_FILE") or kind == "unknown":
            bad.append((fid, rec, kind))
        out.append([fid, f.name, kind, f.stat().st_size, "ok", rec["first"],
                    rec["last"], rec["form"], rec["link_kind"]])

    with open(a.inventory, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file_id", "filename", "type", "bytes", "status",
                    "first", "last", "form", "link_kind"])
        w.writerows(out)

    print(f"\ntypes: {dict(tally)}")
    print(f"wrote {a.inventory}  |  files in {raw}")
    if bad:
        print(f"\n{len(bad)} need attention:")
        for fid, rec, why in bad[:25]:
            print(f"  {rec['first']} {rec['last']:<18} {rec['form']:<9} "
                  f"[{rec['link_kind']}] {why}")
        if len(bad) > 25:
            print(f"  ... and {len(bad) - 25} more, see {a.inventory}")


if __name__ == "__main__":
    main()
