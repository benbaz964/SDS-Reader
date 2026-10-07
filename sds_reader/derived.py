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
_DERMATITIS_KEYWORDS = ["dermatitis", "eczema", "dermal irritation", "skin condition"]
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


def sentences_with_keywords(text: str, keywords: List[str]) -> List[str]:
    pattern = _compile_keywords(keywords)
    hits = []
    seen = set()
    for sentence in _split_sentences(text):
        if pattern.search(sentence):
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
    nums = section_numbers if section_numbers is not None else sorted(section_texts.keys())
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


def normalize_composition(composition_table: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
    if not composition_table:
        return []
    out = []
    for row in composition_table:
        name_k = _find_key(row, ["chemical name", "chemical_name", "name", "substance", "component"])
        cas_k = _find_key(row, ["cas"])
        conc_k = _find_key(row, ["concentration", "%", "conc"])
        out.append({
            "name": (row.get(name_k) or "").strip() if name_k else "",
            "cas": (row.get(cas_k) or "").strip() if cas_k else "",
            "concentration": (row.get(conc_k) or "").strip() if conc_k else "",
        })
    return [r for r in out if r["name"] or r["cas"]]


def normalize_exposure_limits(exposure_limits: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
    if not exposure_limits:
        return []
    out = []
    for row in exposure_limits:
        comp_k = _find_key(row, ["component", "name", "substance", "chemical"])
        ltel_k = _find_key(row, ["twa", "ltel", "8 hr", "8-hr", "long"])
        stel_k = _find_key(row, ["stel", "short", "ceiling"])
        type_k = _find_key(row, ["source", "type", "list", "jurisdiction"])
        out.append({
            "component": (row.get(comp_k) or "").strip() if comp_k else "",
            "ltel": (row.get(ltel_k) or "").strip() if ltel_k else "",
            "stel": (row.get(stel_k) or "").strip() if stel_k else "",
            "wel_type": (row.get(type_k) or "").strip() if type_k else "",
        })
    return out


def _match_exposure_row(name: str, cas: str, exposure_rows: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    name_low = (name or "").lower()
    for row in exposure_rows:
        comp_low = (row.get("component") or "").lower()
        if comp_low and (comp_low in name_low or name_low in comp_low):
            return row
    return None


def _windows_around(text: str, needle: str, window: int = 300) -> str:
    if not needle:
        return ""
    chunks = []
    low = text.lower()
    needle_low = needle.lower()
    start = 0
    while True:
        idx = low.find(needle_low, start)
        if idx == -1:
            break
        chunks.append(text[max(0, idx - window): idx + len(needle) + window])
        start = idx + len(needle)
    return " ".join(chunks)


_NEGATION_RE = re.compile(r"\b(not|no|non|without|negative|absence of|did not|does not|no evidence)\b", re.IGNORECASE)


def _flag(text_window: str, keywords: List[str]) -> str:
    """Yes only if a keyword is mentioned WITHOUT a negation cue shortly
    before it in the same sentence (e.g. 'did not indicate mutagenic
    potential' should not flag as Yes)."""
    pattern = _compile_keywords(keywords)
    for sentence in _split_sentences(text_window):
        m = pattern.search(sentence)
        if not m:
            continue
        preceding = sentence[max(0, m.start() - 40):m.start()]
        if _NEGATION_RE.search(preceding):
            continue
        return "Yes"
    return "Not stated"


def build_substance_breakdown(composition_rows: List[Dict[str, str]],
                                exposure_rows: List[Dict[str, str]],
                                full_text: str,
                                tox_text: Optional[str] = None) -> List[Dict[str, str]]:
    """Per-ingredient breakdown row, combining composition + exposure-limit
    tables with a heuristic scan for toxicological flags mentioned near each
    ingredient's name/CAS number. The scan is restricted to Sections 2/11/12
    (tox_text) rather than the whole document where possible, since a fixed
    character window around a name/CAS mentioned early (e.g. in the
    composition table) can otherwise spill into unrelated sections on short
    SDS's. Flags are necessarily heuristic (SDS's rarely tag hazard-by-hazard
    per component in a machine-friendly way) - treat 'Yes' as 'worth
    checking the source text', not as a certified classification."""
    scan_text = tox_text if tox_text else full_text
    rows = []
    for comp in composition_rows:
        name, cas = comp["name"], comp["cas"]
        window = _windows_around(scan_text, name, window=150) + " " + _windows_around(scan_text, cas, window=150)
        exp = _match_exposure_row(name, cas, exposure_rows) or {}
        rows.append({
            "name": name or "(unnamed component)",
            "cas": cas or "N/A",
            "concentration": comp.get("concentration") or "Not stated",
            "wel_type": exp.get("wel_type") or "Not stated",
            "ltel": exp.get("ltel") or "Not stated",
            "stel": exp.get("stel") or "Not stated",
            "carcinogenic": _flag(window, _CARCINOGEN_KEYWORDS),
            "mutagenic": _flag(window, _MUTAGEN_KEYWORDS),
            "reproductive_toxicant": _flag(window, _REPROTOX_KEYWORDS),
            "sensitiser": _flag(window, _SENSITISER_KEYWORDS),
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
        m = re.match(r"^(-?\d+(?:[.,]\d+)?(?:\s*-\s*-?\d+(?:[.,]\d+)?)?)", value.strip())
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
    repro_bits = [values.get(k) for k in ("reproductive_toxicity", "teratogenicity")]
    repro_bits = [b for b in repro_bits if b]
    d["reproductive_toxicity_combined"] = " ".join(dict.fromkeys(repro_bits)) if repro_bits else None

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
    d["hand_protection"] = values.get("ppe_skin")  # ppe_skin's labels are hand/glove-oriented
    d["eye_protection"] = values.get("ppe_eye")
    d["skin_protection"] = values.get("ppe_body") or values.get("ppe_general")

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
    d["substance_breakdown"] = build_substance_breakdown(comp_rows, exp_rows, full_text, tox_text=tox_text)

    return d
