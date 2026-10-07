#!/usr/bin/env python3
"""
SDS Reader - offline Safety Data Sheet extraction tool.

Usage
-----
Single file:
    python cli.py file --input path/to/sds.pdf --outdir ./output

Whole folder:
    python cli.py folder --input path/to/folder --outdir ./output [--recursive] [--csv]

Optional template (.docx or .txt/.md with {{placeholders}}), overriding the
bundled default summary template:
    python cli.py file --input sds.pdf --outdir ./output --template mytemplate.docx

Hybrid mode (local Ollama reconciliation pass) - see the DEFAULT_USE_LLM
toggle below to change the default without passing --llm every time; the
--llm / --no-llm flags always override whatever the toggle is set to.
Requires `ollama serve` running and a model already pulled:
    python cli.py folder --input ./my_sds_folder --outdir ./output --llm

Every run produces exactly two files per document: `<name>_extracted.json`
and `<name>_summary.docx`. In folder mode, add --csv for one combined
spreadsheet across the whole batch.

For people who can't run Python, the same engine ships as a Windows desktop
app - see sds_reader_app.py and the README's "Desktop app" section.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from sds_reader.extractor import extract_sds, SDSRecord
from sds_reader.batch import process_folder
from sds_reader import llm_reconcile, pipeline

DEFAULT_TEMPLATE = pipeline.DEFAULT_TEMPLATE

# ---------------------------------------------------------------------------
# Code-level toggle: flip this to True to run the hybrid Ollama pass by
# default without needing --llm on every command. --llm / --no-llm on the
# command line always override this.
# ---------------------------------------------------------------------------
DEFAULT_USE_LLM = False


def _llm_options(args):
    if not args.llm:
        return None
    return pipeline.LLMOptions(host=args.llm_host, model=args.llm_model, timeout=args.llm_timeout)


def _process_one(record: SDSRecord, args, outdir: str):
    """Extracts + (optionally) reconciles + writes the two output files for
    one document. Returns the pipeline result (used for combined output in
    folder mode)."""
    result = pipeline.process_record(record, outdir, template=args.template,
                                     write_json=True, llm=_llm_options(args), log=print)
    _print_summary(record)
    print(f"  Summary document: {result.summary_path}")
    print(f"  Extracted fields (JSON): {result.json_path}")
    return result


def _print_summary(record: SDSRecord):
    print(f"\n=== {os.path.basename(record.source_file)} ===")
    if record.warnings:
        print(f"  ({len(record.warnings)} warning(s))")
    key_fields = ["product_name", "manufacturer_name", "signal_word", "un_number", "cas_number_main"]
    for k in key_fields:
        v = record.values.get(k)
        if v:
            preview = v if isinstance(v, str) else str(v)
            preview = preview.replace("\n", " ")[:100]
            print(f"  {k}: {preview}")


def cmd_file(args):
    record = extract_sds(args.input)
    _process_one(record, args, args.outdir)


def cmd_folder(args):
    records = process_folder(args.input, recursive=args.recursive)
    if not records:
        print("No PDF files found in that folder.")
        return

    results = [_process_one(record, args, args.outdir) for record in records]

    if args.csv:
        csv_path = pipeline.write_combined_csv(results, os.path.join(args.outdir, "_combined_extracted.csv"))
        if csv_path:
            print(f"\nCombined CSV: {csv_path}")


def main():
    parser = argparse.ArgumentParser(description="Offline SDS (Safety Data Sheet) reader.")
    sub = parser.add_subparsers(dest="mode", required=True)

    def add_llm_args(p):
        p.add_argument("--llm", dest="llm", action="store_true", default=DEFAULT_USE_LLM,
                        help=f"Run the hybrid Ollama reconciliation pass (default: {DEFAULT_USE_LLM} - "
                             "see DEFAULT_USE_LLM at the top of cli.py). Requires 'ollama serve' "
                             "running locally. Falls back silently to the regex-only output if unreachable.")
        p.add_argument("--no-llm", dest="llm", action="store_false",
                        help="Force regex-only mode, overriding DEFAULT_USE_LLM.")
        p.add_argument("--llm-host", default=llm_reconcile.DEFAULT_HOST,
                        help=f"Ollama server URL (default: {llm_reconcile.DEFAULT_HOST}).")
        p.add_argument("--llm-model", default=llm_reconcile.DEFAULT_MODEL,
                        help=f"Ollama model name (default: {llm_reconcile.DEFAULT_MODEL}). "
                             "Must already be pulled: `ollama pull <model>`.")
        p.add_argument("--llm-timeout", type=int, default=llm_reconcile.DEFAULT_TIMEOUT,
                        help=f"Per-request timeout in seconds (default: {llm_reconcile.DEFAULT_TIMEOUT}).")

    p_file = sub.add_parser("file", help="Process a single SDS PDF.")
    p_file.add_argument("--input", required=True, help="Path to a single SDS PDF.")
    p_file.add_argument("--outdir", required=True, help="Directory to write output into.")
    p_file.add_argument("--template", help="Optional .docx/.txt template with {{placeholders}} "
                         "(overrides the bundled default summary template).")
    add_llm_args(p_file)
    p_file.set_defaults(func=cmd_file)

    p_folder = sub.add_parser("folder", help="Process every PDF in a folder.")
    p_folder.add_argument("--input", required=True, help="Folder containing SDS PDFs.")
    p_folder.add_argument("--outdir", required=True, help="Directory to write output into.")
    p_folder.add_argument("--recursive", action="store_true", help="Recurse into subfolders.")
    p_folder.add_argument("--csv", action="store_true", help="Also write one combined CSV for the batch.")
    p_folder.add_argument("--template", help="Optional .docx/.txt template with {{placeholders}} "
                           "(overrides the bundled default summary template).")
    add_llm_args(p_folder)
    p_folder.set_defaults(func=cmd_folder)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
