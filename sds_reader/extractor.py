"""
extractor.py
============
The core offline SDS parsing engine.

Strategy
--------
1. Pull text + tables from every page (pdf_utils.py).
2. Locate the 16 standard GHS section headers *in order* within the full
   text (section_boundaries) - this is robust across "SECTION 1:", "1.",
   "1 -", etc. because we search sequentially and only accept a match that
   occurs after the previous section's match.
3. Within each section's text block, run label/synonym-based field
   extraction (schema.py) to pull out individual variables.
4. For table-shaped fields (composition table in Section 3, exposure limits
   in Section 8) use the actual tables pdfplumber found on the relevant
   pages, matched by header-row keywords, instead of regex on flattened text
   (tables rarely survive text-flattening in a clean, parseable way).
5. Return a single flat dict of canonical variables -> values, plus
   diagnostics (which fields were not found, which section headers were not
   located, whether the PDF looks like a scanned/image-only file).
"""

import re
from dataclasses import replace
from typing import Dict, List, Optional, Tuple

from . import pdf_utils, schema, derived


# ---------------------------------------------------------------------------
# Section boundary detection
# ---------------------------------------------------------------------------

def _build_section_pattern(n: int) -> re.Pattern:
    titles = schema.SECTION_TITLES[n]
    # Titles are written with literal spaces; swap those for \s+ so a title
    # that wraps onto a new PDF line (common in narrow/multi-column layouts,
    # e.g. "EXPOSURE\nCONTROLS/PERSONAL PROTECTION") still matches.
    flexible_titles = [re.sub(r" ", r"\\s+", t) for t in titles]
    title_alt = "|".join(flexible_titles)
    # Accept: "SECTION 3: ...", "3. ...", "3 - ...", "Section 3 – ..."
    # header must be followed (within ~90 chars, same or next line) by one of its title keywords.
    pattern = (
        rf"(?im)^[ \t]*(?:section[ \t]*)?{n}[ \t]*[\.\):\-–]?[ \t]*"
        rf"(?:[A-Za-z\s/,&\(\)\-]{{0,10}})?(?:{title_alt})"
    )
    return re.compile(pattern)


_SECTION_PATTERNS = {n: _build_section_pattern(n) for n in range(1, 17)}


def find_section_boundaries(text: str) -> Dict[int, Tuple[int, int]]:
    """Returns {section_number: (start_char_idx, end_char_idx)} for sections found."""
    found_starts: Dict[int, int] = {}
    search_from = 0
    for n in range(1, 17):
        pat = _SECTION_PATTERNS[n]
        m = pat.search(text, search_from)
        if m:
            found_starts[n] = m.start()
            search_from = m.end()
        # if not found, leave a gap - we still keep scanning forward for later sections
        # from the same search_from so one missing header doesn't break the rest.

    # Build (start, end) using the ordered set of sections actually found.
    ordered = sorted(found_starts.items(), key=lambda kv: kv[1])
    boundaries: Dict[int, Tuple[int, int]] = {}
    for i, (n, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else len(text)
        boundaries[n] = (start, end)
    return boundaries


# ---------------------------------------------------------------------------
# Page-offset mapping (so we know which PDF page a char offset falls on)
# ---------------------------------------------------------------------------

def build_page_offsets(pages: List[pdf_utils.PageData]) -> List[Tuple[int, int, int]]:
    """Returns list of (page_number, start_offset, end_offset) matching how
    pdf_utils.full_text() joins page texts with '\n'."""
    offsets = []
    cursor = 0
    for p in pages:
        start = cursor
        length = len(p.text)
        end = start + length
        offsets.append((p.page_number, start, end))
        cursor = end + 1  # +1 for the '\n' joiner
    return offsets


def page_for_offset(offsets: List[Tuple[int, int, int]], char_idx: int) -> int:
    for page_number, start, end in offsets:
        if start <= char_idx <= end:
            return page_number
    return offsets[-1][0] if offsets else 1


def _skip_wrapped_header(text: str, start: int, hard_end: int) -> int:
    """Section headers sometimes wrap across 2+ PDF lines when the title is
    long (e.g. 'IDENTIFICATION OF THE SUBSTANCE/MIXTURE AND OF THE\nCOMPANY/
    UNDERTAKING'). Always skip the header's own first line (regardless of
    case), then keep skipping further lines only while they still look like
    header continuation text (no lowercase letters)."""
    pos = start
    first = True
    while pos < hard_end:
        line_end = text.find("\n", pos)
        if line_end == -1 or line_end > hard_end:
            line_end = hard_end
        line = text[pos:line_end]
        if first or (line.strip() and not re.search(r"[a-z]", line)):
            pos = line_end + 1
            first = False
        else:
            break
    return min(pos, hard_end)


# ---------------------------------------------------------------------------
# Field (label/value) extraction within a section's text
# ---------------------------------------------------------------------------

_STOPWORD_TAIL = re.compile(r"\n\s*\n")  # blank line = likely end of a field's value


def _all_label_alternation(fields: List[schema.FieldDef], exclude: schema.FieldDef) -> str:
    labels = []
    for f in fields:
        if f is exclude or f.kind == "table":
            continue
        labels.extend(f.labels)
    return "|".join(labels)


def _skip_leftover_header_text(section_text: str, start: int, other_labels_pattern: Optional[re.Pattern] = None) -> int:
    """After matching a label synonym, real SDS wording is often longer than
    our synonym (e.g. label 'specific hazards' vs actual text 'Specific
    hazards arising from the chemical:'). If a ':' shows up shortly after
    where our match ended, and it's on the same line, treat everything up
    to and including it as leftover label text and skip past it. This also
    incidentally absorbs plural endings like 'statements:' when we matched
    the singular 'statement'."""
    line_end = section_text.find("\n", start)
    window_end = min(start + 65, line_end if line_end != -1 else start + 65, len(section_text))
    window = section_text[start:window_end]
    colon_m = re.search(r":\s*", window)
    if not colon_m:
        return start
    if re.match(r"\s*[<>≤≥~]?\s*\d", window):
        # Text before the colon starts with a number - that's the value
        # itself ('03/22/2026 Print Date: ...', '14.01.2026 Version: 3.1'),
        # never leftover label wording.
        return start
    period_m = re.search(r"[.!?]\s", window)
    if period_m and period_m.start() < colon_m.start():
        # A sentence boundary appears before the colon - this looks like real
        # content followed by a *different* field's label on the same line,
        # not label overflow text. Don't skip.
        return start
    pre_colon = window[:colon_m.start()]
    if other_labels_pattern and other_labels_pattern.search(pre_colon):
        residue = other_labels_pattern.sub("", pre_colon)
        if re.fullmatch(r"[\s,/&;]*(?:(?:and|or)\b[\s,/&;]*)*", residue, re.IGNORECASE):
            # Nothing but other labels before the colon: a combined heading
            # ('Carcinogenicity, mutagenicity, reproductive toxicity: Not
            # classified ...') - the answer after the colon is ours too.
            return start + colon_m.end()
        # The text up to the colon contains a sibling field's label (e.g.
        # "Clear liquid Odor:" while extracting Appearance) - a different
        # field starts there. Leave `start` alone; the boundary logic
        # below cuts the value off at the right place instead.
        return start
    if re.search(r"\bresult\b", window[:colon_m.end()], re.IGNORECASE):
        # "<Test name> Result:" (e.g. "Buehler Test - Guinea pig Result:",
        # "Ames test Result:") is near-universal toxicology-section phrasing
        # for citing a specific study - it's real content, not leftover
        # label wording, even though it happens to end in a colon.
        return start
    return start + colon_m.end()


# Optional sub-item numbering / bullet in front of a label at the start of a
# line: '4.1 ', '10.5. ', '(d) ', 'h) ', '• '.
_LINE_START_PREFIX = r"^[ \t]*(?:(?:\d{1,2}(?:\.\d{1,2})*\.?|\(?[a-z]\)|[•·*\-])[ \t]+)?"
_SEP = r"[:\-–]"


def _anchored(alt: str) -> str:
    """A label only counts where it is used AS a label: at the start of a
    line (optionally after sub-item numbering) or directly followed by a
    ':'/'-' separator. The same words mid-sentence ('Use skin protection
    cream ...') are content, not the start of a new field - treating them
    as labels used to cut values short or start them in the wrong place."""
    return rf"(?:{_LINE_START_PREFIX}(?P<l1>{alt})|(?P<l2>{alt})(?:(?<=:)|(?=[ \t]*{_SEP})))"


# Words that, directly after a label with no separator, show the label word
# is the subject of a sentence ('Sensitization by inhalation is not
# expected.') rather than a heading - the whole sentence is the value.
_SENTENCE_CONTINUATIONS = {"by", "of", "is", "are", "was", "were", "may", "can", "has", "not", "in"}


def _is_label_use(text: str, m: re.Match) -> bool:
    """Filters out line-start matches that are really a wrapped sentence
    continuing onto a new line ('...Respirators must be used according to
    a\\nrespiratory protection program ...'): a real heading at the start of
    a line is capitalised, unless it follows sub-item numbering like
    '(d) respiratory or skin sensitization;'."""
    if m.group("l1") is None:
        return True
    label = m.group("l1")
    if not label[:1].islower():
        return True
    return bool(text[m.start():m.start("l1")].strip())


def _search_label(regex: re.Pattern, text: str, pos: int) -> Optional[re.Match]:
    for m in regex.finditer(text, pos):
        if _is_label_use(text, m):
            return m
    return None


_BARE_HEADING_STOPLIST = {
    "acute toxicity", "irritation/corrosion", "irritation", "corrosion",
    "sensitization", "sensitisation", "mutagenicity", "carcinogenicity",
    "reproductive toxicity", "teratogenicity", "aspiration hazard",
    "information on toxicological effects", "developmental effects",
    "fertility effects",
    "specific target organ toxicity (single exposure)",
    "specific target organ toxicity (repeated exposure)",
}


def _usable(value: str) -> bool:
    return bool(value) and bool(re.search(r"[A-Za-z0-9]", value)) \
        and value.strip().lower() not in _BARE_HEADING_STOPLIST


def extract_field(section_text: str, fdef: schema.FieldDef, siblings: List[schema.FieldDef]) -> Optional[str]:
    """Value for one field: the text after its label, up to the next label
    (sibling field or another occurrence of our own), a blank line, or - for
    single-line fields - the end of the line. With fdef.collect, every
    occurrence is gathered (e.g. separate respiratory and skin sensitisation
    entries) instead of just the first."""
    label_alt = "|".join(fdef.labels)
    label_re = re.compile(rf"(?im){_anchored(label_alt)}[ \t]*{_SEP}?\s*")
    own_prefix_re = re.compile(rf"(?i)^(?:{label_alt})\s*{_SEP}?\s*")

    other_labels = _all_label_alternation(siblings, fdef)
    all_labels = "|".join(x for x in (other_labels, label_alt) if x)
    # Boundaries include our own labels too: a value ends where the next
    # occurrence of this field starts ('03/22/2026 Print Date: ...').
    boundary_re = re.compile(rf"(?im){_anchored(all_labels)}")
    other_label_re = re.compile(rf"(?im)(?:{other_labels})") if other_labels else None

    found: List[str] = []
    search_pos = 0
    while True:
        m = _search_label(label_re, section_text, search_pos)
        if not m:
            break
        search_pos = m.end()
        grp = "l1" if m.group("l1") is not None else "l2"
        label_text = m.group(grp)
        between = section_text[m.end(grp):m.end()]
        start = m.end()

        nxt = re.match(r"([a-z]+)\b", section_text[start:])
        if not re.search(_SEP, between) and "\n" not in between and nxt \
                and nxt.group(1) in _SENTENCE_CONTINUATIONS:
            # 'Sensitization by skin contact is not expected.' - the label
            # word is the subject of a sentence; keep the whole sentence.
            line_end = section_text.find("\n", start)
            cleaned = _clean(section_text[m.start(grp):line_end if line_end != -1 else len(section_text)])
            if _usable(cleaned):
                found.append(cleaned)
                if not fdef.collect:
                    break
            continue

        if "\n" not in between:
            # Leftover label wording can only be on the label's own line.
            start = _skip_leftover_header_text(section_text, start, other_label_re)

        # Hard ceiling on the value's end: a full paragraph (up to a blank
        # line) for multiline fields, or the rest of the current line for
        # single-line fields - and in both cases the next label (even on the
        # same line, e.g. "Appearance: Clear liquid   Odor: Citrus") first.
        if fdef.multiline:
            blank = _STOPWORD_TAIL.search(section_text, start)
            hard_end = blank.start() if blank else len(section_text)
        else:
            line_end = section_text.find("\n", start)
            hard_end = line_end if line_end != -1 else len(section_text)
        bm = _search_label(boundary_re, section_text, start)
        end = min(hard_end, bm.start()) if bm else hard_end

        cleaned = _clean(section_text[start:end])
        # 'Reactivity\nReactivity The reactivity data ...' - drop a repeat
        # of our own label at the start of the value.
        stripped = own_prefix_re.sub("", cleaned, count=1).strip()
        if stripped and stripped != cleaned and _usable(stripped):
            cleaned = stripped
        if _usable(cleaned):
            if fdef.keep_label:
                lab = _clean(label_text)
                cleaned = f"{lab[:1].upper()}{lab[1:]}: {cleaned}"
            found.append(cleaned)
            if not fdef.collect:
                break
        # Otherwise this occurrence was a bare heading with nothing after it
        # before the next label (common when a PDF's text extraction groups
        # all subsection titles together separately from their values) - try
        # the next occurrence of this same label instead of giving up.

    if not found:
        return None
    return "\n".join(dict.fromkeys(found))


_HEADING_NUMBER_RE = re.compile(r"(?m)^[ \t]*\d{1,2}\.\d{1,2}\.?[ \t]+(?=[A-Z])")
_BARE_HEADING_NUMBER_RE = re.compile(r"(?m)^[ \t]*\d{1,2}\.\d{1,2}\.?[ \t]*$\n?")
_INLINE_TRAILING_NUMBER_RE = re.compile(r"(?<![Ss]ection)[ \t]\d{1,2}\.\d{1,2}\.?(?=[ \t]+[A-Z]|[ \t]*$)")


def strip_subitem_headings(text: str) -> str:
    """Removes GHS sub-item numbering like '10.1.', '4.2', '6.4.' - both as
    a line-leading heading marker ('10.1. Reactivity' -> 'Reactivity') and
    as a bare standalone line/trailing token left over after a sibling-
    boundary split (e.g. a dangling '7.2.' with nothing else on the line).
    The heading TITLE text itself (e.g. 'Reactivity', 'Incompatible
    materials') is intentionally kept - only the leading digits go.

    Safety rule: never let this strip a value down to nothing. A real
    version number ('4.0') has the exact same shape as a leftover GHS
    sub-item marker ('10.6.') - the only reliable way to tell them apart is
    that a genuine leftover artifact always has other real content around
    it (that's what made it "leftover" rather than "the whole answer"). If
    stripping would empty the text out, the number was the actual content,
    not a stray heading marker - leave it alone."""
    if not text:
        return text
    stripped = _HEADING_NUMBER_RE.sub("", text)
    stripped = _INLINE_TRAILING_NUMBER_RE.sub("", stripped)
    stripped = _BARE_HEADING_NUMBER_RE.sub("", stripped)
    if not stripped.strip() and text.strip():
        return text
    return stripped


_PAGE_NUMBER_LINE_RE = re.compile(r"(?im)^\s*(?:page\s+)?\d{1,3}\s*/\s*\d{1,3}\s*$")
_SEPARATOR_LINE_RE = re.compile(r"^[ \t]*[_\-=]{5,}[ \t]*$", re.MULTILINE)
_BARE_SDS_TITLE_RE = re.compile(r"(?im)^\s*safety data sheet\s*$")
_RUNNING_HEADER_RE = re.compile(r"(?im)^.{0,80}\brevision date\b.{0,40}\d{1,2}[-/][A-Za-z0-9]{2,9}[-/]\d{2,4}\s*$")
# A bare document/footer code like 'FSUB0550' - but never a GHS pictogram
# code ('GHS02'), which has exactly this shape and is real content.
_BARE_DOC_CODE_RE = re.compile(r"^(?!GHS\d{2}$)[A-Z]{2,10}\d{2,10}$")


_TRAILING_NUM_LINE_RE = re.compile(r"^(.*?)[ \t]+(\d{1,4})$")
_TRAILING_FRACTION_LINE_RE = re.compile(r"^(.*?)[ \t]+(\d{1,4})\s*/\s*(\d{1,4})$")

# Short answers that legitimately repeat verbatim many times across a real
# SDS (e.g. "No data available" for a dozen different toxicology sub-fields)
# - never treat these as boilerplate no matter how often they repeat.
_GENERIC_ANSWER_STOPLIST = {
    "no data available", "not applicable", "not available", "none", "n/a",
    "not classified", "no", "yes", "negative", "positive", "not known",
    "not stated", "not determined", "no further information",
    "no additional information", "not relevant", "none known",
    "not applicable for mixtures", "value not relevant for classification",
    "not classifiable", "no information available",
}


_DANGLING_WORD_RE = re.compile(
    r"\b(?:by|of|and|or|the|in|to|for|with|a|an|as|at|is|are|was|were|be|been)$",
    re.IGNORECASE,
)

# How many non-blank lines at the top and bottom of each page count as the
# header/footer zone. Running headers/footers only ever live there, so only
# lines in these zones are candidates for removal.
_PAGE_ZONE_LINES = 4


def _is_generic_answer(s: str) -> bool:
    return s.lower().rstrip(" .") in _GENERIC_ANSWER_STOPLIST


def strip_page_boilerplate(page_texts: List[str]) -> List[str]:
    """Generalized page-header/footer stripper: rather than hardcoding known
    vendor patterns (Fisher's 'FSUB0550', ChemicalBook's 'Chemical Book N',
    etc.), detect the *shape* any of them share, in two ways:

    1. A short text prefix followed by a number (or an 'N/M' page-of-total
       fraction) that increases with the page count, on 3+ pages.
    2. A line that repeats VERBATIM on 3+ pages (e.g. 'Issue Date:
       10.12.2015' on every page) - guarded by a stoplist of common short
       generic SDS answers.

    Both passes only ever look at - and only ever remove - lines in each
    page's header/footer zone (its first/last few non-blank lines). Earlier
    versions scanned the whole document, which deleted real content that an
    SDS legitimately repeats: e.g. 'Product: Based on available data, the
    classification criteria are not met.' as the answer to six different
    toxicology headings, or the 'Product name: X' line in Section 1 when
    the same text also ran as a page header.

    Returns the cleaned text of each page (same length/order as the input)."""
    pages_lines = [t.split("\n") for t in page_texts]
    zones = []
    for lines in pages_lines:
        idx = [i for i, l in enumerate(lines) if l.strip()]
        zones.append(set(idx[:_PAGE_ZONE_LINES] + idx[-_PAGE_ZONE_LINES:]))

    # --- Pass 1: prefix + increasing number/fraction ---
    prefix_nums: Dict[str, List[int]] = {}
    for lines, zone in zip(pages_lines, zones):
        for i in sorted(zone):
            s = lines[i].strip()
            m = _TRAILING_FRACTION_LINE_RE.match(s) or _TRAILING_NUM_LINE_RE.match(s)
            if not m:
                continue
            prefix = m.group(1).strip()
            if 2 <= len(prefix) <= 90:
                prefix_nums.setdefault(prefix.lower(), []).append(int(m.group(2)))
    boilerplate_prefixes = {
        p for p, nums in prefix_nums.items()
        if len(nums) >= 3 and all(b >= a for a, b in zip(nums, nums[1:])) and nums[-1] > nums[0]
    }

    # --- Pass 2: exact-repeat lines (counted once per page) ---
    # A genuine page header/footer is a complete short phrase or code. A line
    # that ends mid-sentence on a preposition/conjunction/article is a
    # wrapped SENTENCE FRAGMENT, not a header.
    page_counts: Dict[str, int] = {}
    for lines, zone in zip(pages_lines, zones):
        seen = set()
        for i in zone:
            s = lines[i].strip()
            if not s or len(s) > 160 or len(s) < 3 or _is_generic_answer(s):
                continue
            if not s.endswith((".", ":", ")", "-")) and _DANGLING_WORD_RE.search(s):
                continue
            seen.add(s)
        for s in seen:
            page_counts[s] = page_counts.get(s, 0) + 1
    exact_repeated = {s for s, c in page_counts.items() if c >= 3}

    cleaned = []
    for lines, zone in zip(pages_lines, zones):
        kept = []
        for i, line in enumerate(lines):
            s = line.strip()
            # Pass 3: a bare 'N/M' page-number line is never real content.
            if _PAGE_NUMBER_LINE_RE.match(s):
                continue
            if i in zone:
                if s in exact_repeated:
                    continue
                m = _TRAILING_FRACTION_LINE_RE.match(s) or _TRAILING_NUM_LINE_RE.match(s)
                if m and m.group(1).strip().lower() in boilerplate_prefixes:
                    continue
            kept.append(line)
        cleaned.append("\n".join(kept))
    return cleaned


_SECTION_TITLE_LINE_RE = re.compile(r"^\s*section\s+\d{1,2}\.\s+[a-z][a-z /\-]{2,60}$", re.IGNORECASE)


def strip_pdf_boilerplate(text: str) -> str:
    """Strips repeated running-header/footer noise that shows up when a raw
    section block happens to span a page break: page numbers ('Page 3/11'),
    long underscore/dash separator rules, a bare repeated 'SAFETY DATA
    SHEET' title line, the running '<product> Revision Date <date>' header
    many vendors (Fisher/Thermo in particular) repeat on every page, and a
    'Section N. Title' line repeating mid-block after a page break (the true
    header was already consumed before this text region starts, so any
    further occurrence is by definition a page-break repeat, not content)."""
    if not text:
        return text
    text = _SEPARATOR_LINE_RE.sub("", text)
    lines = []
    for line in text.split("\n"):
        if _PAGE_NUMBER_LINE_RE.match(line):
            continue
        if _BARE_SDS_TITLE_RE.match(line):
            continue
        if _RUNNING_HEADER_RE.match(line):
            continue
        if _BARE_DOC_CODE_RE.match(line.strip()):
            continue
        if _SECTION_TITLE_LINE_RE.match(line):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Only after a line that already ended its sentence - 'Safety\nglasses' is a
# genuine wrap, 'criteria are not met.\nfertility' is a stray label tail.
_LONE_WORD_LINE_RE = re.compile(r"(?<=[.!?:)])[ \t]*\n[ \t]*[a-z]{2,20}[ \t]*(?=\n|$)")
_HYPHEN_BREAK_RE = re.compile(r"(?<=[a-z])-\n(?=[a-z])")
_SOFT_BREAK_RE = re.compile(r"[ \t]*\n[ \t]*(?=[a-z(])")


def _rejoin_wrapped_lines(value: str) -> str:
    """Undo PDF line-wrapping inside a single field value:
      - a line that is just one lowercase word with no punctuation is the
        tail of a label that wrapped in a two-column layout ('Reproductive
        toxicity -' ... 'fertility') or stray hidden text - drop it;
      - 'remov-\\ning' -> 'removing';
      - a line starting lowercase continues the previous sentence."""
    value = _LONE_WORD_LINE_RE.sub("", value)
    value = _HYPHEN_BREAK_RE.sub("", value)
    return _SOFT_BREAK_RE.sub(" ", value)


def _clean(value: str) -> str:
    value = value.replace("\x00", "")
    value = strip_pdf_boilerplate(value)
    value = strip_subitem_headings(value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{2,}", "\n", value)
    value = _rejoin_wrapped_lines(value.strip())
    value = value.strip(" \n\t")
    # Strip a leading separator (':' / '-' / '\u2013' / ';') but never eat the
    # minus sign of a negative number (e.g. "Melting point: -95 C").
    if not re.match(r"^-\d", value):
        value = value.lstrip(" \n\t:-\u2013;")
    value = value.rstrip(" \n\t:\u2013")
    return value


# ---------------------------------------------------------------------------
# Table matching (composition table, exposure limits table)
# ---------------------------------------------------------------------------

_COMPOSITION_HEADER_HINTS = ["cas", "chemical name", "substance", "component", "concentration", "%", "ec no", "ec number"]
_EXPOSURE_HEADER_HINTS = ["twa", "stel", "ceiling", "pel", "exposure limit", "oel", "tlv"]


def _score_header(row: List[str], hints: List[str]) -> int:
    joined = " | ".join(c.lower() for c in row if c)
    return sum(1 for h in hints if h in joined)


def _cell(c: Optional[str]) -> str:
    # Cells wrap inside the PDF ('ALCOHOLS, C9-11, ETHOXYLATED\nPROPOXYLATED',
    # '...OXAZOLIDINE\n]'); one value should read as one line.
    s = re.sub(r"\s+", " ", c or "").strip()
    return re.sub(r"\s+([\]\)])", r"\1", s)


def _table_to_dicts(table: List[List[str]]) -> List[Dict[str, str]]:
    if not table or len(table) < 2:
        return []
    header = [_cell(c) for c in table[0]]
    rows = []
    for raw_row in table[1:]:
        row = {}
        for i, cell in enumerate(raw_row):
            key = header[i] if i < len(header) and header[i] else f"col_{i+1}"
            row[key] = _cell(cell)
        # skip fully-empty rows
        if any(v for v in row.values()):
            rows.append(row)
    return rows


_TABLE_SUBJECT_HINTS = ["component", "chemical", "substance", "ingredient", "name", "cas"]


def _exposure_table_is_malformed(table: List[List[str]]) -> bool:
    """Multi-jurisdiction limit tables (one small table per country, or a
    header row on a previous page) come out of pdfplumber with a data row
    or a country name as the 'header' - e.g. 'Polska | TWA: | STEL:'.
    Rendering those as key: value pairs produces nonsense, so detect them
    and fall back to the plain limit lines from the text instead."""
    header = " | ".join(_cell(c).lower() for c in table[0] if c)
    return not any(h in header for h in _TABLE_SUBJECT_HINTS)


_LIMIT_KEYWORD_RE = re.compile(
    r"\b(?:TWA|STEL|TLV|PEL|WELs?|LTEL|OELs?|MAK|VME|VLE|AGW|IDLH|"
    r"long[- ]term exposure limit|short[- ]term exposure limit)\b", re.IGNORECASE)
_LIMIT_VALUE_RE = re.compile(r"\d[\d.,]*\s*(?:mg/m3|mg/m³|mg/m\^3|ppm|f/cc|f/ml|ml/m3|fibres?/ml)", re.IGNORECASE)
_LIMIT_EXCLUDE_RE = re.compile(r"\b(?:DNEL|DMEL|PNEC|LD ?50|LC ?50|EC ?50|systemic effects)\b", re.IGNORECASE)
_LIMIT_LEADING_KEYWORD_RE = re.compile(
    r"^(?:TWA|STEL|TLV|LTEL|long[- ]term|short[- ]term|8[- ]?h(?:ou)?r)", re.IGNORECASE)


def exposure_limit_lines(section8_text: str, max_lines: int = 40) -> List[str]:
    """Lines of Section 8 that state an exposure limit: a limit keyword
    (TWA/STEL/WEL/TLV...) or a value with a concentration unit, excluding
    DNEL/PNEC/LD50-style figures that aren't workplace limits. When a limit
    line starts with the keyword ('Long-term exposure limit (8-hour TWA):
    350 mg/m3'), the line before it - usually the substance name - is kept
    too so the value isn't orphaned."""
    lines = [l.strip() for l in section8_text.split("\n")]
    keep: List[int] = []
    for i, line in enumerate(lines):
        if not line or _LIMIT_EXCLUDE_RE.search(line):
            continue
        if _LIMIT_KEYWORD_RE.search(line) or _LIMIT_VALUE_RE.search(line):
            if _LIMIT_LEADING_KEYWORD_RE.match(line) and i > 0 and (i - 1) not in keep:
                j = i - 1
                while j >= 0 and not lines[j]:
                    j -= 1
                if j >= 0 and len(lines[j]) < 120 and not _LIMIT_EXCLUDE_RE.search(lines[j]) \
                        and not re.fullmatch(r"(?i)(?:occupational\s+)?exposure\s+limits?(?:\s+values?)?:?", lines[j]):
                    keep.append(j)
            keep.append(i)
    return [lines[i] for i in sorted(set(keep))][:max_lines]


_CAS_RE = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")
_PERCENT_RE = re.compile(r"([<>≤≥]=?\s*\d{1,3}(?:\.\d+)?\s*%|\d{1,3}(?:\.\d+)?\s*(?:-|–|to)\s*\d{1,3}(?:\.\d+)?\s*%|\d{1,3}(?:\.\d+)?\s*%)")


_CARD_NAME_PCT_RE = re.compile(
    r"^([A-Z][A-Za-z0-9 ,\-\(\)/]{3,80}?)\s+"
    r"(\d{1,3}(?:\.\d+)?\s*-\s*\d{1,3}(?:\.\d+)?\s*%|\d{1,3}(?:\.\d+)?\s*%)\s*$"
)


_EC_RE = re.compile(r"\b(\d{2,3}-\d{3}-\d)\b")
_NO_CAS_WITH_EC_RE = re.compile(
    r"CAS\s*number\s*:?\s*[—–\-]\s*EC\s*number\s*:?\s*(\d{2,3}-\d{3}-\d)", re.IGNORECASE
)


def extract_composition_no_cas_placeholder(section3_text: str) -> List[Dict[str, str]]:
    """Third prose fallback: UVCB/complex substances (common for hydrocarbon
    solvents, distillates, etc.) often have NO discrete CAS number at all -
    just a 'CAS number: —' dash placeholder with an EC number given instead.
    This is a standard REACH pattern, not a one-off vendor quirk, so it's
    worth its own extractor rather than just failing silently. Uses the EC
    number as the identifier and searches nearby text for the ingredient
    name and concentration."""
    m = _NO_CAS_WITH_EC_RE.search(section3_text)
    if not m:
        return []
    ec_number = m.group(1)
    before = section3_text[:m.start()]
    pct_match = None
    for pct_match in _PERCENT_RE.finditer(before):
        pass  # take the last (closest to the CAS/EC line) match
    concentration = pct_match.group(1).strip() if pct_match else ""
    # Name: text before the (last) percentage match, trimmed to a reasonable
    # length and cleaned of line-wrap noise.
    name_source = before[:pct_match.start()] if pct_match else before
    name_source = re.sub(r"\s+", " ", name_source).strip()
    # Strip a leading section header / numbered sub-item fragment (e.g.
    # "...edients 3.1. Substances Hydrocarbons..." -> "Hydrocarbons...") -
    # take everything after the LAST such marker rather than a blind
    # character-count trim, which can cut mid-word.
    header_m = None
    for header_m in re.finditer(r"\bSECTION\s+\d{1,2}[:.]|\b\d{1,2}\.\d{1,2}\.\s+[A-Z][a-z]+", name_source):
        pass
    if header_m:
        name_source = name_source[header_m.end():]
    name = name_source[-100:].strip(" ,.-")
    if name:
        return [{"chemical_name": name, "cas_number": f"N/A (EC {ec_number})", "concentration": concentration}]
    return []


def extract_composition_from_card_layout(section3_text: str) -> List[Dict[str, str]]:
    """Second prose fallback, for a 'card' layout some vendors use instead of
    a bordered table: an ingredient NAME and its % on one line, then a
    'CAS number: X' line shortly after (rather than everything on one
    sentence, which is what extract_composition_from_prose expects)."""
    lines = section3_text.split("\n")
    rows = []
    for i, line in enumerate(lines):
        m = _CARD_NAME_PCT_RE.match(line.strip())
        if not m:
            continue
        name, pct = m.group(1).strip(), m.group(2).strip()
        cas = ""
        for lookahead in lines[i + 1: i + 4]:
            cas_m = _CAS_RE.search(lookahead)
            if cas_m:
                cas = cas_m.group(1)
                break
        if name and cas:
            rows.append({"chemical_name": name, "cas_number": cas, "concentration": pct})
    return rows


_LABELLED_NAME_RE = re.compile(
    r"(?i:product|substance|chemical)\s+(?i:name)\s*:\s*(.+?)(?=\s+[A-Z][A-Za-z /]{1,25}\s*:|$)")


def extract_composition_from_prose(section3_text: str) -> List[Dict[str, str]]:
    """Fallback for SDS's that list ingredients as prose sentences instead of
    a table, e.g. 'Sodium hydroxide, CAS 1310-73-2, 5-10%.' We split on CAS
    numbers as anchors and grab the nearest chemical-name-looking text before
    each one and the nearest percentage after it. Line breaks are treated as
    spaces so a name that wraps ('...and Reactive\\ndiluent (...)') isn't cut."""
    section3_text = re.sub(r"\s+", " ", section3_text)
    rows = []
    for m in _CAS_RE.finditer(section3_text):
        cas = m.group(1)
        # chemical name: text between the previous sentence boundary and this CAS number.
        # Strip the "CAS number"/"CAS No."/"CAS :" label text FIRST so the
        # length cap below isn't wasted on it (that was truncating the real name).
        before = section3_text[:m.start()]
        before = re.sub(r",?\s*\bCAS(?:[\s\-]*(?:No\.?|Number))?\s*[:#]?\s*$", "", before, flags=re.I)
        labelled = None
        for labelled in _LABELLED_NAME_RE.finditer(before[-200:]):
            pass
        if labelled:
            # 'Product name : Isopropyl alcohol  Synonyms : IPA  CAS : 67-63-0'
            name = labelled.group(1).strip(" ,.-")
        else:
            name_match = re.search(
                r"(?<![A-Za-z0-9])((?:\d[\d,']*-)?[A-Za-z][A-Za-z0-9\-,\'\(\) ]{2,70})[,:]?\s*$", before)
            name = name_match.group(1).strip(" ,.-") if name_match else None
        if name:
            # Keep only the last clause ('... by weight, and Reactive diluent
            # (...)' -> 'Reactive diluent (...)') and strip generic sentence
            # lead-ins ("This product contains Sodium hydroxide" -> "Sodium hydroxide").
            name = re.split(r"\s*(?:\bby weight\b,?|,?\s\band\b|;)\s+", name)[-1]
            name = re.sub(r"^(?:this\s+(?:product|mixture|material)\s+contains\s+|contains\s+|and\s+)",
                           "", name, flags=re.I).strip(" ,.-")
        # concentration: nearest % expression within ~40 chars after the CAS number
        after = section3_text[m.end(): m.end() + 40]
        pct_match = _PERCENT_RE.search(after)
        pct = pct_match.group(1).strip() if pct_match else ""
        if name:
            rows.append({"chemical_name": name, "cas_number": cas, "concentration": pct})
    return rows


def find_best_table(pages: List[pdf_utils.PageData], page_range: Tuple[int, int], hints: List[str]) -> Optional[List[List[str]]]:
    best_table = None
    best_score = 0
    lo, hi = page_range
    for p in pages:
        if not (lo <= p.page_number <= hi):
            continue
        for t in p.tables:
            if not t:
                continue
            score = _score_header(t[0], hints)
            if score > best_score:
                best_score = score
                best_table = t
    return best_table if best_score > 0 else None


def find_matching_tables(pages: List[pdf_utils.PageData], page_range: Tuple[int, int],
                         hints: List[str]) -> List[List[List[str]]]:
    """The best-scoring table plus every other table in range that has the
    same header row - composition tables that run over a page break, or
    that a vendor splits into several blocks ('Other substances with
    occupational exposure limits:', 'Other components:'), repeat their
    header, and all of them are ingredients."""
    best = find_best_table(pages, page_range, hints)
    if not best:
        return []
    key = [_cell(c).lower() for c in best[0]]
    lo, hi = page_range
    out = []
    for p in pages:
        if lo <= p.page_number <= hi:
            for t in p.tables:
                if t and (t is best or [_cell(c).lower() for c in t[0]] == key):
                    out.append(t)
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

class SDSRecord:
    def __init__(self):
        self.values: Dict[str, object] = {}
        self.warnings: List[str] = []
        self.source_file: Optional[str] = None
        self.sections_found: List[int] = []
        self.sections_missing: List[int] = []
        self.section_texts: Dict[int, str] = {}  # {section_number: raw_text}, used by llm_reconcile.py

    def to_dict(self) -> Dict:
        d = {"source_file": self.source_file}
        d.update(self.values)
        d["_warnings"] = self.warnings
        d["_sections_found"] = self.sections_found
        d["_sections_missing"] = self.sections_missing
        return d


_DATE_RES = (
    re.compile(r"\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}"),
    re.compile(r"\d{1,2}[-\s][A-Za-z]{3,9}[-\s]\d{2,4}"),
    re.compile(r"\d{4}[/\-]\d{1,2}[/\-]\d{1,2}"),
)


def _looks_like_date(s: str) -> bool:
    # Rejects PDF font-glyph corruption ('01/(cid:20)(cid:20)/11') and
    # other garbage - a real date is only digits/separators/month letters.
    s = s.strip()
    return any(r.fullmatch(s) for r in _DATE_RES)


def _first_date_in(s: str) -> Optional[str]:
    hits = [m for r in _DATE_RES for m in r.finditer(s)]
    return min(hits, key=lambda m: m.start()).group(0) if hits else None


def _looks_like_version_token(s: str) -> bool:
    # A real version/revision value is a short token (digits, maybe a
    # dot or letter) - never contains a space. This single check catches
    # both "REVISION NUMBERING HAS" (missing word boundary) and "rather
    # than the" (a prose sentence mentioning "revision date", not an
    # actual field) without needing a separate fix for each.
    s = s.strip()
    return bool(s) and " " not in s and len(s) <= 15


def _strip_running_section_titles(section_text: str, n: int) -> str:
    """A section that spans a page break often repeats its own title as a
    running header ('Section 8. Exposure controls/personal protection') at
    the top of the next page. The real header has already been skipped, so
    any later occurrence is noise that would otherwise end up inside a
    value ('None.\\nSection 8.')."""
    return re.sub(rf"(?im)^[ \t]*section[ \t]*{n}[ \t]*[\.:\-]?[ \t]+\S.*(?:\n|$)", "", section_text)


# Fields worth a whole-document look when a sheet doesn't use the 16-section
# GHS layout at all (pre-2012 MSDS with Roman-numeral sections, etc.), so the
# user at least gets the product, supplier and ingredients.
_NO_SECTION_FALLBACK_FIELDS = ("product_name", "manufacturer_name")


def extract_sds(path: str) -> SDSRecord:
    record = SDSRecord()
    record.source_file = path

    pages = pdf_utils.extract_pdf(path)
    text_unstripped = pdf_utils.full_text(pages)  # kept for version/date/name fallbacks - see below
    clean_pages = [
        pdf_utils.PageData(page_number=p.page_number, text=t, tables=p.tables)
        for p, t in zip(pages, strip_page_boilerplate([p.text for p in pages]))
    ]
    pages = clean_pages
    text = pdf_utils.full_text(pages)

    if len(text.strip()) < 200:
        record.warnings.append(
            "Very little text extracted - this PDF may be a scanned image "
            "without a text layer. OCR is not run by default (offline build "
            "has none installed); consider OCR'ing the file first."
        )

    boundaries = find_section_boundaries(text)
    offsets = build_page_offsets(pages)

    record.sections_found = sorted(boundaries.keys())
    record.sections_missing = [n for n in range(1, 17) if n not in boundaries]
    if record.sections_missing:
        missing_str = ", ".join(str(n) for n in record.sections_missing)
        record.warnings.append(f"Could not locate header for section(s): {missing_str}. "
                                f"Fields in those sections were skipped.")

    fields_by_section: Dict[int, List[schema.FieldDef]] = {}
    for f in schema.ALL_FIELDS:
        fields_by_section.setdefault(f.section, []).append(f)

    RAW_BLOCK_SECTIONS = (4, 5, 6, 7)
    for n in RAW_BLOCK_SECTIONS:
        record.values[f"_raw_section_{n}"] = None

    section_bodies: Dict[int, str] = {}
    for n, fdefs in fields_by_section.items():
        if n not in boundaries:
            for f in fdefs:
                record.values[f.var] = None
            continue

        start, end = boundaries[n]
        content_start = _skip_wrapped_header(text, start, end)
        section_text = _strip_running_section_titles(text[content_start:end], n)
        section_bodies[n] = section_text

        if n in RAW_BLOCK_SECTIONS:
            cleaned = strip_pdf_boilerplate(section_text)
            cleaned = strip_subitem_headings(cleaned)
            record.values[f"_raw_section_{n}"] = cleaned.strip()

        for f in fdefs:
            if f.kind == "table":
                continue  # handled separately below
            val = extract_field(section_text, f, fdefs)
            record.values[f.var] = val
            if val is None:
                record.warnings.append(f"Field '{f.var}' (section {n}) not found.")

    if not boundaries:
        # Not a GHS-format sheet - salvage the basics from the whole text.
        s1 = [replace(f, multiline=False) for f in schema.SECTION_1]
        for f in s1:
            if f.var in _NO_SECTION_FALLBACK_FIELDS:
                record.values[f.var] = extract_field(text, f, s1)

    if not record.values.get("product_name"):
        # The product name is often ALSO the running page header, and on
        # some sheets the only 'Product name:' line is in that header.
        s1 = [replace(f, multiline=False) for f in schema.SECTION_1]
        pn = next(f for f in s1 if f.var == "product_name")
        record.values["product_name"] = extract_field(text_unstripped, pn, s1)

    # --- Table fields ---
    def _table_dicts_look_valid(rows) -> bool:
        if not rows:
            return False
        if len(rows) == 1:
            # A single-row "table" with a very long key is almost always a
            # borderless/card-style layout that pdfplumber mis-parsed as one
            # merged cell, not a genuine one-ingredient table - the CAS-
            # number prose fallback below handles this layout far better.
            longest_key = max((len(k) for k in rows[0].keys()), default=0)
            if longest_key > 60:
                return False
        return True

    def _prose_composition(block: str):
        rows = extract_composition_from_card_layout(block)
        if not rows:
            rows = extract_composition_from_prose(block)
        if not rows:
            rows = extract_composition_no_cas_placeholder(block)
        return rows

    if 3 in boundaries:
        start, _ = boundaries[3]
        end = boundaries.get(4, (len(text), None))[0]
        p_lo = page_for_offset(offsets, start)
        p_hi = page_for_offset(offsets, end)
        table_rows = []
        for t in find_matching_tables(pages, (p_lo, p_hi), _COMPOSITION_HEADER_HINTS):
            table_rows.extend(_table_to_dicts(t))
        if table_rows and _table_dicts_look_valid(table_rows):
            record.values["composition_table"] = table_rows
        else:
            block = section_bodies.get(3) or text[start:end]
            prose_rows = _prose_composition(block)
            if prose_rows:
                record.values["composition_table"] = prose_rows
                record.warnings.append("Composition data was written as prose (no table detected) - "
                                        "extracted via CAS-number pattern matching; double-check names/percentages.")
            else:
                record.values["composition_table"] = None
                record.warnings.append("Composition table (section 3) not found via table extraction; "
                                        "check the raw text manually if this SDS uses an unusual layout.")
    elif not boundaries:
        record.values["composition_table"] = \
            [r for line in text.split("\n") for r in extract_composition_from_prose(line)] or None
    else:
        record.values["composition_table"] = None

    record.values["exposure_limits"] = None
    record.values["exposure_limit_lines"] = None
    if 8 in boundaries:
        start, _ = boundaries[8]
        end = boundaries.get(9, (len(text), None))[0]
        p_lo = page_for_offset(offsets, start)
        p_hi = page_for_offset(offsets, end)
        table = find_best_table(pages, (p_lo, p_hi), _EXPOSURE_HEADER_HINTS)
        if table and not _exposure_table_is_malformed(table):
            record.values["exposure_limits"] = _table_to_dicts(table)
        lines = exposure_limit_lines(section_bodies.get(8, ""))
        record.values["exposure_limit_lines"] = "\n".join(lines) if lines else None

    # A revision-date label sometimes shares its line with other dates
    # ('Revision Date: 03/22/2026 Print Date: 03/25/2026'); keep only the date.
    rev = record.values.get("revision_date")
    if rev and not _looks_like_date(rev):
        record.values["revision_date"] = _first_date_in(rev)

    # Many vendors (Fisher/Thermo in particular) print a title block with
    # "Creation Date", "Revision Date", "Revision Number" ABOVE Section 1
    # entirely - outside every section boundary, so the normal per-section
    # loop never sees it. Other vendors (Fuchs among them) only state the
    # version via a running header/footer repeated on every page, which the
    # boilerplate stripper above correctly removes (it was polluting
    # unrelated fields) - but that means it's also gone from the `text` this
    # loop searched. Fall back to the untouched original text if Section 16
    # didn't give us a version/date from the (cleaned) search.
    if not record.values.get("version_number"):
        for pattern in (
            r"(?im)(?<!the )revision\s*number\b\s*[:\-]?\s*(\S+)",
            r"(?im)\bversion\s*[:\-]?\s*n?\W?\s*(\d+(?:\.\d+)?)\b",  # tolerates French/EU "Version: N°2"
            r"(?im)\brevision\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*$",  # bare "Revision: 9" (no "number"/"date" word)
        ):
            for m in re.finditer(pattern, text_unstripped, re.MULTILINE):
                candidate = m.group(1).strip(" .")
                if _looks_like_version_token(candidate):
                    record.values["version_number"] = candidate
                    break
            if record.values.get("version_number"):
                break
    if not record.values.get("revision_date"):
        for pattern in (
            r"(?im)(?<!the )revision\s*date\b\s*[:\-]?\s*([\d/\.\-]+|\d{1,2}[-\s][A-Za-z]{3,9}[-\s]\d{2,4})",
            r"(?im)\bdate\s*[:\-]?\s*(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})",  # bare "Date: 17/02/2011" cover-page style
        ):
            for m in re.finditer(pattern, text_unstripped):
                candidate = m.group(1).strip(" .")
                if _looks_like_date(candidate):
                    record.values["revision_date"] = candidate
                    break
            if record.values.get("revision_date"):
                break

    section_texts = {n: text[boundaries[n][0]:boundaries[n][1]] for n in boundaries}
    record.section_texts = section_texts

    tox_sections_text = "\n".join(
        text[boundaries[n][0]:boundaries[n][1]] for n in (2, 11, 12) if n in boundaries
    )
    record.values.update(derived.compute_derived_fields(
        record.values, text, tox_text=tox_sections_text or None, section_texts=section_texts))

    return record
