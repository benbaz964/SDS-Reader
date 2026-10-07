__version__ = "1.0.0"

from .extractor import extract_sds, SDSRecord
from .batch import process_folder, find_pdfs
from .template_filler import fill_template
from . import derived
from . import target_format
from . import llm_reconcile
from . import pipeline

__all__ = ["extract_sds", "SDSRecord", "process_folder", "find_pdfs", "fill_template",
           "derived", "target_format", "llm_reconcile", "pipeline", "__version__"]
