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
    period_m = re.search(r"[.!?]\s", window)
    if period_m and period_m.start() < colon_m.start():
        # A sentence boundary appears before the colon - this looks like real
        # content followed by a *different* field's label on the same line,
        # not label overflow text. Don't skip.
        return start
    if other_labels_pattern and other_labels_pattern.search(window[:colon_m.end()]):
        # The text up to the colon matches a *sibling* field's own label
        # (e.g. "Clear liquid Odor:" while extracting Appearance) - that's a
        # different field starting here, not leftover wording from ours.
        # Leave `start` alone; the sibling-boundary logic below will cut the
        # value off at the right place instead.
        return start
    if re.search(r"\bresult\b", window[:colon_m.end()], re.IGNORECASE):
        # "<Test name> Result:" (e.g. "Buehler Test - Guinea pig Result:",
        # "Ames test Result:") is near-universal toxicology-section phrasing
        # for citing a specific study - it's real content, not leftover
        # label wording, even though it happens to end in a colon.
        return start
    return start + colon_m.end()


_SUBITEM_PREFIX = r"(?:^[ \t]*(?:\d{1,2}\.\d{1,2}\.?|\([a-z]\))[ \t]+)?"


_BARE_HEADING_STOPLIST = {
    "acute toxicity", "irritation/corrosion", "irritation", "corrosion",
    "sensitization", "sensitisation", "mutagenicity", "carcinogenicity",
    "reproductive toxicity", "teratogenicity", "aspiration hazard",
    "information on toxicological effects", "developmental effects",
    "fertility effects",
    "specific target organ toxicity (single exposure)",
    "specific target organ toxicity (repeated exposure)",
}


def extract_field(section_text: str, fdef: schema.FieldDef, siblings: List[schema.FieldDef]) -> Optional[str]:
    label_alt = "|".join(fdef.labels)
    label_pat = re.compile(rf"(?im)(?:{label_alt})\s*[:\-]?\s*")

    other_labels = _all_label_alternation(siblings, fdef)
    other_labels_pattern = re.compile(rf"(?im)(?:{other_labels})") if other_labels else None

    search_pos = 0
    while True:
        m = label_pat.search(section_text, search_pos)
        if not m:
            return None
        start = _skip_leftover_header_text(section_text, m.end(), other_labels_pattern)

        # Hard ceiling on the value's end: a full paragraph (up to a blank line)
        # for multiline fields, or just the rest of the current physical line
        # for single-line fields - but in both cases, a *sibling* label appearing
        # before that ceiling (even sharing the same line, e.g. "Appearance:
        # Clear liquid   Odor: Citrus") should cut the value off first.
        if fdef.multiline:
            hard_end = len(section_text)
            blank = _STOPWORD_TAIL.search(section_text, start)
            if blank:
                hard_end = blank.start()
        else:
            line_end = section_text.find("\n", start)
            hard_end = line_end if line_end != -1 else len(section_text)

        end_candidates = [hard_end]
        if other_labels:
            next_label = re.compile(rf"(?im){_SUBITEM_PREFIX}(?:{other_labels})\s*[:\-]?\s*")
            nm = next_label.search(section_text, start)
            if nm:
                end_candidates.append(nm.start())

        end = min(end_candidates)
        cleaned = _clean(section_text[start:end])
        if cleaned and cleaned.strip().lower() not in _BARE_HEADING_STOPLIST:
            return cleaned
        # This occurrence of the label had nothing after it before hitting a
        # sibling/blank line (e.g. a bare heading in a heading-only cluster,
        # common when a PDF's text extraction groups all subsection titles
        # together separately from their values). Try the next occurrence of
        # this same label further in the section rather than giving up.
        search_pos = m.end()


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
_BARE_DOC_CODE_RE = re.compile(r"^[A-Z]{2,10}\d{2,10}$")


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


def strip_repeated_boilerplate_lines(text: str) -> str:
    """Generalized page-footer/header stripper: rather than hardcoding known
    vendor patterns (Fisher's 'FSUB0550', ChemicalBook's 'Chemical Book N',
    etc.), detect the *shape* any of them share, in two ways:

    1. A short text prefix followed by a number (or an 'N/M' page-of-total
       fraction) that increases roughly in step with the page count,
       repeated 3+ times. Requiring monotonic increase (not just
       repetition) is what keeps this safe: a real repeated short answer
       like 'No data available' never ends in a number, and a numbered
       content list ('Component 1', 'Component 2'...) would need the exact
       same prefix AND strictly increasing numbers AND 3+ occurrences to
       false-trigger, which real ingredient/component lists don't produce.
    2. A line that repeats VERBATIM 3+ times and never varies at all (e.g.
       'Issue Date: 10.12.2015' on every page) - guarded by a stoplist of
       common short generic SDS answers so a real repeated answer is never
       mistaken for a running header just because it's short and frequent."""
    lines = text.split("\n")

    # --- Pass 1: prefix + increasing number/fraction ---
    occurrences: Dict[str, List[Tuple[int, int]]] = {}
    for i, line in enumerate(lines):
        s = line.strip()
        m = _TRAILING_FRACTION_LINE_RE.match(s) or _TRAILING_NUM_LINE_RE.match(s)
        if not m:
            continue
        prefix = m.group(1).strip()
        if not (2 <= len(prefix) <= 90):
            continue
        try:
            num = int(m.group(2))
        except ValueError:
            continue
        occurrences.setdefault(prefix.lower(), []).append((i, num))

    boilerplate_prefixes = set()
    for prefix, occs in occurrences.items():
        if len(occs) < 3:
            continue
        nums = [n for _, n in occs]
        if all(b > a for a, b in zip(nums, nums[1:])):
            boilerplate_prefixes.add(prefix)

    # --- Pass 2: exact-repeat short lines ---
    # A genuine page header/footer is a complete short phrase or code. A line
    # that happens to repeat 3+ times but ends mid-sentence on a preposition/
    # conjunction/article is a wrapped SENTENCE FRAGMENT, not a header - some
    # vendors reuse the exact same templated legal sentence for several
    # toxicology sub-items ("No component...is classified by" + a different
    # final word each time), which would otherwise false-trigger here.
    _DANGLING_WORD_RE = re.compile(
        r"\b(?:by|of|and|or|the|in|to|for|with|a|an|as|at|is|are|was|were|be|been)$",
        re.IGNORECASE,
    )
    exact_counts: Dict[str, int] = {}
    for line in lines:
        s = line.strip()
        if not s or len(s) > 160 or len(s) < 8:
            continue
        if s.lower() in _GENERIC_ANSWER_STOPLIST:
            continue
        if not s.endswith((".", ":", ")", "-")) and _DANGLING_WORD_RE.search(s):
            continue
        exact_counts[s] = exact_counts.get(s, 0) + 1
    exact_repeated = {s for s, c in exact_counts.items() if c >= 3}

    if not boilerplate_prefixes and not exact_repeated:
        text_after_passes = text
    else:
        kept = []
        for line in lines:
            s = line.strip()
            if s in exact_repeated:
                continue
            m = _TRAILING_FRACTION_LINE_RE.match(s) or _TRAILING_NUM_LINE_RE.match(s)
            if m and m.group(1).strip().lower() in boilerplate_prefixes:
                continue
            kept.append(line)
        text_after_passes = "\n".join(kept)

    # --- Pass 3: bare 'N/M' page-number lines, unconditionally (no
    # frequency check needed - a standalone page fraction is never real
    # content, unlike the other two passes which need care to avoid
    # false-triggering on genuine repeated text). ---
    return "\n".join(
        line for line in text_after_passes.split("\n")
        if not _PAGE_NUMBER_LINE_RE.match(line.strip())
    )


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


def _clean(value: str) -> str:
    value = value.replace("\x00", "")
    value = strip_pdf_boilerplate(value)
    value = strip_subitem_headings(value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{2,}", "\n", value)
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


def _table_to_dicts(table: List[List[str]]) -> List[Dict[str, str]]:
    if not table or len(table) < 2:
        return []
    header = [ (c or "").strip() for c in table[0] ]
    rows = []
    for raw_row in table[1:]:
        row = {}
        for i, cell in enumerate(raw_row):
            key = header[i] if i < len(header) and header[i] else f"col_{i+1}"
            row[key] = (cell or "").strip()
        # skip fully-empty rows
        if any(v for v in row.values()):
            rows.append(row)
    return rows


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


def extract_composition_from_prose(section3_text: str) -> List[Dict[str, str]]:
    """Fallback for SDS's that list ingredients as prose sentences instead of
    a table, e.g. 'Sodium hydroxide, CAS 1310-73-2, 5-10%.' We split on CAS
    numbers as anchors and grab the nearest chemical-name-looking text before
    each one and the nearest percentage after it."""
    rows = []
    for m in _CAS_RE.finditer(section3_text):
        cas = m.group(1)
        # chemical name: text between the previous sentence boundary and this CAS number.
        # Strip the "CAS number"/"CAS No." label text FIRST so the length cap
        # below isn't wasted on it (that was truncating the real name).
        before = section3_text[:m.start()]
        before = re.sub(r",?\s*\bCAS(?:\s*(?:No\.?|Number))?\s*$", "", before, flags=re.I)
        name_match = re.search(r"([A-Za-z][A-Za-z0-9\-,\'\(\) ]{2,70})[,:]?\s*$", before)
        name = name_match.group(1).strip(" ,.-") if name_match else None
        if name:
            # Strip generic sentence lead-ins that sometimes get swept up
            # ("This product contains Sodium hydroxide" -> "Sodium hydroxide").
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


def extract_sds(path: str) -> SDSRecord:
    record = SDSRecord()
    record.source_file = path

    pages = pdf_utils.extract_pdf(path)
    text = pdf_utils.full_text(pages)
    text_unstripped = text  # kept for version/date fallback - see below
    text = strip_repeated_boilerplate_lines(text)

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

    for n, fdefs in fields_by_section.items():
        if n not in boundaries:
            for f in fdefs:
                record.values[f.var] = None
            continue

        start, end = boundaries[n]
        content_start = _skip_wrapped_header(text, start, end)
        section_text = text[content_start:end]

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

    if 3 in boundaries:
        start, _ = boundaries[3]
        end = boundaries.get(4, (len(text), None))[0]
        p_lo = page_for_offset(offsets, start)
        p_hi = page_for_offset(offsets, end)
        table = find_best_table(pages, (p_lo, p_hi), _COMPOSITION_HEADER_HINTS)
        table_rows = _table_to_dicts(table) if table else None
        if table_rows and _table_dicts_look_valid(table_rows):
            record.values["composition_table"] = table_rows
        else:
            card_rows = extract_composition_from_card_layout(text[start:end])
            prose_rows = card_rows if card_rows else extract_composition_from_prose(text[start:end])
            if not prose_rows:
                prose_rows = extract_composition_no_cas_placeholder(text[start:end])
            if prose_rows:
                record.values["composition_table"] = prose_rows
                record.warnings.append("Composition data was written as prose (no table detected) - "
                                        "extracted via CAS-number pattern matching; double-check names/percentages.")
            else:
                record.values["composition_table"] = None
                record.warnings.append("Composition table (section 3) not found via table extraction; "
                                        "check the raw text manually if this SDS uses an unusual layout.")
    else:
        record.values["composition_table"] = None

    if 8 in boundaries:
        start, _ = boundaries[8]
        end = boundaries.get(9, (len(text), None))[0]
        p_lo = page_for_offset(offsets, start)
        p_hi = page_for_offset(offsets, end)
        table = find_best_table(pages, (p_lo, p_hi), _EXPOSURE_HEADER_HINTS)
        record.values["exposure_limits"] = _table_to_dicts(table) if table else None
    else:
        record.values["exposure_limits"] = None

    # Many vendors (Fisher/Thermo in particular) print a title block with
    # "Creation Date", "Revision Date", "Revision Number" ABOVE Section 1
    # entirely - outside every section boundary, so the normal per-section
    # loop never sees it. Other vendors (Fuchs among them) only state the
    # version via a running header/footer repeated on every page, which the
    # boilerplate stripper above correctly removes everywhere (it was
    # polluting unrelated fields) - but that means it's also gone from the
    # `text` this loop searched. Fall back to the untouched original text if
    # Section 16 didn't give us a version/date from the (cleaned) search.
    # (Searches text_unstripped directly, not a sliced "preamble" - boundary
    # offsets were computed against the stripped text and don't line up with
    # the unstripped one, which is a different length.)
    def _looks_like_date(s: str) -> bool:
        # Rejects PDF font-glyph corruption ('01/(cid:20)(cid:20)/11') and
        # other garbage - a real date is only digits/separators/month letters.
        s = s.strip()
        return bool(re.match(r"^\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}$", s)) or \
            bool(re.match(r"^\d{1,2}[-\s][A-Za-z]{3,9}[-\s]\d{2,4}$", s)) or \
            bool(re.match(r"^\d{4}[/\-]\d{1,2}[/\-]\d{1,2}$", s))

    def _looks_like_version_token(s: str) -> bool:
        # A real version/revision value is a short token (digits, maybe a
        # dot or letter) - never contains a space. This single check catches
        # both "REVISION NUMBERING HAS" (missing word boundary) and "rather
        # than the" (a prose sentence mentioning "revision date", not an
        # actual field) without needing a separate fix for each.
        s = s.strip()
        return bool(s) and " " not in s and len(s) <= 15

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
