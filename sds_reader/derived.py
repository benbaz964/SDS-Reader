"""
derived.py
==========
Second-pass computed fields that build on the raw section-by-section
extraction in extractor.py. These aren't simple "find this label" lookups -
they combine/reshape multiple raw fields (e.g. splitting "H225 Highly
flammable liquid..." into a code list and a wording-only string), or scan
the *whole* document for keyword mentions that aren't tied to one section
(e.g. "asthma" can turn up in Section 2, 11, or a company-specific note).

Ground rule from the person's spec: never rewrite the SDS's own wording -
only ever trim leading codes/numbers or select/combine whole sentences.
"""

import re
from typing import Dict, List, Optional

_H_CODE_RE = re.compile(r"\bH\d{3}[a-zA-Z]{0,2}\b")

_ILLNESS_KEYWORDS = [
    "asthma", "dermatitis", "eczema", "sensiti", "silicosis", "dermal",
    "occupational asthma", "contact dermatitis", "cancer", "carcinogen",
    "respiratory disease", "occupational disease", "pneumoconiosis",
]
_DUST_VAPOUR_FUME_KEYWORDS = ["dust", "vapour", "vapor", "fume", "mist", "aerosol"]
_DUST_KEYWORDS = ["dust"]
_VAPOUR_KEYWORDS = ["vapour", "vapor"]
_FUME_KEYWORDS = ["fume"]
_DERMATITIS_KEYWORDS = ["dermatitis", "dermatosis", "eczema", "dermal irritation", "skin condition"]

# Mentions that contain a keyword but are equipment or property names, not
# a hazard: 'chemical fume hood', 'fume scrubbers', 'Vapour pressure: 1.9 hPa'.
_KEYWORD_EXCLUSIONS = re.compile(
    r"\bfumes?\s+(?:hood|cupboard|scrubber|extract(?:ion|or))s?\b"
    r"|\bvapou?r\s+(?:pressure|density)\b", re.IGNORECASE)
# Section 9 (physical properties) only ever mentions vapour/dust as property
# names - never a scan source for hazard mentions.
_HAZARD_SCAN_EXCLUDED_SECTIONS = {9}
_ASTHMA_KEYWORDS = ["asthma"]
_ILLNESS_KEYWORDS_GENERIC = [
    "illness", "disease", "chronic bronchitis", "occupational disease",
    "silicosis", "pneumoconiosis", "syndrome",
]
_ASPHYXIANT_KEYWORDS = ["asphyxia"]
_DUSTINESS_KEYWORDS = ["dust", "volatil", "friable"]
_CARCINOGEN_KEYWORDS = ["carcinogen"]
_MUTAGEN_KEYWORDS = ["mutagen"]
_REPROTOX_KEYWORDS = ["reproductive toxic", "reprotoxic", "teratogen", "fertility"]
_SENSITISER_KEYWORDS = ["sensitis", "sensitiz"]


# ---------------------------------------------------------------------------
# Sentence-level keyword scanning
# ---------------------------------------------------------------------------

_LABEL_LINE_RE = re.compile(r"^[A-Z][\w\s/,\.\(\)\-]{1,100}:")
_SECTION_HEADER_LINE_RE = re.compile(r"^\s*SECTION\s+\d{1,2}\s*[:\.]", re.IGNORECASE)


_HYPHEN_WRAP_RE = re.compile(r"[a-zA-Z]-$")


def _join_wrapped_lines(text: str) -> List[str]:
    """PDF line breaks are ambiguous: some are hard boundaries between
    'Label: value' fields (no terminal punctuation, but the NEXT line starts
    a new label), others are just a long sentence wrapping mid-thought (no
    terminal punctuation, and the next line is lowercase continuation text
    with no label pattern). Merge only the latter case. A bare 'SECTION N:'
    header line is always treated as a hard boundary in both directions,
    regardless of punctuation, since it's never a genuine sentence fragment.
    A line ending in a hyphenated word-wrap ('va-' continuing as 'pour' on
    the next line) is rejoined WITHOUT a space and without the hyphen
    ('vapour') - justified-text PDFs do this constantly, and joining with a
    space instead ('va- pour') silently breaks keyword matching on exactly
    the words (dust/vapour/fume) this scanner is looking for."""
    lines = []
    buffer = ""
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            if buffer:
                lines.append(buffer)
                buffer = ""
            continue
        if buffer:
            is_header = _SECTION_HEADER_LINE_RE.match(buffer) or _SECTION_HEADER_LINE_RE.match(line)
            if not is_header and _HYPHEN_WRAP_RE.search(buffer) and line[:1].islower():
                buffer = buffer[:-1] + line
                continue
            if (not is_header and not re.search(r"[.!?:)]\s*$", buffer)
                    and not _LABEL_LINE_RE.match(line)):
                buffer = buffer + " " + line
                continue
            lines.append(buffer)
        buffer = line
    if buffer:
        lines.append(buffer)
    return lines


def _split_sentences(text: str) -> List[str]:
    # SDS text is full of short "Label: value" lines with no terminal
    # period, so line breaks are treated as hard boundaries by default -
    # except where a line is clearly a mid-sentence word-wrap rather than a
    # new field (see _join_wrapped_lines). Only *within* a resulting line do
    # we further split on sentence-ending punctuation.
    sentences = []
    for line in _join_wrapped_lines(text):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            continue
        for part in re.split(r"(?<=[.!?])\s+", line):
            part = part.strip()
            if part:
                sentences.append(part)
    return sentences


def _compile_keywords(keywords: List[str]) -> re.Pattern:
    # Leading \b only (not trailing) so stems like "sensitis" still match
    # "sensitisation"/"sensitizer", but "dust" won't match inside "Industrial".
    return re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keywords) + r")", re.IGNORECASE)


_MAX_SENTENCE_LEN = 350


def _clip_around_keyword(sentence: str, pattern: re.Pattern, context_chars: int = 120) -> str:
    """Safety net: if a 'sentence' is suspiciously long (almost always a
    failed line-merge rather than a genuine long sentence), don't return the
    whole runaway blob - clip to a window around the actual keyword hit.
    Snaps to word boundaries so it never cuts a word in half (e.g. "other"
    becoming "Products: er ...")."""
    if len(sentence) <= _MAX_SENTENCE_LEN:
        return sentence
    m = pattern.search(sentence)
    if not m:
        return sentence[:_MAX_SENTENCE_LEN] + "..."
    start = max(0, m.start() - context_chars)
    end = min(len(sentence), m.end() + context_chars)
    # Snap outward to the nearest whitespace so a word is never cut in half.
    while start > 0 and not sentence[start - 1].isspace():
        start -= 1
    while end < len(sentence) and not sentence[end].isspace():
        end += 1
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(sentence) else ""
    return f"{prefix}{sentence[start:end].strip()}{suffix}"


_HEADING_NUMBER_RE = re.compile(r"(?m)^[ \t]*\d{1,2}\.\d{1,2}\.?[ \t]+(?=[A-Z])")
_INLINE_TRAILING_NUMBER_RE = re.compile(r"[ \t]\d{1,2}\.\d{1,2}\.?(?=[ \t]+[A-Z]|[ \t]*$)")


def _strip_subitem_numbers(text: str) -> str:
    text = _HEADING_NUMBER_RE.sub("", text)
    text = _INLINE_TRAILING_NUMBER_RE.sub("", text)
    return text.strip()


def _has_real_keyword(sentence: str, pattern: re.Pattern) -> bool:
    """A keyword hit that survives after removing equipment/property phrases
    ('fume hood', 'vapour pressure')."""
    return bool(pattern.search(_KEYWORD_EXCLUSIONS.sub(" ", sentence)))


def sentences_with_keywords(text: str, keywords: List[str]) -> List[str]:
    pattern = _compile_keywords(keywords)
    hits = []
    seen = set()
    for sentence in _split_sentences(text):
        if _has_real_keyword(sentence, pattern):
            clipped = _strip_subitem_numbers(_clip_around_keyword(sentence, pattern))
            key = clipped.lower()[:120]
            if key not in seen:
                seen.add(key)
                hits.append(clipped)
    return hits


def scan_sections_for_keywords(section_texts: Optional[Dict[int, str]], keywords: List[str],
                                 section_numbers: Optional[List[int]] = None) -> Optional[str]:
    """Scans each section's text SEPARATELY (never concatenated) so a
    keyword hit can't merge content across a section boundary - the failure
    mode that let e.g. a Section 5 fire-hazard sentence bleed straight into
    Section 9's physical-properties block in the old whole-document scan.
    Falls back to nothing (None) if section_texts wasn't provided."""
    if not section_texts:
        return None
    nums = section_numbers if section_numbers is not None else \
        [n for n in sorted(section_texts.keys()) if n not in _HAZARD_SCAN_EXCLUDED_SECTIONS]
    hits = []
    seen = set()
    for n in nums:
        sec_text = section_texts.get(n)
        if not sec_text:
            continue
        for hit in sentences_with_keywords(sec_text, keywords):
            key = hit.lower()[:120]
            if key not in seen:
                seen.add(key)
                hits.append(hit)
    return " | ".join(hits) if hits else None


def _join_or_none(sentences: List[str]) -> Optional[str]:
    return " ".join(sentences) if sentences else None


# ---------------------------------------------------------------------------
# Hazard statement code/text split
# ---------------------------------------------------------------------------

_H_CODE_WITH_SEP_RE = re.compile(r"\bH\d{3}[a-zA-Z]{0,2}\b[ \t]*[-:][ \t]*|\bH\d{3}[a-zA-Z]{0,2}\b[ \t]*")


def split_hazard_codes_and_text(hazard_statements: Optional[str]):
    if not hazard_statements:
        return [], None
    codes = list(dict.fromkeys(_H_CODE_RE.findall(hazard_statements)))  # dedupe, keep order
    text_only = _H_CODE_WITH_SEP_RE.sub("", hazard_statements)
    text_only = re.sub(r"[ \t]{2,}", " ", text_only).strip(" .")
    return codes, (text_only or None)


# ---------------------------------------------------------------------------
# Composition / exposure-limit table normalization
# ---------------------------------------------------------------------------

def _find_key(row: Dict[str, str], contains_any: List[str]) -> Optional[str]:
    for k in row.keys():
        lk = k.lower()
        if any(token in lk for token in contains_any):
            return k
    return None


_STRAY_PERCENT_RE = re.compile(r"(?<=\s)\d{1,3}(?:[.,]\d+)?\s*%(?=\s|$)")


def normalize_composition(composition_table: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
    if not composition_table:
        return []
    out = []
    for row in composition_table:
        name_k = _find_key(row, ["chemical name", "chemical_name", "name", "substance", "component"])
        # Some vendors give EC/EINECS numbers in an 'Identifier' column instead of CAS.
        cas_k = _find_key(row, ["cas"]) or _find_key(row, ["identifier", "ec no", "einecs"])
        conc_k = _find_key(row, ["concentration", "%", "conc"])
        name = (row.get(name_k) or "").strip() if name_k else ""
        # Two-column layouts can drop a '100%' into the middle of a name
        # ('..., cyclics, 100% aromatics (2-25%)'); a bare % never belongs there.
        name = re.sub(r"\s{2,}", " ", _STRAY_PERCENT_RE.sub("", name)).strip()
        out.append({
            "name": name,
            "cas": ((row.get(cas_k) or "").strip() if cas_k else "").lstrip("*"),
            "concentration": (row.get(conc_k) or "").strip() if conc_k else "",
        })
    return [r for r in out if r["name"] or r["cas"]]


def normalize_exposure_limits(exposure_limits: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
    """One row per component with its long-term (TWA) and short-term (STEL)
    limits. Handles both table shapes vendors use: a column per limit type
    ('Component | TWA | STEL | Source') and a row per limit type
    ('Chemical name | Type | Exposure Limit Values | Source', with
    'TWA' and 'STEL' as separate rows for the same substance)."""
    if not exposure_limits:
        return []
    merged: Dict[str, Dict[str, str]] = {}
    for row in exposure_limits:
        comp_k = _find_key(row, ["component", "name", "substance", "chemical"])
        type_k = _find_key(row, ["type"])
        source_k = _find_key(row, ["source", "list", "jurisdiction", "basis"])
        component = (row.get(comp_k) or "").strip() if comp_k else ""
        entry = merged.setdefault(component.lower(), {"component": component, "ltel": "", "stel": "", "wel_type": ""})
        limit_type = (row.get(type_k) or "").strip().upper() if type_k else ""
        if limit_type in ("TWA", "STEL", "LTEL", "CEILING"):
            value_k = _find_key(row, ["value", "limit"])
            value = (row.get(value_k) or "").strip() if value_k else ""
            slot = "stel" if limit_type in ("STEL", "CEILING") else "ltel"
            entry[slot] = entry[slot] or value
        else:
            ltel_k = _find_key(row, ["twa", "ltel", "8 hr", "8-hr", "long"])
            stel_k = _find_key(row, ["stel", "short", "ceiling"])
            entry["ltel"] = entry["ltel"] or ((row.get(ltel_k) or "").strip() if ltel_k else "")
            entry["stel"] = entry["stel"] or ((row.get(stel_k) or "").strip() if stel_k else "")
        source = (row.get(source_k) or "").strip() if source_k else ""
        entry["wel_type"] = entry["wel_type"] or _limit_basis(source)
    return list(merged.values())


def _limit_basis(source: str) -> str:
    """'UK. EH40 Workplace Exposure Limits (WELs), as amended' -> 'WEL (EH40)'."""
    if not source:
        return ""
    if re.search(r"\bEH ?40\b|\bWELs?\b", source, re.IGNORECASE):
        return "WEL (EH40)"
    return source


def _match_exposure_row(name: str, cas: str, exposure_rows: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    name_low = (name or "").lower()
    for row in exposure_rows:
        comp_low = (row.get("component") or "").lower()
        if comp_low and (comp_low in name_low or name_low in comp_low):
            return row
    return None


_LIMIT_VALUE = r"([<>]?\s*\d[\d.,]*\s*(?:mg/m3|mg/m³|ppm|f/cc|f/ml|ml/m3))"
_TWA_RE = re.compile(r"(?:\bTWA\b|\bLTEL\b|long[- ]term)[^\n]{0,40}?" + _LIMIT_VALUE, re.IGNORECASE)
_STEL_RE = re.compile(r"(?:\bSTEL\b|short[- ]term)[^\n]{0,40}?" + _LIMIT_VALUE, re.IGNORECASE)


def _limits_from_lines(name: str, limit_lines: Optional[str]) -> Dict[str, str]:
    """Fallback when there's no usable limits table: find the line(s) that
    name this component in the Section 8 limit text and read the TWA/STEL
    values off them ('Barium chloride STEL: 1.5 mg/m3 15 min TWA: 0.5
    mg/m3 (8hr)'), including the following line when the values sit under
    the name ('Hydrocarbons, C9-C12, ...' / 'Long-term exposure limit
    (8-hour TWA): 350 mg/m3')."""
    if not name or not limit_lines:
        return {}
    key = re.sub(r"\s+", " ", name.lower())[:25]
    lines = limit_lines.split("\n")
    for i, line in enumerate(lines):
        if key and key in re.sub(r"\s+", " ", line.lower()):
            window = line
            if i + 1 < len(lines) and not _TWA_RE.search(line) and not _STEL_RE.search(line):
                window += "\n" + lines[i + 1]
            twa, stel = _TWA_RE.search(window), _STEL_RE.search(window)
            if twa or stel:
                return {"ltel": twa.group(1).strip() if twa else "",
                        "stel": stel.group(1).strip() if stel else ""}
    return {}


_NEGATION_RE = re.compile(r"\b(not|no|non|without|negative|absence of|did not|does not|no evidence)\b", re.IGNORECASE)
# Negative conclusions that come AFTER the keyword ('Carcinogenicity Based on
# available data the classification criteria are not met').
_TRAILING_NEGATION_RE = re.compile(
    r"\b(?:criteria (?:are|is|were) not met|not classified|not classifiable|not expected|no data|"
    r"no evidence|no information|not available|not applicable|not determined|not known|no known|"
    r"negative|none)\b", re.IGNORECASE)
# A category-name form of a keyword ('Carcinogenicity:', 'Sensitisation',
# 'Reproductive toxicity -') is a heading, not an assertion about anything.
_HEADING_WORD_RE = re.compile(r"\w*(?:icity|i[sz]ation|ity)\b", re.IGNORECASE)


def _keyword_is_asserted(sentence: str, m: re.Match) -> bool:
    # The word the match ends in ('Reproductive toxic' -> 'toxicity').
    last_word_start = m.start() + sentence[m.start():m.end()].rfind(" ") + 1
    word = _HEADING_WORD_RE.match(sentence, last_word_start)
    if word and word.end() > m.end():
        rest = sentence[word.end():]
        # A heading: starts the sentence, is followed by a separator, is the
        # whole of what's left ('Germ cell mutagenicity'), or runs straight
        # into the next capitalised line ('Germ cell mutagenicity Ames test').
        if m.start() == 0 or re.match(r"\s*(?:[:\-–;]|[.)]?\s*$)|\s+[A-Z]", rest):
            return False
    # Any negation earlier in the same sentence ('No component of this
    # product ... is identified as a carcinogen').
    if _NEGATION_RE.search(sentence[:m.start()]):
        return False
    if _TRAILING_NEGATION_RE.search(sentence[m.end():]):
        return False
    return True


def _flag(sentences: List[str], keywords: List[str]) -> str:
    """Yes only if one of these sentences actually asserts the hazard: the
    keyword isn't just a heading ('Carcinogenicity:') and isn't negated
    before ('did not indicate mutagenic potential') or after ('...the
    classification criteria are not met')."""
    pattern = _compile_keywords(keywords)
    for sentence in sentences:
        if any(_keyword_is_asserted(sentence, m) for m in pattern.finditer(sentence)):
            return "Yes"
    return "Not stated"


def _mentions(sentence_low: str, name: str, cas: str) -> bool:
    name_low = (name or "").lower()
    return (len(name_low) >= 3 and name_low in sentence_low) or (bool(cas) and cas.lower() in sentence_low)


def _sentences_about(composition_rows: List[Dict[str, str]], scan_text: str) -> List[List[str]]:
    """For each ingredient, the sentences that are actually about it: ones
    that name it (or its CAS number), plus the sentences that follow a line
    consisting of just its name (the 'Alkanolamine' / 'No sensitizing
    effect (guinea pig)' sub-heading layout). On a single-ingredient sheet
    every sentence is about that ingredient.

    Earlier versions used a fixed character window around each name, which
    attributed a hazard sentence to whichever ingredient happened to be
    printed nearby (e.g. chromium flagged as a sensitiser because the line
    before said 'Nickel is a known skin sensitizer')."""
    sentences = _split_sentences(scan_text)
    if len(composition_rows) <= 1:
        return [sentences for _ in composition_rows]
    about: List[List[str]] = [[] for _ in composition_rows]
    subject: Optional[int] = None
    subject_left = 0
    for s in sentences:
        low = s.lower()
        named = [i for i, r in enumerate(composition_rows) if _mentions(low, r["name"], r["cas"])]
        if named:
            for i in named:
                about[i].append(s)
            # A bare sub-heading naming one ingredient sets the subject for
            # the couple of sentences under it; any other naming sentence ends it.
            is_heading = len(named) == 1 and len(s) <= len(composition_rows[named[0]]["name"]) + 15
            subject, subject_left = (named[0], 2) if is_heading else (None, 0)
        elif subject is not None and subject_left > 0:
            about[subject].append(s)
            subject_left -= 1
    return about


def build_substance_breakdown(composition_rows: List[Dict[str, str]],
                                exposure_rows: List[Dict[str, str]],
                                full_text: str,
                                tox_text: Optional[str] = None,
                                limit_lines: Optional[str] = None) -> List[Dict[str, str]]:
    """Per-ingredient breakdown row, combining composition + exposure-limit
    data with a scan for toxicological flags in the sentences about each
    ingredient. The scan is restricted to Sections 2/11/12 (tox_text) where
    possible. Flags are necessarily heuristic (SDS's rarely tag hazard-by-
    hazard per component in a machine-friendly way) - treat 'Yes' as 'worth
    checking the source text', not as a certified classification."""
    scan_text = tox_text if tox_text else full_text
    about = _sentences_about(composition_rows, scan_text)
    rows = []
    for comp, sentences in zip(composition_rows, about):
        name, cas = comp["name"], comp["cas"]
        exp = _match_exposure_row(name, cas, exposure_rows) or {}
        if not (exp.get("ltel") or exp.get("stel")):
            exp = {**exp, **_limits_from_lines(name, limit_lines)}
        rows.append({
            "name": name or "(unnamed component)",
            "cas": cas or "N/A",
            "concentration": comp.get("concentration") or "Not stated",
            "wel_type": exp.get("wel_type") or "Not stated",
            "ltel": exp.get("ltel") or "Not stated",
            "stel": exp.get("stel") or "Not stated",
            "carcinogenic": _flag(sentences, _CARCINOGEN_KEYWORDS),
            "mutagenic": _flag(sentences, _MUTAGEN_KEYWORDS),
            "reproductive_toxicant": _flag(sentences, _REPROTOX_KEYWORDS),
            "sensitiser": _flag(sentences, _SENSITISER_KEYWORDS),
        })
    return rows


# ---------------------------------------------------------------------------
# Main entry point - called by extractor.extract_sds()
# ---------------------------------------------------------------------------

def compute_derived_fields(values: Dict, full_text: str, tox_text: Optional[str] = None,
                             section_texts: Optional[Dict[int, str]] = None) -> Dict:
    d = {}

    def _looks_like_cas_or_placeholder(s: str) -> bool:
        if not s:
            return False
        s = s.strip()
        if re.match(r"^\d{2,7}-\d{2}-\d$", s):
            return True
        # UVCB/complex substances often have no discrete CAS number and use
        # a placeholder instead (em-dash, "N/A", "Not applicable", etc.)
        return bool(re.match(r"^[—–\-]$", s)) or s.lower() in (
            "n/a", "not applicable", "not assigned", "none", "not available",
        )

    # --- CAS number display logic ---
    # Section 1's explicitly-stated CAS number is authoritative when present -
    # some single-substance SDS's (e.g. a hydrate) list 2+ rows in the
    # composition table (the hydrate AND its anhydrous parent CAS for
    # regulatory cross-reference) while still being one substance with one
    # CAS number given up front. Only fall back to the composition-table
    # count heuristic when Section 1 doesn't state one directly.
    comp_rows = normalize_composition(values.get("composition_table"))
    cas_main = values.get("cas_number_main")
    if cas_main and _looks_like_cas_or_placeholder(cas_main):
        d["cas_number_display"] = cas_main if re.match(r"^\d", cas_main) else "N/A (no CAS assigned)"
    elif len(comp_rows) > 1:
        d["cas_number_display"] = "N/A (mixture)"
    elif len(comp_rows) == 1 and comp_rows[0]["cas"]:
        d["cas_number_display"] = comp_rows[0]["cas"]
    else:
        d["cas_number_display"] = None

    # --- SDS version & date ---
    version = values.get("version_number")
    date = values.get("revision_date")
    if version and date:
        d["sds_version_date"] = f"Version {version} ({date})"
    elif date:
        d["sds_version_date"] = date
    elif version:
        d["sds_version_date"] = f"Version {version}"
    else:
        d["sds_version_date"] = None

    # --- Simple date/version aliases (target-schema naming) ---
    d["sds_date"] = values.get("revision_date")
    d["sds_version"] = values.get("version_number")

    # --- Overall (primary) composition percentage ---
    if len(comp_rows) == 1 and comp_rows[0].get("concentration"):
        d["overall_breakdown_percentage"] = comp_rows[0]["concentration"]
    elif comp_rows:
        # Multiple ingredients: report the highest-concentration one, since
        # that's usually what "overall breakdown %" means on a target sheet
        # for a substance that's mostly one thing with minor additives.
        def _pct_upper_bound(row):
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*%?\s*$", row.get("concentration", "") or "")
            return float(m.group(1).replace(",", ".")) if m else -1
        best = max(comp_rows, key=_pct_upper_bound)
        d["overall_breakdown_percentage"] = best.get("concentration") or None
    else:
        d["overall_breakdown_percentage"] = None

    # --- Numeric-only melting/boiling point (strips trailing unit text,
    # e.g. "660 C" -> "660"; leaves ranges like "110-144 C" -> "110-144").
    # Supports both decimal styles ("-89.5" and the European "-89,5"). ---
    def _numeric_prefix(value):
        if not value:
            return None
        m = re.match(r"^(-?\d+(?:[.,]\d+)?(?:\s*(?:-|–|to)\s*-?\d+(?:[.,]\d+)?)?)", value.strip())
        return m.group(1).strip() if m else value
    d["melting_point_numeric"] = _numeric_prefix(values.get("melting_point"))
    d["boiling_point_numeric"] = _numeric_prefix(values.get("boiling_point"))

    # --- Hazard statement code/text split ---
    codes, text_only = split_hazard_codes_and_text(values.get("hazard_statements"))
    d["hazard_codes"] = ", ".join(codes) if codes else None
    d["hazard_statement_text"] = text_only or values.get("hazard_statements")

    # --- Reactive / incompatible materials ---
    reactive_bits = [values.get(k) for k in ("reactivity", "hazardous_reactions", "conditions_to_avoid")]
    reactive_bits = [b for b in reactive_bits if b]
    d["reactive_materials"] = " ".join(reactive_bits) if reactive_bits else None

    incompat_bits = [values.get(k) for k in ("incompatible_materials_storage", "incompatible_materials_stability")]
    incompat_bits = [b for b in incompat_bits if b]
    d["incompatible_materials"] = " ".join(dict.fromkeys(incompat_bits)) if incompat_bits else None

    # --- Keyword-scan fields: prefer the toxicology-relevant sections
    # (2/11/12) as the canonical source - that's where a "Dust hazard" or
    # "may cause dermatitis" callout actually lives on a real SDS - and only
    # fall back to a whole-document scan if nothing turns up there. This
    # avoids pulling in duplicative mentions from Sections 4-7 that just
    # happen to reuse the same word in an unrelated handling instruction.
    def _scan_prefer_tox(keywords):
        if section_texts:
            hit = scan_sections_for_keywords(section_texts, keywords, section_numbers=[2, 11, 12])
            if hit:
                return hit
            hit = scan_sections_for_keywords(section_texts, keywords)
            if hit:
                return hit
        # No section boundaries available (shouldn't normally happen) -
        # fall back to the old whole-document scan as a last resort.
        if tox_text:
            hit = _join_or_none(sentences_with_keywords(tox_text, keywords))
            if hit:
                return hit
        return _join_or_none(sentences_with_keywords(full_text, keywords))

    # --- Dustiness / volatility ---
    d["dustiness_volatility"] = _scan_prefer_tox(_DUSTINESS_KEYWORDS)

    # --- Whole-document keyword scans ---
    d["health_hazard_mentions"] = _scan_prefer_tox(_ILLNESS_KEYWORDS)
    d["dusts_vapours_fumes_mentions"] = _scan_prefer_tox(_DUST_VAPOUR_FUME_KEYWORDS)
    d["asphyxiant_mentions"] = _scan_prefer_tox(_ASPHYXIANT_KEYWORDS)

    # --- Target-schema separated categories (own keyword list each) ---
    d["dust_mentions"] = _scan_prefer_tox(_DUST_KEYWORDS)
    d["vapour_mentions"] = _scan_prefer_tox(_VAPOUR_KEYWORDS)
    d["fume_mentions"] = _scan_prefer_tox(_FUME_KEYWORDS)
    d["dermatitis_mentions"] = _scan_prefer_tox(_DERMATITIS_KEYWORDS)
    d["asthma_mentions"] = _scan_prefer_tox(_ASTHMA_KEYWORDS)
    d["illness_mentions"] = _scan_prefer_tox(_ILLNESS_KEYWORDS_GENERIC)


    other_bits = [values.get(k) for k in ("hazards_not_otherwise_classified", "stot_single", "stot_repeated", "aspiration_hazard")]
    other_bits = [b for b in other_bits if b]
    d["other_health_hazards"] = " ".join(other_bits) if other_bits else None

    # --- Reproductive toxicity + teratogenicity combined (some vendors list
    # these as separate toxicology sub-items; both are relevant to "Reproductive
    # Toxins" in the target export schema) ---
    repro_lines = [line for k in ("reproductive_toxicity", "teratogenicity")
                   for line in (values.get(k) or "").split("\n") if line.strip()]
    d["reproductive_toxicity_combined"] = "\n".join(dict.fromkeys(repro_lines)) if repro_lines else None

    # --- Fallback for older/pre-CLP narrative-style SDS's that never use
    # separate "Carcinogenicity:"/"Mutagenicity:"/"Reproductive toxicity:"
    # sub-headings at all, instead covering all three in one free-text
    # sentence (e.g. "not expected to be carcinogenic, mutagenic...or toxic
    # for reproduction"). Only fires if the labeled field truly wasn't found. ---
    if not values.get("carcinogenicity"):
        hit = scan_sections_for_keywords(section_texts, ["carcinogen"], section_numbers=[11]) if section_texts else None
        if hit:
            d["carcinogenicity_fallback"] = hit
    if not values.get("germ_cell_mutagenicity"):
        hit = scan_sections_for_keywords(section_texts, ["mutagen"], section_numbers=[11]) if section_texts else None
        if hit:
            d["germ_cell_mutagenicity_fallback"] = hit
    if not d["reproductive_toxicity_combined"]:
        hit = scan_sections_for_keywords(section_texts, ["reprodu", "teratogen"], section_numbers=[11]) if section_texts else None
        if hit:
            d["reproductive_toxicity_combined"] = hit

    # --- PPE aliases (split hand vs. general skin/body protection) ---
    d["storage_requirements"] = values.get("storage_conditions")
    d["respiratory_protection"] = values.get("ppe_respiratory")
    d["eye_protection"] = values.get("ppe_eye")
    # ppe_skin's labels are hand/glove-oriented. A bare "Skin protection"
    # heading holds gloves on some sheets (Sigma/Merck style) and clothing on
    # others, so route it by what it actually talks about.
    skin_general = values.get("ppe_skin_general")
    general_is_gloves = bool(skin_general and re.search(r"\bglove", skin_general, re.IGNORECASE))
    d["hand_protection"] = values.get("ppe_skin") or (skin_general if general_is_gloves else None)
    d["skin_protection"] = values.get("ppe_body") or (None if general_is_gloves else skin_general) \
        or values.get("ppe_general")

    # --- Combined narrative sections (verbatim sub-parts, just concatenated) ---
    spill_bits = [values.get(k) for k in ("release_personal_precautions", "release_environmental_precautions", "release_containment_cleanup")]
    d["spillage_release_advice"] = " ".join(b for b in spill_bits if b) or None

    fire_bits = [values.get(k) for k in ("extinguishing_media_suitable", "extinguishing_media_unsuitable", "fire_specific_hazards", "fire_protective_equipment")]
    d["fire_advice"] = " ".join(b for b in fire_bits if b) or None

    first_aid_bits = []
    for label, key in [("General", "first_aid_general"), ("Inhalation", "first_aid_inhalation"),
                        ("Skin", "first_aid_skin"), ("Eyes", "first_aid_eye"),
                        ("Ingestion", "first_aid_ingestion"), ("Symptoms", "first_aid_symptoms"),
                        ("Medical attention", "first_aid_medical_attention")]:
        v = values.get(key)
        if v:
            first_aid_bits.append(f"{label}: {v}")
    d["first_aid"] = " | ".join(first_aid_bits) if first_aid_bits else None

    # --- Per-ingredient breakdown table ---
    exp_rows = normalize_exposure_limits(values.get("exposure_limits"))
    d["substance_breakdown"] = build_substance_breakdown(comp_rows, exp_rows, full_text, tox_text=tox_text,
                                                         limit_lines=values.get("exposure_limit_lines"))

    return d
