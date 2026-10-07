"""
target_format.py
=================
Produces the exact field set/naming the person specified as their required
extraction output (SDS Date, SDS Version, Substance Name, CAS No., Hazard
Pictogram, Signal Word, Hazard Codes, Hazard Statements, Overall Breakdown
percentage, First Aid, Fire Aid, Spillage, Safe Handling, Storage
Conditions, Exposure limits, PPE_RPE/Hands/Eyes/Skin, Melting/Boiling
Point, Reactivity, Incompatible materials, Carcinogenetic, Mutagens,
Reproductive Toxins, Sensitiser, Dust, Vapour, Fume, Dermatitis, Asthma,
Illness), with "Not stated in SDS" as the fallback text throughout.

Design choice: for the free-text narrative fields (First Aid, Fire Aid,
Spillage, Safe Handling, Storage Conditions) this uses the *verbatim raw
section text* captured by extractor.py (see `_raw_section_N` in
SDSRecord.values) rather than re-synthesizing from individually-parsed
sub-fields. Recombining parsed sub-fields is where wording/character loss
has historically crept in (a sub-field regex trimming a boundary slightly
wrong, a sub-label not being recognized and silently dropped); reproducing
the section close to verbatim avoids that failure mode entirely for these
long free-text fields, at the minor cost of also including the original
sub-labels/preamble text as found in the source PDF.
"""

import re
from typing import Dict, Optional

from . import schema

_NOT_STATED = "Not stated in SDS"


def _v(values: Dict, key: str) -> str:
    val = values.get(key)
    if val in (None, "", []):
        return _NOT_STATED
    return val


def _v_chain(values: Dict, *keys: str) -> str:
    """Like _v, but tries each key in order and returns the first non-empty one."""
    for key in keys:
        val = values.get(key)
        if val not in (None, "", []):
            return val
    return _NOT_STATED


def _split_handling_storage(raw_section_7: Optional[str]):
    """Section 7 covers both Handling and Storage in one block; split on the
    storage sub-label so each gets its own target field."""
    if not raw_section_7:
        return _NOT_STATED, _NOT_STATED
    storage_field = next(f for f in schema.SECTION_7 if f.var == "storage_conditions")
    label_alt = "|".join(storage_field.labels)
    pat = re.compile(rf"(?im)(?:{label_alt})")
    m = pat.search(raw_section_7)
    if not m:
        return raw_section_7.strip() or _NOT_STATED, _NOT_STATED
    handling = raw_section_7[:m.start()].strip()
    storage = raw_section_7[m.start():].strip()
    return handling or _NOT_STATED, storage or _NOT_STATED


def _exposure_limits_text(values: Dict) -> str:
    table = values.get("exposure_limits")
    if table:
        lines = []
        for row in table:
            parts = []
            seen_values = set()
            for k, v in row.items():
                if not v:
                    continue
                # Degenerate case: pdfplumber sometimes duplicates a cell's
                # text as both its own "header" and its value on malformed
                # multi-jurisdiction tables - don't render "X: X".
                if k.strip() == v.strip() or v.strip() in seen_values:
                    if v.strip() not in seen_values:
                        parts.append(v.strip())
                else:
                    parts.append(f"{k}: {v}")
                seen_values.add(v.strip())
            if parts:
                lines.append(", ".join(parts))
        if lines:
            return "\n".join(lines)
    text = values.get("exposure_limits_text")
    return text if text else _NOT_STATED


def build_target_format(values: Dict) -> Dict:
    """Returns a flat dict using the person's exact required field names."""
    raw4 = values.get("_raw_section_4")
    raw5 = values.get("_raw_section_5")
    raw6 = values.get("_raw_section_6")
    raw7 = values.get("_raw_section_7")
    safe_handling, storage_conditions = _split_handling_storage(raw7)

    return {
        "SDS Date": _v(values, "sds_date"),
        "SDS Version": _v(values, "sds_version"),
        "Substance Name": _v(values, "product_name"),
        "CAS No.": _v(values, "cas_number_display"),
        "Hazard Pictogram": _v(values, "pictograms"),
        "Signal Word": _v(values, "signal_word"),
        "Hazard Codes": _v(values, "hazard_codes"),
        "Hazard Statements": _v(values, "hazard_statement_text"),
        "Overall Breakdown_percentage": _v(values, "overall_breakdown_percentage"),
        "First Aid": raw4.strip() if raw4 else _NOT_STATED,
        "Fire Aid": raw5.strip() if raw5 else _NOT_STATED,
        "Spillage": raw6.strip() if raw6 else _NOT_STATED,
        "Safe Handling": safe_handling,
        "Storage Conditions": storage_conditions,
        "Exposure limits": _exposure_limits_text(values),
        "PPE_RPE": _v(values, "respiratory_protection"),
        "PPE_Hands": _v(values, "hand_protection"),
        "PPE_Eyes": _v(values, "eye_protection"),
        "PPE_Skin": _v(values, "skin_protection"),
        "Melting Point": _v(values, "melting_point_numeric"),
        "Boiling Point": _v(values, "boiling_point_numeric"),
        "Reactivity": _v_chain(values, "reactivity", "chemical_stability"),
        "Incompatible materials": _v(values, "incompatible_materials"),
        "Carcinogenetic": _v_chain(values, "carcinogenicity", "carcinogenicity_fallback"),
        "Mutagens": _v_chain(values, "germ_cell_mutagenicity", "germ_cell_mutagenicity_fallback"),
        "Reproductive Toxins": _v(values, "reproductive_toxicity_combined"),
        "Sensitiser": _v(values, "sensitization"),
        "Dust": _v(values, "dust_mentions"),
        "Vapour": _v(values, "vapour_mentions"),
        "Fume": _v(values, "fume_mentions"),
        "Dermatitis": _v(values, "dermatitis_mentions"),
        "Asthma": _v(values, "asthma_mentions"),
        "Illness": _v(values, "illness_mentions"),
    }
