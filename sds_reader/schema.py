"""
schema.py
=========
Defines the canonical variable set for a Safety Data Sheet (SDS), based on the
globally-harmonized 16-section GHS format used by:
  - US OSHA HazCom 2012 (29 CFR 1910.1200 Appendix D)
  - EU REACH Annex II / CLP
  - ANSI Z400.1 / Z129.1
  - ISO 11014

Almost every SDS produced anywhere in the world (Sigma-Aldrich, Fisher, VWR,
Merck, Honeywell, 3M, BASF, Univar, etc.) follows this 16-section skeleton,
even though wording, header numbering style, and table layout vary a lot.
That's what makes a synonym-driven, section-anchored parser reliable across
vendors without needing a template per manufacturer.

Each field maps to:
  - a canonical variable name (used in the final output dict / template fill)
  - the SDS section it normally lives in (1-16)
  - a list of label synonyms/regex fragments seen across real-world SDS's,
    used to locate the value inside that section's text block.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class FieldDef:
    var: str                # canonical variable name
    section: int             # 1-16
    labels: List[str]        # synonym label variants (regex-escaped at match time is NOT assumed;
                              # they are treated as flexible regex fragments, see extractor.py)
    multiline: bool = True    # whether value may span multiple lines/until next label
    kind: str = "text"        # "text" | "list" | "table"


# ---------------------------------------------------------------------------
# SECTION 1 - Identification
# ---------------------------------------------------------------------------
SECTION_1 = [
    FieldDef("product_name", 1, [
        r"product name", r"product description", r"ghs product identifier", r"substance name",
        r"trade name", r"material name", r"commercial product name", r"\bname\s*:",
    ]),
    FieldDef("other_identification", 1, [r"other means of(?:\s+identification)?\s*:"]),
    FieldDef("material_uses", 1, [r"material uses"]),
    FieldDef("chemical_name_1", 1, [r"chemical name"]),
    FieldDef("reach_registration_number", 1, [r"reach registration number"]),
    FieldDef("reach_registration_notes", 1, [r"reach registration notes"]),
    FieldDef("ec_number_1", 1, [r"\bec number\b"]),
    FieldDef("product_code", 1, [
        r"product code", r"product number", r"item number", r"catalog(?:ue)? number", r"cat\.?\s*no\.?",
        r"cb\s*number",
    ]),
    FieldDef("brand", 1, [r"\bbrand\b"]),
    FieldDef("synonyms", 1, [r"synonyms", r"other means of identification", r"other names"]),
    FieldDef("cas_number_main", 1, [r"cas[\s#\-\.]*no\.?", r"cas[\s\-]*number", r"\bcas\s*:"], multiline=False),
    FieldDef("product_use", 1, [
        r"recommended use", r"use of the substance", r"identified uses",
        r"relevant identified uses",
    ]),
    FieldDef("use_restrictions", 1, [r"uses advised against", r"restrictions on use"]),
    FieldDef("manufacturer_name", 1, [
        r"manufacturer", r"supplier", r"\bcompany\b", r"details of the supplier",
        r"responsible party",
    ]),
    FieldDef("manufacturer_address", 1, [r"address"]),
    FieldDef("manufacturer_phone", 1, [
        r"telephone", r"phone number", r"company phone", r"tel[\.:]",
    ]),
    FieldDef("emergency_phone", 1, [
        r"emergency (telephone|phone)", r"emergency contact", r"emergency number",
        r"24[\s\-]?hour", r"chemtrec",
    ]),
]

# ---------------------------------------------------------------------------
# SECTION 2 - Hazard(s) identification
# ---------------------------------------------------------------------------
SECTION_2 = [
    FieldDef("ghs_classification", 2, [
        r"classification of the substance", r"ghs classification",
        r"classification in accordance with", r"hazard classification",
    ], kind="list"),
    FieldDef("signal_word", 2, [r"signal word"], multiline=False),
    FieldDef("hazard_statements", 2, [
        r"(?<!full text of )hazard\s+statements?", r"hazard\(s\) identification.*statement",
    ], kind="list"),
    FieldDef("precautionary_statements", 2, [r"(?<!full text of )precautionary statements?"], kind="list"),
    FieldDef("pictograms", 2, [r"pictograms?", r"ghs symbols?", r"hazard symbols?"], kind="list"),
    FieldDef("hazards_not_otherwise_classified", 2, [
        r"hazards not otherwise classified", r"hnoc", r"other hazards",
    ]),
]

# ---------------------------------------------------------------------------
# SECTION 3 - Composition / information on ingredients  (table-heavy)
# ---------------------------------------------------------------------------
SECTION_3 = [
    FieldDef("composition_table", 3, [
        r"composition", r"information on ingredients", r"hazardous components",
        r"chemical name.*cas", r"ingredient",
    ], kind="table"),
]

# ---------------------------------------------------------------------------
# SECTION 4 - First-aid measures
# ---------------------------------------------------------------------------
SECTION_4 = [
    FieldDef("first_aid_general", 4, [r"general information", r"general advice"]),
    FieldDef("first_aid_inhalation", 4, [r"inhalation"]),
    FieldDef("first_aid_skin", 4, [r"skin contact", r"skin[\s:]"]),
    FieldDef("first_aid_eye", 4, [r"eye contact", r"eyes?[\s:]"]),
    FieldDef("first_aid_ingestion", 4, [r"ingestion", r"if swallowed"]),
    FieldDef("first_aid_symptoms", 4, [
        r"most important symptoms", r"symptoms and effects",
    ]),
    FieldDef("first_aid_medical_attention", 4, [
        r"indication of.*medical attention", r"immediate medical attention",
        r"notes to physician",
    ]),
]

# ---------------------------------------------------------------------------
# SECTION 5 - Fire-fighting measures
# ---------------------------------------------------------------------------
SECTION_5 = [
    FieldDef("extinguishing_media_suitable", 5, [
        r"(?<!un)suitable extinguishing (?:media|agents?)", r"extinguishing (?:media|agents?)",
    ]),
    FieldDef("extinguishing_media_unsuitable", 5, [r"unsuitable extinguishing (?:media|agents?)"]),
    FieldDef("fire_specific_hazards", 5, [
        r"speci(?:fic|al) hazards", r"hazardous combustion products", r"unusual fire",
    ]),
    FieldDef("fire_protective_equipment", 5, [
        r"protective equipment for fire[- ]?fighters", r"special protective equipment",
        r"advice for firefighters",
    ]),
]

# ---------------------------------------------------------------------------
# SECTION 6 - Accidental release measures
# ---------------------------------------------------------------------------
SECTION_6 = [
    FieldDef("release_personal_precautions", 6, [r"personal precautions"]),
    FieldDef("release_environmental_precautions", 6, [r"environmental precautions"]),
    FieldDef("release_containment_cleanup", 6, [
        r"methods?.{0,20}for containment", r"methods?.{0,20}clean(?:ing)?[\s-]?up",
        r"clean[\s-]?up",
    ]),
]

# ---------------------------------------------------------------------------
# SECTION 7 - Handling and storage
# ---------------------------------------------------------------------------
SECTION_7 = [
    FieldDef("handling_precautions", 7, [
        r"precautions for safe handling", r"handling\s*:",
    ]),
    FieldDef("storage_conditions", 7, [
        r"conditions for safe storage", r"storage\s*:",
    ]),
    FieldDef("incompatible_materials_storage", 7, [r"incompatible materials?\s*:"]),
]

# ---------------------------------------------------------------------------
# SECTION 8 - Exposure controls / personal protection
# ---------------------------------------------------------------------------
SECTION_8 = [
    FieldDef("exposure_limits", 8, [
        r"exposure limit", r"occupational exposure limit", r"control parameters",
    ], kind="table"),
    FieldDef("exposure_limits_text", 8, [
        r"occupational exposure limits?", r"control parameters?",
    ]),
    FieldDef("engineering_controls", 8, [
        r"(?:appropriate\s+)?engineering(?:\s+controls)?\s*:", r"exposure controls",
    ]),
    FieldDef("ppe_eye", 8, [r"eye[\s/]*(?:(?:and|/)\s*face\s*)?protection"]),
    FieldDef("ppe_skin", 8, [r"hand protection", r"gloves?\s*:"]),
    FieldDef("ppe_body", 8, [
        r"other skin and body", r"skin (?:and body )?protection", r"body protection",
    ]),
    FieldDef("ppe_respiratory", 8, [r"respiratory protection"]),
    FieldDef("ppe_general", 8, [r"personal protective equipment\s*:", r"general hygiene\s*:"]),
]

# ---------------------------------------------------------------------------
# SECTION 9 - Physical and chemical properties
# ---------------------------------------------------------------------------
SECTION_9 = [
    FieldDef("appearance", 9, [r"appearance", r"physical state"], multiline=False),
    FieldDef("color", 9, [r"\bcolou?r\b"], multiline=False),
    FieldDef("odor", 9, [r"\bodou?r\b(?!\s*threshold)"], multiline=False),
    FieldDef("odor_threshold", 9, [r"odou?r threshold"], multiline=False),
    FieldDef("ph", 9, [r"\bph\b"], multiline=False),
    FieldDef("melting_point", 9, [r"melting point(?:\s*/\s*range)?", r"freezing point"], multiline=False),
    FieldDef("boiling_point", 9, [
        r"(?:initial\s+)?boiling point(?:\s*(?:/|and)\s*(?:boiling\s+)?range)?",
    ], multiline=False),
    FieldDef("flash_point", 9, [r"flash point"], multiline=False),
    FieldDef("evaporation_rate", 9, [r"evaporation rate"], multiline=False),
    FieldDef("flammability", 9, [r"flammability"], multiline=False),
    FieldDef("flammability_limits", 9, [
        r"upper/lower flammability", r"explosive limit", r"flammable limit",
    ], multiline=False),
    FieldDef("vapor_pressure", 9, [r"vapor pressure", r"vapour pressure"], multiline=False),
    FieldDef("vapor_density", 9, [r"vapor density", r"vapour density"], multiline=False),
    FieldDef("relative_density", 9, [
        r"relative density", r"specific gravity", r"density(?!\s*of)",
    ], multiline=False),
    FieldDef("solubility", 9, [r"solubility"], multiline=False),
    FieldDef("partition_coefficient", 9, [
        r"partition coefficient", r"log ?kow", r"n-octanol/water",
    ], multiline=False),
    FieldDef("autoignition_temperature", 9, [r"auto[\s-]?ignition temperature"], multiline=False),
    FieldDef("decomposition_temperature", 9, [r"decomposition temperature"], multiline=False),
    FieldDef("viscosity", 9, [r"viscosity"], multiline=False),
    FieldDef("explosive_properties", 9, [r"explosive propert"], multiline=False),
    FieldDef("oxidizing_properties", 9, [r"oxidi[sz]ing propert"], multiline=False),
    FieldDef("molecular_weight", 9, [r"molecular weight"], multiline=False),
    FieldDef("percent_volatile", 9, [r"percent volatile", r"volatility"], multiline=False),
]

# ---------------------------------------------------------------------------
# SECTION 10 - Stability and reactivity
# ---------------------------------------------------------------------------
SECTION_10 = [
    FieldDef("reactivity", 10, [r"reactivity"]),
    FieldDef("chemical_stability", 10, [r"chemical stability"]),
    FieldDef("hazardous_reactions", 10, [r"possibility of hazardous reactions"]),
    FieldDef("conditions_to_avoid", 10, [r"conditions to avoid"]),
    FieldDef("incompatible_materials_stability", 10, [r"incompatible materials?", r"materials?\s+to\s+avoid"]),
    FieldDef("hazardous_decomposition_products", 10, [r"hazardous decomposition"]),
]

# ---------------------------------------------------------------------------
# SECTION 11 - Toxicological information
# ---------------------------------------------------------------------------
SECTION_11 = [
    FieldDef("acute_toxicity", 11, [r"acute toxicity", r"ld50", r"lc50"]),
    FieldDef("skin_corrosion_irritation", 11, [r"skin corrosion", r"skin irritation"]),
    FieldDef("eye_damage_irritation", 11, [r"eye damage", r"eye irritation"]),
    FieldDef("sensitization", 11, [r"sensiti[sz]ation"]),
    FieldDef("germ_cell_mutagenicity", 11, [r"germ cell mutagenicity", r"germ cell", r"mutagenicity"]),
    FieldDef("carcinogenicity", 11, [r"carcinogenicity"]),
    FieldDef("reproductive_toxicity", 11, [r"reproductive toxicity"]),
    FieldDef("teratogenicity", 11, [r"teratogenicity"]),
    FieldDef("developmental_effects", 11, [r"developmental effects"]),
    FieldDef("fertility_effects", 11, [r"fertility effects"]),
    FieldDef("numerical_toxicity_measures", 11, [r"numerical measures of toxicity"]),
    FieldDef("stot_single", 11, [r"(?:stot|specific target organ toxicity).{0,20}single"]),
    FieldDef("stot_repeated", 11, [r"(?:stot|specific target organ toxicity).{0,20}repeated"]),
    FieldDef("aspiration_hazard", 11, [r"aspiration hazard"]),
    FieldDef("info_likely_routes", 11, [r"information on the likely(?:\s+routes?\s+of\s+exposure)?\s*:"]),
    FieldDef("tox_info_on_ingredients", 11, [
        r"toxicological information on ingredients", r"toxicological effects",
    ]),
    FieldDef("other_toxicological_info", 11, [r"other information on hazards", r"other adverse effects"]),
]

# ---------------------------------------------------------------------------
# SECTION 12 - Ecological information
# ---------------------------------------------------------------------------
SECTION_12 = [
    FieldDef("ecotoxicity", 12, [r"ecotoxicity"]),
    FieldDef("persistence_degradability", 12, [r"persistence and degradability"]),
    FieldDef("bioaccumulative_potential", 12, [r"bioaccumulative potential"]),
    FieldDef("mobility_in_soil", 12, [r"mobility in soil"]),
    FieldDef("other_adverse_effects", 12, [r"other adverse effects"]),
]

# ---------------------------------------------------------------------------
# SECTION 13 - Disposal considerations
# ---------------------------------------------------------------------------
SECTION_13 = [
    FieldDef("disposal_methods", 13, [r"disposal method", r"waste treatment", r"waste disposal"]),
]

# ---------------------------------------------------------------------------
# SECTION 14 - Transport information
# ---------------------------------------------------------------------------
SECTION_14 = [
    FieldDef("un_number", 14, [r"un[\s-]?number"], multiline=False),
    FieldDef("un_proper_shipping_name", 14, [r"proper shipping name", r"un proper shipping"], multiline=False),
    FieldDef("transport_hazard_class", 14, [r"transport hazard class", r"hazard class"], multiline=False),
    FieldDef("packing_group", 14, [r"packing group"], multiline=False),
    FieldDef("environmental_hazards_transport", 14, [r"environmental hazard"], multiline=False),
    FieldDef("transport_special_precautions", 14, [r"special precautions"], multiline=False),
]

# ---------------------------------------------------------------------------
# SECTION 15 - Regulatory information
# ---------------------------------------------------------------------------
SECTION_15 = [
    FieldDef("regulatory_information", 15, [
        r"safety, health and environmental regulations", r"national regulations",
        r"regulatory information", r"sara", r"tsca", r"reach",
    ]),
]

# ---------------------------------------------------------------------------
# SECTION 16 - Other information
# ---------------------------------------------------------------------------
SECTION_16 = [
    FieldDef("revision_date", 16, [
        r"(?<!the )revision date", r"date of issue", r"date of preparation", r"last updated",
        r"version date", r"print date",
    ], multiline=False),
    FieldDef("version_number", 16, [
        r"version number", r"(?<!the )revision number\b",
    ], multiline=False),
    FieldDef("other_information", 16, [r"other information", r"disclaimer", r"prepared by"]),
]

ALL_FIELDS: List[FieldDef] = (
    SECTION_1 + SECTION_2 + SECTION_3 + SECTION_4 + SECTION_5 + SECTION_6 +
    SECTION_7 + SECTION_8 + SECTION_9 + SECTION_10 + SECTION_11 + SECTION_12 +
    SECTION_13 + SECTION_14 + SECTION_15 + SECTION_16
)

# Canonical section header patterns -> section number.
# Written to tolerate: "SECTION 1:", "1.", "1 -", "Section 1 –", no "SECTION" word at all, etc.
SECTION_TITLES = {
    1: ["identification"],
    2: ["hazards identification", "hazard identification", "hazard(s) identification", "hazards? identification"],
    3: ["composition", "information on ingredients"],
    4: ["first[\\s-]?aid measures", "first[\\s-]?aid"],
    5: ["fire[\\s-]?fighting measures", "fire fighting measures"],
    6: ["accidental release measures"],
    7: ["handling and storage"],
    8: ["exposure controls", "personal protection"],
    9: ["physical and chemical properties"],
    10: ["stability and reactivity"],
    11: ["toxicological information"],
    12: ["ecological information"],
    13: ["disposal considerations"],
    14: ["transport information"],
    15: ["regulatory information"],
    16: ["other information"],
}
