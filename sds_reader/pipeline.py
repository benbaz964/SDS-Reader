"""
pipeline.py
===========
The per-document "extract -> (optionally) reconcile -> write outputs" flow,
shared by the command-line tool (cli.py) and the desktop app
(sds_reader_app.py) so both always produce identical results.
"""

import csv
import json
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from . import llm_reconcile, schema, derived
from .extractor import SDSRecord, extract_sds
from .target_format import build_target_format
from .template_filler import PLACEHOLDER_RE, ROW_PLACEHOLDER_RE, fill_template

DEFAULT_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates",
                                "default_summary_template.docx")
SUPPORTED_TEMPLATE_EXTS = (".docx", ".txt", ".md")

Log = Callable[[str], None]


def _noop(_msg: str):
    pass


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def known_placeholders() -> List[str]:
    """Every variable name a template can use as {{name}}: all raw schema
    fields plus every derived/computed field."""
    names: Set[str] = {f.var for f in schema.ALL_FIELDS}
    names.update(derived.compute_derived_fields({}, "").keys())
    names.add("source_file")
    return sorted(names)


SUBSTANCE_BREAKDOWN_COLUMNS = [
    "name", "cas", "concentration", "wel_type", "ltel", "stel",
    "carcinogenic", "mutagenic", "reproductive_toxicant", "sensitiser",
]


def _template_text(template_path: str) -> str:
    if template_path.lower().endswith(".docx"):
        from docx import Document
        doc = Document(template_path)
        parts = [p.text for p in doc.paragraphs]

        def walk(container):
            for table in container.tables:
                for row in table.rows:
                    for cell in row.cells:
                        parts.extend(p.text for p in cell.paragraphs)
                        walk(cell)
        walk(doc)
        for section in doc.sections:
            parts.extend(p.text for p in section.header.paragraphs)
            parts.extend(p.text for p in section.footer.paragraphs)
        return "\n".join(parts)
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


@dataclass
class TemplateCheck:
    path: str
    used: List[str] = field(default_factory=list)      # recognised placeholders
    unknown: List[str] = field(default_factory=list)   # placeholders that won't be filled
    row_fields: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.used or self.row_fields)


def check_template(template_path: str) -> TemplateCheck:
    """Reads a template and reports which placeholders it uses, so a user
    can be warned up front about typos instead of finding '{{prodcut_name}}'
    left unfilled in every output document."""
    ext = os.path.splitext(template_path)[1].lower()
    if ext not in SUPPORTED_TEMPLATE_EXTS:
        raise ValueError(f"Unsupported template type '{ext}'. Use a .docx, .txt or .md file.")
    text = _template_text(template_path)
    known = set(known_placeholders())
    result = TemplateCheck(path=template_path)
    for name in dict.fromkeys(m.group(1) for m in PLACEHOLDER_RE.finditer(text)):
        (result.used if name in known else result.unknown).append(name)
    for list_name, fld in dict.fromkeys((m.group(1), m.group(2)) for m in ROW_PLACEHOLDER_RE.finditer(text)):
        label = f"row:{list_name}.{fld}"
        if list_name == "substance_breakdown" and fld in SUBSTANCE_BREAKDOWN_COLUMNS:
            result.row_fields.append(label)
        else:
            result.unknown.append(label)
    return result


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

# Maps target-schema field names (as reconciled by the LLM pass) back onto
# the underlying raw record.values keys the docx template actually reads
# from, so an LLM improvement to e.g. "Exposure limits" also shows up in the
# Word summary, not just the JSON.
_LLM_FEEDBACK_MAP = {
    "Exposure limits": ["exposure_limits_text"],
    "PPE_Hands": ["ppe_skin", "hand_protection"],
    "PPE_RPE": ["ppe_respiratory", "respiratory_protection"],
    "Vapour": ["vapour_mentions"],
}


@dataclass
class LLMOptions:
    host: str = llm_reconcile.DEFAULT_HOST
    model: str = llm_reconcile.DEFAULT_MODEL
    timeout: int = llm_reconcile.DEFAULT_TIMEOUT


@dataclass
class DocumentResult:
    source_file: str
    extracted: Dict
    summary_path: Optional[str] = None
    json_path: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None


MAX_PDF_BYTES = 100 * 1024 * 1024  # real SDSs are well under this; refuse pathological files


def _safe_stem(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    # Keep output names to safe filename characters only.
    return "".join(c if c.isalnum() or c in " ._-()" else "_" for c in stem).strip() or "sds"


def unique_path(path: str) -> str:
    """Never clobber an existing file (a previous run's output, or a user's
    own template that happens to share the name) - add ' (2)', ' (3)'..."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def validate_pdf(path: str):
    """Cheap sanity checks before handing a file to the PDF parser."""
    size = os.path.getsize(path)
    if size == 0:
        raise ValueError("file is empty")
    if size > MAX_PDF_BYTES:
        raise ValueError(f"file is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB - not processed")
    with open(path, "rb") as f:
        if b"%PDF" not in f.read(1024):
            raise ValueError("not a valid PDF file")


def _run_llm(record: SDSRecord, regex_data: Dict, llm: LLMOptions, log: Log) -> Dict:
    log("  Running local AI reconciliation (can take a couple of minutes on CPU)...")
    result = llm_reconcile.hybrid_reconcile(
        regex_data, record.values, record.section_texts,
        host=llm.host, model=llm.model, timeout=llm.timeout,
    )
    if not result.llm_available:
        log(f"  AI reconciliation skipped: {result.error}")
        return regex_data
    for target_key, raw_keys in _LLM_FEEDBACK_MAP.items():
        val = result.values.get(target_key)
        if val and val != "Not stated in SDS":
            for raw_key in raw_keys:
                record.values[raw_key] = val
    log(f"  AI reconciliation: {len(result.changes)} field(s) improved.")
    return result.values


def process_record(record: SDSRecord, outdir: str, template: Optional[str] = None,
                   write_json: bool = True, llm: Optional[LLMOptions] = None,
                   log: Log = _noop, overwrite: bool = True) -> DocumentResult:
    """Writes `<name>_summary.<template ext>` (and optionally
    `<name>_extracted.json`) for one already-extracted record. With
    overwrite=False, existing files are kept and a numbered name is used."""
    regex_data = build_target_format(record.values)
    final_data = _run_llm(record, regex_data, llm, log) if llm else regex_data

    # Built AFTER the (optional) LLM pass so the summary reflects any
    # reconciled values too, not just the pre-reconciliation regex output.
    os.makedirs(outdir, exist_ok=True)
    stem = _safe_stem(record.source_file)
    template = template or DEFAULT_TEMPLATE
    ext = os.path.splitext(template)[1].lower()
    if ext not in SUPPORTED_TEMPLATE_EXTS:
        raise ValueError(f"Unsupported template type '{ext}'. Use a .docx, .txt or .md file.")
    pick = (lambda p: p) if overwrite else unique_path

    result = DocumentResult(source_file=record.source_file, extracted=final_data,
                            warnings=list(record.warnings))
    result.summary_path = pick(os.path.join(outdir, f"{stem}_summary{ext}"))
    if os.path.normcase(os.path.abspath(result.summary_path)) == os.path.normcase(os.path.abspath(template)):
        result.summary_path = unique_path(result.summary_path)  # never overwrite the template itself
    fill_template(template, record.values, result.summary_path)

    if write_json:
        result.json_path = pick(os.path.join(outdir, f"{stem}_extracted.json"))
        with open(result.json_path, "w", encoding="utf-8") as f:
            json.dump(final_data, f, indent=2, ensure_ascii=False)
    return result


def process_pdf(path: str, outdir: str, template: Optional[str] = None,
                write_json: bool = True, llm: Optional[LLMOptions] = None,
                log: Log = _noop, overwrite: bool = True) -> DocumentResult:
    """Never raises for a bad PDF - the failure is reported on the result so
    one broken file doesn't stop a whole batch."""
    try:
        validate_pdf(path)
        record = extract_sds(path)
        return process_record(record, outdir, template, write_json, llm, log, overwrite)
    except Exception as e:
        return DocumentResult(source_file=path, extracted={}, error=str(e))


def write_combined_csv(results: List[DocumentResult], csv_path: str) -> Optional[str]:
    rows = [r.extracted for r in results if r.extracted]
    if not rows:
        return None
    fieldnames = ["Source File"] + list(rows[0].keys())
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:  # -sig so Excel detects UTF-8
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            if not r.extracted:
                continue
            row = {k: (v.replace("\n", " ") if isinstance(v, str) else v) for k, v in r.extracted.items()}
            row["Source File"] = os.path.basename(r.source_file)
            writer.writerow(row)
    return csv_path


def find_pdfs_in(paths: List[str], recursive: bool = False) -> List[str]:
    """Expands a mix of PDF file paths and folder paths into a sorted,
    de-duplicated list of PDF files."""
    from .batch import find_pdfs
    found: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            found.extend(find_pdfs(p, recursive=recursive))
        elif p.lower().endswith(".pdf") and os.path.isfile(p):
            found.append(p)
    seen = set()
    unique = []
    for p in found:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique
