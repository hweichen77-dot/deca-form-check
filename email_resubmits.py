# /// script
# requires-python = ">=3.10"
# ///
import csv, sys, pathlib, argparse, re, json, subprocess
from collections import defaultdict

FORM_NAMES = {
    "contract": "Parent-Student Contract",
    "conduct": "DECA Trip Code of Conduct",
    "formB": "California DECA Form B (medical release)",
}

FIELD_NAMES = {
    "parent_sig": "parent/guardian signature",
    "student_sig": "student signature",
    "parent_name": "parent printed name",
    "student_name": "student printed name",
    "delegate_name": "name of delegate",
    "home_address": "home address",
    "dob": "date of birth",
    "high_school": "name of high school",
    "allergies": "known allergies",
    "medication": "special medication",
    "tetanus": "date of last tetanus shot",
    "heart_condition": "heart condition history",
    "physical_restrictions": "physical restrictions",
    "other_conditions": "other conditions",
    "family_doctor": "family doctor",
    "insurance_company": "insurance company name",
    "policy_number": "insurance policy number",
}

SUBJECT = "Your DECA registration forms need one more thing"


def humanize_reason(raw):
    out = []
    for part in raw.split("; "):
        part = part.strip()
        if part.startswith("("):
            continue
        m = re.match(r"^(\w+): (.+)$", part)
        if not m:
            continue
        field, problem = m.group(1), m.group(2)
        label = FIELD_NAMES.get(field, field.replace("_", " "))
        if "date blank" in problem:
            out.append(f"the date next to the {label}")
        elif "not signed" in problem or problem == "blank":
            out.append(label)
        else:
            out.append(f"{label} ({problem})")
    return out


def body_for(first, items):
    lines = [f"Hi {first},", ""]
    if len(items) == 1:
        form, fields = items[0]
        lines.append(f"Your {FORM_NAMES.get(form, form)} came back incomplete. "
                     "These are blank:")
    else:
        n = {2: "Two", 3: "Three"}.get(len(items), str(len(items)))
        lines.append(f"{n} of your DECA forms came back incomplete. "
                     "Here is what is missing:")
    lines.append("")
    for form, fields in items:
        if len(items) > 1:
            lines.append(f"{FORM_NAMES.get(form, form)}")
        for f in fields:
            lines.append(f"  - {f}")
        lines.append("")
    lines += [
        "Fill those in and re-upload through the registration form. "
        "Anything you already filled out is fine, so you do not need to redo it.",
        "",
        "If you are not sure which part I mean, reply and I will send a screenshot.",
        "",
        "Jason",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdicts", default="verdicts.csv")
    ap.add_argument("--manifest", default="manifest.csv")
    ap.add_argument("--out", default="drafts.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show", action="store_true", help="print each email in full")
    a = ap.parse_args()

    manifest = list(csv.DictReader(open(a.manifest)))
    by_id = {m["file_id"]: m for m in manifest if m["file_id"]}

    per_student = defaultdict(list)
    unmatched = []
    for row in csv.DictReader(open(a.verdicts)):
        if row["verdict"] != "incorrect":
            continue
        rec = by_id.get(row.get("file_id", ""))
        if not rec:
            unmatched.append(row["file"])
            continue
        key = (rec["first"], rec["last"], rec["student_email"] or rec["dad_email"])
        fields = humanize_reason(row["reasons"])
        if fields:
            per_student[key].append((row["form"], fields))

    drafts = []
    for (first, last, email), items in sorted(per_student.items()):
        if not email:
            unmatched.append(f"{first} {last} (no email address)")
            continue
        drafts.append({"to": email, "name": f"{first} {last}",
                       "subject": SUBJECT, "body": body_for(first, items)})

    if a.limit:
        drafts = drafts[:a.limit]

    with open(a.out, "w") as fh:
        for d in drafts:
            fh.write(json.dumps(d) + "\n")

    print(f"{len(drafts)} emails prepared -> {a.out}")
    if unmatched:
        print(f"{len(unmatched)} could not be matched to a student:")
        for u in unmatched[:10]:
            print(f"   {u}")
    if a.show:
        for d in drafts:
            print("\n" + "=" * 60)
            print(f"To: {d['to']}  ({d['name']})")
            print(f"Subject: {d['subject']}\n")
            print(d["body"])
    else:
        print("\nRun with --show to read them. Nothing has been sent.")


if __name__ == "__main__":
    main()
