# SDS Reader — Offline Safety Data Sheet Extractor

A fully offline tool that reads Safety Data Sheet (SDS) PDFs — one at a time
or in bulk from a folder — and extracts their contents into ~100 structured
variables. Works across vendors/formats because it's built around the
**GHS 16-section standard** (OSHA HazCom 2012, EU REACH/CLP, ANSI Z400.1,
ISO 11014) that essentially every modern SDS follows, rather than being
hard-coded to one manufacturer's layout.

No internet connection is used or required at run time — parsing is 100%
local (pdfplumber for PDF text/tables, regex/heuristics for field matching).

## Why this generalizes across formats

Real-world SDS's vary in wording ("Trade name" vs "Product name" vs "Product
identifier"), header style ("SECTION 1:" vs "1." vs "1 –"), and whether
ingredient/exposure-limit data is a real PDF table or a prose sentence. To
handle that:

1. **Section headers** are located by searching, in order, for section
   number 1 → 16 followed by any of several known title phrasings for that
   section. This tolerates "SECTION 3: COMPOSITION/INFORMATION ON
   INGREDIENTS", "3. Composition and Information on Ingredients", "3 -
   Ingredients", etc.
2. **Fields** within each section are found via a synonym dictionary
   (`sds_reader/schema.py`) — e.g. `flash_point` matches "Flash point",
   `manufacturer_name` matches "Manufacturer", "Supplier", "Company", etc.
3. **Tables** (composition, exposure limits) are matched by scoring each
   PDF table's header row against expected keywords (CAS, TWA, STEL, %...)
   rather than assuming a fixed column order.
4. **Prose fallback**: if Section 3 has no real table (older-style SDS
   sometimes just write "Sodium hydroxide, CAS 1310-73-2, 5-10%." as a
   sentence), a CAS-number-anchored regex pulls out name/CAS/% triples.
5. It was validated against **15 synthetic SDS documents** spanning gas
   cylinders, lab reagents (Sigma-Aldrich-style), EU/REACH format with
   decimal commas and numbered sub-items (BASF-style), a pre-GHS
   Roman-numeral MSDS, a minimal one-page sheet, an 8-row multi-page
   ingredient table, a two-column layout, paints, solid metal alloys,
   pharmaceutical excipients, and prose-only (no table) ingredient lists —
   plus **eight real-world vendor PDFs** (Fisher Scientific, ChemicalBook,
   Fuchs Lubricants, Ingevity, INEOS, SC Johnson, Bartoline, and a 2011-era
   pre-CLP Houghton/RS Components document using R-phrases instead of
   H-codes), which is what actually drove most of the current robustness:
   real documents have wording variety, page breaks mid-section, and layout
   quirks that a from-scratch reconstruction can't fully anticipate.
   Comparing extraction output field-by-field against those real PDFs
   surfaced and fixed real bugs, including several that turned out to be
   the tool's *own* boilerplate-stripping and cross-reference logic firing
   on legitimate content it was never meant to touch: "the revision date"
   and "the revision numbering" (self-referential prose in a document's own
   Section 16 commentary) being mistaken for the actual revision date/
   number fields; "revision number" matching *inside* "revision
   numbering" for want of a word boundary; a bare `\bversion\b` label
   matching "...up to date version." in ordinary prose; a "CAS number"
   mention inside unrelated REACH-registration commentary being mistaken
   for a real CAS value; and a bare page-number line ("1/16") that was
   being stripped from raw text blocks and individual fields but never
   from the whole-document text the Vapour/Dust/Fume keyword scanner
   works from, letting it leak into those specific fields. Also fixed: a
   common REACH pattern for UVCB/complex substances that have no discrete
   CAS number at all (just a "CAS number: —" placeholder with an EC
   number instead) - now recognized as its own composition-extraction
   case rather than falling through to nothing; and a recurring
   column-layout wrap ("Other skin and body [value text]...protection"
   with the label's last word pushed onto the wrapped value line) that
   had shown up in multiple unrelated vendors' Section 8, now handled by
   recognizing "Other skin and body" as a trigger on its own rather than
   requiring "protection" to appear immediately after it. You should
   still spot-check results for anything safety-critical — see
   **Limitations** below.

## Desktop app (no Python needed)

For PCs where Python can't be installed or run, SDS Reader ships as a
standalone Windows app - download `SDS-Reader.exe` from the
[Releases](../../releases/latest) page (or the project website) and
double-click it. No installer, no admin rights, no internet.

1. **Add SDS PDFs** - pick individual files (*Add PDFs…*) or a whole folder
   (*Add folder…*, optionally including subfolders).
2. **Choose the output template** - the built-in summary, or *Upload…* your
   own `.docx` / `.txt` / `.md` containing `{{placeholders}}`. The app checks
   the template straight away, reports how many placeholders it recognised,
   and flags typos. *Save built-in template to edit* gives you a starting
   point; *Placeholder list* shows every available placeholder (double-click
   to copy). Your template choice is remembered between runs.
3. **Choose an output folder** and press **Process**. One filled document
   per SDS, plus an optional combined CSV for the batch. Existing files are
   never overwritten.

If a work PC blocks the single `.exe` (some policies block programs that
unpack into `%TEMP%`), use `SDS-Reader-portable.zip` instead: extract it and
run `SDS-Reader.exe` from the extracted folder.

Run the app from source with `python sds_reader_app.py`.

### Building & releasing

Releases are built by GitHub Actions (`.github/workflows/build.yml`) - on
every push the app is built and self-tested; pushing a version tag publishes
a GitHub Release with `SDS-Reader.exe`, the portable zip, the starter
template and `SHA256SUMS.txt`:

1. Bump `__version__` in `sds_reader/__init__.py` (and `build/version_info.txt`).
2. `git tag v1.0.0 && git push origin v1.0.0` (the tag must match `__version__`).

To build locally: `pip install -r requirements.txt pyinstaller`, then
`pyinstaller --noconfirm --clean build/sds_reader.spec` (output in `dist/`).
`python build/make_icon.py` regenerates the app/website icons.

### Website

`docs/` is a static, script-free landing page, deployed to GitHub Pages by
`.github/workflows/pages.yml` (Settings → Pages → Source: *GitHub Actions*).
Download links use the placeholder `__REPO__`, which the Pages workflow
replaces with `owner/repo` automatically. If you host `docs/` somewhere
else, replace `__REPO__` in `docs/index.html` yourself.

## Installation (Python / command line)

```bash
pip install -r requirements.txt
```

(Both are pure-Python-plus-C-extension packages; no network access needed
after install, and no data ever leaves your machine.)

## Usage

Every run now produces a filled **Safety Data Sheet Summary** document
(`.docx` by default) as the primary output — you don't need to pass a
template to get one; a professionally-formatted default template is bundled
and used automatically. Pass `--template` only if you want to override it
with your own layout.

### Single file
```bash
python cli.py file --input path/to/sds.pdf --outdir ./output
```
Writes exactly two files:
- `output/<filename>_extracted.json` - a curated, flat JSON using a specific
  fixed field set (SDS Date, Substance Name, CAS No., First Aid, Fire Aid,
  Spillage, Safe Handling, Storage Conditions, PPE_RPE/Hands/Eyes/Skin,
  Melting/Boiling Point, Reactivity, Incompatible materials, Carcinogenetic,
  Mutagens, Reproductive Toxins, Sensitiser, Dust, Vapour, Fume, Dermatitis,
  Asthma, Illness, etc. - see `sds_reader/target_format.py` for the exact
  list) with `"Not stated in SDS"` for anything not found. This is the
  easiest output to read without opening Office - plain JSON, one flat
  object per SDS.
- `output/<filename>_summary.docx` - the same content laid out as a
  formatted Word report (see below).

### Folder (batch)
```bash
python cli.py folder --input path/to/folder --outdir ./output --csv
```
Processes every `.pdf` in the folder (add `--recursive` for subfolders),
producing the same two files per SDS, plus (with `--csv`) one
`_combined_extracted.csv` spreadsheet across the whole batch - one row per
SDS, easy to open in Excel/Sheets.

### Using your own template instead of the bundled one
Give it a `.docx` or `.txt`/`.md` file containing placeholders like
`{{product_name}}`, `{{flash_point}}` (see **Template placeholders** below
for the full list), and it's used instead of the default:

```bash
python cli.py file   --input sds.pdf    --outdir ./output --template my_template.docx
python cli.py folder --input ./folder   --outdir ./output --template my_template.docx
```
`.docx` placeholders work even if Word split `{{product_name}}` across
multiple formatting runs (a common cause of failed find/replace scripts),
and work inside regular table cells too.

**Per-ingredient tables**: to get a table that automatically grows to one
row per composition ingredient (like the bundled template's Substance
Breakdown table), mark a single table row with placeholders of the form
`{{row:substance_breakdown.fieldname}}` — one per cell — using the field
names in `substance_breakdown` below. That row is cloned once per ingredient
and removed if there are none.

### Optional: hybrid reconciliation via a local Ollama model
The regex/heuristic engine above is the default and always available - no
model, no GPU, no network. If you have [Ollama](https://ollama.com) running
locally, `--llm` adds an optional second pass that targets the specific
things regex is structurally bad at:

- **Per-ingredient toxicology flags** (carcinogen/mutagen/reproductive
  toxicant/sensitiser in the Substance Breakdown table) - genuinely a
  reading-comprehension task ("which sentence is about which ingredient"),
  not a pattern-matching one. This is the single biggest accuracy win.
- **Cleaning up known-weak fields** - multi-jurisdiction exposure-limit
  tables, borderless PPE comparison tables, and Section 9 physical-property
  blocks that come through as a run-on text block when the source PDF has
  no table borders for pdfplumber to detect.

```bash
ollama serve                      # in another terminal, if not already running
ollama pull llama3.1:8b           # any capable local model works; see notes below

python cli.py file --input sds.pdf --outdir ./output --llm
python cli.py folder --input ./my_sds_folder --outdir ./output --llm   # works the same way in batch mode
python cli.py file --input sds.pdf --outdir ./output --llm --llm-model mistral:7b --llm-host http://localhost:11434
```

**Toggle it on by default** instead of typing `--llm` every time by editing
`DEFAULT_USE_LLM = False` near the top of `cli.py` to `True`. `--llm` /
`--no-llm` on the command line always override whichever way the toggle is
set.

**The LLM pass is the priority path when enabled** - it writes to the exact
same two files as the regex-only path (`<filename>_extracted.json` and
`<filename>_summary.docx`), just with reconciled values where it improved
on the regex baseline - no extra files, no separate JSON to know to look
for. Its improvements also feed into the Word summary (the Substance
Breakdown table, exposure limits, and PPE fields all reflect the reconciled
values, not just the JSON). The console prints how many fields it changed
for each document so you have a sense of what happened without opening
anything.

**Design rules this follows, worth knowing:**
- The regex output is always the floor. If Ollama isn't running, the model
  isn't pulled, or a response comes back malformed/empty, `--llm` silently
  falls back to the plain regex result for that document - `--llm` can only
  add detail, never produce something worse than running without it.
- The model is explicitly instructed, in every prompt, to only use text
  that's actually in the source document and to answer "Not stated"/"Not
  stated in SDS" rather than guess - the same "don't alter SDS wording,
  don't invent" principle as the regex path, just applied by an LLM instead
  of a pattern matcher.
- It only touches specific known-weak fields (see `REPAIR_FIELD_SOURCES` in
  `sds_reader/llm_reconcile.py`) plus the toxicology breakdown - not a
  blanket re-extraction of everything, which would be slower and riskier
  for no benefit on fields the regex engine already gets right.
- Each Ollama call is capped at 1500 generated tokens (`num_predict`) so a
  single document can't hang indefinitely if a model rambles instead of
  stopping cleanly - worth knowing if you're troubleshooting something that
  seems stuck rather than just slow (CPU-only local inference is genuinely
  slow, especially across a big folder; that's expected).
- **A note on validation**: this was built and tested against a mock Ollama
  server to confirm the request/response handling, JSON-mode recovery, and
  every failure path (unreachable server, malformed JSON, empty response)
  all behave correctly - but actual output *quality* depends on the model
  you point it at and hasn't been validated against a real model in this
  build environment. Spot-check a few documents, especially early on with a
  new model, before trusting it at scale. Any reasonably capable
  instruction-following local model (Llama 3.1 8B, Mistral 7B, Qwen2.5 7B,
  etc.) should work; bigger models will generally be more reliable on the
  toxicology reading-comprehension task specifically.

### Using it as a library
```python
from sds_reader import extract_sds, process_folder, fill_template

record = extract_sds("sds.pdf")
print(record.values["product_name"], record.values["flash_point"])
print(record.warnings)          # anything that couldn't be found
print(record.sections_missing)  # section numbers not located at all

records = process_folder("./my_sds_folder")           # batch
fill_template("template.docx", record.values, "out.docx")
```

## The bundled default summary template

`sds_reader/templates/default_summary_template.docx` produces a
professionally formatted report with these sections, built specifically
around workplace-safety/COSHH-style reporting needs:

- **Substance Overview** — name, CAS number (`N/A (mixture)` if more than
  one composition ingredient was found), SDS version & date, hazard
  pictograms, H-statements (wording only, codes stripped), H-codes (listed
  separately), dustiness/volatility mentions
- **Substance Breakdown** — one table row per ingredient: name, CAS no.,
  concentration, workplace exposure limit type/LTEL/STEL, and heuristic
  Yes/Not-stated flags for carcinogen/mutagen/reproductive-toxicant/sensitiser
  mentions found near that ingredient anywhere in the document
- **Reactivity & Compatibility** — reactive materials, incompatible materials
- **Other Health Hazard Mentions** — named illnesses (asthma, dermatitis,
  etc.), dusts/vapours/fumes mentions, asphyxiation risk, other hazards
- **Handling & Storage**, **Personal Protective Equipment** (respiratory,
  hand, eye, skin/body — hand and skin/body protection are kept separate),
  **Spillage/Release Advice**, **Fire-Fighting Advice**, **First Aid
  Measures**

Wording throughout is reproduced verbatim from the source SDS wherever
possible (only leading hazard-statement codes/numbers are stripped, per the
"don't alter SDS wording" design goal) — this tool selects and combines
sentences, it doesn't paraphrase them.

## What gets extracted

~100 raw variables across all 16 GHS sections, plus the derived/computed
fields used by the summary template above. Raw fields include:

- **Section 1**: product_name, product_code, manufacturer_name/address/phone,
  emergency_phone, product_use, use_restrictions
- **Section 2**: ghs_classification, signal_word, hazard_statements,
  precautionary_statements, pictograms
- **Section 3**: composition_table (list of {chemical_name, cas_number,
  concentration, ...})
- **Section 4**: first_aid_inhalation/skin/eye/ingestion, symptoms
- **Section 5**: extinguishing media, fire hazards, firefighter PPE
- **Section 6**: spill response (personal/environmental precautions,
  containment)
- **Section 7**: handling_precautions, storage_conditions
- **Section 8**: exposure_limits (table), engineering_controls, PPE fields
- **Section 9**: appearance, odor, pH, melting/boiling/flash point, vapor
  pressure/density, density, solubility, autoignition temp, etc. (~20 fields)
- **Section 10**: reactivity, stability, incompatibilities, decomposition
- **Section 11**: acute toxicity, irritation, sensitization, carcinogenicity,
  STOT, aspiration hazard
- **Section 12**: ecotoxicity, persistence, bioaccumulation, mobility
- **Section 13**: disposal_methods
- **Section 14**: un_number, shipping name, hazard class, packing group
- **Section 15**: regulatory_information
- **Section 16**: revision_date, version_number, other_information

Every record also includes `_warnings` (fields it couldn't find, in case a
particular SDS omits or unusually phrases something), `_sections_found`, and
`_sections_missing`, so you can tell confident extractions from gaps at a
glance instead of silently getting blank data.

Full field-by-field synonym list: `sds_reader/schema.py`.

### Derived fields (used by the summary template)

Computed in `sds_reader/derived.py` from the raw fields above — combine,
reshape, or keyword-scan rather than doing a fresh label lookup:

| Variable | What it is |
|---|---|
| `cas_number_display` | CAS number, or `N/A (mixture)` if >1 composition ingredient |
| `sds_version_date` | Combined version + revision date |
| `hazard_codes` | Just the H-codes (e.g. `H225, H315`) |
| `hazard_statement_text` | Hazard statement wording with the codes stripped |
| `reactive_materials` | Combined reactivity / hazardous-reactions / conditions-to-avoid |
| `incompatible_materials` | Combined Section 7 + Section 10 incompatibility text |
| `dustiness_volatility` | Sentences mentioning dust/volatility/friable/powder, anywhere in the doc |
| `health_hazard_mentions` | Sentences mentioning asthma, dermatitis, cancer, sensitisation, etc. |
| `dusts_vapours_fumes_mentions` | Sentences mentioning dust/vapour/fume/mist/aerosol |
| `asphyxiant_mentions` | Sentences mentioning asphyxiation |
| `other_health_hazards` | HNOC + STOT + aspiration hazard, combined |
| `storage_requirements`, `respiratory_protection`, `hand_protection`, `eye_protection`, `skin_protection` | Section 7/8 PPE aliases (hand protection and skin/body protection are kept separate) |
| `spillage_release_advice`, `fire_advice`, `first_aid` | Combined Section 6/5/4 narratives |
| `substance_breakdown` | List of per-ingredient dicts: `name`, `cas`, `concentration`, `wel_type`, `ltel`, `stel`, `carcinogenic`, `mutagenic`, `reproductive_toxicant`, `sensitiser` |

## Project layout

```
cli.py                                     command-line entry point
sds_reader_app.py                          Windows desktop app (frozen into SDS-Reader.exe)
build/                                     PyInstaller spec, exe version info, icon generator
docs/                                      project website (GitHub Pages)
.github/workflows/                         build/self-test/release + website deploy
sds_reader/
  pipeline.py                              shared extract -> fill template -> write outputs flow
  schema.py                                canonical raw variable list + label synonyms
  pdf_utils.py                             PDF text/table extraction (pdfplumber)
  extractor.py                             section splitting + field/table extraction engine
  derived.py                               computed/combined fields + keyword scanning
  target_format.py                         fixed-schema JSON export (SDS Date, First Aid, Dust, etc.)
  llm_reconcile.py                         optional hybrid Ollama reconciliation pass (--llm)
  batch.py                                 folder scanning
  template_filler.py                       {{placeholder}} + dynamic table-row filling
  templates/default_summary_template.docx  bundled default output template
test_data/                                 2 synthetic sample SDS PDFs + example templates
test_data_batch2/                          12 more synthetic SDS PDFs covering different formats
                                            (gas cylinders, EU/REACH, pre-GHS, multi-page tables,
                                            two-column layouts, prose-only ingredients, etc.)
test_data_batch3/                          reconstructed real-world validation document
test_data_batch4/                          8 real vendor SDS's (Fisher Scientific, ChemicalBook,
                                            Fuchs Lubricants, Ingevity, Houghton/RS Components -
                                            2011 pre-CLP, INEOS, SC Johnson, Bartoline) - kept
                                            local only (gitignored), not redistributed
test_mock_ollama.py                        mock Ollama server for testing llm_reconcile.py's
                                            HTTP plumbing without needing a real Ollama install
```

## Limitations & things worth knowing

- **This is a regex/heuristic parser, not a fine-tuned model.** No training
  pipeline runs here - field matching is entirely rule-based (section
  boundaries + label synonyms + table-header scoring). That makes it fast,
  fully offline, and easy to audit/extend, but it can't promise 100%
  accuracy on every possible real-world SDS the way a large trained model
  might. It has been hardened against 15 structurally different documents
  (see below) and a growing synonym list, but a genuinely unusual document
  can still need a synonym added - that's a one-line fix in `schema.py`,
  not a rebuild.
- **Narrative fields use verbatim source text on purpose.** For the free-text
  sections most prone to information loss when re-synthesized from parsed
  sub-fields (First Aid, Fire Aid, Spillage, Safe Handling, Storage
  Conditions in the `_extracted.json` output), the tool captures the
  *raw section text* almost verbatim rather than reassembling it from
  individually-parsed sub-fields, then strips numbered sub-item markers
  ("4.1.", "10.5.", etc.) and repeated PDF running-header/footer/page-number
  noise from it. This was a deliberate trade-off after testing showed
  sub-field recombination was where wording/character loss crept in;
  verbatim capture avoids that failure mode for these fields at the cost of
  also including the source SDS's own sub-labels/preamble text.
- **Complex multi-jurisdiction exposure-limit tables** (e.g. separate UK/EU/
  Ireland OEL columns in one table) and **borderless/whitespace-formatted
  tables** (some vendors' glove-material comparison tables in Section 8)
  can come through as a rougher, harder-to-read text block rather than a
  cleanly structured one - pdfplumber's table detection depends on the PDF
  actually drawing table lines/borders, and doesn't always succeed on these
  layouts. One document tested had exposure limits given as ~15 separate
  small tables (one per country), which the current "pick the single best-
  scoring table" approach isn't built to aggregate - it'll grab one of them
  rather than combining all fifteen. The underlying data is usually still
  present in the raw document, just not perfectly reformatted or complete
  for documents with this specific many-small-tables pattern.
- **Documents with no labeled toxicology/stability sub-items at all**
  (older, pre-2015 EU "preparation" format in particular) sometimes cover
  Section 10/11 in pure running prose with no "Reactivity:" or
  "Carcinogenicity:" labels whatsoever. Keyword-sentence fallbacks catch
  the common case where a *combined* sentence covers several topics at
  once (e.g. "not expected to be carcinogenic, mutagenic...or toxic for
  reproduction" filling in all three fields), but a field with literally
  no matching label anywhere and no combined-sentence mention either will
  correctly - if unhelpfully - come back "Not stated in SDS" rather than
  the tool guessing at an answer.
- **Scanned/image-only PDFs**: if a PDF has no real text layer (a photo or
  scan of a paper SDS), pdfplumber can't extract text and this tool can't
  read it without OCR, which isn't bundled (keeping this 100% offline with
  no extra heavy dependencies). You'll get a warning in `_warnings` when
  this happens. If you hit this often, `ocrmypdf` (open-source, runs
  offline) can pre-process files before handing them to this tool.
- **Pre-GHS / non-standard-format sheets**: this tool is built around the
  16-section GHS format used since ~2012. A pre-2012 "MATERIAL SAFETY DATA
  SHEET" with Roman-numeral sections (I, II, III...) or another non-GHS
  layout won't be recognized — it degrades gracefully (no crash, everything
  comes back "Not stated in SDS" with `_sections_found` empty) rather than
  producing wrong data, but it won't extract anything either. If you
  regularly deal with these, they'd need their own section-pattern set
  added to `schema.py`.
- **Per-ingredient toxicology flags are proximity heuristics, not NLP.**
  The Substance Breakdown table's carcinogen/mutagen/reproductive-toxicant/
  sensitiser columns work by scanning text near each ingredient's name/CAS
  number in Sections 2/11/12 for the relevant keyword, with basic negation
  handling (so "did not indicate mutagenic potential" won't false-flag).
  On short SDS's where multiple ingredients are listed close together,
  a hazard genuinely tied to one ingredient can occasionally get attributed
  to its neighbor too, since there's no real entity-relationship parsing
  behind it — that's why the template calls these "Yes = worth checking the
  source", not certified classifications.
- **One "main" value per field**: fields are captured as the first matching
  occurrence in their section. For most SDS's there's exactly one, but a
  very unusual document repeating a label twice in one section would only
  keep the first hit.
- **Composition/exposure tables**: matched by scoring header-row keywords.
  If a vendor uses a table with no recognizable header row at all (very
  rare), it'll fall through to the prose-based fallback or come back empty.
- **Always spot-check for anything safety-critical.** This tool is meant to
  save you from re-typing SDS data by hand, not to be the final authority —
  treat `_warnings` and `_sections_missing` as your checklist of what to
  verify manually before relying on the output.

## Extending it

To add a new field: open `sds_reader/schema.py`, find the right
`SECTION_n` list, and add a `FieldDef("your_var_name", section_number,
["label synonym 1", "label synonym 2", ...])`. It'll automatically show up
in every extraction and be available as a template placeholder — no other
code changes needed.
