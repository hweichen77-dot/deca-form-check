# deca-form-check

Sorts DECA registration forms into three folders so you only open the ones that need attention.

Our chapter collects three signed forms from every student through a Google Form. At around 200 students that is close to 600 PDFs, and checking them by hand takes an afternoon and still misses things. This reads them instead and tells you which ones to send back, and why.

It runs purely on your local machine.

## Setting up

Everything runs on a Mac with Homebrew. Once, per machine:

```
brew install uv tesseract rclone
git clone https://github.com/hweichen77-dot/deca-form-check.git ~/deca-form-check
cd ~/deca-form-check
```

`uv` installs each script's Python packages on first run, so there is no `pip install` step. `tesseract` reads scanned and photographed forms. `rclone` is only needed for pulling files out of Google Drive.

Clone into `~/deca-form-check` specifically. The scripts default their working folders (`raw/`, `sorted/`, `templates/`, `verdicts.csv`) to that path, and cloning somewhere else means passing `--raw`, `--out`, `--csv`, and `--templates` on every run.

If you will be downloading from Drive, connect rclone to the Google account that owns the registration form. This opens a browser once and then remembers you:

```
rclone config create decadrive drive scope=drive.readonly
```

## Running it

There are two ways in, depending on whether the forms are already on your machine.

### If you already have the PDFs in a folder

```
uv run deca_check.py ~/path/to/forms
```

That is the whole thing. It prints one line per file as it goes and finishes with a count.

### Full run from the Google Form

Four commands, in order. Each reads what the one before it wrote.

The data comes from the Google Sheet named "(Form Checking) 2026-27 VC DECA Registration Form (Responses)", which is the response sheet linked to the registration Google Form. Download it first. Open that sheet in Google Sheets and choose File, Download, Microsoft Excel. It lands in `~/Downloads` with the same name and an `.xlsx` extension.

```
uv run extract_ids.py ~/Downloads/"(Form Checking) 2026-27 VC DECA Registration Form (Responses).xlsx" manifest.csv
```

Reads the spreadsheet and writes `manifest.csv`, one row per student per form, with the Drive file id for each upload. Prints how many students there are and how many cells were empty.

```
uv run fetch.py manifest.csv inventory.csv
```

Downloads every file in the manifest into `raw/<file_id>/`, checks each one is really a PDF or image and not a Drive sign-in page, and writes `inventory.csv`. Safe to rerun, it skips anything already downloaded. Ends with a list of students whose file could not be fetched, if any.

```
uv run deca_check.py raw
```

Checks every downloaded file. Writes `verdicts.csv` and fills `sorted/correct`, `sorted/incorrect`, and `sorted/not_sure` with links to the originals.

```
uv run email_resubmits.py --show
```

Groups the `incorrect` verdicts by student and prints one draft email per person naming exactly which fields are blank. Also writes them to `drafts.jsonl`. Nothing is sent. Read through them, then send however you normally would.

### After the run

Open `sorted/not_sure/` and look at each file by eye. These are the ones the tool could not measure. `verdicts.csv` has the reason for every file, including these.

Once resubmits are in, drop the new files into a folder and run `deca_check.py` on just that folder, or rerun the whole chain. `fetch.py` will pick up the new Drive ids from a fresh manifest and skip the ones it already has.

### Every flag

`deca_check.py`

```
uv run deca_check.py <folder or file>
    --debug          print every field measurement, for working out why a form was misread
    --no-ocr         skip tesseract, much faster, scanned forms all land in not_sure
    --quiet          no per-file lines, just the final count
    --templates DIR  reference forms to self-check against (default ~/deca-form-check/templates)
    --out DIR        where sorted/ goes (default ~/deca-form-check/sorted)
    --csv FILE       where verdicts go (default ~/deca-form-check/verdicts.csv)
```

`extract_ids.py`

```
uv run extract_ids.py <responses.xlsx> <manifest.csv>
```

`fetch.py`

```
uv run fetch.py <manifest.csv> <inventory.csv>
    --remote NAME    rclone remote (default decadrive:)
    --raw DIR        download folder (default ~/deca-form-check/raw)
    --batch N        file ids per rclone call (default 25)
    --workers N      parallel rclone calls (default 4)
```

`email_resubmits.py`

```
uv run email_resubmits.py
    --verdicts FILE  (default verdicts.csv)
    --manifest FILE  (default manifest.csv)
    --out FILE       (default drafts.jsonl)
    --show           print each draft in full
    --limit N        only the first N students, for checking a few
```

`make_template.py`

```
uv run make_template.py <signed.pdf> <templates/name.pdf>
    --pages 5        keep only page 5 (or 4-5) in the output
```

`analyze.py`

```
uv run analyze.py <file or folder>       # no arguments means ~/Downloads
```

## The three folders

A form only lands in `correct` on positive evidence. When the tool cannot measure something it says so rather than assuming the form is fine. A wrong `incorrect` costs a student one annoyed email. A wrong `correct` sends someone to a conference on an unsigned medical release, so the two mistakes are not treated as equally bad.

| Folder | Meaning |
| --- | --- |
| `correct` | Every required signature has a mark, every date is filled, every required field has something in it |
| `incorrect` | A field was found and measured as empty. This is a real defect you can email a student about |
| `not_sure` | Something could not be measured. A label that would not locate, a page that would not OCR, an unreadable file |

Reasons are written per form into `verdicts.csv`, so a resubmit email can name the exact blank fields instead of saying the form was rejected.

## What gets checked

The Parent-Student Contract and the Trip Code of Conduct each need a printed name, a signature, and a date from the parent and from the student.

California DECA Form B is longer. The blank marks the student's part with two red boxes, and everything outside them is already filled in by the school. The tool checks the fifteen fields that belong to the student, which are the delegate's name, home address, date of birth and school, both signatures with their dates, the medical section, and the insurance company and policy number.

The chapter advisor and School/ROP signatures come pre-signed on the blank, so they are not the student's responsibility and are not checked. A missing tetanus date routes to review rather than a resubmit, since a family may genuinely not know it.

## How it tells a signed line from a blank one

Two different measurements, depending on what the file contains.

In a PDF with a text layer, handwriting is vector path segments. A blank signature line has none at all, and a signed one in our sample had between 173 and 2,869. That is a categorical difference rather than a tuned threshold, which is why it holds up on forms nobody has looked at yet.

A scan or a photo has no vector paths, so OCR runs over the signature area at zero confidence. Tesseract returns nothing on an empty line and garbage on a scrawl, things like `y =` or `se ©` or `Amander qe`. Any token at all means somebody wrote something. Pixel darkness was tried first and abandoned, because two signatures on the same page in the same pen measured 0.0489 and 0.0637, and on one large scan a signed line read darker than a blank one on the reference. There is no threshold that separates them.

Ink also has to be visible. A white shape is not a signature, and neither is ink that a later white box paints over, which is what happens when someone whites out a mistake in a PDF editor. Printed underlines are skipped whether they are drawn as rectangles or as flat filled lines.

Two details took a while to get right. Every measurement is a multiple of the matched label's own height rather than a fixed number of points, because a phone scan can be four times the size of a letter page and fixed offsets quietly miss the answer. And a signature is looked for both beside and above its line, since people sign wherever there is room, but only ink counts in the region above, because text up there is the previous line of the form rather than an answer.

## References and pages

Files in `templates/` describe each form's layout. There can be several per form, because a form that has been revised a few times exists in several layouts at once. Each one is checked at startup, and a label that cannot be located on any reference prints a warning before the run rather than letting a pile of forms quietly pass.

Page count is not treated as evidence. Students submit anything from the full packet to the signature page on its own, and both are fine when the fields are filled. The scanner finds fields by their labels wherever those labels sit, then reports which pages carried something and how many had nothing to check. Separator pages, the ones holding nothing but the word "Tab 1", are skipped and named in the output. A document with no recognisable fields anywhere goes to `not_sure`.

`make_template.py` turns a signed form into a blank reference by wiping the areas where answers go and leaving the printed labels and rules alone.

## Known limits

Form B's two `Phone:` fields and its top-right `Date:` are not checked. Three fields on that page share the label "Phone" and several share "Date", and telling them apart needs positional rules that are not written yet. The doctor's phone is the exception, since it can be found by looking along the same line as the doctor's name.

Ink hidden under a single white box is detected. Ink covered jointly by two adjacent boxes is not, because the check tests one shape at a time.

Some scans defeat the label matcher entirely. Those go to `not_sure`, which is where they belong, though it does mean a stack of Form B scans can produce a bigger review pile than the other two forms.

Tested against thirteen labelled samples covering blank templates, stylus signatures, typed fields, scans, a phone photo, a Google Docs screenshot, and signature pages submitted on their own, plus corrupt, empty, and unrelated files. Every one landed in the right folder. Broken files fail with a reason instead of stopping the run.

## About the files

The forms hold medical information about minors, dates of birth, home addresses, and signatures. Nothing in this repo includes them. `.gitignore` covers the working folders, the CSVs, and the reference templates, which carry real staff signatures. Keep the downloaded forms off shared drives and delete them once the resubmits are in. The spreadsheet is the record, not your Downloads folder.

## How the pieces fit together

For a full run against the Google Form responses, the scripts go in this order. Each one reads the file the previous one wrote.

```
extract_ids.py   responses.xlsx  ->  manifest.csv
fetch.py         manifest.csv    ->  raw/<file_id>/<name>  +  inventory.csv
deca_check.py    raw/            ->  verdicts.csv  +  sorted/
email_resubmits.py  verdicts.csv + manifest.csv  ->  drafts.jsonl
```

If you already have the PDFs in a folder, skip straight to `deca_check.py`. `make_template.py` and `analyze.py` are maintenance tools you reach for when a form changes, not part of the weekly run.

Every script carries its dependencies in a header comment, so `uv run script.py` installs what it needs into a throwaway environment. There is no requirements file to keep in sync.

## Every file, and how it works

The repo tracks eight files. Six are Python scripts, and the other two are this README and `.gitignore`. Each script is self-contained. The first few lines of each one declare its Python version and packages in the inline format that `uv` understands, so `uv run script.py` creates a private environment with the right packages on first use and reuses it after that. Nobody needs to `pip install` anything or activate a virtualenv.

### deca_check.py

The checker, and the reason the repo exists. It takes a folder of PDFs and images, works out which of the three DECA forms each one is, finds every field that a student or parent was supposed to fill, measures whether something was written there, and sorts the file into `correct`, `incorrect`, or `not_sure`. Everything else in the repo either prepares its input or reads its output. About 650 lines, and it helps to know the shape before opening it.

```
uv run deca_check.py raw/                      # check a folder
uv run deca_check.py one_form.pdf --debug      # one file, print every measurement
uv run deca_check.py raw/ --no-ocr             # skip tesseract, fast, scans go to not_sure
uv run deca_check.py raw/ --out ./sorted --csv ./verdicts.csv --templates ./templates
```

`FORM_SPECS` at the top is the only place a form is described. Each entry has fingerprint phrases used to recognise the form from its text, filename hints as a tie-breaker, a `geom` saying whether answers normally sit to the right of a label or above it, and two dictionaries of fields. `signatures` lists signature lines that must also have a date next to them. `text_fields` lists everything else that must have something written in it. Each field maps to a list of label spellings, because the same form has been re-typeset over the years and "Signature of PARENT" and "Signature of Parent" are both in circulation. A label spelt `Anchor|Label` means "the word Label on the same line as Anchor", which is how the doctor's phone number is told apart from the other two Phone fields on Form B. Adding a fourth form means adding one entry here and dropping a reference into `templates/`.

`main` parses flags, loads the templates, walks the input folder for PDFs and images, and runs `analyse` then `verdict` on each. It wipes and recreates the three `sorted/` subfolders on every run, writes one symlink per file into whichever folder the verdict picked, and writes `verdicts.csv`. A file that blows up anywhere in the pipeline gets a `not_sure` row with the exception text as its reason, so one corrupt upload cannot stop the other 599. The console shows one line per file as it goes, with the verdict, the form type, the filename, and the first eighty characters of the reasons.

`open_doc` hands everything to PyMuPDF. HEIC photos from iPhones get converted to PNG in memory first, since PyMuPDF cannot read them directly. The function also reports whether the input is a PDF or an image, which decides later whether OCR is forced.

`page_bundles` prepares each page once. It pulls the native text layer as word boxes, classifies the page with `page_role`, and decides whether to OCR. A page is `junk` when it has no images, no visible vector paths, and under 24 characters of text, which is exactly what a "Tab 1" separator sheet looks like. OCR runs only when the page contains an image and carries fewer than 320 characters of native text, since a page with a real text layer does not need it. When it does run, `render` rasterises the page at 150 dpi, `ocr_words` sends it through Tesseract, and the returned boxes are scaled back to PDF points and merged with the native words. Every word box carries a confidence, 100 for native text and whatever Tesseract reported for OCR.

`detect_form_text` joins all the words from all pages and scores each spec. A fingerprint phrase found in the text is worth two points, a filename hint one. Highest score wins, and a document that matches nothing is `unknown`, which `verdict` turns into `not_sure`.

`find_label` is where most of the difficulty lives. Word boxes from a PDF do not line up with how a human reads a label. "Name of Delegate:" can arrive as three words or one, with the colon attached or floating. So the function normalises every word to lowercase alphanumerics, finds a word whose first three characters match the start of the target, then walks rightwards along the same visual line (within 0.6 of the word's height), gluing words together until the accumulated string equals the target or clearly diverges. Short targets of six characters or fewer must match exactly, so "Date" does not match "Date of Birth". Longer targets match on prefix, so a label with a trailing typo still locates. It returns the bounding rectangle of the matched words, and everything downstream is measured relative to that rectangle.

`regions` turns a label rectangle into the one or two areas where an answer might be. The important idea is that every offset is a multiple of the label's own height, never a fixed number of points. A letter-size PDF page is 612 points wide and a phone photo of the same form can be 2,400, and the same label is proportionally the same size on both, so measuring in label heights makes one set of numbers work for everything. The `right` region runs from the end of the label to a stop word, and `right_span` works that out by walking the words on the line. It skips past suffixes like "Printed" or "Name" that belong to the label, skips underscore runs since those are the blank itself, and stops before the next label on the line (anything ending in a colon, or one of a few known words like "Date" and "Phone"). The `above` region is a box floating over the label, for forms where the signature line is drawn above its caption.

`probe` looks inside those regions and measures three things. `ink_in` counts vector path points that land in the region, after `paintable` has thrown out anything that would not show on paper. `text_in` collects OCR or native words in the region above a confidence floor of 60, drops boilerplate words like "signature" and "parent", and joins what is left. `dark_frac` renders the region and reports the share of pixels darker than 165 on a 0 to 255 grey scale. Signatures are allowed to use both regions, and the best one wins on the tuple (ink, text length, darkness). Text fields only get their preferred region, because the text above a field is the previous line of the form, not an answer. For the fallback region of a signature, text is discarded for the same reason and only ink counts.

`paintable`, `is_visible`, and `is_rule` do the filtering described earlier under "How it tells a signed line from a blank one". A path with both stroke and fill lighter than 0.75 luminance is invisible on white paper and never counts. A rectangle at most 1.2 points tall and at least 8 wide is a printed underline. A path fully inside a later invisible shape has been whited out and is dropped. None of this reads PDF metadata or AcroForm field values. It reads the page content stream, meaning the actual drawing operations, because that is what a stylus signature in Preview or Acrobat produces.

`analyse` runs the above for one file and returns a dictionary. For each field in the spec it tries each page in order until a label locates, then probes. A signature entry records `signed` (five or more ink points, or any OCR token at all, even at zero confidence), `ink_unverified` (a scan with over 12 percent dark pixels but nothing OCR could read), and `maybe_ink` (one to four stray path points in a vector PDF). It then looks for a "Date" label within about four label heights below the signature and probes that too. A text entry records `filled`. The result also lists which pages carried fields, which content pages carried none, and any pages skipped as separators. With `--debug` every probe prints its numbers, which is how you find out why a particular form was misread.

`verdict` reads that dictionary and produces one of three words plus a list of reasons. A field that could not be located is `unsure`. A field located and measured empty is `bad`. A signature with `ink_unverified` or `maybe_ink` is `unsure` with a message telling the reviewer what to look at. Any `bad` makes the form `incorrect`, and if there were also unsure fields they are appended in parentheses so the resubmit email still names every real defect. Only `unsure` gives `not_sure`. With neither, the form is `correct`. Fields listed under `soft_fields` in the spec, which today is just the tetanus date, go to `unsure` rather than `bad` when empty.

`load_templates` runs `analyse` on every file in `templates/` before the real run. It is a self-test rather than a gate. For each form it prints which labels each reference could locate and how many content pages it has, and warns loudly if any label in the spec was never found on any reference. That warning is the signal that a form has been re-typeset and `FORM_SPECS` needs a new spelling. The profiles are stored in `TEMPLATES` but do not currently affect verdicts. If `templates/` is missing the function returns an empty dictionary and the run continues, so a fresh clone works without it.

Tuning constants sit near the top of the file. `INK_MIN` is five path points. `DARK_MIN`, `DARK_MAYBE`, and `DARK_UNVERIFIED` are pixel darkness fractions. `OCR_DPI` is 150. `OCR_IF_TEXT_UNDER` is the character count above which a page is assumed to have a real text layer. Change these with a labelled test set in front of you, because each was arrived at by watching a specific sample go wrong.

Known gap. A signature pasted as a picture into a PDF that still has its text layer is not caught. The page has enough native text that OCR is skipped, and a picture is not a vector path, so the line reads as blank. None of the samples did this, but it is the most likely way a future submission gets a wrong `incorrect`. The fix is to test whether any image on the page overlaps the signature region, and it has not been written yet.

### make_template.py

Turns a signed form into a blank reference for `templates/`. References describe a form's layout so the checker can confirm at startup that it still knows where every field is. They are not supposed to carry anyone's answers or signature, so this script takes a real submission and erases every answer while leaving the printed labels, underlines, and boilerplate alone.

```
uv run make_template.py signed.pdf templates/contract_D.pdf
uv run make_template.py signed.pdf templates/contract_sigpage.pdf --pages 5
```

It imports `deca_check` and reuses `analyse`, `find_label`, and `regions`, so the areas it wipes are exactly the areas the checker will later probe. `field_regions` opens the file, runs `analyse` to learn which form it is, then for every signature and text label in the spec finds the label on each page and computes the answer region. It pads that region upward by a few label heights and downward by one or two so a tall signature is fully covered, and for each signature also finds the Date label below it and clears that as well. It prints each rectangle as it goes, so you can see what will be erased before it happens.

`main` then adds a white redaction annotation over each rectangle and applies them with PyMuPDF's redaction engine, which removes any image inside the box and any line art that touches it. The result has printed labels and rules intact and nothing else. A `--pages` flag keeps only a page or range, for building a reference that models the "signature page only" submission. The output is saved with `garbage=4` so the redacted content is actually gone from the file, not just hidden under a white box.

One thing it does not do. Fields that are not in `FORM_SPECS` are not touched, so anything pre-printed on the form, such as the chapter advisor's signature on Form B, stays. Check the result by eye before adding it to `templates/`.

### extract_ids.py

Reads the registration responses spreadsheet, the Google Sheet named "(Form Checking) 2026-27 VC DECA Registration Form (Responses)" exported as `.xlsx`, and writes `manifest.csv`, one row per student per form. This is the first step of a full run, and the only place the spreadsheet is read.

```
uv run extract_ids.py ~/Downloads/"(Form Checking) 2026-27 VC DECA Registration Form (Responses).xlsx" manifest.csv
```

The Google Form stores an uploaded file as a Drive link in the response cell. `file_id` pulls the id out with three regexes, covering the `?id=` shape Forms writes for uploads and the `/file/d/` and `/document/d/` shapes a student produces if they paste a share link instead. It also reports which shape it saw, so a run prints how many students uploaded versus pasted, and how many cells were empty or could not be parsed.

Column positions are hard-coded at the top in `FORMS` and `COLS`, as zero-based indexes into the sheet row. Columns 24, 25, and 26 are the three form uploads. First name, last name, grade, emails, and phone are read from their own columns. If the form gains or loses a question, those numbers shift and this file needs updating. The run ends by printing how many rows are missing each contact field, which is a quick way to notice that the spreadsheet columns have moved. If every row shows every field missing, the indexes are wrong.

Output columns are the contact fields plus `sheet_row`, `form`, `file_id`, `link_kind`, and `raw_url`. `fetch.py` reads `file_id` to know what to download. `email_resubmits.py` joins on `file_id` to know who to write to. Rows with an empty `file_id` are kept so the count of students who skipped a form is visible, but they are ignored downstream.

### fetch.py

Downloads every file in `manifest.csv` from Google Drive into `raw/<file_id>/<original filename>` and writes `inventory.csv` describing what arrived. It is the only script that touches the network.

```
rclone config create decadrive drive scope=drive.readonly     # once, opens a browser
uv run fetch.py manifest.csv inventory.csv
uv run fetch.py manifest.csv inventory.csv --batch 50 --workers 8
```

It uses `rclone backend copyid`, which fetches Drive files by id rather than by path. That matters because Form uploads all land in one folder with names the student chose, and two students can upload files with the same name. Ids are grouped into batches of 25 and four batches run in parallel, so a full chapter's worth of forms comes down in a couple of minutes. Files already present on disk are skipped, so an interrupted run resumes from where it stopped rather than starting over.

`sniff` reads the first 32 bytes of each downloaded file and matches them against known magic numbers for PDF, JPEG, PNG, GIF, TIFF, HEIC, and zip. The reason this exists is that an unauthenticated Drive request does not fail. Google returns HTTP 200 with a sign-in page, so a naive downloader produces a folder of HTML named `.pdf`. A file that begins with `<` or `{` is reported as `NOT_A_FILE_login_page`, and the run ends with a list of every student whose file was missing or unreadable, so you know who to chase before the checker ever runs.

Before doing anything it checks that rclone is installed and that the named remote exists, and prints the one-line `rclone config create` command if not. The remote needs to be signed in as an account that can see the uploads, which in practice means the account that owns the Google Form. Read-only scope is enough.

### email_resubmits.py

Reads `verdicts.csv`, keeps only the `incorrect` rows, groups them by student, and writes one draft email per student to `drafts.jsonl`. It sends nothing. There is no send flag, and the file is meant to be read over before anything goes out. `not_sure` forms are deliberately excluded, because those need a person to look before anyone is told their form is wrong.

```
uv run email_resubmits.py                       # writes drafts.jsonl
uv run email_resubmits.py --show                # print every draft in full
uv run email_resubmits.py --limit 5 --show      # check a handful first
```

Matching a verdict back to a student works through the symlink. `deca_check.py` writes `sorted/incorrect/<name>.pdf` as a link to `raw/<file_id>/<name>.pdf`, so resolving the link and taking the parent folder name gives the Drive file id, and that id is a key in `manifest.csv`. Verdicts for files that did not come through `fetch.py` will not resolve this way and are listed as unmatched at the end. Students with neither a student email nor a father email are listed there too, since the mother column is not consulted.

`humanize_reason` translates the checker's field keys into words a student recognises, so `delegate_name: blank` becomes "name of delegate" and `parent_sig: date blank` becomes "the date next to the parent/guardian signature". Anything in parentheses in the reason string, which is where `verdict` puts the unverified fields, is skipped, since those need a human look and should not be sent to a student as a defect.

`body_for` assembles the message. One form gets a short sentence and a list. Several forms get a heading per form. The closing lines say that everything already filled in is fine and offer a screenshot on reply. The subject line and the sign-off name are constants near the top of the file. Change the name before using it.

`drafts.jsonl` has one JSON object per line with `to`, `name`, `subject`, and `body`. It is deliberately a neutral format, so whatever sends the mail, whether a Gmail script, a mail merge, or copying and pasting, can read it without caring how it was made.

### analyze.py

A diagnostic dump, used when a form does something unexpected or when adding a new form type. It answers the question "what is actually inside this file" without making any decision about it.

```
uv run analyze.py some_form.pdf
uv run analyze.py raw/                          # every file in the folder
uv run analyze.py                               # defaults to ~/Downloads
```

For each page it prints whether the page is a text layer or a photo, its dimensions in points, how many AcroForm widgets it has and which are filled, how many ink and free-text annotations exist, and counts of vector path segments split into squiggles (six or more segments with some height) and rules (flat and wide). On text pages it lists where signature-related words like "signature", "date", and "guardian" appear, with coordinates. On photo pages it renders the page and reports dark, coloured, and blue-ink pixel fractions. Images that are not PDFs get the pixel report only.

Most of what it measures turned out not to be useful for the verdict. Nobody signs through AcroForm fields, ink annotations are rare, and pixel darkness could not separate signed from blank. It stays in the repo because it is the fastest way to see what a new submission style looks like before deciding how the checker should handle it. Note that it imports PyMuPDF under its older name `fitz` and needs numpy, both declared in its header.

### README.md

This file. The first half explains what the tool does and how it decides, for someone who wants to run it. The second half, from "How the pieces fit together" onward, explains each file for someone who wants to change it. Keep both halves current when the code changes, and keep the reasoning for the thresholds in here, since the code deliberately carries no comments.

### .gitignore

Lists every folder and file type that can carry student data, plus the reference templates. Concretely that is the working folders (`raw/`, `sorted/`, `samples/`, `samples2/`, `testset/`, `edgetest/`), every CSV, every PDF and image extension, `templates/`, and the usual Python and macOS noise. The templates are excluded because the Form B reference carries real staff signatures on its fourth page and the repo is public. Before committing anything new, check that it does not contain a name, a date of birth, or a signature. If a file would be a problem in a public repo, it is a problem here too.

## Folders and working files

None of these are in git. They appear on your machine as you run the scripts.

`templates/` holds reference forms, one or more per form type, checked at startup by `load_templates`. Build a new one from any signed submission with `make_template.py`. The current set on the maintainer's machine has one Conduct form, one Form B, and three Contract layouts covering names on page one, parent and student signatures split across pages five and six, and a signature page submitted alone. A fresh clone runs without this folder, it just skips the startup self-check.

`raw/` is where `fetch.py` puts downloads, one folder per Drive file id. `sorted/` is rebuilt on every checker run and holds only symlinks into `raw/` or wherever the input lived. Deleting `sorted/` loses nothing. Deleting `raw/` means fetching again, which is fine and is what you should do once the resubmits are in.

`manifest.csv` comes from `extract_ids.py`. `inventory.csv` comes from `fetch.py` and is the place to look when a download failed. `verdicts.csv` is the checker's output with one row per file and the reasons written out. `drafts.jsonl` comes from `email_resubmits.py`. All four hold student names, emails, or phone numbers.

`samples/`, `samples2/`, `testset/`, `edgetest/`, `newsamples/`, and `regress/` are labelled inputs used while tuning. `regress/` is the thirteen-file set mentioned above and is the one to run after any change to `deca_check.py`. If a file there changes folder, the change needs a reason.
