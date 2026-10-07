"""
batch.py
========
Point the reader at a folder and it will process every PDF inside it
(non-recursive by default, recursive optional), producing one SDSRecord per
file plus a combined summary.
"""

import os
from typing import List

from .extractor import extract_sds, SDSRecord


def find_pdfs(folder: str, recursive: bool = False) -> List[str]:
    pdfs = []
    if recursive:
        for root, _, files in os.walk(folder):
            for fn in files:
                if fn.lower().endswith(".pdf"):
                    pdfs.append(os.path.join(root, fn))
    else:
        for fn in os.listdir(folder):
            if fn.lower().endswith(".pdf"):
                pdfs.append(os.path.join(folder, fn))
    return sorted(pdfs)


def process_folder(folder: str, recursive: bool = False) -> List[SDSRecord]:
    records = []
    for path in find_pdfs(folder, recursive=recursive):
        try:
            rec = extract_sds(path)
        except Exception as e:
            rec = SDSRecord()
            rec.source_file = path
            rec.warnings.append(f"FAILED TO PARSE: {e}")
        records.append(rec)
    return records
