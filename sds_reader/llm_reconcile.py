"""
llm_reconcile.py
=================
Optional hybrid layer: the regex/heuristic engine in extractor.py + derived.py
remains the deterministic, always-available baseline. This module adds an
OPTIONAL second pass through a local Ollama model that targets the specific
things regex is structurally bad at:

  1. Per-ingredient toxicology flags (carcinogen/mutagen/reproductive
     toxicant/sensitiser) - genuinely a reading-comprehension task ("which
     sentence is about which ingredient"), not a pattern-matching one.
  2. Cleaning up fields that came back genuinely messy from a malformed
     table (multi-jurisdiction exposure limits, borderless comparison
     tables) or a punctuation-free physical-properties block.
  3. Filling in fields that came back "Not stated in SDS" by giving the
     model the raw section text directly, in case the regex label synonym
     list simply didn't recognize this vendor's phrasing.

Design rules, in priority order:
  - The regex output is the FLOOR. The LLM pass can only replace a value if
    it returns a well-formed response; any failure (unreachable server,
    timeout, malformed JSON, empty response) silently falls back to the
    regex value - it never produces something worse than running without
    this module at all.
  - Every change the LLM makes is logged (field, old value, new value) so
    the person can review deltas instead of trusting it blindly.
  - The model is explicitly instructed to only use text that's actually in
    the document and to say "Not stated in SDS" rather than infer/guess -
    this is pointed out in every prompt, not assumed.
  - Nothing here is required - if Ollama isn't running, extraction still
    works exactly as before; this is purely additive.

This talks to Ollama's REST API directly via urllib (stdlib only, no new
dependency), since some deployment environments for this tool are
explicitly locked down to minimal Python installs.
"""

import json
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "llama3.1:8b"
DEFAULT_TIMEOUT = 120  # seconds - local models on CPU can be slow


@dataclass
class LLMChange:
    field: str
    old_value: object
    new_value: object
    reason: str = ""


@dataclass
class ReconcileResult:
    values: Dict
    changes: List[LLMChange] = field(default_factory=list)
    llm_available: bool = True
    error: Optional[str] = None

    def to_log_dict(self) -> Dict:
        return {
            "llm_available": self.llm_available,
            "error": self.error,
            "changes": [
                {"field": c.field, "old_value": c.old_value, "new_value": c.new_value, "reason": c.reason}
                for c in self.changes
            ],
        }


# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------

class OllamaError(Exception):
    pass


class OllamaClient:
    def __init__(self, host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL, timeout: int = DEFAULT_TIMEOUT):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout

    def is_available(self) -> Tuple[bool, Optional[str]]:
        """Checks the server is up AND the requested model is actually pulled -
        both are common setup gaps, and we want a clear message for either."""
        try:
            req = urllib.request.Request(f"{self.host}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            return False, f"Cannot reach Ollama at {self.host} ({e}). Is 'ollama serve' running?"
        except Exception as e:
            return False, f"Unexpected error contacting Ollama: {e}"

        model_names = [m.get("name", "") for m in data.get("models", [])]
        # Ollama model names may or may not include a ":tag" suffix - match loosely.
        base_requested = self.model.split(":")[0]
        if not any(base_requested == n.split(":")[0] for n in model_names):
            available = ", ".join(model_names) or "(none pulled)"
            return False, (f"Model '{self.model}' not found on this Ollama server. "
                            f"Available: {available}. Run: ollama pull {self.model}")
        return True, None

    def generate_json(self, system: str, user: str) -> Optional[dict]:
        """Sends a prompt with Ollama's JSON output mode and temperature 0
        (deterministic). Returns the parsed dict, or None on any failure -
        callers must treat None as 'keep the existing value', never as an
        error to propagate loudly, since this whole layer is optional."""
        payload = {
            "model": self.model,
            "system": system,
            "prompt": user,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0, "num_predict": 1500},
        }
        try:
            req = urllib.request.Request(
                f"{self.host}/api/generate",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                outer = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

        raw = outer.get("response", "")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Some models wrap JSON in a code fence even in JSON mode - try to recover.
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    return None
            return None


# ---------------------------------------------------------------------------
# Per-ingredient toxicology reconciliation (the flagship use case)
# ---------------------------------------------------------------------------

_BREAKDOWN_SYSTEM = """You are a meticulous safety-data-sheet analyst. You will be given a list of \
chemical ingredients and the toxicological-information text from a Safety Data Sheet (SDS). For EACH \
ingredient, decide whether the SDS text indicates it is a carcinogen, mutagen, reproductive toxicant, \
and/or sensitiser.

Rules you must follow exactly:
- Base every answer ONLY on the provided text. Do not use outside chemistry knowledge about the substance.
- If the text explicitly says something is NOT a carcinogen/mutagen/etc (e.g. "not classified", "no data \
available", "not mutagenic"), answer "No".
- If the text says nothing at all about a given ingredient for a given hazard, answer "Not stated".
- Only answer "Yes" if the text clearly attributes that specific hazard to that specific ingredient (by \
name or by being the only substance discussed in that passage).
- Never guess. When genuinely ambiguous, prefer "Not stated" over "Yes".
- Respond with ONLY a JSON object, no other text, in this exact shape:
{"ingredients": [{"name": "<exact name as given>", "carcinogenic": "Yes|No|Not stated", \
"mutagenic": "Yes|No|Not stated", "reproductive_toxicant": "Yes|No|Not stated", \
"sensitiser": "Yes|No|Not stated"}]}"""


def reconcile_substance_breakdown(composition_rows: List[Dict], tox_text: str,
                                    client: OllamaClient) -> Tuple[List[Dict], List[LLMChange]]:
    """Re-derives the four toxicology flags per ingredient using actual
    reading comprehension instead of the proximity-window heuristic in
    derived.py. Falls back to the existing heuristic values on any failure."""
    changes: List[LLMChange] = []
    if not composition_rows or not tox_text or not tox_text.strip():
        return composition_rows, changes

    ingredient_list = "\n".join(f"- {r.get('name', '(unnamed)')}" for r in composition_rows)
    user = (f"Ingredients:\n{ingredient_list}\n\n"
            f"Toxicological information text from the SDS:\n\"\"\"\n{tox_text[:6000]}\n\"\"\"")

    result = client.generate_json(_BREAKDOWN_SYSTEM, user)
    if not result or "ingredients" not in result or not isinstance(result["ingredients"], list):
        return composition_rows, changes

    llm_by_name = {}
    for item in result["ingredients"]:
        if isinstance(item, dict) and item.get("name"):
            llm_by_name[item["name"].strip().lower()] = item

    updated_rows = []
    valid_flags = {"Yes", "No", "Not stated"}
    for row in composition_rows:
        new_row = dict(row)
        llm_item = llm_by_name.get((row.get("name") or "").strip().lower())
        if llm_item:
            for flag_key in ("carcinogenic", "mutagenic", "reproductive_toxicant", "sensitiser"):
                new_val = llm_item.get(flag_key)
                if new_val in valid_flags and new_val != row.get(flag_key):
                    changes.append(LLMChange(
                        field=f"substance_breakdown.{row.get('name')}.{flag_key}",
                        old_value=row.get(flag_key), new_value=new_val,
                        reason="LLM read the toxicology text directly rather than using proximity matching",
                    ))
                    new_row[flag_key] = new_val
        updated_rows.append(new_row)
    return updated_rows, changes


# ---------------------------------------------------------------------------
# Targeted field repair (messy tables, unrecognized label wording)
# ---------------------------------------------------------------------------

_REPAIR_SYSTEM = """You are helping clean up an automated extraction from a Safety Data Sheet (SDS). \
You will be given one or more fields that were extracted automatically, each with the raw source text \
they were extracted from, in case the automated pass missed something or produced a messy/garbled result \
(this commonly happens with tables that don't have clear borders in the source PDF).

Rules you must follow exactly:
- Only use information that is ACTUALLY PRESENT in the provided source text. Never invent, infer, or use \
outside knowledge.
- Do not paraphrase or reword the SDS's own language - keep the original wording, just organize/clean it.
- If the current extracted value already looks correct and complete, return it UNCHANGED.
- If the current value is "Not stated in SDS" and the source text genuinely doesn't contain that \
information either, keep it as "Not stated in SDS" - do not fabricate something to fill the gap.
- If the current value is garbled, incomplete, or duplicated (common with malformed table extraction), \
rewrite it using ONLY the words and data present in the provided source text, organized clearly.
- Respond with ONLY a JSON object, no other text, mapping each field name you were given to its \
(possibly unchanged) value as a string:
{"field_name": "value", ...}"""


def reconcile_fields(current_values: Dict[str, str], source_texts: Dict[str, str],
                       client: OllamaClient) -> Tuple[Dict[str, str], List[LLMChange]]:
    """Targeted repair pass for a handful of named fields, each paired with
    its own relevant raw source text (e.g. Exposure limits -> Section 8 raw
    text). Only fields present in both dicts are sent. Returns the possibly-
    updated values dict (all original fields preserved) plus a change log."""
    changes: List[LLMChange] = []
    fields_to_send = {k: v for k, v in current_values.items() if k in source_texts and source_texts[k].strip()}
    if not fields_to_send:
        return current_values, changes

    parts = []
    for field_name, current_val in fields_to_send.items():
        parts.append(
            f"### Field: {field_name}\n"
            f"Current extracted value: {current_val!r}\n"
            f"Source text:\n\"\"\"\n{source_texts[field_name][:3000]}\n\"\"\"\n"
        )
    user = "\n".join(parts)

    result = client.generate_json(_REPAIR_SYSTEM, user)
    updated = dict(current_values)
    if not result or not isinstance(result, dict):
        return updated, changes

    for field_name in fields_to_send:
        new_val = result.get(field_name)
        if isinstance(new_val, str) and new_val.strip() and new_val.strip() != str(current_values.get(field_name, "")).strip():
            changes.append(LLMChange(field=field_name, old_value=current_values.get(field_name), new_value=new_val,
                                      reason="LLM cleanup/repair pass using the field's raw source text"))
            updated[field_name] = new_val
    return updated, changes


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# Fields worth a repair pass, and which raw-text source to give the model for
# each. Kept short/targeted on purpose - these are specifically the fields
# that testing showed come back weakest from the regex engine, not a blanket
# re-extraction of everything (which would be slower and riskier for no
# benefit on fields regex already gets right).
REPAIR_FIELD_SOURCES = {
    "Exposure limits": 8,
    "PPE_Hands": 8,
    "PPE_RPE": 8,
    "Vapour": 9,
}


def hybrid_reconcile(extracted_fields: Dict, raw_values: Dict, section_texts: Dict[int, str],
                       host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL,
                       timeout: int = DEFAULT_TIMEOUT) -> ReconcileResult:
    """Runs the full optional LLM reconciliation pass:
      1. Per-ingredient toxicology flags (substance_breakdown), if present.
      2. Targeted repair of known-weak fields using their own section text.

    `extracted_fields` is the target_format.py output dict.
    `raw_values` is the full raw SDSRecord.values dict (for substance_breakdown).
    `section_texts` is {section_number: raw_section_text} from extractor.py.
    Always returns a ReconcileResult - check .llm_available / .error if you
    want to report why nothing changed."""
    client = OllamaClient(host=host, model=model, timeout=timeout)
    available, err = client.is_available()
    if not available:
        return ReconcileResult(values=extracted_fields, changes=[], llm_available=False, error=err)

    all_changes: List[LLMChange] = []
    result_fields = dict(extracted_fields)

    # --- 1. Per-ingredient toxicology ---
    comp_rows = raw_values.get("substance_breakdown")
    tox_text = "\n".join(section_texts.get(n, "") for n in (2, 11, 12) if section_texts.get(n))
    if comp_rows and tox_text:
        updated_rows, changes = reconcile_substance_breakdown(comp_rows, tox_text, client)
        if changes:
            raw_values["substance_breakdown"] = updated_rows
            all_changes.extend(changes)

    # --- 2. Targeted field repair ---
    source_texts = {
        field_name: section_texts.get(sec_num, "")
        for field_name, sec_num in REPAIR_FIELD_SOURCES.items()
    }
    updated_fields, changes = reconcile_fields(result_fields, source_texts, client)
    result_fields = updated_fields
    all_changes.extend(changes)

    return ReconcileResult(values=result_fields, changes=all_changes, llm_available=True, error=None)
